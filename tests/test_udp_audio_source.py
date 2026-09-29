from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import unittest

import numpy as np

from hearable.audio_sources import AudioSourceRegistry


class UdpAudioSourceTest(unittest.TestCase):
    def test_udp_pcm16le_emits_canonical_audio_frames(self):
        async def run() -> None:
            source = AudioSourceRegistry.create(
                "udp",
                {
                    "listen_address": "127.0.0.1",
                    "listen_port": 0,
                    "audio_format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "source_timeout_ms": 50,
                },
                root=Path.cwd(),
                chunk_ms=80,
                mode="realtime",
            )
            source.open()
            await source.start()
            port = source.listen_port
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            samples = np.arange(1280, dtype="<i2")
            sender.sendto(samples.tobytes(), ("127.0.0.1", port))

            frame = None
            for _ in range(20):
                frame = await source.read_chunk()
                if frame is not None:
                    break
                await asyncio.sleep(0.01)
            await source.close()
            sender.close()

            self.assertIsNotNone(frame)
            assert frame is not None
            self.assertEqual(frame.seq, 1)
            self.assertEqual(frame.sample_rate, 16000)
            self.assertEqual(frame.channels, 1)
            self.assertEqual(len(frame.samples), 1280)
            self.assertEqual(frame.samples.dtype, np.float32)
            self.assertAlmostEqual(float(frame.samples[1]), 1 / 32768.0)

        asyncio.run(run())

    def test_udp_source_tracks_odd_byte_errors(self):
        async def run() -> None:
            source = AudioSourceRegistry.create(
                "udp",
                {
                    "listen_address": "127.0.0.1",
                    "listen_port": 0,
                    "audio_format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                },
                root=Path.cwd(),
                chunk_ms=80,
                mode="realtime",
            )
            source.open()
            await source.start()
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"\x01", ("127.0.0.1", source.listen_port))

            self.assertIsNone(await source.read_chunk())
            telemetry = source.telemetry()
            await source.close()
            sender.close()

            self.assertEqual(telemetry["rx_errors"], 1)
            self.assertEqual(telemetry["state"], "ERROR")

        asyncio.run(run())

    def test_udp_source_drops_partial_buffer_on_source_timeout(self):
        async def run() -> None:
            source = AudioSourceRegistry.create(
                "udp",
                {
                    "listen_address": "127.0.0.1",
                    "listen_port": 0,
                    "audio_format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "source_timeout_ms": 20,
                },
                root=Path.cwd(),
                chunk_ms=80,
                mode="realtime",
            )
            source.open()
            await source.start()
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            samples = np.arange(640, dtype="<i2")
            sender.sendto(samples.tobytes(), ("127.0.0.1", source.listen_port))

            self.assertIsNone(await source.read_chunk())
            telemetry = source.telemetry()
            await source.close()
            sender.close()

            self.assertEqual(telemetry["state"], "NO_PACKETS")
            self.assertEqual(telemetry["source_timeouts"], 1)
            self.assertEqual(telemetry["partial_samples_dropped_on_timeout"], 640)
            self.assertEqual(telemetry["queue_depth_samples"], 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
