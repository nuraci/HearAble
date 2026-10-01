# `hearable-audio-relay/1` — the wire format

The transport between a receiver that is playing something and the HearAble node
that listens. Written as a contract: another receiver should be implementable
from this page without reading the plugin.

Two separate channels, for one reason: a slow or absent receiver must never stall
Enigma2. So audio never travels on the control queue, and control never waits on
audio.

| | CONTROL | MEDIA |
|---|---|---|
| direction | receiver → box (a request) | box → receiver (a push) |
| transport | HTTP GET on port 8770 | TCP, MPEG-TS, port chosen by the receiver |
| who connects | the receiver | the box |
| cadence | every 500 ms | continuous, at playback rate |
| inside Enigma2 | yes, the plugin | no |

## CONTROL

```
GET http://<box>:8770/control?receiver=<ip>:<port>&want_lead_ms=<wanted>&lead_s=<measured>
```

The request is also the announcement: the receiver says where it wants the audio,
how much head start it wants, and how far ahead of the viewer it has measured
itself to be. Those last two are different numbers and must not be confused —
`want_lead_ms` is a request, `lead_s` is a measurement. A receiver that stops
asking disappears after `RECEIVER_TTL_S` (8 s) and the relay stops by itself.
`receiver` is URL-encoded: the colon arrives as `%3A` and must be decoded.

The response is JSON with stable fields:

| field | meaning |
|---|---|
| `protocol` | `hearable-audio-relay/1` |
| `source_type` | `file`, `stream`, `dvb` or `unknown`, deduced from the service reference |
| `service_reference` | the Enigma2 reference |
| `media` | the path on the box, for a file |
| `source_epoch` | an increasing integer; changes at every discontinuity |
| `state` | `idle`, `waiting_for_receiver`, `streaming`, `paused` |
| `selected_audio_track` / `selected_audio_language` / `audio_track_count` | the track the viewer chose |
| `position_s` / `length_s` | the decoder's position and the duration, in seconds |
| `sampled_wall_epoch` | when that position was read |
| `relay_started_at_media_s` | where the current relay began reading |
| `relay_lead_ms` | the lead in force, in milliseconds |
| `relay_head_start_s` | a fixed margin, 5.0 |
| `relay_spawns` / `relay_exits` / `last_reason` | diagnostics |
| `wire` | `{container, codec, transcoded, timestamps}` |

For a file, `position_s` comes from the service's `getPlayPosition()`, in 90 kHz
units converted to seconds. It sits **1099 ms ahead** of the picture actually on
screen (measured, 91 ms spread over ten samples): it is the decoder's position,
not the television's, and that is exactly what is wanted here, because the point
of the architecture is to take the audio before presentation.

## MEDIA

### Live broadcast — the PID tap

For `source_type: dvb` there is **no ffmpeg on the box at all**. The plugin runs
a PID filter on the demux of the service Enigma2 is already showing, and writes
the transport packets straight at the receiver:

```
open("/dev/dvb/adapter0/demux0", O_RDWR | O_NONBLOCK)
DMX_SET_PES_FILTER    pid    = <the selected audio PID>
                      input  = DMX_IN_FRONTEND
                      output = DMX_OUT_TSDEMUX_TAP
                      flags  = DMX_IMMEDIATE_START
```

What comes off the filter is already MPEG-TS carrying the broadcaster's own
timestamps, and the receiver's decoder reads a single-PID stream without a
programme table. One process fewer, and none of its buffering.

`DMX_IN_FRONTEND` reads the stream *entering* the receiver rather than what the
decoder is presenting, which costs no tuner and puts the tap **64 ms ahead** of
the picture.

The audio is whatever the broadcaster sends: MPEG layer II, AAC and AC-3+ have
all been seen working. The `wire.codec` field names one of them for information;
nothing in the path depends on it, because the tap forwards packets and the
receiver's decoder handles the rest.

### A file on the box's own storage

For `source_type: file` the box runs a second `ffmpeg`, outside Enigma2:

```
ffmpeg -v error -nostdin -re -copyts -ss <position + lead + head_start>
       -i <media> -map 0:a:<track> -c copy -f mpegts tcp://<receiver>
```

* **No transcoding.** `-c copy`: the selected track travels as it is, 191–211
  kbit/s measured. The receiver pays no encoding.
