# HearAble Subtitle Sinks

HearAble now treats subtitle output as a pluggable layer:

```text
Formatter -> SubtitleState -> SubtitleSink
```

The core publishes a technology-neutral `SubtitleState` and does not need to
know whether the display is a browser, terminal, null benchmark sink, LED
matrix, OLED panel, serial transport or future hardware.

## Contract

A `SubtitleSink` provides:

```text
open()
publish(state)
clear()
flush()
close()
telemetry()
```

The base contract does not require ACKs, render timing, brightness, speaker
colors or animation support. Those are sink-specific capabilities.

## SubtitleState

Fields:

- `seq`
- `timestamp`
- `upper_line`
- `lower_line`
- `stable`
- `unstable`
- `final_text`
- `final_text_truncated`
- `raw`
- `is_final`
- `clear`
- `audio_processed`
- `confidence`
- `led`
- `latency`
- `commit_events`
- `metrics`
- `metadata`

Browser/WebSocket fields are derived by `BrowserSubtitleSink` when needed.

## Implemented

- `browser`: preserves the current WebSocket UI, latest-state coalescing and
  optional browser render ACK telemetry.
- `null`: stores states in memory and emits no display or ACK telemetry.
- `console`: prints the two visible rows to stdout for diagnostics.
- `octagon_udp`: sends subtitle state datagrams to an Octagon-side subtitle
  receiver/overlay process.

## JSON

Default:

```text
config/subtitle_sink.json
```

Browser:

```json
{
  "type": "browser",
  "options": {}
}
```

Null benchmark sink:

```json
{
  "type": "null",
  "options": {}
}
```

Console diagnostics:

```json
{
  "type": "console",
  "options": {}
}
```

Octagon UDP output:

```json
{
  "type": "octagon_udp",
  "options": {
    "host": "127.0.0.1",
    "port": 18081,
    "format": "json",
    "max_datagram_bytes": 4096
  }
}
```

Runnable example files:

```text
config/subtitle_sink_null.json
config/subtitle_sink_console.json
config/subtitle_sink_octagon_udp.json
```

## Future Examples

These are not implemented:

```json
{ "type": "led_matrix", "options": { "transport": "serial", "device": "/dev/ttyUSB0" } }
```

Transport details should stay below the sink boundary unless multiple concrete
sinks need the same transport abstraction.

Adding a sink should require:

1. Implement the contract.
2. Consume `SubtitleState`.
3. Register the type in `SubtitleSinkRegistry`.
4. Add JSON.
5. Add tests for telemetry/capabilities where relevant.
