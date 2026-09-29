from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import unittest
import wave

from tools.latency_harness import (
    aggregate_repeated,
    align_words,
    absolute_deadline_ns,
    audio_duration_ms,
    classify_visible_loss,
    drift_growth_indicator,
    eof_audit,
    gate_result,
    latency_rows,
    latency_growth_indicator,
    make_reference,
    pacing_sleep_seconds,
    reconstruct_visible_stream,
    sha256_file,
    source_clock_drift_ms,
    synthetic_even_split_words,
    validate_reference,
    wer_counts,
)
from tools.run_latency_regression import verify_manifest
from tools.run_latency_regression import parse_profile_events
from tools.stability_forensic import (
    candidate_configs,
    changed_stability_variables,
    irreversible_error_rate,
    replay,
)
from hearable.realtime_server import Broadcaster, LatencyTracker


def make_wav(path: Path, seconds: float = 1.0) -> None:
    frames = int(16000 * seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * frames)


def reference(audio: Path, transcript: Path, text: str = "ciao ciao mondo") -> dict:
    transcript.write_text(text, encoding="utf-8")
    return make_reference(
        sample_id="sample",
        language="it-IT",
        audio_file=str(audio),
        transcript_file=str(transcript),
        transcript_text=text,
        alignment_engine="test",
        alignment_version="1",
        authoritative=True,
        words=synthetic_even_split_words(text, audio_duration_ms(audio)),
    )


