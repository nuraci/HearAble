"""Unit coverage for the Enigma2 OSD plugin's transport and receiver behaviour.

The plugin runs inside enigma2, where a mistake is a black television rather
than a stack trace, so the two parts that can be tested without a set-top box
are tested here: the hand-rolled WebSocket server and the receiver-side
subtitle state. Only the screen itself needs the real box.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugin/HearAbleOSD"))
sys.path.insert(0, str(ROOT / "hearable"))

from hearable import enigma2_protocol as e2

from subtitle_session import SubtitleSession
from wsserver import OPCODE_TEXT, WebSocketServer, encode_frame


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Client:
    """The smallest WebSocket client that can drive the server under test."""

    def __init__(self, port: int, path: str = "/hearable") -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        self.handshake = self._read_until(b"\r\n\r\n")

    def _read_until(self, marker: bytes) -> bytes:
        buffer = b""
        deadline = time.time() + 5
        while marker not in buffer and time.time() < deadline:
            buffer += self.sock.recv(4096)
        return buffer

    @property
    def accepted(self) -> bool:
        return b"101" in self.handshake.split(b"\r\n")[0]

    def send_text(self, payload: str) -> None:
        self.send_frame(payload.encode("utf-8"), OPCODE_TEXT, final=True)

    def send_frame(self, payload: bytes, opcode: int, final: bool = True) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([(0x80 if final else 0x00) | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack("!H", len(payload))
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", len(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class ServerHarness:
    def __init__(self) -> None:
        self.port = free_port()
        self.received: list[str] = []
        self.server = WebSocketServer(port=self.port,
                                      on_message=lambda text, client: self.received.append(text))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ServerHarness":
        self.server.start()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.server.stop()

    def _pump(self) -> None:
        while not self._stop.is_set():
            self.server.poll()
            time.sleep(0.005)

    def settle(self, seconds: float = 0.4) -> None:
        time.sleep(seconds)


class WebSocketServerTest(unittest.TestCase):
    def test_handshake_and_text_frames_round_trip(self):
        with ServerHarness() as harness:
            client = Client(harness.port)
            self.assertTrue(client.accepted, harness.server.protocol_errors)
            client.send_text('{"hello": "world"}')
            client.send_text("x" * 300)          # crosses into the 16-bit length form
            harness.settle()
            self.assertEqual(harness.received, ['{"hello": "world"}', "x" * 300])
            client.close()

    def test_a_wrong_path_is_refused_rather_than_served(self):
        with ServerHarness() as harness:
            client = Client(harness.port, path="/somewhere-else")
            self.assertFalse(client.accepted)
            self.assertEqual(harness.received, [])
            client.close()

    def test_fragmented_frames_are_refused_not_half_supported(self):
        with ServerHarness() as harness:
            client = Client(harness.port)
            client.send_frame(b"half a message", OPCODE_TEXT, final=False)
            harness.settle()
            self.assertEqual(harness.received, [])
            self.assertEqual(harness.server.client_count, 0)
            client.close()

    def test_a_handler_that_raises_does_not_take_the_server_down(self):
        with ServerHarness() as harness:
            harness.server.on_message = lambda text, client: (_ for _ in ()).throw(RuntimeError("boom"))
            client = Client(harness.port)
            client.send_text("first")
            harness.settle()
            self.assertGreaterEqual(harness.server.protocol_errors, 1)
            self.assertEqual(harness.server.client_count, 1)
            client.close()

    def test_server_frames_are_never_masked(self):
        frame = encode_frame(b"ack")
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[1] & 0x80, 0, "a server frame must not set the mask bit")


class SubtitleSessionTest(unittest.TestCase):
    def state(self, seq: int, epoch: int, upper: str, lower: str = "") -> str:
        return json.dumps(e2.subtitle_state(seq=seq, source_id="test", source_epoch=epoch,
                                            upper_line=upper, lower_line=lower))

    def test_only_the_newest_state_is_ever_pending(self):
        session = SubtitleSession()
        for seq in range(1, 21):
            session.handle_text(self.state(seq, 0, f"linea {seq}"))
        self.assertEqual(session.states_received, 20)
        self.assertEqual(session.states_superseded, 19)
        self.assertEqual(session.take_render(), ("linea 20", ""))
        self.assertIsNone(session.take_render(), "a second render has nothing new to paint")

    def test_a_stale_epoch_never_reaches_the_screen(self):
        session = SubtitleSession()
        session.handle_text(self.state(1, 5, "corrente"))
        session.take_render()
        session.handle_text(self.state(2, 4, "vecchia"))
        self.assertEqual(session.guard.rejected_stale_epoch, 1)
        self.assertIsNone(session.take_render())
        self.assertEqual(session.upper_line, "corrente")

    def test_a_replayed_sequence_never_reaches_the_screen(self):
        session = SubtitleSession()
        session.handle_text(self.state(10, 0, "corrente"))
        session.take_render()
        session.handle_text(self.state(9, 0, "vecchia"))
        self.assertEqual(session.guard.rejected_stale_seq, 1)
        self.assertIsNone(session.take_render())

    def test_hello_reopens_a_stream_a_restarted_producer_would_otherwise_lose(self):
        session = SubtitleSession()
        session.handle_text(self.state(500, 7, "prima"))
        session.take_render()
        # A restarted producer comes back at epoch 0, sequence 1.
        session.handle_text(json.dumps(e2.hello(role=e2.ROLE_PRODUCER, session_id="riavviato")))
        session.handle_text(self.state(1, 0, "dopo il riavvio"))
        self.assertEqual(session.take_render(), ("dopo il riavvio", ""))
        self.assertEqual(session.guard.streams_started, 1)

    def test_a_clear_is_not_overwritten_by_the_state_that_follows_it(self):
        session = SubtitleSession()
        session.handle_text(self.state(1, 0, "qualcosa"))
        session.take_render()
        session.handle_text(json.dumps(e2.clear_subtitles(seq=2, source_epoch=0, reason="test")))
        session.handle_text(self.state(3, 0, "nuovo"))
        # Both arrive between renders: the clear is applied first, so the screen
        # ends on the new state rather than blank, and the state never swallows
        # the clear.
        self.assertEqual(session.take_render(), ("nuovo", ""))

    def test_a_source_change_blanks_the_previous_source(self):
        session = SubtitleSession()
        session.handle_text(self.state(1, 0, "sorgente A"))
        session.take_render()
        session.handle_text(json.dumps(e2.source_changed(
            seq=2, source_id="B", previous_source_id="A", source_epoch=1, reason="test")))
        self.assertEqual(session.take_render(), ("", ""))

    def test_the_control_queue_is_bounded(self):
        session = SubtitleSession(control_queue_limit=8)
        for seq in range(1, 101):
            session.handle_text(json.dumps(
                e2.clear_subtitles(seq=seq, source_epoch=0, reason=f"flood {seq}")))
        self.assertEqual(len(session._control), 8)
        self.assertEqual(session.controls_dropped, 92)

    def test_malformed_input_is_counted_rather_than_raised(self):
        session = SubtitleSession()
        for bad in ("not json at all", "[]", '{"type": "nonsense", "protocol_version": 1}'):
            self.assertIsNone(session.handle_text(bad))
        self.assertEqual(session.messages_invalid, 3)

    def test_a_cumulative_transcript_is_refused_by_the_contract(self):
        session = SubtitleSession()
        message = json.loads(self.state(1, 0, "linea"))
        message["final_text"] = "l'intera trascrizione fino a qui"
        session.handle_text(json.dumps(message))
        self.assertEqual(session.messages_invalid, 1)
        self.assertEqual(session.states_received, 0)


class PublicationSchedulerTest(unittest.TestCase):
    """The delay that holds a subtitle back until the picture has caught up.

    Measured on the direct demux tap, the subtitle arrives about a second and a
    half *before* the image it belongs to, so the fix is to hold the subtitle
    rather than to delay the picture. These tests pin the two things that makes
    dangerous: a state must not be painted early, and a held state must never
    become a backlog.
    """

    def state(self, seq: int, epoch: int, upper: str, lower: str = "") -> str:
        return json.dumps(e2.subtitle_state(seq=seq, source_id="test", source_epoch=epoch,
                                            upper_line=upper, lower_line=lower))

    def test_a_state_is_held_for_the_delay_and_then_painted(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(120)
        session.handle_text(self.state(1, 0, "attesa"))
        self.assertIsNone(session.take_render(), "painted before its instant")
        self.assertTrue(session.scheduler_state()["pending"])
        time.sleep(0.15)
        self.assertEqual(session.take_render(), ("attesa", ""))
        self.assertEqual(session.scheduler_state()["published"], 1)
        self.assertFalse(session.scheduler_state()["pending"])

    def test_the_delay_survives_ticks_that_paint_nothing(self):
        """The regression that cost an evening: an idle tick reset the delay."""
        session = SubtitleSession()
        session.set_publish_delay_ms(1500)
        for _ in range(50):
            self.assertIsNone(session.take_render())
        self.assertEqual(session.publish_delay_ms, 1500)

    def test_waiting_costs_freshness_never_a_backlog(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(80)
        for seq in range(1, 31):
            session.handle_text(self.state(seq, 0, f"linea {seq}"))
            self.assertIsNone(session.take_render())
        time.sleep(0.1)
        self.assertEqual(session.take_render(), ("linea 30", ""),
                         "the held states replayed instead of coalescing")
        self.assertEqual(session.scheduler_state()["coalesced_while_waiting"], 29)
        self.assertIsNone(session.take_render())

    def test_raising_the_delay_does_not_restart_a_state_that_nearly_waited(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(100)
        session.handle_text(self.state(1, 0, "quasi"))
        time.sleep(0.09)
        session.set_publish_delay_ms(200)
        due = session.scheduler_state()["due_in_ms"]
        self.assertLess(due, 120, "the deadline restarted rather than being recomputed")
        self.assertGreater(due, 80)

    def test_lowering_the_delay_can_make_a_held_state_due_at_once(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(2000)
        session.handle_text(self.state(1, 0, "subito"))
        self.assertIsNone(session.take_render())
        session.set_publish_delay_ms(0)
        self.assertEqual(session.take_render(), ("subito", ""))

    def test_a_source_change_discards_whatever_is_waiting(self):
        """A zap must not let the previous channel's words arrive afterwards."""
        session = SubtitleSession()
        session.set_publish_delay_ms(300)
        session.handle_text(self.state(1, 0, "canale vecchio"))
        session.handle_text(json.dumps(e2.source_changed(seq=2, source_id="test",
                                                         source_epoch=1)))
        session.take_render()
        self.assertEqual(session.scheduler_state()["invalidated_by_barrier"], 1)
        time.sleep(0.35)
        self.assertIsNone(session.take_render(), "a held state crossed the barrier")
        self.assertEqual(session.upper_line, "")

    def test_a_clear_discards_whatever_is_waiting(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(300)
        session.handle_text(self.state(1, 0, "da cancellare"))
        session.handle_text(json.dumps(e2.clear_subtitles(seq=2, source_epoch=0, reason="test")))
        session.take_render()
        time.sleep(0.35)
        self.assertIsNone(session.take_render())
        self.assertEqual(session.upper_line, "")

    def test_a_hello_drops_the_held_state_of_the_previous_stream(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(300)
        session.handle_text(self.state(1, 0, "vecchio produttore"))
        session.handle_text(json.dumps(e2.hello(role="producer", session_id="s2")))
        time.sleep(0.35)
        self.assertIsNone(session.take_render())

    def test_zero_delay_paints_in_the_same_tick(self):
        session = SubtitleSession()
        session.handle_text(self.state(1, 0, "immediato"))
        self.assertEqual(session.take_render(), ("immediato", ""))
        self.assertIsNone(session.scheduler_state()["due_in_ms"])

    def test_continuous_speech_is_still_painted(self):
        """The defect that a single pending slot had, measured on the box.

        While somebody talks, a new state arrives every couple of hundred
        milliseconds. With one slot each arrival pushed the previous deadline
        back, and in three minutes of television the plugin held 197 states and
        painted three. The delay line paints every one of them, a delay later.
        """
        session = SubtitleSession()
        session.set_publish_delay_ms(150)
        painted = []
        start = time.monotonic()
        seq = 0
        while time.monotonic() - start < 0.9:
            seq += 1
            session.handle_text(self.state(seq, 0, f"parla {seq}"))
            time.sleep(0.05)
            lines = session.take_render()
            if lines is not None:
                painted.append(lines[0])
        self.assertGreater(len(painted), 8,
                           "the screen starved while the producer kept talking")
        self.assertEqual(painted, sorted(painted, key=lambda s: int(s.split()[1])),
                         "the delayed states arrived out of order")

    def test_the_delay_line_is_bounded(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(3000)
        for seq in range(1, 501):
            session.handle_text(self.state(seq, 0, f"linea {seq}"))
        depth = session.scheduler_state()["queue_depth"]
        self.assertLessEqual(depth, 64)
        self.assertEqual(session.scheduler_state()["dropped_overflow"], 500 - depth)
        session.set_publish_delay_ms(0)
        self.assertEqual(session.take_render(), ("linea 500", ""),
                         "the newest state was not the one that survived")


    def test_nothing_waits_through_the_switch_being_off(self):
        """What the plugin does every tick while HearAble is off."""
        session = SubtitleSession()
        session.set_publish_delay_ms(1500)
        session.handle_text(self.state(1, 0, "un attimo prima dello spegnimento"))
        session.drop_pending("hearable_off")
        self.assertEqual(session.scheduler_state()["queue_depth"], 0)
        self.assertEqual(session.scheduler_state()["invalidated_by_barrier"], 1)
        self.assertIsNone(session.take_render())


    def test_the_diagnostic_stamp_travels_with_the_state(self):
        """What the F2/F3 campaign needed: a second clock, sampled at arrival.

        On the box it is the audio decoder's PTS, so the wait can be reported in
        the broadcast's own time rather than only in the box's.
        """
        ticks = iter([1000, 2000, 3000])
        session = SubtitleSession(stamp=lambda: next(ticks))
        session.set_publish_delay_ms(60)
        session.handle_text(self.state(1, 0, "con marca temporale"))
        time.sleep(0.08)
        self.assertEqual(session.take_render(), ("con marca temporale", ""))
        painted = session.last_painted
        self.assertEqual(painted["stamp_at_arrival"], 1000)
        self.assertEqual(painted["seq"], 1)
        self.assertEqual(painted["delay_ms_in_force"], 60)
        self.assertIsNotNone(painted["arrived_monotonic"])

    def test_without_a_stamp_the_field_is_absent_rather_than_invented(self):
        session = SubtitleSession()
        session.handle_text(self.state(1, 0, "senza marca"))
        session.take_render()
        self.assertIsNone(session.last_painted["stamp_at_arrival"])


    def test_the_delay_is_clamped_to_a_non_negative_whole_number(self):
        session = SubtitleSession()
        session.set_publish_delay_ms(-500)
        self.assertEqual(session.publish_delay_ms, 0)


if __name__ == "__main__":
    unittest.main()
