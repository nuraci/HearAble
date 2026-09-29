# HearAble ⇄ Enigma2 Subtitle Protocol, version 1

The contract between the HearAble producer and the SF8008 renderer plugin.

This document and `hearable/enigma2_protocol.py` are the single source of truth.
Both ends import that module, so the wire format cannot drift between them. If
the SF8008 agent has already frozen a different transport, **this document must be
reconciled with theirs rather than forked** — the producer side is the flexible one.

## Transport

Persistent WebSocket over the trusted LAN.

```text
producer (HearAble PC / Arduino UNO Q)  ──ws──▶  renderer (SF8008 plugin)
                                        ◀──ack──
```

- Default endpoint: `ws://<receiver>:8790/hearable`.
- Text frames carrying one JSON object each.
- The producer connects out to the renderer. The renderer listens.
- No TLS and no authentication in version 1: the LAN is the trust boundary. Do not
  expose the port beyond it.

## Two rules that shape everything

**1. Never send a cumulative transcript.** A `subtitle_state` carries the two
display lines and nothing else — no `final_text`, no `raw`, no running transcript.
`validate()` rejects those fields outright. Sending the growing transcript would
make traffic and memory quadratic in session length.

**2. The producer never waits for the renderer.** ACKs are observational. A slow
or absent renderer costs subtitle freshness, never backpressure into the ASR
pipeline. There is exactly one pending subtitle state; a newer one replaces it.

## Messages

Every message carries `type` and `protocol_version`. All except the handshake also
carry an integer `seq`.

| Type | Direction | Purpose |
|---|---|---|
| `hello` | both | Identify role, project and protocol version |
| `capabilities` | both | Line count, line length, ACK support |
| `subtitle_state` | producer → renderer | The two lines to display |
| `clear_subtitles` | producer → renderer | Blank the display now |
| `source_changed` | producer → renderer | The channel changed; a new epoch begins |
| `heartbeat` | producer → renderer | Liveness while no subtitle is flowing |
| `render_ack` | renderer → producer | This seq reached the screen |
| `error` | both | Something was refused |

### subtitle_state

```json
{
  "type": "subtitle_state",
  "protocol_version": 1,
  "seq": 1842,
  "source_id": "1:0:19:2B66:3F3:1:C00000:0:0:0:",
  "source_epoch": 7,
  "upper_line": "riga precedente stabile",
  "lower_line": "riga corrente stabile",
  "clear": false,
  "generated_monotonic_ms": 123456789.0
}
```

- `upper_line` / `lower_line`: at most **40 characters** each. The producer clips at
  the network boundary, so a formatter bug cannot put an over-long line on the wire.
- `seq`: strictly increasing within one epoch of one sender session.
- `source_epoch`: increments on every source change. See below.
- `generated_monotonic_ms`: the **producer's** monotonic clock. Never subtract it
  from a renderer timestamp; the two machines have unrelated clocks.

### clear_subtitles

```json
{"type": "clear_subtitles", "protocol_version": 1, "seq": 1843,
 "source_epoch": 8, "reason": "source_changed"}
```

Clearing must take effect immediately and must not wait for new speech.

### source_changed

```json
{"type": "source_changed", "protocol_version": 1, "seq": 1844,
 "source_id": "1:0:19:2B67:...", "previous_source_id": "1:0:19:2B66:...",
 "source_epoch": 8, "reason": "service_changed"}
```

### render_ack

```json
{"type": "render_ack", "protocol_version": 1, "seq": 1842,
 "source_epoch": 7, "rendered_monotonic_ms": 987654321.0}
```

The renderer may acknowledge only the latest seq it painted; it is not required to
acknowledge every state it received.

## source_epoch

A channel change bumps the epoch. Packets from the old channel are still in flight
when it happens, and without the epoch they would land on the new channel's picture.

```text
channel A, epoch 12
  ↓ zap
channel B, epoch 13
  ↓
a late epoch-12 subtitle arrives  →  DISCARD
```

The receiver rule, implemented by `EpochGuard`:

- `epoch < current` → reject as stale.
- `epoch > current` → adopt it and restart sequence tracking, because `seq` is only
  monotonic within an epoch.
- `epoch == current` and `seq <= latest_seq` → reject as out of order.

## Ordering: one state slot, one control queue

Coalescing applies to `subtitle_state` only.

```text
subtitle_state  →  single slot, newest wins, older is dropped unsent
control         →  bounded FIFO (source_changed, clear_subtitles), never coalesced
```

Control messages must not supersede each other: a `clear_subtitles` that swallowed
the `source_changed` before it would leave the renderer on the wrong epoch. The
send loop drains control before the pending state.

When the source changes, any pending subtitle state is **dropped rather than sent**:
it belongs to the channel being left.

## Heartbeat and reconnect

- The producer sends `heartbeat` when no subtitle has been sent for `heartbeat_ms`
  (default 2000).
- On disconnect the producer retries with bounded exponential backoff (250 ms →
  5000 ms by default) and re-handshakes.
- **After reconnect the producer does not replay history.** The renderer receives
  the current state, because anything older is by definition obsolete.
- Subtitle transport failure never restarts the ASR. Audio input and subtitle
  output are independent failure domains.

## Timing

Use monotonic clocks for local durations only. Never subtract raw monotonic
timestamps taken on two different machines. Without validated clock
synchronisation, report round-trip and ACK timing, not one-way latency.

## Versioning

`protocol_version` is `1`. A receiver must reject a version it does not implement
rather than guess. Add optional fields for compatible extensions; change the
version for anything a version-1 peer would misinterpret.
