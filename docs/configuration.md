# HearAble Configuration

HearAble uses JSON configuration files under `config/`.

Command-line arguments still have priority over JSON defaults. Use JSON files
for stable presets and CLI flags for one-off overrides during tests.

## Runtime Server Config

Default file:

```text
config/realtime.json
```

The realtime server loads this file automatically unless `--runtime-config` is
provided.

```bash
python3 -m hearable.realtime_server --runtime-config config/realtime.json
```

Main fields:

| Field | Purpose |
| --- | --- |
| `model` | Nemotron GGUF model path. Relative paths resolve from the repo root. |
| `lib_dir` | NeMo-Speech.cpp build directory containing `libnemo_speech_asr_c.so`. |
| `language` | Recognition language, currently `it-IT`. |
| `target` | Optional PipeWire target sink/source identifier. Empty means default capture routing. |
| `input_wav` | Optional 16 kHz mono WAV replay input. Empty means live PipeWire capture. |
| `host` / `port` | aiohttp UI/WebSocket bind address. |
| `duration` | Auto-stop duration in seconds. `0.0` means no duration limit. |
| `chunk_ms` | ASR push chunk size in milliseconds. |
| `gpu` | `-1` for CPU, `0` or higher for GPU backend. |
| `rnnt_right_context` | Nemotron RNNT right-context setting. |
| `capture_latency` | PipeWire capture latency string passed to `pw-record`. |
| `max_words` | Stabilizer maximum fallback word window. |
| `stabilizer_history_size` | Number of partial hypotheses retained by the stabilizer. |
| `stabilizer_min_confirmations` | Partial confirmations required before stable fallback text grows. |
| `stabilizer_timeout_sec` | Silence timeout used by the stabilizer finalization fallback. |
| `max_chunks` | Stop after N ASR chunks. `0` disables the limit. |
| `flush_on_stop` | Ask ASR to flush final events on shutdown. |
| `metrics_output` | Optional metrics JSON path. Directory paths are written as `metrics.json`. |
| `events_output` | Optional events JSONL path. |
| `include_latency_records` | Include per-state latency records in metrics JSON. |
| `ui_max_fps` | Server-side latest-state send cap for browser/UI updates. |
| `ui_final_text_chars` | Maximum cumulative transcript characters sent to the browser debug view. |
| `led_config` | LED subtitle formatter config path. |
| `pending_render_limit` | Maximum pending browser render ACK records retained by the server. |
| `final_ack_wait` | Grace period for final render ACKs before metrics are written. |
| `mode` | `realtime` for wall-clock pacing or `throughput` for unpaced benchmark replay. |
| `require_browser_ready` | Delay ASR startup until the browser sends instrumentation readiness. |
| `browser_ready_timeout` | Maximum seconds to wait for browser readiness when required. |
| `verbose_pipeline` | Print per-chunk pipeline trace logs. |
| `audio_source_config` | Optional default path for pluggable AudioSource JSON. |
| `subtitle_sink_config` | Optional default path for pluggable SubtitleSink JSON. |

## Audio Source Config

Default:

```text
config/audio_source.json
```

Shape:

```json
{
  "type": "pipewire",
  "options": {
    "target": "alsa_output.pci-0000_05_00.1.2.hdmi-stereo",
    "capture_latency": "20ms"
  }
}
```

Implemented types: `pipewire`, `wav`, `udp`.

For WAV replay, use:

```json
{
  "type": "wav",
  "options": {
    "path": "benchmarks/audio/browser_capture_16k_mono.wav"
  }
}
```

Runnable example file:

```text
config/audio_source_wav_browser_capture.json
```

For UDP raw PCM16LE input, use:

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

Runnable UDP example files:

```text
config/audio_source_udp_pcm16le.json
config/realtime_two_line_udp.json
config/uno_q_udp_relay.json
```

The canonical ASR input remains PCM mono 16 kHz float32. See
`docs/audio_sources.md`.

## Subtitle Sink Config

Default:

```text
config/subtitle_sink.json
```

Shape:

```json
{
  "type": "browser",
  "options": {}
}
```

Implemented types: `browser`, `null`, `console`, `octagon_udp`.

Use `null` for core/stability benchmarks without a browser and `console` for
diagnostic terminal output. Browser ACK/render telemetry is optional and
browser-specific. Runnable example files:

