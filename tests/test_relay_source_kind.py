"""What the relay decides it is carrying, from the reference Enigma2 gives it.

A recording played from the disk is a type-1 reference, like a live channel.
Read as a channel, the relay went to the tuner for its audio and the subtitles
stayed blank for the whole recording — found by playing one back on the
receiver, with HearAble on and nothing on the screen.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin/HearAbleOSD"))

from relay import AudioRelay, recording_path, source_kind  # noqa: E402

# Copied from the receiver while the recording was playing.
RECORDING = ("1:0:0:0:0:0:0:0:0:0:/media/hdd/movie/20261010 0808 - Italia1 HD - "
             "New Looney Tunes - PrimaTv.ts:New Looney Tunes - PrimaTv")
LIVE = "1:0:19:610:CB:217C:EEEE0000:0:0:0:"
STICK = "4097:0:1:0:0:0:0:0:0:0:/media/sdc1/film.mkv"


class SourceKind(unittest.TestCase):
    def test_a_live_channel_is_dvb(self):
        self.assertEqual(source_kind(LIVE), "dvb")

    def test_a_recording_is_a_file_even_though_its_type_is_1(self):
        self.assertEqual(source_kind(RECORDING), "file")

    def test_a_file_on_the_stick_is_still_a_file(self):
        self.assertEqual(source_kind(STICK), "file")

    def test_a_url_is_still_a_stream(self):
        self.assertEqual(source_kind("4097:0:1:0:0:0:0:0:0:0:http%3a//x/y.m3u8:Name"), "stream")

    def test_nothing_is_unknown(self):
        self.assertEqual(source_kind(None), "unknown")
        self.assertEqual(source_kind(""), "unknown")


class RecordingPath(unittest.TestCase):
    def test_the_path_stops_before_the_title(self):
        self.assertEqual(
            recording_path(RECORDING),
            "/media/hdd/movie/20261010 0808 - Italia1 HD - New Looney Tunes - PrimaTv.ts")

    def test_a_colon_in_the_name_comes_back_as_a_colon(self):
        ref = "1:0:0:0:0:0:0:0:0:0:/media/hdd/movie/Film%3a parte 2.ts:Film: parte 2"
        self.assertEqual(recording_path(ref), "/media/hdd/movie/Film: parte 2.ts")

    def test_a_live_channel_has_no_path(self):
        self.assertEqual(recording_path(LIVE), "")

    def test_a_stick_reference_is_left_to_the_old_rule(self):
        self.assertEqual(recording_path(STICK), "")


class MediaPath(unittest.TestCase):
    def setUp(self):
        self.relay = AudioRelay(lambda: {})

    def test_the_relay_opens_the_recording_not_its_title(self):
        self.assertTrue(self.relay._media_path(RECORDING).endswith("PrimaTv.ts"))

    def test_the_stick_path_is_unchanged(self):
        self.assertEqual(self.relay._media_path(STICK), "/media/sdc1/film.mkv")


class SpawnCommand(unittest.TestCase):
    """A recording carries the broadcast's clock; the relay must not pass it on."""

    def test_the_relay_counts_from_the_start_of_the_file(self):
        import tempfile
        import relay as relay_module

        captured = []

        class FakePopen:
            def __init__(self, command, **_):
                captured.append(command)

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            saved = relay_module.subprocess.Popen, relay_module.FFMPEG_LOG
            relay_module.subprocess.Popen = FakePopen
            relay_module.FFMPEG_LOG = str(Path(tmp) / "ffmpeg.log")
            try:
                relay = AudioRelay(lambda: {})
                relay.receiver_present = lambda: True
                relay._spawn(57.0, 0, recording_path(RECORDING), "test")
            finally:
                relay_module.subprocess.Popen, relay_module.FFMPEG_LOG = saved

        command = captured[0]
        self.assertIn("-copyts", command)
        self.assertIn("-start_at_zero", command)
        self.assertLess(command.index("-start_at_zero"), command.index("-i"))


if __name__ == "__main__":
    unittest.main()
