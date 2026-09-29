"""Phase 7 integration — real subprocesses and sockets against local mocks.

These exercise what unit tests cannot: an actual MPEG-TS demuxed by ffmpeg, a
real WebSocket carrying the protocol, reconnection, and a channel change end to
end. No SF8008 is involved; the mocks stand in for it.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web

from hearable.audio_sources import Enigma2AudioSource
from hearable.subtitle_sinks import Enigma2SubtitleSink, SubtitleState
from tools.mock_enigma2_server import SERVICES, build_app, build_ts_fixture
from tools.mock_enigma2_subtitle_receiver import build_app as build_receiver

FIXTURE = ROOT / "benchmarks/network/fixtures/mock_service.ts"
API_PORT = 8811
STREAM_PORT = 8812
SINK_PORT = 8813


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


class Enigma2AudioIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ffmpeg_available():
            raise unittest.SkipTest("ffmpeg is not available")
        build_ts_fixture(FIXTURE, seconds=30,
                         speech_wav=ROOT / "benchmarks/audio/italian_latency_001_smoke30s.wav")

    def test_canonical_audio_and_channel_change(self):
        async def run():
            first = next(iter(SERVICES))
            api = build_app(FIXTURE, 30, first)
            stream = build_app(FIXTURE, 30, first)
            stream["state"] = api["state"]
            runners = [await serve(api, API_PORT), await serve(stream, STREAM_PORT)]

            source = Enigma2AudioSource(
                host="127.0.0.1", chunk_ms=160,
                openwebif_port=API_PORT, stream_port=STREAM_PORT, service_poll_ms=200,
            )
            result: dict = {}
            try:
                await asyncio.to_thread(source.open)
                await source.start()
                result["track_language"] = source.selected_track["language"]

                frames, samples = 0, 0
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and frames < 20:
                    frame = await source.read_chunk()
                    if frame is None:
                        continue
                    self.assertEqual(frame.sample_rate, 16000)
                    self.assertEqual(frame.channels, 1)
                    frames += 1
                    samples += len(frame.samples)
                result["frames"] = frames
                result["seconds"] = samples / 16000.0

                other = [ref for ref in SERVICES if ref != first][0]
                await asyncio.to_thread(
                    lambda: urllib.request.urlopen(
                        f"http://127.0.0.1:{API_PORT}/api/zap?sRef={urllib.parse.quote(other)}",
                        timeout=5,
                    ).read()
                )
                change = None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    await source.read_chunk()
                    change = source.take_source_change()
                    if change:
                        break
                result["change"] = change

                resumed = 0
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and resumed < 3:
                    if await source.read_chunk() is not None:
                        resumed += 1
                result["resumed_frames"] = resumed
            finally:
                await source.close()
                for runner in runners:
                    await runner.cleanup()
            return result

        result = asyncio.run(run())
        self.assertEqual(result["track_language"], "ita", "Italian track must be preferred")
        self.assertGreaterEqual(result["frames"], 10)
        self.assertGreater(result["seconds"], 1.0)
        self.assertIsNotNone(result["change"], "the zap must be detected")
        self.assertEqual(result["change"]["source_epoch"], 1)
        self.assertGreaterEqual(result["resumed_frames"], 3, "audio must resume on the new service")

    def test_no_orphan_media_subprocess_after_close(self):
        async def run():
            first = next(iter(SERVICES))
            api = build_app(FIXTURE, 30, first)
            stream = build_app(FIXTURE, 30, first)
            stream["state"] = api["state"]
            runners = [await serve(api, API_PORT + 10), await serve(stream, STREAM_PORT + 10)]
            source = Enigma2AudioSource(
                host="127.0.0.1", chunk_ms=160,
                openwebif_port=API_PORT + 10, stream_port=STREAM_PORT + 10,
            )
            await asyncio.to_thread(source.open)
            await source.start()
            pids = [proc.pid for proc in source.procs]
            await source.read_chunk()
            await source.close()
            await asyncio.sleep(0.5)
            for runner in runners:
                await runner.cleanup()
            return pids

        pids = asyncio.run(run())
        self.assertTrue(pids)
        for pid in pids:
            with self.subTest(pid=pid):
                alive = subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0
                self.assertFalse(alive, f"ffmpeg pid {pid} survived close()")


class SubtitleTransportIntegration(unittest.TestCase):
    def test_reconnect_sends_only_the_latest_state_not_a_replay(self):
        async def run():
            receiver = build_receiver(render_fps=0, ack_delay_ms=0, quiet=True)
            runner = await serve(receiver, SINK_PORT)
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=SINK_PORT,
                                       source_id="s", reconnect_initial_ms=50)
            await sink.open()
            for _ in range(100):
                if sink.connected:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(sink.connected)

            # Drop the receiver, keep producing, then bring it back.
            await runner.cleanup()
            await asyncio.sleep(0.3)
            for index in range(50):
                await sink.publish(SubtitleState(seq=index, timestamp=0.0,
                                                 upper_line="vecchio", lower_line=f"stato {index}"))
            receiver2 = build_receiver(render_fps=0, ack_delay_ms=0, quiet=True)
            runner2 = await serve(receiver2, SINK_PORT)
            await asyncio.sleep(2.0)
            counters = receiver2["renderer"].counters()
            telemetry = sink.telemetry()
            await sink.close()
            await runner2.cleanup()
            return counters, telemetry

        counters, telemetry = asyncio.run(run())
        # A replayed backlog would show up as dozens of received states.
        self.assertLessEqual(counters["received"], 3,
                             f"reconnect replayed a queue: {counters}")
        self.assertEqual(telemetry["max_pending"], 1)
        if counters["received"]:
            self.assertEqual(counters["display"]["lower_line"], "stato 49")

    def test_producer_survives_a_subtitle_transport_that_never_exists(self):
        async def run():
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=9, source_id="s",
                                       reconnect_initial_ms=50, connect_timeout_sec=0.2)
            await sink.open()
            produced = 0
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                await asyncio.wait_for(
                    sink.publish(SubtitleState(seq=produced, timestamp=0.0,
                                               upper_line="a", lower_line="b")),
                    timeout=0.5,
                )
                produced += 1
                await asyncio.sleep(0.01)
            telemetry = sink.telemetry()
            await sink.close()
            return produced, telemetry

        produced, telemetry = asyncio.run(run())
        self.assertGreater(produced, 50, "the producer must keep running with no renderer")
        self.assertFalse(telemetry["connected"])
        self.assertEqual(telemetry["max_pending"], 1)

    def test_stale_epoch_states_are_rejected_by_the_receiver(self):
        async def run():
            receiver = build_receiver(render_fps=0, ack_delay_ms=0, quiet=True)
            runner = await serve(receiver, SINK_PORT + 1)
            sink = Enigma2SubtitleSink(host="127.0.0.1", port=SINK_PORT + 1, source_id="A")
            await sink.open()
            for _ in range(100):
                if sink.connected:
                    break
                await asyncio.sleep(0.05)
            await sink.publish(SubtitleState(seq=1, timestamp=0.0, upper_line="", lower_line="canale A"))
            await asyncio.sleep(0.3)
            await sink.source_changed("B")
            await asyncio.sleep(0.3)
            await sink.publish(SubtitleState(seq=2, timestamp=0.0, upper_line="", lower_line="canale B"))
            await asyncio.sleep(0.3)
            # Inject a straggler from the old epoch, exactly as the network would.
            import json as _json
            from hearable import enigma2_protocol as _e2
            await sink._ws.send_json(_e2.subtitle_state(
                seq=99999, source_id="A", source_epoch=0,
                upper_line="", lower_line="parola del canale A in ritardo"))
            await asyncio.sleep(0.5)
            counters = receiver["renderer"].counters()
            await sink.close()
            await runner.cleanup()
            return counters

        counters = asyncio.run(run())
        self.assertGreaterEqual(counters["epoch_rejected_stale_epoch"], 1)
        self.assertNotIn("canale A", counters["display"]["lower_line"])


if __name__ == "__main__":
    unittest.main()
