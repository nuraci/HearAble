import json
from pathlib import Path
import tempfile
import unittest

from hearable.led_subtitles.asr_commit import AsrCommitter
from hearable.led_subtitles.config import LedSubtitleConfig
from hearable.led_subtitles.layout import is_orphan_word
from hearable.led_subtitles.popon import PopOnBlock, PopOnScheduler
from hearable.led_subtitles.rollup import RollUpRenderer
from hearable.realtime_server import load_runtime_defaults
from hearable.realtime_server import LedSubtitleBridge
from hearable.stabilizer import SubtitleStabilizer


class LedSubtitleRulesTest(unittest.TestCase):
    def setUp(self):
        self.config = LedSubtitleConfig()

    def bridge_with_config(self, values):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        with handle:
            json.dump(values, handle)
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return LedSubtitleBridge(str(path))

    def test_exactly_40_chars_does_not_scroll_but_41st_does(self):
        renderer = RollUpRenderer(self.config)
        exact = "1234567890 1234567890 1234567890 1234567"

        renderer.ingest_words(exact.split(), now_ms=0)

        self.assertEqual(len(renderer.frame().bottom), 40)
        self.assertEqual(renderer.frame().top, "")
        self.assertEqual(renderer.scroll_count, 0)

        renderer.ingest_words(["x"], now_ms=1600)

        self.assertEqual(renderer.frame().top, exact)
        self.assertEqual(renderer.frame().bottom, "x")
        self.assertEqual(renderer.scroll_count, 1)

    def test_comma_at_character_30_triggers_soft_break(self):
        renderer = RollUpRenderer(self.config)
        text = "1234567890 1234567890 abcdefg,"

        renderer.ingest_words(text.split(), now_ms=0)

        self.assertEqual(len(renderer.frame().top), 30)
        self.assertEqual(renderer.frame().top, text)
        self.assertEqual(renderer.frame().bottom, "")

    def test_comma_at_character_20_does_not_trigger_soft_break(self):
        renderer = RollUpRenderer(self.config)
        text = "1234567890 abcdefgh,"

        renderer.ingest_words(text.split(), now_ms=0)

        self.assertEqual(len(renderer.frame().bottom), 20)
        self.assertEqual(renderer.frame().bottom, text)
        self.assertEqual(renderer.frame().top, "")

    def test_orphan_word_moves_to_next_line_with_following_word(self):
        renderer = RollUpRenderer(self.config)
        text = "abcdefghij abcdefghij abcdefghij nella"

        renderer.ingest_words(text.split(), now_ms=0)
        renderer.ingest_words(["casa"], now_ms=1600)

        self.assertEqual(renderer.frame().top, "abcdefghij abcdefghij abcdefghij")
        self.assertEqual(renderer.frame().bottom, "nella casa")

    def test_orphan_word_is_not_displayed_alone(self):
        renderer = RollUpRenderer(self.config)

        renderer.ingest_words(["di"], now_ms=0)

        self.assertEqual(renderer.frame().bottom, "")

        renderer.ingest_words(["fibre"], now_ms=100)

        self.assertEqual(renderer.frame().bottom, "di fibre")

    def test_orphan_words_are_configurable(self):
        config = LedSubtitleConfig(orphan_words=("custom",))

        self.assertTrue(is_orphan_word("custom", config))
        self.assertFalse(is_orphan_word("di", config))

    def test_asr_correction_does_not_rewrite_committed_word(self):
        committer = AsrCommitter(self.config)

        shown = committer.ingest("oggi vado al mare").committed_words
        shown += committer.ingest("oggi vado al mare").committed_words
        correction = committer.ingest("oggi vado al bar")

        self.assertEqual(shown, ["oggi", "vado", "al", "mare"])
        self.assertEqual(correction.committed_words, [])
        self.assertEqual(committer.committed_text, "oggi vado al mare")

    def test_commit_reason_stable_and_lookahead(self):
        committer = AsrCommitter(LedSubtitleConfig(commit_stable_updates=2, commit_lookahead=1))

        self.assertEqual(committer.ingest("oggi vado").commit_events[0]["reason"], "LOOKAHEAD")
        second = committer.ingest("oggi vado al")

        self.assertEqual(second.commit_events[0]["reason"], "STABLE_AND_LOOKAHEAD")

    def test_commit_reason_stable_without_lookahead(self):
        committer = AsrCommitter(LedSubtitleConfig(commit_stable_updates=2, commit_lookahead=1))

        committer.ingest("oggi")
        second = committer.ingest("oggi")

        self.assertEqual(second.commit_events[0]["reason"], "STABLE_UPDATES")

    def test_commit_reason_force_final(self):
        committer = AsrCommitter(LedSubtitleConfig())

        result = committer.force_ingest("oggi vado", reason="ASR_FINAL")

        self.assertEqual([event["reason"] for event in result.commit_events], ["ASR_FINAL", "ASR_FINAL"])

    def test_one_hypothesis_word_that_disappears_is_never_shown(self):
        committer = AsrCommitter(self.config)

        first = committer.ingest("ciao errore")
        second = committer.ingest("ciao")

        self.assertEqual(first.committed_words, [])
        self.assertEqual(second.committed_words, ["ciao"])
        self.assertNotIn("errore", committer.committed_words)

    def test_30_cps_stream_scrolls_at_most_every_1500_ms(self):
        renderer = RollUpRenderer(self.config)
        now = 0

        for _ in range(40):
            renderer.ingest_words(["aaaa"], now_ms=now)
            now += 167

        intervals = [
            right - left
            for left, right in zip(renderer.scroll_times_ms, renderer.scroll_times_ms[1:])
        ]
        self.assertTrue(intervals)
        self.assertTrue(all(interval >= 1500 for interval in intervals))

    def test_six_seconds_of_idle_clears_display(self):
        renderer = RollUpRenderer(self.config)

        renderer.ingest_words(["ciao"], now_ms=0)
        renderer.tick(now_ms=6000)

        self.assertEqual(renderer.frame().top, "")
        self.assertEqual(renderer.frame().bottom, "")

    def test_contiguous_popon_blocks_keep_dark_gap(self):
        scheduler = PopOnScheduler(self.config)
        first = PopOnBlock(text="primo blocco", start_ms=0, end_ms=2000)
        second = PopOnBlock(text="secondo blocco", start_ms=2000, end_ms=4000)

        events = scheduler.events([first, second])

        self.assertEqual(events[0].kind, "on")
        self.assertEqual(events[1].kind, "off")
        self.assertEqual(events[1].time_ms, 2000)
        self.assertEqual(events[2].kind, "on")
        self.assertEqual(events[2].time_ms, 2120)

    def test_short_popon_block_lasts_at_least_one_second(self):
        scheduler = PopOnScheduler(self.config)
        block = PopOnBlock(text="dodici chars", start_ms=0)

        events = scheduler.events([block])

        self.assertEqual(events[0].kind, "on")
        self.assertEqual(events[1].kind, "off")
        self.assertGreaterEqual(events[1].time_ms - events[0].time_ms, 1000)

    def test_realtime_bridge_exports_led_payload_and_idle_clear(self):
        bridge = LedSubtitleBridge("config/led_subtitles.json")

        payload, changed = bridge.update(
            "buongiorno a tutti benvenuti nella sala oggi vediamo come funziona",
            is_final=False,
            now_ms=0,
        )
        payload, changed = bridge.update(
            "buongiorno a tutti benvenuti nella sala oggi vediamo come funziona",
            is_final=False,
            now_ms=350,
        )

        self.assertTrue(changed)
        self.assertEqual(payload["max_chars"], 40)
        self.assertLessEqual(len(payload["previous_line"]), 40)
        self.assertLessEqual(len(payload["current_line"]), 40)
        self.assertTrue(payload["current_line"])

        payload, changed = bridge.tick(now_ms=2000)

        self.assertTrue(changed)
        self.assertTrue(payload["current_line"])

        payload, changed = bridge.tick(now_ms=7600)

        self.assertTrue(changed)
        self.assertEqual(payload["previous_line"], "")
        self.assertEqual(payload["current_line"], "")

    def test_realtime_bridge_flushes_held_tail_after_silence(self):
        bridge = LedSubtitleBridge("config/led_subtitles.json")
        transcript = "però se noi facciamo una colazione ricca di fibre"

        payload, _ = bridge.update(transcript, is_final=False, now_ms=0)
        before = f"{payload['previous_line']} {payload['current_line']}"
        payload, changed = bridge.tick(now_ms=500)
        after = f"{payload['previous_line']} {payload['current_line']}"

        self.assertTrue(changed)
        self.assertIn("colazione", before)
        self.assertIn("ricca", before)
        self.assertNotIn("fibre", before)
        self.assertIn("colazione", after)
        self.assertIn("ricca", after)
        self.assertIn("fibre", after)
        self.assertTrue(bridge.last_commit_events)
        self.assertTrue(all(event["reason"] == "TAIL_FLUSH_TIMEOUT" for event in bridge.last_commit_events))

    def test_conservative_bridge_holds_tail_to_avoid_raw_fragments(self):
        bridge = self.bridge_with_config(
            {
                "COMMIT_STABLE_UPDATES": 2,
                "COMMIT_LOOKAHEAD": 2,
                "COMMIT_TAIL_HOLD_WORDS": 6,
                "COMMIT_TAIL_FLUSH_MS": 650,
                "SHOW_UNSTABLE_TAIL": False,
            }
        )
        transcripts = [
            "però se noi fac una cola ri di fibre",
            "però se noi facciamo una colazione ricca di fibre al mattino migliora molto la concentrazione",
            "però se noi facciamo una colazione ricca di fibre al mattino migliora molto la concentrazione",
        ]

        payload = {}
        for index, transcript in enumerate(transcripts):
            payload, _ = bridge.update(transcript, is_final=False, now_ms=index * 350)

        rendered = f"{payload['previous_line']} {payload['current_line']}"

        self.assertNotIn(" fac ", f" {rendered} ")
        self.assertNotIn(" cola ", f" {rendered} ")
        self.assertNotIn(" ri ", f" {rendered} ")
        self.assertIn("colazione", rendered)
        self.assertIn("ricca", rendered)

    def test_default_led_config_uses_promoted_c_tail1(self):
        bridge = LedSubtitleBridge("config/led_subtitles.json")

        self.assertEqual(bridge.config.commit_stable_updates, 2)
        self.assertEqual(bridge.config.commit_lookahead, 1)
        self.assertEqual(bridge.config.commit_tail_hold_words, 1)
        self.assertEqual(bridge.config.commit_tail_flush_ms, 450)
        self.assertFalse(bridge.config.show_unstable_tail)

    def test_realtime_preset_loads_runtime_defaults(self):
        defaults = load_runtime_defaults("config/realtime_two_line_vulkan.json")

        self.assertEqual(defaults["chunk_ms"], 80)
        self.assertEqual(defaults["rnnt_right_context"], 3)
        self.assertTrue(defaults["led_config"].endswith("/config/led_subtitles.json"))

    def test_stabilizer_preserves_stable_prefix_when_clipping_duplicate_words(self):
        stabilizer = SubtitleStabilizer(max_words=3)

        stable, unstable = stabilizer._fit_lines("a", "b a c")

        self.assertEqual(stable, "")
        self.assertEqual(unstable, "b a c")


