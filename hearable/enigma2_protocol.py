"""HearAble <-> Enigma2 subtitle protocol, version 1.

The shared contract between the HearAble producer (PC today, Arduino UNO Q later)
and the SF8008 renderer plugin. Both ends import this module, so the wire format
cannot drift between them; `docs/enigma2_hearable_protocol.md` documents it.

Two rules shape everything here:

* **Never send a cumulative transcript.** A subtitle_state carries the two display
  lines and nothing else. Sending the growing transcript would make the stream
  quadratic in session length, which is exactly the cost already measured in the
  PC baseline's metrics bookkeeping.
* **The producer never waits for the renderer.** ACKs are observational. Nothing
  in this module blocks.
"""
from __future__ import annotations

import time
from typing import Any

PROTOCOL_VERSION = 1

#: Display contract: two lines, each at most this many characters.
MAX_LINE_CHARS = 40

ROLE_PRODUCER = "hearable_producer"
ROLE_RENDERER = "enigma2_renderer"

MESSAGE_TYPES = frozenset({
    "hello",
    "capabilities",
    "subtitle_state",
    "clear_subtitles",
    "source_changed",
    "heartbeat",
    "render_ack",
    "error",
})


class ProtocolError(ValueError):
    """A message that does not satisfy the version 1 contract."""


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def clip_line(text: str) -> str:
    """Enforce the 40-character display contract at the network boundary.

    The formatter is expected to have done this already; doing it again here
    means a formatter bug cannot put an over-long line on the wire.
    """
    line = " ".join((text or "").split())
    return line[:MAX_LINE_CHARS]


def hello(*, role: str, project: str = "HearAble", session_id: str = "") -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "role": role,
        "project": project,
        "session_id": session_id,
        "generated_monotonic_ms": monotonic_ms(),
    }


def capabilities(
    *,
    role: str,
    max_line_chars: int = MAX_LINE_CHARS,
    lines: int = 2,
    supports_render_ack: bool = False,
    supports_clear: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "type": "capabilities",
        "protocol_version": PROTOCOL_VERSION,
        "role": role,
        "max_line_chars": max_line_chars,
        "lines": lines,
        "supports_render_ack": supports_render_ack,
        "supports_clear": supports_clear,
    }
    if extra:
        payload.update(extra)
    return payload


def subtitle_state(
    *,
    seq: int,
    source_id: str,
    source_epoch: int,
    upper_line: str,
    lower_line: str,
    clear: bool = False,
    generated_monotonic_ms: float | None = None,
    audio_position_ms: float | None = None,
) -> dict[str, Any]:
    """Build a subtitle_state.

    `audio_position_ms` is optional and says how far into the programme the audio
    behind this subtitle was. A live receiver does not need it — it aligns with
    its own fixed A/V delay — but it lets a player that controls its own playback
    position line the picture up exactly, which is what the simulator does.
    """
    payload = {
        "type": "subtitle_state",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "source_id": str(source_id),
        "source_epoch": int(source_epoch),
        "upper_line": clip_line(upper_line),
        "lower_line": clip_line(lower_line),
        "clear": bool(clear),
        "generated_monotonic_ms": (
            monotonic_ms() if generated_monotonic_ms is None else float(generated_monotonic_ms)
        ),
    }
    if audio_position_ms is not None:
        payload["audio_position_ms"] = float(audio_position_ms)
    return payload


def clear_subtitles(*, seq: int, source_epoch: int, reason: str) -> dict[str, Any]:
    return {
        "type": "clear_subtitles",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "source_epoch": int(source_epoch),
        "reason": str(reason),
        "generated_monotonic_ms": monotonic_ms(),
    }


def source_changed(
    *, seq: int, source_id: str, source_epoch: int, previous_source_id: str = "", reason: str = "service_changed"
) -> dict[str, Any]:
    return {
        "type": "source_changed",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "source_id": str(source_id),
        "previous_source_id": str(previous_source_id),
        "source_epoch": int(source_epoch),
        "reason": str(reason),
        "generated_monotonic_ms": monotonic_ms(),
    }


