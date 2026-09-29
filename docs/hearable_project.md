# HearAble — Subtitles for the television you already own

*Italian live subtitling · DVB-T2 · on-device. Version 1.5.1, September 2026.
Orchestrated by Nunzio Raciti; written by Claude (Opus) to his direction. See
[How this was built](#10-how-this-was-built).*

HearAble puts **Italian subtitles on live broadcast television**, about a second
behind the voice, for a viewer who is hard of hearing. It runs on a consumer
satellite/terrestrial receiver and a fanless mini PC on a private cable between
them. Nothing leaves the house, nothing is subscribed to, and **the remote
control still works**.

| | |
|---|---:|
| Subtitle lag behind the voice | ~1.0 s |
| Real-time factor, thirty minutes of live television | 0.4848 |
| Audio on the wire | 26 kB/s |
| Tuners consumed | 0 |

Every figure in this document is a measurement taken on the real hardware, or is
labelled as not measured.

---

## 1. What it is for

Italian broadcasters caption some programmes and not others. Live news, regional
windows, sport, talk shows, most of daytime — these arrive with no captions at
all, or with captions that lag so far behind that they describe a different
sentence than the one on screen. For someone who is hard of hearing, that is the
difference between watching television and watching people move their mouths.

HearAble was built for exactly that case: **subtitle whatever is on, now, in
Italian, on the television itself** — not on a phone, not on a tablet propped
against the screen, and without asking the viewer to learn anything beyond one
button.

Three constraints shaped every decision that follows.

- **It must not break the television.** The set-top box has one terrestrial
  tuner. If subtitling costs a tuner, or swallows a remote-control key, or
  blanks the picture, the viewer loses more than they gain.
- **It must run in the house.** No cloud speech service, no account, no upload of
  what someone watches in their living room.
- **It must be operable by pressing one button.** The viewer presses `F4`.
  Everything else — waking the recogniser, connecting, tearing down, powering off
  afterwards — is the system's problem.

```
┌────────────────────────────────┐
│        By Nunzio Raciti        │
│      HearAble · Avvio...       │
└────────────────────────────────┘
```

*The greeting, as it appears on the television. Four seconds, then the first real
subtitle replaces it.*

---

## 2. The first question: where does the audio come from?

Everything downstream is ordinary. Getting clean programme audio off a closed
set-top box, without cost, is the part that had to be invented.

| Route | Verdict | Why |
|---|---|---|
| Microphone near the TV | REJECTED | Room noise, and the viewer's own hearing aid feedback. The recogniser would be fed the worst version of a signal that exists perfectly inside the box. |
| HDMI audio extractor | REJECTED | Extra hardware in the living room, and the audio arrives already decoded and re-encoded — after the decoder's own output buffer, which is exactly the delay we are trying not to pay. |
| Enigma2 stream port 8001 | REJECTED | It works, and it *holds the tuner*. Zapping to a channel on another multiplex left the box with no service at all. Measured, not assumed. |
| **DVB demux PID filter** | **CHOSEN** | Read the selected audio PID straight off the demux of the service that is *already tuned*. Costs no tuner, no re-encode, and arrives ahead of the picture. |

### The demux tap

The receiver runs a Hisilicon DVB stack and exposes the standard Linux DVB API.
Enigma2 holds twenty-four PID filters open on `demux0` while showing a channel;
HearAble's tap is the twenty-fifth. It is userspace only — no kernel module, no
patched image, nothing persistent.

```c
/* the whole mechanism, in five calls */
open("/dev/dvb/adapter0/demux0", O_RDWR | O_NONBLOCK)
DMX_SET_BUFFER_SIZE   1 MiB
DMX_SET_PES_FILTER    pid    = <the audio PID the viewer selected>
                      input  = DMX_IN_FRONTEND
                      output = DMX_OUT_TSDEMUX_TAP
                      flags  = DMX_IMMEDIATE_START
read()                /* 188-byte MPEG-TS packets */
```

Three output modes were tried on the real box rather than inferred from the
header file, which mattered:

| Mode | Result |
|---|---|
| `DMX_OUT_TS_TAP` | Opens successfully, delivers **zero bytes** — it routes to the `dvr` device, not to this descriptor |
| `DMX_OUT_TAP` (PES) | Works, 127.3 kbit/s, first data after 0.27 s |
| `DMX_OUT_TSDEMUX_TAP` | Works, 132.8 kbit/s, first data after **0.03 s** — **chosen** |

**Why `DMX_IN_FRONTEND` is the whole trick.** The filter reads the stream
*entering* the box, not the stream the decoder is presenting. HearAble therefore
sits **ahead of the viewer's ears**, and every millisecond of that head start is
a millisecond the recogniser gets for free. It is also why the picture and the
subtitles can never drift apart for a reason of our making: we are not in the
picture's path at all.

---

## 3. The road, including the parts that failed

Five campaigns. Two of them ended in a closed door, and those two are the reason
the shipped design looks the way it does.

### PC baseline — prove the recogniser before building anything around it

Nemotron 3.5 ASR Streaming 0.6B, quantised `q8_0`, running on NeMo-Speech.cpp. On
a desktop Xeon it transcribes Italian at **RTF 0.476** under realistic pacing with
a display word error rate of **4.74 %**.

A Radeon RX 6600 over Vulkan is faster in raw throughput, but under pacing — the
only mode that matters for live audio, because the GPU cannot overlap work it has
not received yet — its advantage collapses from 5.7× to 2.2×. That single
observation is why the shipped appliance has no GPU.

### Arduino UNO Q — CLOSED

Can a €50 board host the recogniser? No, and it was worth five measured attempts
to be sure.

| Path | Result | Decisive number |
|---|---|---|
| CPU, native build | NO-GO | RTF 2.3146 |
| Vulkan, capability and build | PASS | 57.9 MB of shaders, zero errors |
| Vulkan, execution | blocked | 0.267 GFLOP/s, then driver refusal |
| GPU measured on its own | POOR | 2.1 GFLOP/s, 3.43 GB/s bandwidth |
| DSP / Hexagon / QNN | does not exist | no CDSP domain in the SoC |
| CPU, taken to the end | insufficient margin | needs 7.33×, physics allows 2.79× |

The closing number is the honest one: reaching real time needs **7.33×**, and the
physics of the memory bus allows **2.79×**. Amdahl never got a chance; bandwidth
closed the door first.

Two things did survive, and they matter: the same sources compile on the board
with its own gcc, and the transcript is *bit-identical to the PC's* — the same 74
words, **WER 0.00 %**. Different architecture, different compiler, different SIMD
kernels, not one letter moved.

### The 64-millisecond correction

The pipeline had been holding its subtitles back by 1.5 s, because a
screenshot-based measurement said the tap ran **2.4 s** ahead of the picture.
Asking the decoder directly — `AUDIO_GET_PTS` against the PTS of the packet the
PID filter is handing over *in the same instant* — gives **64 ms**, spread 32 ms
over a hundred samples.

The publication delay went to zero and stayed there. The viewer's own ear had
said so before the instrument did.

### T9 Plus, Intel N95 — the appliance that shipped

A fanless mini PC at half of real time, on a private cable, woken by the decoder
and switched off by itself. `T9_HEARABLE_GO`. Thirty minutes of deliberate abuse
with fifteen interventions: zero lost chunks, zero stale subtitles. Nineteen
injected faults, nineteen unattended recoveries.

### A/V delay — CLOSED

Could we delay the television instead of hurrying the subtitles? The idea: hold
the picture back by about a second so the subtitles catch up, while HearAble
keeps reading the live signal.

**The mechanism works.** With timeshift running, the tap stays 0.072 s from live
while the decoder presents a buffered picture 51.36 s behind, and the delay can be
set from the plugin by seeking to the live edge minus the amount wanted.

It was closed anyway, by a measurement taken over two minutes instead of ten
seconds:

| When | Delay |
|---|---:|
| Straight after the seek (2000 ms asked) | 0.577 s |
| ~30 s later | 0.225 s |
| ~1 min later | **0.065 s** — the live edge |

**The delay drains back to live on its own.** The earlier table calling it "stable
to 48 ms" had measured *reaching* the delay, not *holding* it — every row was a
ten-second window.

### The habit that produced most of the findings

Every defect of consequence in this project was found by doing the real thing, not
the convenient imitation of it. Unplugging the cable found a dead socket that
`ip link down` could not reproduce. Pressing the remote found two defects a
nineteen-case matrix had passed. Cutting mains power answered a Wake-on-LAN
question that months of software-off testing could not. The measurements are only
as good as the thing they are taken on.

---

## 4. How it fits together

Two machines, one private cable, two small protocols going in opposite
directions.

```
   DVB-T2
     │
     ▼
┌──────────────────────────────┐          ┌──────────────────────────────┐
│  DECODER · SF8008            │          │  MINI PC · INTEL N95         │
│                              │          │                              │
│  frontend1 ──▶ demux0        │          │  AAC decode + resample       │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│              PID tap         │          │  Nemotron 0.6B q8_0          │
│              (+64 ms)        │          │  RTF 0.4848                  │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│               relay ─────────┼──────────┼─▶ stabiliser → two lines     │
│              (ffmpeg)        │ 26 kB/s  │            │                 │
│                              │  AAC     │            │                 │
│  OSD panel ◀─────────────────┼──────────┼────────────┘                 │
│  (two lines) │               │ subtitle │   WebSocket                  │
└──────────────┼───────────────┘  _state  └──────────────────────────────┘
               ▼
          television            private cable · 10.77.0.0/24
                                no router, no DHCP, no gateway
```

The audio leaves the decoder compressed and never comes back; only two lines of
text return. The television is fed by the decoder exactly as it always was —
HearAble draws on top of the picture and is never in its path.

The split is deliberate. The decoder is a closed appliance with a 4.4 kernel and
no room to run a speech model; the mini PC has the compute but cannot draw on the
television. So each does the one thing it can: the box taps and paints, the mini
PC listens and recognises.

---

## 5. The decoder side

One Enigma2 plugin, about two thousand lines, doing four jobs: it taps, it relays,
it paints, and it wakes the other machine.

| | |
|---|---|
| Hardware | Octagon SF8008 V3 Supreme Combo (Hisilicon), one DVB-T2/C tuner on `frontend1`, one DVB-S2X on `frontend0` |
| Image | openATV 7.5.1, Linux 4.4.35, Enigma2 |
| Plugin | `/usr/lib/enigma2/python/Plugins/Extensions/HearAbleOSD`, loaded at `WHERE_SESSIONSTART` |
| Listens on | `:8770` — `/hearable` (WebSocket, subtitles in) and `/control` (HTTP, relay control) |
| Persistent state | `/etc/enigma2/hearable_look.json`, `hearable_sync.json`, `hearable_wol.json` |

### Drawing on a television

The on-screen display is an Enigma2 skin built at runtime, and almost every line
of it encodes a defect that was found on a real screen.

**The high byte is transparency.** Enigma2 colours are `#AARRGGBB` where `AA` is
*transparency*, not opacity. White text written as `#ffffffff` is the skin's own
definition of invisible: the glyphs were drawn see-through and took the colour of
whatever was behind them — green over a green bar, blue over a blue one. White is
`#00ffffff`.

**The window is the band, not the screen.** A full-screen transparent window looks
identical and behaves differently: it takes the remote control with it. Volume
still worked, because that is handled globally, but `MENU` did nothing — the
television looked broken. The window is now sized to exactly what it draws.

The panel is two lines of centred white text on a black band at **85 % opacity**,
each line about 7.5 % of screen height, touching, sitting 7 % up from the bottom
edge so it stays inside the safe area at any size. That geometry is broadcast
practice, not what happened to fit: it reads as one block from across a room. The
OSD canvas is **1280 × 720** even on an HD service, which is a trap worth naming —
laying the band out in 1080p coordinates puts it off the bottom of the screen.

#### What the viewer can change — `/etc/enigma2/hearable_look.json`

| Key | Range | In use | Effect |
|---|---:|---:|---|
| `font_scale` | 0.8 – 1.6 | 1.2 | Type size, with the box sized after the type so descenders keep their tails |
| `line_gap` | 0.25 – 1.0 | 0.25 | Distance between the two lines. Halved twice on the viewer's instruction |
| `background_opacity` | 0.0 – 1.0 | 0.85 | Band opacity. Below ~0.8 white text vanishes into a bright picture |
| `background_enabled` | bool | true | Band on or off entirely |
| `padding` | 0.0 – 0.5 | 0.0 | Extra room inside each line box |

A change applies immediately: the dialog is destroyed and re-instantiated rather
than requiring an Enigma2 restart, which would blank the picture.

### The remote control

Three keys, and nothing else taken from Enigma2.

| Code | Key | Does |
|---:|---|---|
| 62 | `F4` | HearAble on / off. On also sends the magic packet that wakes the mini PC |
| 61 | `F3` | Publication delay up, 100 ms per press |
| 60 | `F2` | Publication delay down |

**HearAble starts off after every boot.** The state is deliberately not persisted:
subtitles are something the viewer asks for, and a box that comes back from a
power cut already drawing on the screen is a box that has made a decision for
them.

```
┌────────────────────────────────┐
│        HearAble 1.5.1          │
│          Arrivederci           │
└────────────────────────────────┘
```

*Switching off. The version is shown here because it is the one place a viewer
without a terminal can read it.*

### The relay: sending audio without sending the programme

An earlier design streamed the whole container — video included — and required the
receiving machine to hold a bit-identical copy of the media so the decoder's play
position could be recovered by matching a frame off its screen. That is
unaffordable on a small machine, and unnecessary for live television.

The relay instead sends **only the selected audio track, still compressed**:
26 kB/s of MPEG-TS produced by an `ffmpeg` process outside Enigma2, so a slow or
absent receiver can never stall the user interface.

- **The relay connects out.** The mini PC listens and announces itself in its
  control poll. That is what lets the stream start exactly where the viewer is,
  and why an absent receiver costs the decoder nothing at all: no process is
  started, no port is held.
- **The stream says where it starts.** `-c copy` can only seek to a container
  boundary, so the relay begins about 0.65 s *before* the position asked for. The
  first packets of each epoch are probed for their timestamp and the excess is
  discarded at the receiver — both ends know a real timestamp, so the difference
  is simply dropped rather than corrected through a feedback loop.
- **A receiver that stops asking disappears** after an 8 s TTL, and the relay
  stops by itself.

### Waking the other machine

Pressing `F4` sends a Wake-on-LAN magic packet over the private link — broadcast
to `10.77.0.255` on ports 9 and 7, retried up to fifteen times at twenty-second
intervals, which gives a five-minute window. Measured wake time from soft-off:
**32.0 s**, three cycles out of three.

For a while the packet was proven in a test harness and the plugin never sent it.
The first real use found that immediately. It is now part of switching HearAble
on, and the reverse is handled too: powering the decoder off sends a farewell
datagram so the mini PC does not wait out its idle timer.

---

## 6. The mini PC side

A fanless N95 box that nobody logs into: it wakes when asked, recognises, and
switches itself off when the television stops talking.

| | |
|---|---|
| Hardware | T9 Plus, Intel N95 — 4 cores / 4 threads, 800 MHz – 3.40 GHz, L1d 128 KiB · L2 2 MiB · L3 6 MiB, 15 GiB RAM |
| SIMD | `sse4_2`, `avx`, `avx2`, `fma`, `f16c`, `bmi2`, `avx_vnni` |
| OS | Debian 13 trixie, Linux 6.12.107 |
| Model | Nemotron 3.5 ASR Streaming 0.6B, `q8_0`, 741 548 352 bytes, sha256 `a5c435f2…f429ae` |
| Runtime | NeMo-Speech.cpp at `4f96762`, ggml, CPU only — no GPU in the machine and none wanted |
| Network | `enp1s0` = `10.77.0.2` static, private cable to the decoder. Wi-Fi for maintenance only |

### What actually made it fast

No compute code was optimised for this machine. The speed came from one line of
sysfs.

| Condition | RTF |
|---|---:|
| Baseline, governor `powersave` | 0.6741 |
| Governor `performance` | 0.4896 |
| Ten minutes of real television | 0.4962 |
| Thirty minutes of deliberate abuse | 0.4848 |

*Lower is faster; 1.0 is the edge of viability.*

**The governor is worth 27.6 % of the real-time factor.** The recogniser works in
bursts — roughly 105 ms of compute every 160 ms — and `intel_pstate` in
`powersave` never sees a burst long enough to clock up. Switching the governor is
worth more than `-march=native` and AVX-VNNI together, *by a factor of twenty*.
The AVX-VNNI instruction the CPU advertises would fold today's `vpmaddubsw` +
`vpmaddwd` int8 product into a single `vpdpbusd`; it remains an unpulled lever,
and a small one.

### The four units that make it an appliance

| Unit | Job | Notable |
|---|---|---|
| `hearable-governor` | Sets every core to `performance` at boot, back to `powersave` on stop | A dependency of the pipeline, not an afterthought |
| `hearable-t9` | The recognition pipeline | `Restart=always`, never `on-failure` |
| `hearable-idle-watchdog` | Powers the machine off when nobody is watching | Runs as root, independent of the recogniser |
| `hearable-rgb` | The status ring on the case | Missing `pyserial` logs once and continues without it |

**Two systemd details that cost real downtime.** `StartLimitIntervalSec` under
`[Service]` is silently ignored — the restart limit simply does not exist. It
belongs in `[Unit]`. And `Restart=on-failure` is wrong for an appliance: a
measurement tool once stopped the pipeline with `SIGINT`, systemd saw exit code 0,
and the service stayed dead for twenty-three minutes with nobody the wiser. A
clean exit is still an appliance that has stopped making subtitles.

### Switching itself off

The watchdog asks two questions and needs both answers to act: is anything
connected to the audio port (read from `/proc/net/tcp` on port 9010), and is the
decoder still watching (its `/api/powerstate`, falling back to a ping)? With no
audio session it starts a timer — twenty minutes by default, three when the
decoder has gone to standby, both in `/etc/default/hearable` — and powers the
machine down. Any returning session cancels it.

The practical consequence is that the viewer never thinks about the mini PC at
all. It appears when `F4` is pressed and is gone a few minutes after the
television goes quiet.

### The status ring

The case has an RGB ring on a CH340 serial bridge. The protocol is a five-byte
frame — `0xFA`, mode, brightness, speed, checksum — at 10 000 baud, with
brightness and speed inverted relative to what you would guess.

**The colour map scored 0 out of 5.** The first mapping of states to colours was
derived from the controller's documentation and tested blind against the person
who would actually look at the ring. It was wrong on all five states. The shipped
map is the one read off the hardware by the viewer; it scores 6 out of 6.
Documentation describes intent, eyes describe the ring.

### Surviving the network

The single genuine defect the thirty-minute torture campaign found was in the
audio receiver, and only a physically unplugged cable exposed it: a socket timeout
was being swallowed with `continue`, so a dead connection was held forever —
eighteen respawns, three half-open sockets, 80 kB of unread data. The receiver now
gives up after 15 s of silence and sets `SO_KEEPALIVE` with a 10 s idle, 5 s
interval and 3 probes.

---

## 6b. It also subtitles what you play on the box

Live television is the hard case, and it is the one this document has been
about. But the same chain subtitles **a film played from the receiver's own
storage** — a USB stick, an internal drive — with no extra component and no
second code path.

Nothing special happens for it. The relay is a second reader of whatever the
decoder is playing: for live television it reads the demux, for a file it reads
the file, and in both cases it forwards only the selected audio track,
compressed, from ahead of the viewer's position. The receiver sees
`source_type: file` instead of `dvb` in the control response, and carries on.

### Measured

Eight checks against a real film on the box's storage, with the remote driving
pause, seek, a change of file and `F4`:

| Check | Result | |
|---|---|---:|
| First subtitle after play starts | PASS | 36.1 s |
| Pause | PASS | panel freezes, relay `paused`, **0** new stale states |
| Resume | PASS | 1.2 s |
| Seek forward | PASS | 19.1 s |
| Seek backward | PASS | 64.9 s |
| Change to a different film | PASS | new epoch 41 → 42, **0** stale from the previous file |
| `F4` off then on | PASS | 5.4 s |
| Audio track change | `NOT_TESTABLE` | the film had one track |

`FILE_STALE_SUBTITLES: 0` across the whole run: nothing from before a pause, a
seek or a file change was ever painted afterwards. An earlier regression at
v1.2.0 had already closed the same path with pause, resume, seek and track
change all passing.

### Two things that are deliberately different

**The sync keys are refused on a file source.** `F2` and `F3` adjust the
publication delay, which exists to compensate for how far ahead of the picture
the tap runs. On a file there is no such offset to compensate, so pressing them
does nothing and says so, rather than silently applying a correction with no
meaning.

**The first subtitle takes longer.** 36.1 s against about 4 s on live
television, because starting a film means the relay spawning, the stream
starting from a container boundary, and the recogniser having heard nothing yet.
Once running, it behaves the same.

### What was not tested

The measurements were taken with the film on the box's USB storage
(`/media/hdd`). An NVMe or internal drive is the same path as far as HearAble is
concerned — it reads whatever Enigma2 is playing, and never touches the storage
itself — but that has not been measured, and this document does not claim it.


## 7. The two protocols

Deliberately small, deliberately separate, and each with one rule that shapes
everything else.

### Subtitles — `hearable-enigma2/1`

A persistent WebSocket carrying one JSON object per text frame. The producer
connects out; the renderer listens. No TLS and no authentication — the private
cable is the trust boundary.

| Message | Direction | Purpose |
|---|---|---|
| `hello` | both | Role, project, protocol version |
| `capabilities` | both | Line count, line length, ACK support |
| `subtitle_state` | producer → renderer | The two lines to display |
| `clear_subtitles` | producer → renderer | Blank the display now |
| `source_changed` | producer → renderer | The channel changed; a new epoch begins |
| `heartbeat` | producer → renderer | Liveness while nothing is being said |
| `render_ack` | renderer → producer | This sequence number reached the screen |

**Never send a cumulative transcript.** A `subtitle_state` carries the two display
lines and nothing else; the validator rejects a running transcript outright,
because sending one makes traffic and memory quadratic in session length.

**The producer never waits.** Acknowledgements are observational — a slow renderer
costs subtitle freshness, never backpressure into the recogniser. There is exactly
one pending state, and a newer one replaces it.

### Audio — `hearable-audio-relay/1`

Two channels in opposite directions, so that a slow receiver can never stall
Enigma2.

| | Control | Media |
|---|---|---|
| Direction | receiver → box | box → receiver |
| Transport | HTTP GET on `:8770` | TCP, MPEG-TS, port chosen by the receiver |
| Who connects | the receiver | the box |
| Cadence | every 500 ms | continuous, at playback rate |
| Inside Enigma2 | yes, the plugin | no, a separate `ffmpeg` |

The control request doubles as the announcement and the heartbeat: the receiver
says where it wants the audio, how much head start it wants, and how far ahead of
the viewer it has measured itself to be. Those last two are different numbers and
must not be confused — one is a request, the other a measurement.

---

## 8. Where the second goes

The subtitle arrives about a second after the word is spoken. Almost all of it is
the recogniser deciding, and that is a choice rather than an accident.

Measured end to end: **p50 1.072 s** between the audio being consumed and the
subtitle state being emitted (n = 1461, p05 0.893 s). Reconstructed from the
settings in use:

| Component | Cost | What it buys |
|---|---:|---|
| `chunk_ms: 160` | ~80 ms avg | Audio reaches the recogniser in blocks |
| `rnnt_right_context: 1` | ~160 ms | The model looks one block ahead before deciding |
| ASR compute | 74 ms | Nemotron on the CPU, p50 |
| Stabiliser confirmations | 320–480 ms | A word becomes "stable" only after N agreeing passes |
| Tail hold + flush | ≤450 ms | The last word is held until another arrives |
| Panel repaint cap | ≤166 ms | Six repaints a second, so corrections do not flicker |
| Publication delay | 0 ms | Nothing — and that is the correction |

More than half of that second is spent *not* showing something that might be
wrong. The stabiliser waits for a word to be confirmed, and the unconfirmed tail
is not displayed at all. A faster subtitle that rewrites itself in front of the
reader is worse than a slower one that does not, and for a reader who depends on
the text rather than glancing at it, it is much worse.

**An instrument disagreeing with an eye, resolved.** Shifting the subtitle by 3 s
was clearly visible in three independent clock domains — 3018.9 ms on the
monotonic clock, 3008.0 ms on the PTS, +3.31 s on a filmed marker — and completely
invisible on screen. The reason is that the panel repaints only every 0.55 s and
the recogniser's own arrival noise is 3.75 s wide from p05 to p95: the noise is
wider than the shift. The verdict was recorded as an invalid measurement method,
and no code was changed to chase it.

---

## 9. What is not finished, and what was never measured

Recorded as gaps rather than quietly omitted, because a missing measurement that
looks like a result is the most expensive thing in this project.

| Item | State | Detail |
|---|---|---|
| Field testing | IN PROGRESS | The system is in daily use. One issue is outstanding and still to be characterised. |
| Long A/V delays | NOT MEASURED | 1.37 s and 7.56 s were each observed for ten seconds only. Whether a large delay holds for hours is unknown — and the branch is closed regardless. |
| Timeshift in RAM | INCONCLUSIVE | The buffer appeared in `tmpfs` and flash writes did not stop. Not a negative result; an unanswered question. |
| AVX-VNNI | AVAILABLE | The CPU advertises it and the build does not use it. Expected to be small next to the governor. |
| Tool default address | BY DECISION | About 33 tools still default to the decoder's old address. Deliberately not replaced: a hard-coded address would be wrong again at the next change. |
| Full reboot on install | OBSERVED | Installing the plugin restarts the whole box, not just Enigma2 as the command claims. Seen, not investigated. |

### What has been answered

- **Wake-on-LAN survives a mains cut.** Everything previously known about waking
  the mini PC was measured from soft-off, with the power supply keeping the
  network card alive. With the power physically pulled from both machines, the
  mini PC still came up 6.4 minutes after the decoder — when `F4` was pressed —
  with the clocks of the two machines agreeing to within a second and the power
  button untouched.
- **The demux tap does not cost a tuner.** Thirty cross-multiplex zaps, thirty
  successes, no lost services.
- **The pipeline survives being attacked.** Nineteen injected faults, nineteen
  unattended recoveries; thirty minutes of deliberate interference with zero lost
  chunks and zero stale subtitles.

### The cold start found a defect, which is what cold starts are for

The plugin reported an uptime of 78 minutes on a decoder that had been on for 12.
The box sets its wall clock *after* Enigma2 starts — 88 minutes of wall clock
consumed in two minutes of real time, measured directly — so every elapsed time
carried the jump. Uptime was the harmless end of it; the same clock also held the
four seconds the greeting is supposed to stand for, and the rule that takes a
stale subtitle down. All durations now run on the monotonic clock, with a test
that refuses to let any duration be measured on the wall clock again.

---

## 10. How this was built

HearAble was **orchestrated by Nunzio Raciti and written by Claude** (Anthropic's
Opus model) working to his direction. He set the goal and the constraints, made
every design decision, owned the hardware, and did the testing that mattered
most. Claude was the material executor: the code, the tests, the measurement
tools and these reports.

The division is visible in the findings. Several defects of consequence were
found not by a test but by the person using the real thing:

| Found by | What it found |
|---|---|
| Pressing buttons on the remote | Two faults a nineteen-case automated matrix had passed |
| Physically unplugging a cable | A dead socket `ip link down` could not reproduce |
| Cutting mains power | That Wake-on-LAN survives it — unanswerable by software-off testing |
| Reading the status ring by eye | A colour map that scored 0 out of 5 against its own documentation |
| Listening to the television | That the subtitles were late, before the instrument agreed |

### Nothing advanced on an opinion

Every phase was gated by tests and measurements. That is the working rule of the
project, not a description added afterwards.

**246 automated tests**, all runnable without a set-top box: the wire protocol,
the receiver-side renderer and its epoch and sequence rejection, the stabiliser,
the commit rules, the configuration loaders, the clock discipline, and the
version consistency between the package and the plugin.

**Purpose-built gates** for everything that needs real hardware — the LAN gate,
the live session, the UI lifecycle, boot cycles, Wake-on-LAN, the idle timer, the
file player, a nineteen-case failure matrix. Each produces a verdict and a
machine-readable block rather than a log someone has to interpret.

**Campaign reports** under `benchmark/results/`, every line labelled
**MEASURED**, **DERIVED** or **NOT_MEASURED**, so a gap can never be read as a
result.

**Verdicts that are allowed to say no.** Two investigations were closed by the
numbers that closed them: the UNO Q needs 7.33× and the memory bandwidth allows
2.79×; the A/V delay is reachable but drains back to live in about two minutes.
A gate that produces too little evidence reports `NON_MISURATO` rather than
passing on nothing.

**And when a measurement disagreed with the instrument, the instrument lost.**
The tap was believed to run 2.4 s ahead of the picture, and the pipeline was
built to compensate for it. Asking the decoder directly — two clocks read in the
same instant, packet in hand — gave 64 ms. Wrong by a factor of forty, and wrong
in the direction that made the product worse.

---

*HearAble 1.5.1 — Italian realtime subtitling for DVB-T2. Orchestrated by Nunzio
Raciti, written by Claude (Opus) to his direction. Octagon SF8008 V3 Supreme
Combo · T9 Plus Intel N95 · Nemotron 3.5 ASR Streaming 0.6B on NeMo-Speech.cpp.*