class HeldTailFlushesOnSilenceTest(unittest.TestCase):
    """The last word must appear during the pause, not at the head of the next sentence.

    The last word of a hypothesis is held back on purpose, so that a word the
    recogniser is still revising does not flicker in front of the reader. It is
    released by `tick()` after COMMIT_TAIL_FLUSH_MS of quiet.

    "Quiet" has to mean *the hypothesis stopped changing*. It used to mean *no
    event arrived*, which is not the same thing at all: a cache-aware streaming
    recogniser emits a result for every chunk, including while nobody is
    speaking. Each of those identical results restamped the timer, so the timer
    never expired, and the held word waited for the next utterance — a sentence
    ended one word short and the sentence after it opened with a word belonging
    to the one before.

    That is a defect found in daily use, not by a test, which is why there is a
    test now.
    """

    CHUNK_MS = 160

    def _bridge(self):
        return LedSubtitleBridge("config/led_subtitles.json")

    def _speak(self, bridge, hypotheses, start_ms):
        """One growing hypothesis per chunk, the way the recogniser produces them."""
        now = start_ms
        for hypothesis in hypotheses:
            bridge.update(hypothesis, False, now)
            now += self.CHUNK_MS
            bridge.tick(now)
        return now

    def _stay_silent(self, bridge, chunks, start_ms, still_emitting):
        now = start_ms
        for _ in range(chunks):
            if still_emitting:
                # The recogniser keeps handing over the same hypothesis.
                bridge.update(bridge._last_input_text, False, now)
            bridge.tick(now)
            now += self.CHUNK_MS
        return now

    def _visible(self, bridge):
        frame = bridge.payload()
        return " ".join(part for part in (frame["previous_line"], frame["current_line"]) if part)

    def test_tail_is_released_while_the_recogniser_keeps_repeating_itself(self):
        bridge = self._bridge()
        end = self._speak(bridge, ["Buonasera", "Buonasera a", "Buonasera a tutti"], 0)

        self.assertNotIn("tutti", self._visible(bridge), "la coda non dovrebbe essere ancora uscita")

        # Long enough for COMMIT_TAIL_FLUSH_MS (450 ms) to pass several times over.
        self._stay_silent(bridge, chunks=8, start_ms=end, still_emitting=True)

        self.assertIn("tutti", self._visible(bridge),
                      "l'ultima parola e' rimasta in mano durante il silenzio")

    def test_tail_is_released_when_the_recogniser_goes_quiet(self):
        """The case that always worked, kept so a fix cannot trade one for the other."""
        bridge = self._bridge()
        end = self._speak(bridge, ["Buonasera", "Buonasera a", "Buonasera a tutti"], 0)
        self._stay_silent(bridge, chunks=8, start_ms=end, still_emitting=False)

        self.assertIn("tutti", self._visible(bridge))

    def test_a_repeated_hypothesis_does_not_postpone_the_flush(self):
        """The mechanism itself: an unchanged hypothesis must not restamp the timer."""
        bridge = self._bridge()
        bridge.update("Buonasera a tutti", False, 0)
        stamped = bridge._last_input_ms

        bridge.update("Buonasera a tutti", False, 5_000)

        self.assertEqual(bridge._last_input_ms, stamped,
                         "un'ipotesi identica ha spostato in avanti il timer del silenzio")

    def test_a_changed_hypothesis_does_restart_the_timer(self):
        bridge = self._bridge()
        bridge.update("Buonasera a", False, 0)

        bridge.update("Buonasera a tutti", False, 5_000)

        self.assertEqual(bridge._last_input_ms, 5_000)


if __name__ == "__main__":
    unittest.main()
