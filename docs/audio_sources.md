# HearAble Audio Sources

HearAble now treats audio acquisition as a pluggable layer:

```text
AudioSource -> CanonicalAudioFrame -> ASR
```

The portable ASR pipeline consumes `CanonicalAudioFrame` and does not need to
know whether audio came from PipeWire, WAV replay, HDMI, USB, ReSpeaker or a
future network source.

## Contract

An `AudioSource` provides:

```text
open()
start()
read_chunk()
stop()
close()
```

`read_chunk()` returns `CanonicalAudioFrame` or `None` when no frame is
currently available. The source exposes `eof` when the stream is finished.

## Canonical Audio

All implemented sources produce:

```text
PCM mono 16 kHz float32
```

`CanonicalAudioFrame` fields:

- `seq`
- `samples`
- `sample_rate`
- `channels`
- `source_start_time`
- `source_end_time`
- `monotonic_available_time_ns`
- `wall_available_time_ns`
- `expected_feed_monotonic_ns`
- `source_clock_drift_ms`
- `dropped_chunks`

WAV replay preserves absolute-deadline pacing in realtime mode and keeps
throughput mode unpaced.

## Implemented

- `pipewire`: captures the selected PipeWire sink monitor through `pw-record`
  and converts to canonical audio with `ffmpeg`.
- `wav`: replays a 16 kHz mono WAV file through the same ASR/stabilizer path.
- `udp`: receives raw PCM signed 16-bit little-endian datagrams and converts
  them to canonical 16 kHz mono float32 frames.
- `fake`: test-only source.

## JSON

Default:

```text
config/audio_source.json
```

PipeWire:

```json
{
  "type": "pipewire",
  "options": {
    "target": "",
    "capture_latency": "20ms"
  }
}
```

WAV:

```json
{
  "type": "wav",
  "options": {
    "path": "benchmarks/audio/browser_capture_16k_mono.wav"
  }
}
```

UDP PCM16LE:

```json
{
  "type": "udp",
  "options": {
    "listen_address": "0.0.0.0",
    "listen_port": 18080,
    "audio_format": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "source_timeout_ms": 1000,
    "max_datagram_bytes": 65535,
    "rx_buffer_bytes": 1048576
  }
}
```

Existing CLI flags remain compatible. `--input-wav` selects `wav`; `--target`
and `--capture-latency` override PipeWire options.

Runnable WAV example:

```text
config/audio_source_wav_browser_capture.json
```

The UDP source exposes telemetry in realtime metrics under `audio_source`,
including packet counts, byte counts, truncation/error counts, inter-packet
gap, queue depth and source state. If no datagram arrives for
`source_timeout_ms`, the state becomes `NO_PACKETS`; this is source absence,
not valid acoustic silence. Any partial, sub-chunk audio buffered at timeout is
dropped so stale audio is not joined with a later source restart.

## Future Examples

These are not implemented:

```json
{ "type": "hdmi_audio", "options": { "device": "hw:0,0" } }
```

```json
{ "type": "respeaker_xvf3800", "options": { "device": "/dev/snd/..." } }
```

Adding a source should require:

1. Implement the contract.
2. Produce canonical audio.
3. Preserve timestamps where available.
4. Register the type in `AudioSourceRegistry`.
5. Add JSON and tests.
