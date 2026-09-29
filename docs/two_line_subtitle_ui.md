# HearAble Two-Line Subtitle UI

The browser subtitle view now has two logical rows. With `?formatter=led`, the
rows come from the server-side LED formatter and obey `MAX_CHARS=40` from
`config/led_subtitles.json`:

- `previous_line`: the last completed subtitle segment.
- `current_line`: the live segment currently being composed.

The server still sends the latest coalesced subtitle state with monotonic `seq`
values. In LED mode the browser renders `msg.led` directly; in browser fallback
mode it derives the two-line presentation from `stable`, `unstable` and
`is_final`. It does not render the cumulative `final_text` transcript in the
main subtitle area.

Browser fallback promotion rule:

- explicit `is_final` promotes the current visible segment;
- terminal `.`, `?` or `!` promotes the current visible segment;
- if no new subtitle state arrives for `1200 ms`, the pause fallback promotes
  the current line.

In browser fallback mode, line 2 is not append-only. Incoming ASR text first
lands in a pending line. Visible text is exposed at a readable cadence, so fast
partial revisions do not flash on every ASR update. Tail corrections are
debounced for `350 ms`; if the ASR keeps rewriting, pending text is forced
visible after `800 ms` so the line does not appear stalled.

The page enforces two physical subtitle rows. Rows do not wrap. In LED mode,
long segments are packed by the server into two rolling rows of up to `40`
characters each. In browser fallback mode, the older word-count formatter still
uses `max_line_words`.

Rendering remains coalesced:

- server-side latest-state coalescing is unchanged;
- browser rendering is capped by `ui_fps`, default `6`;
- `?formatter=led` renders `msg.led.previous_line/current_line` directly;
- `?formatter=browser` keeps the older browser-side word-count formatter;
- pending visible changes are flushed after `display_update_ms`/debounce timing;
- DOM text is mutated only when `previous_line` or `current_line` changes;
- render ACKs are still sent for processed visible states to preserve latency
  and backlog telemetry.

Configuration:

- server `--runtime-config` default: `config/realtime.json`;
- live launcher preset: `config/realtime_two_line_vulkan.json`;
- server `--ui-max-fps` default comes from runtime config, currently `6.0`;
- server `--led-config` default comes from runtime config, currently
  `config/led_subtitles.json`;
- LED row packing and commit behavior live in `config/led_subtitles.json`;
- browser query `?formatter=` default: `led`;
- browser query `?ui_fps=` default: `6`;
- browser query `?sentence_pause_ms=` default: `1200`;
- browser query `?display_update_ms=` default: `250`;
- browser query `?rewrite_debounce_ms=` default: `350`;
- browser query `?max_pending_hold_ms=` default: `800`;
- browser query `?max_line_words=` default: `6`, browser fallback only;
- browser query `?font_scale=` default: `1`, accepted range `0.55` to `1.4`;
- `?debug=1` shows raw/final diagnostic text outside the main subtitle area.

Validated profiles:

- `V2_BROWSER_PROFILE_C`: browser-connected baseline for Profile C.
- `C_tail1`: v1.0.2 controlled baseline candidate, stored in
  `benchmarks/stability_optimization/C_tail1_led_subtitles.json`.
- `C_tail1` changes only `COMMIT_TAIL_HOLD_WORDS` from `2` to `1`; all row
  packing values stay unchanged: `MAX_CHARS=40`, `SOFT_BREAK_MIN=28`,
  `SCROLL_MS=120`, `MIN_LINE_HOLD_MS=1500`, `IDLE_CLEAR_MS=5500`.

The `C_tail1` browser confirmation and 30-minute real-world run both kept the
browser path below the stability budget:

```text
browser confirmation render p95: 52.2 ms
30-minute render p95:           16.4 ms
30-minute T4->T6 ACK p95:       37.7 ms
30-minute dropped chunks:       0
```

The live launcher still accepts environment overrides for operational values:

- `PORT`, default `8765`;
- `DURATION`, default `1800`;
- `TARGET`, optional PipeWire target;
- `OUTPUT_DIR`, optional manual run artifact directory.

For the full JSON field list see `docs/configuration.md`.

Limitations:

- the pause fallback is presentation-only; it does not change ASR finalization;
- punctuation quality depends on the existing Nemotron/Stabilizer output;
- extremely long single words may still be ellipsized by the browser.
- `./scripts/run_two_line_subtitles.sh` uses `config/led_subtitles.json`, which
  now carries the promoted `C_tail1` settings.
