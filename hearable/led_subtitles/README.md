# HearAble LED Subtitles

This package contains the hardware-independent LED subtitle formatter.

It targets two rows of 40 characters, suitable for a future matrix such as
16x320. It does not include a physical matrix driver because the hardware
protocol and input transport are not specified yet.

Run the terminal simulator without hardware:

```bash
cd /opt/devel/hearable
python tools/led_subtitle_simulator.py --demo
```

Run the live Vulkan pipeline with the same LED formatter feeding the browser:

```bash
cd /opt/devel/hearable
./scripts/run_two_line_subtitles.sh
```

The script prints a browser URL using `formatter=led`. In that mode the server
sends the already formatted 40-character rows in the WebSocket payload.

Run with manual ASR hypotheses from standard input:

```bash
cd /opt/devel/hearable
printf '%s\n' \
  'buongiorno a tutti' \
  'buongiorno a tutti benvenuti' \
  'buongiorno a tutti benvenuti nella sala' \
  | python tools/led_subtitle_simulator.py
```

Run the rule tests:

```bash
cd /opt/devel/hearable
python -m unittest discover -s tests -p 'test_*.py'
```

LED display configuration lives in one file:

```text
config/led_subtitles.json
```

The most frequently tuned fields are:

- `MAX_CHARS`: maximum characters per physical row, currently `40`;
- `COMMIT_TAIL_HOLD_WORDS`: held ASR tail words used to avoid fragments;
- `COMMIT_TAIL_FLUSH_MS`: silence timeout before held words are displayed;
- `MIN_LINE_HOLD_MS`: minimum time before the top line can be replaced;
- `IDLE_CLEAR_MS`: silence timeout before both rows clear;
- `ORPHAN_WORDS`: Italian short/linking words kept away from lonely line
  endings.

Realtime server settings live separately in `config/realtime.json`; the live
Vulkan two-line launcher uses `config/realtime_two_line_vulkan.json`.

Main modules:

- `config.py`: loads the single configuration file.
- `layout.py`: word-safe line packing and soft-break rules.
- `asr_commit.py`: ASR word commit policy.
- `rollup.py`: live two-line roll-up renderer.
- `popon.py`: pre-generated pop-on block scheduler.
- `terminal_display.py`: fake terminal display for tests and development.
