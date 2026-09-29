from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
import wave

import numpy as np

from hearable.audio_sources import CANONICAL_CHANNELS
from hearable.audio_sources import CANONICAL_SAMPLE_RATE
from hearable.audio_sources import AudioSourceRegistry
from hearable.audio_sources import CanonicalAudioFrame
from hearable.audio_sources import FakeAudioSource
from hearable.io_config import BackendConfigError
from hearable.io_config import load_backend_config
from hearable.subtitle_sinks import ConsoleSubtitleSink
from hearable.subtitle_sinks import NullSubtitleSink
from hearable.subtitle_sinks import SubtitleSinkRegistry
from hearable.subtitle_sinks import SubtitleState
from hearable.realtime_server import Broadcaster
from hearable.realtime_server import BrowserSubtitleSink
from hearable.realtime_server import LatencyTracker


def make_wav(path: Path, frames: int = 1600) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * frames)


class PluggableIoTest(unittest.TestCase):
    def test_backend_config_validation_errors_are_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_type = root / "missing_type.json"
            missing_type.write_text('{"options": {}}', encoding="utf-8")
            bad_options = root / "bad_options.json"
            bad_options.write_text('{"type": "wav", "options": []}', encoding="utf-8")
            malformed = root / "malformed.json"
            malformed.write_text('{"type": ', encoding="utf-8")

            with self.assertRaisesRegex(BackendConfigError, "missing string 'type'"):
                load_backend_config(str(missing_type), kind="audio source", root=root)
            with self.assertRaisesRegex(BackendConfigError, "'options' must be an object"):
                load_backend_config(str(bad_options), kind="audio source", root=root)
            with self.assertRaisesRegex(BackendConfigError, "malformed JSON"):
                load_backend_config(str(malformed), kind="audio source", root=root)

    def test_unknown_backend_lists_available_types(self):
        with self.assertRaisesRegex(BackendConfigError, "available: .*pipewire.*wav"):
            AudioSourceRegistry.create("nope", {}, root=Path.cwd(), chunk_ms=80, mode="throughput")
        with self.assertRaisesRegex(BackendConfigError, "available: .*console.*null"):
            SubtitleSinkRegistry.create("nope", {}, root=Path.cwd())

    def test_wav_source_produces_canonical_frames_and_eof(self):
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.wav"
                make_wav(path, frames=1600)
                source = AudioSourceRegistry.create(
                    "wav",
                    {"path": str(path)},
                    root=Path.cwd(),
                    chunk_ms=80,
                    mode="throughput",
                )
                source.open()
                await source.start()
                first = await source.read_chunk()
                second = await source.read_chunk()
                third = await source.read_chunk()
                await source.close()

                self.assertIsNotNone(first)
                assert first is not None
                self.assertEqual(first.seq, 1)
                self.assertEqual(first.sample_rate, CANONICAL_SAMPLE_RATE)
                self.assertEqual(first.channels, CANONICAL_CHANNELS)
                self.assertEqual(first.samples.dtype, np.float32)
                self.assertEqual(len(first.samples), 1280)
                self.assertIsNotNone(first.expected_feed_monotonic_ns)
                self.assertIsNotNone(first.source_clock_drift_ms)
                self.assertIsNotNone(second)
                self.assertIsNone(third)
                self.assertTrue(source.eof)

        asyncio.run(run())

    def test_fake_source_keeps_core_agnostic_to_concrete_source(self):
        async def run() -> None:
            frame = CanonicalAudioFrame(
                seq=1,
                samples=np.zeros(10, dtype=np.float32),
                sample_rate=16000,
                channels=1,
                source_start_time=0.0,
                source_end_time=0.001,
                monotonic_available_time_ns=1,
                wall_available_time_ns=2,
            )
            source = FakeAudioSource([frame])
            source.open()
            await source.start()

            self.assertIs(await source.read_chunk(), frame)
            self.assertIsNone(await source.read_chunk())
            self.assertTrue(source.eof)

        asyncio.run(run())

    def test_null_and_console_sinks_preserve_seq_and_are_ack_free(self):
        async def run() -> None:
            state = SubtitleState(seq=7, timestamp=1.0, upper_line="prima", lower_line="seconda")
            null = NullSubtitleSink()
            await null.open()
            await null.publish(state)
            await null.flush()
            await null.close()

            stream = io.StringIO()
            console = ConsoleSubtitleSink(stream=stream)
            await console.open()
            await console.publish(state)
            await console.close()

            self.assertEqual(null.states[0].seq, 7)
            self.assertFalse(null.telemetry()["supports_ack"])
            self.assertIn("prima", stream.getvalue())
            self.assertIn("seconda", stream.getvalue())

        asyncio.run(run())

    def test_octagon_udp_sink_sends_subtitle_json(self):
        async def run() -> None:
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(1.0)
            sink = SubtitleSinkRegistry.create(
                "octagon_udp",
                {"host": "127.0.0.1", "port": receiver.getsockname()[1], "format": "json"},
                root=Path.cwd(),
            )
            await sink.open()
            await sink.publish(SubtitleState(seq=3, timestamp=1.0, upper_line="prima", lower_line="seconda"))
            data, _addr = receiver.recvfrom(4096)
            await sink.close()
            receiver.close()

            payload = json.loads(data.decode("utf-8"))
            self.assertEqual(payload["type"], "octagon_subtitle")
            self.assertEqual(payload["seq"], 3)
            self.assertEqual(payload["led"], {})
            self.assertIn("seconda", data.decode("utf-8"))

        asyncio.run(run())

    def test_browser_sink_wraps_existing_coalescing_without_clients(self):
        async def run() -> None:
            latency = LatencyTracker()
            sink = BrowserSubtitleSink(Broadcaster(), latency, max_fps=25)
            await sink.open()
            await sink.publish(SubtitleState(seq=1, timestamp=1.0, upper_line="", lower_line="ciao"), {})
            await sink.flush()
            await sink.clear()
            await sink.close()

            self.assertTrue(sink.telemetry()["supports_ack"])
            self.assertEqual(latency.coalescing_summary()["ui_updates_sent"], 0)

        asyncio.run(run())

    def test_configuration_only_backend_switching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav_cfg = root / "audio.json"
            null_cfg = root / "sink.json"
            audio_path = root / "a.wav"
            make_wav(audio_path)
            wav_cfg.write_text(json.dumps({"type": "wav", "options": {"path": str(audio_path)}}), encoding="utf-8")
            null_cfg.write_text(json.dumps({"type": "null", "options": {}}), encoding="utf-8")

            audio = load_backend_config(str(wav_cfg), kind="audio source", root=root)
            sink = load_backend_config(str(null_cfg), kind="subtitle sink", root=root)

            self.assertEqual(audio.type, "wav")
            self.assertEqual(sink.type, "null")
            self.assertEqual(audio.options["path"], str(audio_path))

    def test_backend_options_are_isolated(self):
        with self.assertRaisesRegex(BackendConfigError, "unknown option"):
            AudioSourceRegistry.create("wav", {"path": "x.wav", "target": "48"}, root=Path.cwd(), chunk_ms=80, mode="throughput")
        with self.assertRaisesRegex(BackendConfigError, "has no options"):
            SubtitleSinkRegistry.create("null", {"target": "48"}, root=Path.cwd())


if __name__ == "__main__":
    unittest.main()