* **No video.** A single `-map` on the chosen audio track.
* **`-re`**: reads at playback rate. A stalled consumer fills the TCP buffer and
  stalls this ffmpeg, not the decoder.
* **`-copyts`**: the timestamps are the source media's own. This is the key point
  of the protocol — see below.

In both cases **the box connects out to the receiver**. With a listening relay
the start position has to be chosen before anyone arrives to read it, and the
audio arrives labelled with an instant that no longer exists.

### The timestamps are the only authority

The receiver derives its position in the media **only** from the PTS of the first
TS packet. Not from the control plane, and not from its own clock. An earlier
version overwrote the PTS with the start the control plane said it had asked
for, and labelled audio from second 504 as second 91.4 — from there on every
subtitle would have landed at the wrong point. The control plane says *what* is
happening; the stream says *where*.

One sample is enough: 1880 bytes suffice to read the first PTS, and the receiver
reads 4096.

### Who regulates the lead

One regulator, and it is the receiver. The relay aims generously forward
(`lead + 5 s`) and never corrects itself; the receiver refuses to pass anything
further ahead than `relay_lead_ms` to the pipeline, and the excess waits in the
queue. Two regulators, one at each end, fight: the relay tears down streams the
allowance has just built, and the receiver sits behind a full queue.

### Who chooses the lead

The receiver, in `config/audio_source_sf8008_relay*.json` as `lead_ms`. The relay
receives it on every poll and uses it **only** to choose where to start reading.

Changing it while a stream is running restarts the relay from the new position —
otherwise the new value would take effect only at the next discontinuity, which
can be an hour away. A change under 100 ms is not worth a join in the audio and
is adopted without a restart.

## Discontinuities

Every event below raises `source_epoch`, restarts the stream from the new
position, and makes the receiver flush its queue. The invariant is that **no
frame from an earlier epoch is delivered after the barrier** — verified as zero
across every barrier observed.

| event | how it is seen | what the relay does |
|---|---|---|
| lead changed | the receiver asks for a `want_lead_ms` at least 100 ms different | restarts from the new position |
| channel changed | the service reference changes | new epoch, the tap moves to the new audio PID |
| pause | the position stops advancing for ≥ 1.5 s | stops, state `paused` |
| resume | the position moves again | restarts from the current position |
| seek | the position jumps more than 4 s from the expected | restarts, `reason=seeked` |
| track change | `selected_audio_track` changes | restarts on the new track |
| end of file / stop | the service is no longer playable | stops, state `idle` |
| receiver gone | no control request for 8 s | stops |
| absurd lead | the receiver reports a gap > 12 s | restarts, at most 3 times, then lets it be |

That last row is worth keeping as written. A receiver that is merely slow is not
fixed by restarting the relay, and retrying indefinitely produced 70 restarts in
90 seconds. After three attempts the relay declares `receiver_cannot_keep_up`
and carries on transmitting.

## Loss, reconnection, backpressure

* A relay that exits is restarted after 2 s, with exponential backoff up to 30 s
  for immediate failures — a receiver that announces itself but is not listening
  would otherwise produce hundreds of attempts before anyone noticed.
* The receiver keeps a bounded queue (40 chunks of 160 ms ≈ 6.4 s). Full is
  normal operation: it is the margin that absorbs network jitter.
* The receiver gives up on a silent connection after 15 s and sets
  `SO_KEEPALIVE` with a 10 s idle, 5 s interval and 3 probes. Without that, a
  swallowed socket timeout holds a dead connection indefinitely — a state only a
  physically unplugged cable produces.
* TS has no application-level retransmission: a lost packet is lost audio, and
  shows up as a decoder error in the telemetry. None has been observed on the
  wired link.

## What is not defined here

IPTV and other `stream` sources. The field exists and is deduced, but only `dvb`
and `file` are implemented.

`source_type` is deduced from the reference Enigma2 declares current: a first
field of `4097` with a path is a file, `4097` with a URL is a stream, `1` is a
broadcast service. It must be deduced rather than assumed — a constant `file`
makes the check below useless, and on a broadcast channel the receiver would
accept audio the relay cannot produce.

The receiver checks `protocol` and `source_type` on every read of the control
plane and refuses what it does not know: at open with an explicit error, and in
flight by flushing the queue and refusing further audio rather than labelling it
wrongly. If both fields are missing entirely the request is accepted — a box
with an older plugin stays usable — but a *different* value is never ignored.
