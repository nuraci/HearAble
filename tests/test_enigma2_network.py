"""Phase 7 — Enigma2 network audio in, SubtitleState out."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hearable import enigma2_protocol as e2
from hearable.audio_sources import AudioSourceRegistry, Enigma2AudioSource
from hearable.io_config import BackendConfigError
from hearable.realtime_server import LedSubtitleBridge
from hearable.stabilizer import SubtitleStabilizer
from hearable.subtitle_sinks import Enigma2SubtitleSink, SubtitleState, SubtitleSinkRegistry


def state(seq: int, upper: str = "riga sopra", lower: str = "riga sotto") -> SubtitleState:
    return SubtitleState(seq=seq, timestamp=0.0, upper_line=upper, lower_line=lower)


class ProtocolRoundTrip(unittest.TestCase):
    def test_subtitle_state_survives_json(self):
        original = e2.subtitle_state(
            seq=42, source_id="1:0:19:X:", source_epoch=3,
            upper_line="prima riga", lower_line="seconda riga",
        )
        restored = e2.validate(json.loads(json.dumps(original, ensure_ascii=False)))
        self.assertEqual(restored, original)

    def test_lines_are_clipped_at_the_network_boundary(self):
        message = e2.subtitle_state(
            seq=1, source_id="s", source_epoch=0,
            upper_line="x" * 200, lower_line="y" * 41,
        )
        self.assertEqual(len(message["upper_line"]), e2.MAX_LINE_CHARS)
        self.assertEqual(len(message["lower_line"]), e2.MAX_LINE_CHARS)
        e2.validate(message)

    def test_over_long_line_is_rejected_by_validation(self):
        message = e2.subtitle_state(seq=1, source_id="s", source_epoch=0, upper_line="a", lower_line="b")
        message["upper_line"] = "z" * 41
        with self.assertRaises(e2.ProtocolError):
            e2.validate(message)

    def test_cumulative_transcript_fields_are_refused(self):
        message = e2.subtitle_state(seq=1, source_id="s", source_epoch=0, upper_line="a", lower_line="b")
        message["final_text"] = "the whole transcript so far"
        with self.assertRaises(e2.ProtocolError):
            e2.validate(message)

    def test_unknown_type_and_version_are_refused(self):
        with self.assertRaises(e2.ProtocolError):
            e2.validate({"type": "nope", "protocol_version": 1, "seq": 1})
        with self.assertRaises(e2.ProtocolError):
            e2.validate({"type": "heartbeat", "protocol_version": 99, "seq": 1})


class EpochAndSequence(unittest.TestCase):
    def test_stale_epoch_is_rejected(self):
        guard = e2.EpochGuard()
        self.assertTrue(guard.accept(e2.subtitle_state(seq=1, source_id="s", source_epoch=12, upper_line="a", lower_line="b")))
        late = e2.subtitle_state(seq=999, source_id="s", source_epoch=11, upper_line="canale vecchio", lower_line="")
        self.assertFalse(guard.accept(late))
        self.assertEqual(guard.counters()["rejected_stale_epoch"], 1)

    def test_out_of_order_sequence_within_an_epoch_is_rejected(self):
        guard = e2.EpochGuard()
        guard.accept(e2.subtitle_state(seq=10, source_id="s", source_epoch=1, upper_line="a", lower_line="b"))
        self.assertFalse(guard.accept(e2.subtitle_state(seq=9, source_id="s", source_epoch=1, upper_line="a", lower_line="b")))
        self.assertEqual(guard.counters()["rejected_stale_seq"], 1)

    def test_new_epoch_restarts_sequence_tracking(self):
        guard = e2.EpochGuard()
        guard.accept(e2.subtitle_state(seq=500, source_id="s", source_epoch=1, upper_line="a", lower_line="b"))
        self.assertTrue(guard.accept(e2.subtitle_state(seq=1, source_id="s", source_epoch=2, upper_line="a", lower_line="b")))

    def test_sink_sequence_is_monotonic(self):
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=1, source_id="s")
            seqs = []
            for index in range(20):
                await sink.publish(state(index))
                seqs.append(sink._pending["seq"])
            return seqs
        seqs = asyncio.run(run())
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))


class Coalescing(unittest.TestCase):
    def test_pending_never_exceeds_one_and_older_states_are_dropped(self):
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=1, source_id="s")
            for index in range(1000):
                await sink.publish(state(index, lower=f"stato {index}"))
            return sink
        sink = asyncio.run(run())
        self.assertEqual(sink.max_pending, 1)
        self.assertEqual(sink.generated, 1000)
        self.assertEqual(sink.coalesced, 999)
        # What survives is the newest, never an old one.
        self.assertEqual(sink._pending["lower_line"], "stato 999")

    def test_publish_does_not_await_the_renderer(self):
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=1, source_id="s")
            # No transport is connected at all; publishing must still return.
            await asyncio.wait_for(sink.publish(state(1)), timeout=1.0)
            return True
        self.assertTrue(asyncio.run(run()))

    def test_source_change_queues_both_control_messages_in_order(self):
        """The clear must not supersede the source_changed that precedes it."""
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=1, source_id="A")
            await sink.publish(state(1, lower="testo del canale A"))
            epoch = await sink.source_changed("B", reason="service_changed")
            return epoch, list(sink._control), sink._pending
        epoch, control, pending = asyncio.run(run())
        self.assertEqual(epoch, 1)
        self.assertEqual([m["type"] for m in control], ["source_changed", "clear_subtitles"])
        self.assertTrue(all(m["source_epoch"] == 1 for m in control))
        self.assertEqual(control[1]["reason"], "source_changed")
        # The previous channel's pending state is dropped, not delivered late.
        self.assertIsNone(pending)

    def test_control_queue_is_bounded(self):
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=1, source_id="A",
                                       control_queue_limit=4)
            for index in range(50):
                await sink.clear(reason=f"r{index}")
            return sink
        sink = asyncio.run(run())
        self.assertEqual(len(sink._control), 4)
        self.assertGreater(sink.control_dropped, 0)
        self.assertEqual(sink._control[-1]["reason"], "r49")


class ContextReset(unittest.TestCase):
    def test_stabilizer_reset_drops_previous_channel_text(self):
        stabilizer = SubtitleStabilizer(min_confirmations=1)
        stabilizer.update("parole del canale a", True)
        stabilizer.reset()
        after = stabilizer.update("parole del canale b")
        self.assertNotIn("canale a", after.stable)
        self.assertEqual(after.final_text, "")

    def test_led_bridge_reset_clears_both_lines(self):
        bridge = LedSubtitleBridge(str(ROOT / "config/led_subtitles.json"))
        for index in range(6):
            bridge.update("testo del canale precedente da rimuovere", False, 1000 + index * 300)
        bridge.reset()
        payload = bridge.payload()
        self.assertEqual(payload.get("previous_line", ""), "")
        self.assertEqual(payload.get("current_line", ""), "")


class AudioSourceConfiguration(unittest.TestCase):
    def make(self, **options) -> Enigma2AudioSource:
        merged = {"host": "192.0.2.10"}
        merged.update(options)
        return AudioSourceRegistry.create("enigma2", merged, root=ROOT, chunk_ms=160, mode="realtime")

    def test_registered_and_canonical_output_shape(self):
        source = self.make()
        self.assertEqual(source.source_type, "enigma2")
        # 160 ms of 16 kHz mono float32.
        self.assertEqual(source.bytes_per_chunk, int(16000 * 0.160) * 4)

    def test_host_is_required_and_never_hardcoded(self):
        with self.assertRaises(BackendConfigError):
            AudioSourceRegistry.create("enigma2", {}, root=ROOT, chunk_ms=160, mode="realtime")

    def test_unknown_option_is_refused(self):
        with self.assertRaises(BackendConfigError):
            self.make(pid=1234)

    def test_credentials_come_from_the_environment(self):
        os.environ["HEARABLE_TEST_E2_USER"] = "operator"
        os.environ["HEARABLE_TEST_E2_PASS"] = "sekrit"
        try:
            source = self.make(username_env="HEARABLE_TEST_E2_USER", password_env="HEARABLE_TEST_E2_PASS")
            self.assertEqual(source.username, "operator")
            # The password must never appear in telemetry that gets written out.
            self.assertNotIn("sekrit", json.dumps(source.telemetry()))
        finally:
            del os.environ["HEARABLE_TEST_E2_USER"], os.environ["HEARABLE_TEST_E2_PASS"]

    def test_no_credentials_in_committed_configs(self):
        for path in sorted((ROOT / "config").glob("*.json")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(config=path.name):
                self.assertNotIn("password\"", text.replace("password_env\"", ""))

    def test_track_policy_prefers_italian_over_description_and_english(self):
        source = self.make()
        tracks = [
            {"audio_order": 0, "language": "eng", "title": "English"},
            {"audio_order": 1, "language": "ita", "title": "Audio Description"},
            {"audio_order": 2, "language": "ita", "title": "Italiano"},
        ]
        chosen, policy = source.select_audio_track(tracks)
        self.assertEqual(chosen["audio_order"], 2)
        self.assertIn("ita", policy)

    def test_explicit_track_index_wins(self):
        source = self.make(audio_track_index=0)
        tracks = [{"audio_order": 0, "language": "eng", "title": ""},
                  {"audio_order": 1, "language": "ita", "title": ""}]
        chosen, policy = source.select_audio_track(tracks)
        self.assertEqual(chosen["audio_order"], 0)
        self.assertIn("configured", policy)

    def test_no_track_available_is_reported_not_guessed(self):
        chosen, policy = self.make().select_audio_track([])
        self.assertIsNone(chosen)
        self.assertIn("no audio track", policy)

    def test_video_is_discarded_without_decoding(self):
        """The real ffmpeg command must drop video at the demuxer, not decode it."""
        captured: dict = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                self.stdout = open(os.devnull, "rb")

        source = self.make()
        real_popen = subprocess.Popen
        real_set_blocking = os.set_blocking
        try:
            subprocess.Popen = FakePopen  # type: ignore[assignment]
            os.set_blocking = lambda *a, **k: None  # type: ignore[assignment]
            source._spawn("http://192.0.2.10:8001/1:0:19:A:", {"audio_order": 1})
        finally:
            subprocess.Popen = real_popen  # type: ignore[assignment]
            os.set_blocking = real_set_blocking  # type: ignore[assignment]

        cmd = captured["cmd"]
        self.assertIn("-vn", cmd, "video must be discarded")
        self.assertIn("-map", cmd)
        self.assertIn("0:a:1", cmd, "only the selected audio track is mapped")
        # Canonical output, and no video decoder or scaler anywhere.
        self.assertEqual(cmd[cmd.index("-ar") + 1], "16000")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertEqual(cmd[cmd.index("-f") + 1], "f32le")
        for forbidden in ("-c:v", "-vcodec", "-vf", "-s"):
            self.assertNotIn(forbidden, cmd)

    def test_stream_url_is_built_from_the_service_reference(self):
        source = self.make(stream_port=8001)
        self.assertEqual(source.stream_url("1:0:19:A:"), "http://192.0.2.10:8001/1:0:19:A:")


class SinkConfiguration(unittest.TestCase):
    def test_registered_with_ack_support(self):
        sink = SubtitleSinkRegistry.create("enigma2", {"host": "192.0.2.10"}, root=ROOT)
        self.assertTrue(sink.telemetry()["supports_ack"])

    def test_host_is_required(self):
        with self.assertRaises(BackendConfigError):
            SubtitleSinkRegistry.create("enigma2", {}, root=ROOT)

    def test_unknown_option_is_refused(self):
        with self.assertRaises(BackendConfigError):
            SubtitleSinkRegistry.create("enigma2", {"host": "h", "retries": 3}, root=ROOT)

    def test_existing_sinks_are_untouched(self):
        for name in ("null", "console", "octagon_udp", "enigma2"):
            self.assertIn(name, SubtitleSinkRegistry.available_types())

    def test_contract_name_and_neutral_name_are_one_implementation(self):
        """`enigma2` is the Phase 7 contract name for a transport that is not
        specific to it. Both names must be the same class, or the two sides of
        the project would drift apart."""
        from hearable.subtitle_sinks import Enigma2SubtitleSink, SubtitleProtocolSink
        self.assertIs(Enigma2SubtitleSink, SubtitleProtocolSink)
        options = {"host": "192.0.2.10", "port": 8790}
        contract = SubtitleSinkRegistry.create("enigma2", options, root=ROOT)
        neutral = SubtitleSinkRegistry.create("subtitle_ws", options, root=ROOT)
        self.assertIs(type(contract), type(neutral))
        self.assertEqual(contract.url, neutral.url)

    def test_each_name_labels_its_own_telemetry(self):
        """So an artifact still says which side of the project produced it."""
        options = {"host": "192.0.2.10"}
        self.assertEqual(
            SubtitleSinkRegistry.create("enigma2", options, root=ROOT).telemetry()["type"],
            "enigma2")
        self.assertEqual(
            SubtitleSinkRegistry.create("subtitle_ws", options, root=ROOT).telemetry()["type"],
            "subtitle_ws")

    def test_unknown_option_is_refused_under_either_name(self):
        for name in ("enigma2", "subtitle_ws"):
            with self.subTest(name=name):
                with self.assertRaises(BackendConfigError):
                    SubtitleSinkRegistry.create(name, {"host": "h", "nope": 1}, root=ROOT)


class StressArtifact(unittest.TestCase):
    def test_transport_stress_passed(self):
        path = ROOT / "benchmarks/network/subtitle_stress.json"
        if not path.exists():
            self.skipTest("run tools/subtitle_transport_stress.py first")
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(report["pass"])
        self.assertFalse(report["checks"]["producer_blocked"])
        self.assertTrue(report["checks"]["pending_bounded"])
        self.assertEqual(report["producer"]["pending_final"], 0)


if __name__ == "__main__":
    unittest.main()