```text
config/subtitle_sink_null.json
config/subtitle_sink_console.json
config/subtitle_sink_octagon_udp.json
```

See `docs/subtitle_sinks.md`.

## Two-Line Vulkan Preset

Default live launcher preset:

```text
config/realtime_two_line_vulkan.json
```

This file contains only overrides from `config/realtime.json`:

```json
{
  "chunk_ms": 80,
  "rnnt_right_context": 3,
  "ui_max_fps": 6.0,
  "led_config": "config/led_subtitles.json",
  "final_ack_wait": 1.0
}
```

The launcher uses this preset automatically:

```bash
./scripts/run_two_line_subtitles.sh
```

Equivalent explicit form:

```bash
./scripts/run_realtime_vulkan.sh \
  --runtime-config config/realtime_two_line_vulkan.json \
  --duration 1800
```

Environment variables still supported by the launcher:

| Variable | Purpose |
| --- | --- |
| `PORT` | UI/WebSocket port, default `8765`. |
| `DURATION` | Auto-stop duration, default `1800`. |
| `TARGET` | PipeWire target passed as `--target`. |
| `OUTPUT_DIR` | Manual run artifact directory. |

## Two-Line UDP Preset

UDP live transport uses:

```text
config/realtime_two_line_udp.json
```

It preserves the same `chunk_ms`, `rnnt_right_context`, browser pacing and LED
formatter config as the Vulkan/PipeWire preset, but selects:

```text
config/audio_source_udp_pcm16le.json
config/subtitle_sink_browser.json
```

Start the receiver with:

```bash
python3 -m hearable.realtime_server \
  --runtime-config config/realtime_two_line_udp.json
```

The default UDP listener is `0.0.0.0:18080`. Keep committed defaults generic;
put private LAN addresses in launcher overrides or local shell variables.

The relay-side default config is:

```text
config/uno_q_udp_relay.json
```

It listens on `0.0.0.0:18079`, forwards to `127.0.0.1:18080`, uses bounded
console diagnostics and assumes raw PCM16LE 16 kHz mono only for sample
preview. Forwarding remains payload-byte-identical.

## LED Subtitle Config

Display and formatter rules live in:

```text
config/led_subtitles.json
```

Important fields:

| Field | Purpose |
| --- | --- |
| `MAX_CHARS` | Maximum characters per physical row. Current LED target is `40`. |
| `SOFT_BREAK_MIN` | Minimum row length before punctuation may trigger a soft break. |
| `COMMIT_STABLE_UPDATES` | Repeated partial count needed before committing a word. |
| `COMMIT_LOOKAHEAD` | Lookahead words required before committing earlier words. |
| `COMMIT_TAIL_HOLD_WORDS` | Tail words held back to avoid ASR fragments such as `fac`, `cola`, `ri`. |
| `COMMIT_TAIL_FLUSH_MS` | Silence timeout before held tail words are forced to display. |
| `MIN_LINE_HOLD_MS` | Minimum time before the previous line can be replaced. |
| `IDLE_CLEAR_MS` | Silence timeout before both display rows are cleared. |
| `ORPHAN_WORDS` | Italian short/linking words that should not appear alone at line end/start. |
| `SHOW_UNSTABLE_TAIL` | Reserved for future matrix rendering of unstable text. |

Current default live readability settings in `config/led_subtitles.json` match
the validated `C_tail1` baseline: `COMMIT_LOOKAHEAD=1`,
`COMMIT_TAIL_HOLD_WORDS=1` and `COMMIT_TAIL_FLUSH_MS=450`.

The frozen v1.0.2 latency/readability baseline copy also lives at:

```text
benchmarks/stability_optimization/C_tail1_led_subtitles.json
```

Relative to Profile C, `C_tail1` changes exactly one stability variable:

```text
COMMIT_TAIL_HOLD_WORDS=1
```

Use either this frozen copy or the default config when testing the controlled
baseline:

```bash
./scripts/run_realtime_vulkan.sh \
  --runtime-config config/realtime_two_line_vulkan.json \
  --led-config config/led_subtitles.json
```

## Version

The release version is defined in:

```text
hearable/__init__.py
```

Check it with:

```bash
python3 -m hearable.realtime_server --version
./scripts/run_two_line_subtitles.sh --version
```

Current release: `1.0.3`.