class LatencyHarnessTest(unittest.TestCase):
    def test_reference_json_schema_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            transcript = Path(tmp) / "a.txt"
            make_wav(audio)
            ref = reference(audio, transcript)

            result = validate_reference(ref, audio_duration_ms_value=1000)

            self.assertTrue(result["valid"])
            self.assertEqual(result["checks"]["word_count"], 3)

    def test_audio_hash_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            transcript = Path(tmp) / "a.txt"
            manifest = Path(tmp) / "manifest.json"
            reference_path = Path(tmp) / "ref.json"
            make_wav(audio)
            ref = reference(audio, transcript)
            reference_path.write_text("{}", encoding="utf-8")
            manifest.write_text(
                '{"schema_version":1,"samples":[{"sample_id":"sample","audio_sha256":"bad","transcript_sha256":"'
                + ref["transcript_sha256"]
                + '","reference_sha256":"'
                + sha256_file(reference_path)
                + '","authoritative":true}]}',
                encoding="utf-8",
            )

            result = verify_manifest(reference_path, ref, manifest)

            self.assertFalse(result["valid"])
            self.assertIn("audio hash mismatch", result["errors"])

    def test_transcript_reference_hash_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            transcript = Path(tmp) / "a.txt"
            manifest = Path(tmp) / "manifest.json"
            reference_path = Path(tmp) / "ref.json"
            make_wav(audio)
            ref = reference(audio, transcript)
            reference_path.write_text("{}", encoding="utf-8")
            manifest.write_text(
                '{"schema_version":1,"samples":[{"sample_id":"sample","audio_sha256":"'
                + ref["audio_sha256"]
                + '","transcript_sha256":"bad","reference_sha256":"'
                + sha256_file(reference_path)
                + '","authoritative":true}]}',
                encoding="utf-8",
            )

            result = verify_manifest(reference_path, ref, manifest)

            self.assertFalse(result["valid"])
            self.assertIn("transcript/reference hash mismatch", result["errors"])

    def test_non_monotonic_timestamps_are_rejected(self):
        ref = {"schema_version": 1, "words": [
            {"index": 0, "word": "uno", "start_ms": 200, "end_ms": 300},
            {"index": 1, "word": "due", "start_ms": 100, "end_ms": 250},
        ]}

        result = validate_reference(ref, audio_duration_ms_value=1000)

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["out_of_order_timestamps"], 1)

    def test_word_outside_audio_duration_is_rejected(self):
        ref = {"schema_version": 1, "words": [
            {"index": 0, "word": "uno", "start_ms": 0, "end_ms": 1200},
        ]}

        result = validate_reference(ref, audio_duration_ms_value=1000)

        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"]["words_outside_audio_duration"], 1)

    def test_repeated_word_alignment(self):
        alignment = align_words(["ciao", "ciao", "mondo"], ["ciao", "mondo"])

        self.assertEqual([item.match_type for item in alignment], ["exact", "deletion", "exact"])

    def test_insertion_deletion_substitution_handling(self):
        self.assertEqual(wer_counts(["a"], ["a", "b"])["insertions"], 1)
        self.assertEqual(wer_counts(["a", "b"], ["a"])["deletions"], 1)
        self.assertEqual(wer_counts(["a"], ["b"])["substitutions"], 1)

    def test_latency_from_start_and_end_and_stability_penalty(self):
        ref = {
            "schema_version": 1,
            "words": [{"index": 0, "word": "ciao", "start_ms": 100, "end_ms": 300}],
        }
        events = [
            {"wall_since_start_ms": 450, "raw": "ciao", "led": {"previous_line": "", "current_line": ""}},
            {"wall_since_start_ms": 700, "raw": "ciao", "led": {"previous_line": "", "current_line": "ciao"}},
        ]

        rows, _summary = latency_rows(ref, events)

        self.assertEqual(rows[0]["asr_latency_from_start"], 350)
        self.assertEqual(rows[0]["asr_latency_from_end"], 150)
        self.assertEqual(rows[0]["stability_penalty"], 250)

    def test_committed_word_rewrite_invariant(self):
        ref = {"schema_version": 1, "words": [{"index": 0, "word": "ciao", "start_ms": 0, "end_ms": 100}]}
        events = [
            {"wall_since_start_ms": 200, "raw": "ciao", "led": {"previous_line": "", "current_line": "ciao"}},
            {"wall_since_start_ms": 300, "raw": "ciao", "led": {"previous_line": "", "current_line": "ciao"}},
        ]

        _rows, summary = latency_rows(ref, events)

        self.assertEqual(summary["words_displayed"], 1)

    def test_display_times_use_reference_index_not_visible_window_index(self):
        ref = {
            "schema_version": 1,
            "words": [
                {"index": index, "word": f"w{index}", "start_ms": index * 100, "end_ms": index * 100 + 50}
                for index in range(20)
            ],
        }
        events = [
            {
                "wall_since_start_ms": 2200,
                "raw": " ".join(f"w{index}" for index in range(20)),
                "led": {"previous_line": "w10 w11 w12 w13", "current_line": "w14 w15 w16 w17 w18 w19"},
            }
        ]

        rows, _summary = latency_rows(ref, events)

        self.assertIsNone(rows[0]["display_state_latency_from_end"])
        self.assertEqual(rows[19]["display_state_latency_from_end"], 250)

    def test_profile_isolation(self):
        profile = {"config": {"COMMIT_TAIL_HOLD_WORDS": 6}}
        copied = dict(profile["config"])
        copied["COMMIT_TAIL_HOLD_WORDS"] = 2

        self.assertEqual(profile["config"]["COMMIT_TAIL_HOLD_WORDS"], 6)

    def test_repeated_run_aggregation(self):
        runs = [
            {"latency_summary": {"display_state_latency_from_end": {"p95": 100.0}}},
            {"latency_summary": {"display_state_latency_from_end": {"p95": 110.0}}},
            {"latency_summary": {"display_state_latency_from_end": {"p95": 120.0}}},
        ]

        result = aggregate_repeated(runs)

        self.assertEqual(result["median"], 110.0)
        self.assertEqual(result["variability"], 20.0)

    def test_baseline_delta_and_optional_regression_gate(self):
        baseline = {"latency_summary": {"display_state_latency_from_end": {"p95": 100.0}}, "visible_accuracy": {"wer": 0.10}}
        candidate = {"latency_summary": {"display_state_latency_from_end": {"p95": 120.0}}, "visible_accuracy": {"wer": 0.10}}

        result = gate_result(candidate, baseline, {"display_latency_p95_multiplier": 1.1})

        self.assertFalse(result["pass"])

    def test_absolute_deadlines_do_not_add_processing_time(self):
        source_start_ns = 1_000_000_000

        first = absolute_deadline_ns(source_start_ns, 0)
        second = absolute_deadline_ns(source_start_ns, 80)

        self.assertEqual(first, source_start_ns)
        self.assertEqual(second, source_start_ns + 80_000_000)

    def test_ideal_fake_clock_realtime_pacing_has_no_cumulative_drift(self):
        source_start_ns = 10_000_000_000
        records = []
        for index in range(3):
            expected = absolute_deadline_ns(source_start_ns, index * 80)
            records.append({"chunk_audio_end_ms": (index + 1) * 80, "source_clock_drift_ms": source_clock_drift_ms(expected, expected)})

        self.assertEqual([item["source_clock_drift_ms"] for item in records], [0.0, 0.0, 0.0])
        self.assertLess(abs(drift_growth_indicator(records)["slope_ms_per_audio_second"]), 0.001)

    def test_source_clock_drift_calculation(self):
        self.assertEqual(source_clock_drift_ms(1_090_000_000, 1_000_000_000), 90.0)

    def test_slow_processing_produces_measurable_lateness(self):
        source_start_ns = 1_000_000_000
        expected = absolute_deadline_ns(source_start_ns, 80)
        actual = expected + 25_000_000

        self.assertEqual(source_clock_drift_ms(actual, expected), 25.0)

    def test_throughput_mode_performs_no_realtime_sleep(self):
        sleep = pacing_sleep_seconds("throughput", 1_000_000_000, 80, 1_000_000_000)

        self.assertEqual(sleep, 0.0)

    def test_realtime_mode_sleeps_until_absolute_deadline(self):
        sleep = pacing_sleep_seconds("realtime", 1_000_000_000, 80, 1_010_000_000)

        self.assertEqual(sleep, 0.07)

    def test_throughput_rtf_excludes_pacing_sleep(self):
        compute_seconds = 0.2
        audio_seconds = 1.0
        pacing_sleep_seconds_total = 1.0

        compute_rtf = compute_seconds / audio_seconds
        wall_rtf = (compute_seconds + pacing_sleep_seconds_total) / audio_seconds

        self.assertEqual(compute_rtf, 0.2)
        self.assertEqual(wall_rtf, 1.2)

    def test_throughput_and_realtime_metrics_remain_separate(self):
        metrics = {"compute": {"compute_rtf": 0.2}, "realtime": {"wall_rtf": 1.0}}

        self.assertNotEqual(metrics["compute"]["compute_rtf"], metrics["realtime"]["wall_rtf"])

    def test_latency_vs_audio_time_drift_detection(self):
        rows = [
            {"T0b_word_end_ms": index * 1000.0, "display_state_latency_from_end": index * 20.0}
            for index in range(20)
        ]

        result = latency_growth_indicator(rows)

        self.assertEqual(result["flag"], "LATENCY_DRIFT_DETECTED")

    def test_raw_sdi_counts(self):
        result = wer_counts(["uno", "due", "tre"], ["uno", "duex", "tre", "extra"])

        self.assertEqual(result["substitutions"], 1)
        self.assertEqual(result["insertions"], 1)
        self.assertEqual(result["deletions"], 0)

    def test_visible_sdi_counts_from_reconstructed_stream(self):
        events = [
            {"wall_since_start_ms": 100, "led": {"previous_line": "", "current_line": "uno"}},
            {"wall_since_start_ms": 200, "led": {"previous_line": "", "current_line": "uno duex"}},
        ]

        visible = reconstruct_visible_stream(events)
        result = wer_counts(["uno", "due"], visible["words"])

        self.assertEqual(result["substitutions"], 1)

    def test_drop_reason_accounting(self):
        result = classify_visible_loss(["uno", "due", "tre"], ["uno", "dux"], ["uno"])

        self.assertEqual(result["reason_counts"]["ASR_SUBSTITUTION"], 1)
        self.assertEqual(result["reason_counts"]["NOT_RECOGNIZED"], 1)

    def test_dropped_words_always_receive_reason_or_unknown(self):
        result = classify_visible_loss(["uno"], [], [])

        self.assertEqual(result["per_word"][0]["loss_reason"], "NOT_RECOGNIZED")

    def test_eof_final_flush_accounting(self):
        events = [
            {
                "wall_since_start_ms": 100,
                "raw": "uno due",
                "stable": "uno",
                "unstable": "due",
                "led": {"previous_line": "", "current_line": "uno"},
            }
        ]

        result = eof_audit(events, ["uno", "due"], ["uno"])

        self.assertEqual(result["unstable_tail_words"], ["due"])
        self.assertTrue(result["final_flush_loss_evidenced"])

    def test_headless_browser_metrics_are_not_available(self):
        result = {"browser_metrics_available": False, "headless": True}

        self.assertFalse(result["browser_metrics_available"])

    def test_v1_artifacts_are_not_v2_paths(self):
        self.assertNotEqual("benchmarks/latency_runs/real_audio", "benchmarks/latency_runs/real_audio_v2")

    def test_abc_parameters_remain_unchanged(self):
        profiles = {
            "A": {"COMMIT_STABLE_UPDATES": 2, "COMMIT_LOOKAHEAD": 2, "COMMIT_TAIL_HOLD_WORDS": 6, "COMMIT_TAIL_FLUSH_MS": 1200, "SHOW_UNSTABLE_TAIL": False},
            "B": {"COMMIT_STABLE_UPDATES": 2, "COMMIT_LOOKAHEAD": 1, "COMMIT_TAIL_HOLD_WORDS": 3, "COMMIT_TAIL_FLUSH_MS": 650, "SHOW_UNSTABLE_TAIL": False},
            "C": {"COMMIT_STABLE_UPDATES": 2, "COMMIT_LOOKAHEAD": 1, "COMMIT_TAIL_HOLD_WORDS": 2, "COMMIT_TAIL_FLUSH_MS": 450, "SHOW_UNSTABLE_TAIL": False},
        }

        self.assertEqual(profiles["A"]["COMMIT_TAIL_HOLD_WORDS"], 6)
        self.assertEqual(profiles["B"]["COMMIT_LOOKAHEAD"], 1)
        self.assertEqual(profiles["C"]["COMMIT_TAIL_FLUSH_MS"], 450)

    def test_seq_survives_through_ack(self):
        tracker = LatencyTracker()
        seq, payload, internal = tracker.create(
            chunk=1,
            event_index=1,
            t0_mono_ns=1,
            t0_wall_ns=1,
            t1_mono_ns=2,
            t2_mono_ns=3,
            t3_mono_ns=4,
            t4_mono_ns=5,
            t4_wall_ns=5,
            is_final=False,
        )
        tracker.mark_sent(seq, payload, internal)
        t4_sent = tracker.pending[seq]["t4_sent_mono_ns"]

        tracker.ack({"rendered_seq": seq, "_server_received_mono_ns": t4_sent + 10_000_000, "browser_receive_to_render_ms": 12.0})

        self.assertEqual(tracker.completed[0]["seq"], seq)
        self.assertEqual(tracker.completed[0]["server_t4_to_t6_ack_ms"], 10.0)

    def test_duplicate_ack_handling(self):
        tracker = LatencyTracker()
        seq, payload, internal = tracker.create(
            chunk=1, event_index=1, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=False
        )
        tracker.mark_sent(seq, payload, internal)
        t4_sent = tracker.pending[seq]["t4_sent_mono_ns"]
        tracker.ack({"rendered_seq": seq, "_server_received_mono_ns": t4_sent})
        tracker.ack({"rendered_seq": seq, "_server_received_mono_ns": t4_sent})

        self.assertEqual(tracker.duplicate_acks, 1)

    def test_out_of_order_ack_handling(self):
        tracker = LatencyTracker()
        seq1, payload1, internal1 = tracker.create(
            chunk=1, event_index=1, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=False
        )
        seq2, payload2, internal2 = tracker.create(
            chunk=2, event_index=2, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=False
        )
        tracker.mark_sent(seq1, payload1, internal1)
        tracker.mark_sent(seq2, payload2, internal2)
        tracker.ack({"rendered_seq": seq2, "_server_received_mono_ns": tracker.pending[seq2]["t4_sent_mono_ns"]})
        tracker.ack({"rendered_seq": seq1, "_server_received_mono_ns": 0})

        self.assertEqual(tracker.out_of_order_acks, 1)

    def test_missing_ack_accounting(self):
        tracker = LatencyTracker()
        seq, payload, internal = tracker.create(
            chunk=1, event_index=1, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=False
        )
        tracker.mark_sent(seq, payload, internal)

        self.assertEqual(tracker.summary()["pending_render_acks"], 1)

    def test_browser_local_render_duration_is_not_server_clock(self):
        tracker = LatencyTracker()
        seq, payload, internal = tracker.create(
            chunk=1, event_index=1, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=False
        )
        tracker.mark_sent(seq, payload, internal)
        t4_sent = tracker.pending[seq]["t4_sent_mono_ns"]
        tracker.ack({
            "rendered_seq": seq,
            "rendered_wall_ms": 999999999,
            "_server_received_mono_ns": t4_sent + 20_000_000,
            "browser_receive_to_dom_ms": 3.0,
            "browser_receive_to_render_ms": 16.0,
            "browser_render_to_ack_send_ms": 1.0,
        })

        record = tracker.completed[0]
        self.assertEqual(record["server_t4_to_t6_ack_ms"], 20.0)
        self.assertEqual(record["browser_receive_to_render_ms"], 16.0)
        self.assertIsNone(record["total_latency_ms"])

    def test_coalesced_states_are_not_ack_failures(self):
        tracker = LatencyTracker()
        tracker.render_states_obsoleted_by_ack = 2

        self.assertEqual(tracker.summary()["ack_anomalies"]["stale_acks"], 0)

    def test_browser_readiness_barrier_records_message(self):
        broadcaster = Broadcaster()
        self.assertFalse(broadcaster.browser_ready.is_set())

        message = {"type": "browser_ready", "instrumentation": {"render_ack": True}}
        broadcaster.mark_browser_ready(message)

        self.assertTrue(broadcaster.browser_ready.is_set())
        self.assertEqual(broadcaster.browser_ready_messages[-1], message)

    def test_audio_wait_can_observe_browser_readiness(self):
        async def wait_for_ready() -> bool:
            broadcaster = Broadcaster()
            broadcaster.mark_browser_ready({"type": "browser_ready"})
            await asyncio.wait_for(broadcaster.browser_ready.wait(), timeout=0.01)
            return True

        self.assertTrue(asyncio.run(wait_for_ready()))

    def test_final_visible_seq_acknowledged(self):
        tracker = LatencyTracker()
        seq, payload, internal = tracker.create(
            chunk=1, event_index=1, t0_mono_ns=1, t0_wall_ns=1, t1_mono_ns=1,
            t2_mono_ns=1, t3_mono_ns=1, t4_mono_ns=1, t4_wall_ns=1, is_final=True
        )
        tracker.mark_sent(seq, payload, internal)
        tracker.ack({"rendered_seq": seq, "_server_received_mono_ns": tracker.pending[seq]["t4_sent_mono_ns"]})

        self.assertEqual(tracker.latest_rendered_seq, tracker.latest_sent_seq)
        self.assertEqual(tracker.summary()["pending_render_acks"], 0)

    def test_profile_events_are_parsed_per_profile(self):
        result = parse_profile_events(["A=/tmp/a.jsonl", "B=/tmp/b.jsonl"])

        self.assertEqual(result, {"A": "/tmp/a.jsonl", "B": "/tmp/b.jsonl"})

    def test_invalid_profile_events_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_profile_events(["missing_separator"])

    def test_irreversible_error_rate_separate_from_deletions(self):
        result = irreversible_error_rate(["uno", "due", "tre"], ["uno", "dux"])

        self.assertEqual(result["counts"]["deletions"], 1)
        self.assertEqual(result["irreversible_errors"], 1)

    def test_candidate_changes_exactly_one_variable(self):
        base = {
            "COMMIT_STABLE_UPDATES": 2,
            "COMMIT_LOOKAHEAD": 1,
            "COMMIT_TAIL_HOLD_WORDS": 2,
            "COMMIT_TAIL_FLUSH_MS": 450,
            "SHOW_UNSTABLE_TAIL": False,
        }

        for config in candidate_configs(base).values():
            self.assertEqual(len(changed_stability_variables(base, config)), 1)

    def test_counterfactual_replay_does_not_mutate_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "led.json"
            path.write_text(
                json.dumps(
                    {
                        "MAX_CHARS": 40,
                        "SOFT_BREAK_MIN": 28,
                        "COMMIT_STABLE_UPDATES": 2,
                        "COMMIT_LOOKAHEAD": 1,
                        "COMMIT_TAIL_HOLD_WORDS": 1,
                        "COMMIT_TAIL_FLUSH_MS": 200,
                        "SCROLL_MS": 120,
                        "MIN_LINE_HOLD_MS": 1500,
                        "IDLE_CLEAR_MS": 5500,
                        "SHOW_UNSTABLE_TAIL": False,
                    }
                ),
                encoding="utf-8",
            )
            reference = {
                "schema_version": 1,
                "words": [
                    {"index": 0, "word": "uno", "start_ms": 0, "end_ms": 100},
                    {"index": 1, "word": "due", "start_ms": 100, "end_ms": 200},
                ],
            }
            events = [
                {"wall_since_start_ms": 0, "raw": "uno", "is_final": False},
                {"wall_since_start_ms": 80, "raw": "uno due", "is_final": False},
            ]
            before = path.read_text(encoding="utf-8")

            result = replay(events, path, reference)

            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertIn("commit_reason_counts", result)

    def test_browser_baseline_path_is_separate_from_stability_outputs(self):
        self.assertNotEqual(
            "benchmarks/latency_runs/browser_v2/profile_c/V2_BROWSER_PROFILE_C.baseline.json",
            "benchmarks/stability_optimization/profile_c_stability_optimization.json",
        )


if __name__ == "__main__":
    unittest.main()
