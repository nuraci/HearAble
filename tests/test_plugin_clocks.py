"""Every elapsed time in the plugin is measured on a clock that cannot jump.

This box sets its wall clock *after* enigma2 has started: a cold boot moves
`time.time()` forward under a plugin that is already running. The first cold
start after the A/V delay work made that visible — the plugin reported an uptime
of 78 minutes on a decoder that had been on for 12 — and `uptime_s` was the
harmless end of it. The same clock carried the greeting and the goodbye, which
are supposed to stand for four seconds, and the rule that takes a stale subtitle
off the screen. A forward correction while the greeting is up would have cleared
it instantly, and a viewer would have seen a message flash and vanish with
nothing in the logs to explain it.

So durations run on `time.monotonic()`, and `time.time()` is left only where an
absolute instant is genuinely wanted: timestamps exported to be correlated with
the clocks of other machines. These tests hold that line, because the mistake is
invisible until a machine boots cold, which is rare and hard to arrange.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugin/HearAbleOSD"))
sys.path.insert(0, str(ROOT / "hearable"))

from subtitle_session import SubtitleSession

SOURCES = ("plugin/HearAbleOSD/plugin.py", "plugin/HearAbleOSD/subtitle_session.py")

# Fields that only ever answer "how long since / how long until". None of them
# may be written from, or compared against, the wall clock.
DURATION_FIELDS = ("started_at", "_lifecycle_since", "_lifecycle_until",
                   "_notice_until", "_sync_dirty_since", "last_state_at")


class PluginClockTest(unittest.TestCase):
    def test_no_duration_is_measured_on_the_wall_clock(self) -> None:
        offenders = []
        for name in SOURCES:
            for number, line in enumerate((ROOT / name).read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if "time.time()" not in code:
                    continue
                if any(field in code for field in DURATION_FIELDS):
                    offenders.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "durata misurata sull'orologio da parete:\n"
                                        + "\n".join(offenders))

    def test_duration_fields_are_only_assigned_from_the_monotonic_clock(self) -> None:
        offenders = []
        for name in SOURCES:
            for number, line in enumerate((ROOT / name).read_text().splitlines(), 1):
                code = line.split("#", 1)[0].strip()
                for field in DURATION_FIELDS:
                    if f"self.{field} =" not in code:
                        continue
                    value = code.split("=", 1)[1].strip()
                    if "time.monotonic()" in value or value in ("None", "0.0"):
                        continue
                    offenders.append(f"{name}:{number}: {code}")
        self.assertEqual(offenders, [], "campo di durata assegnato da un altro orologio:\n"
                                        + "\n".join(offenders))

    def test_a_session_stamps_arrivals_monotonically(self) -> None:
        """Not a reading of the source: a reading of the object itself."""
        session = SubtitleSession()
        self.assertAlmostEqual(session.last_state_at, time.monotonic(), delta=1.0)
        # The wall clock is ~1.7e9 in this era, so the two cannot be confused.
        self.assertGreater(abs(session.last_state_at - time.time()), 1e6)


if __name__ == "__main__":
    unittest.main()