def heartbeat(*, seq: int) -> dict[str, Any]:
    return {
        "type": "heartbeat",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "generated_monotonic_ms": monotonic_ms(),
    }


def render_ack(*, seq: int, source_epoch: int, rendered_monotonic_ms: float | None = None) -> dict[str, Any]:
    return {
        "type": "render_ack",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "source_epoch": int(source_epoch),
        "rendered_monotonic_ms": (
            monotonic_ms() if rendered_monotonic_ms is None else float(rendered_monotonic_ms)
        ),
    }


def error(*, code: str, message: str, seq: int = 0) -> dict[str, Any]:
    return {
        "type": "error",
        "protocol_version": PROTOCOL_VERSION,
        "seq": int(seq),
        "code": str(code),
        "message": str(message),
        "generated_monotonic_ms": monotonic_ms(),
    }


def validate(message: Any) -> dict[str, Any]:
    """Raise ProtocolError unless the message satisfies the v1 contract."""
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    kind = message.get("type")
    if kind not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type: {kind!r}")
    version = message.get("protocol_version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol_version: {version!r}")
    if kind in {"subtitle_state", "clear_subtitles", "source_changed", "render_ack"}:
        if not isinstance(message.get("source_epoch"), int):
            raise ProtocolError(f"{kind} requires an integer source_epoch")
    if kind == "subtitle_state":
        for field in ("upper_line", "lower_line"):
            value = message.get(field)
            if not isinstance(value, str):
                raise ProtocolError(f"subtitle_state.{field} must be a string")
            if len(value) > MAX_LINE_CHARS:
                raise ProtocolError(
                    f"subtitle_state.{field} exceeds {MAX_LINE_CHARS} characters: {len(value)}"
                )
        for forbidden in ("final_text", "raw", "transcript"):
            if forbidden in message:
                raise ProtocolError(
                    f"subtitle_state must not carry a cumulative transcript field: {forbidden}"
                )
    # hello/capabilities are the handshake and carry no sequence: they are not
    # part of the ordered stream and must not be sequence-checked.
    if kind not in {"hello", "capabilities"} and not isinstance(message.get("seq"), int):
        raise ProtocolError(f"{kind} requires an integer seq")
    return message


class EpochGuard:
    """Receiver-side guard: accept a message only if it is not from a stale epoch.

    A channel change bumps source_epoch. Packets still in flight from the old
    channel arrive after the change and must be discarded, or channel A's words
    appear over channel B's picture.
    """

    def __init__(self) -> None:
        self.current_epoch = -1
        self.accepted = 0
        self.rejected_stale_epoch = 0
        self.rejected_stale_seq = 0
        self.latest_seq = -1
        self.streams_started = 0

    def reset(self) -> None:
        """Begin a new stream: forget the epoch and sequence seen so far.

        Called when a producer says `hello`, which happens once per connection
        and never from a packet still in flight after a source change. Without
        it a producer that restarts is silenced permanently: it comes back at
        epoch 0 and sequence 1, which this guard correctly reads as stale
        against the epoch and sequence the previous run had reached, and every
        subtitle it sends from then on is discarded.
        """
        self.current_epoch = -1
        self.latest_seq = -1
        self.streams_started += 1

    def accept(self, message: dict[str, Any]) -> bool:
        epoch = message.get("source_epoch")
        if not isinstance(epoch, int):
            return True
        if epoch < self.current_epoch:
            self.rejected_stale_epoch += 1
            return False
        if epoch > self.current_epoch:
            # A new epoch resets sequence tracking; seq is only monotonic within one.
            self.current_epoch = epoch
            self.latest_seq = -1
        seq = message.get("seq")
        if isinstance(seq, int) and seq <= self.latest_seq:
            self.rejected_stale_seq += 1
            return False
        if isinstance(seq, int):
            self.latest_seq = seq
        self.accepted += 1
        return True

    def counters(self) -> dict[str, int]:
        return {
            "current_epoch": self.current_epoch,
            "accepted": self.accepted,
            "rejected_stale_epoch": self.rejected_stale_epoch,
            "rejected_stale_seq": self.rejected_stale_seq,
            "latest_seq": self.latest_seq,
        }
