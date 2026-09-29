from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import time
from typing import Any, Callable, Protocol

from hearable import enigma2_protocol as e2
from hearable.io_config import BackendConfigError


@dataclass
class SubtitleState:
    seq: int
    timestamp: float
    upper_line: str
    lower_line: str
    stable: str = ""
    unstable: str = ""
    final_text: str = ""
    final_text_truncated: bool = False
    raw: str = ""
    is_final: bool = False
    clear: bool = False
    audio_processed: float | None = None
    confidence: float | None = None
    led: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    commit_events: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_browser_message(self) -> dict[str, Any]:
        return {
            "type": "subtitle",
            "seq": self.seq,
            "stable": self.stable,
            "unstable": self.unstable,
            "final_text": self.final_text,
            "final_text_truncated": self.final_text_truncated,
            "raw": self.raw,
            "is_final": self.is_final,
            "audio_processed": self.audio_processed,
            "confidence": self.confidence,
            "led": self.led,
            "latency": self.latency,
            "commit_events": self.commit_events,
            "metrics": self.metrics,
        }


class SubtitleSink(Protocol):
    sink_type: str

    async def open(self) -> None:
        ...

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        ...

    async def clear(self) -> None:
        ...

    async def flush(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def telemetry(self) -> dict[str, Any]:
        ...


class NullSubtitleSink:
    sink_type = "null"

    def __init__(self) -> None:
        self.states: list[SubtitleState] = []
        self.closed = False

    async def open(self) -> None:
        self.closed = False

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        del internal_latency
        self.states.append(state)

    async def clear(self) -> None:
        self.states.append(SubtitleState(seq=0, timestamp=time.monotonic(), upper_line="", lower_line="", clear=True))

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    def telemetry(self) -> dict[str, Any]:
        return {"type": self.sink_type, "published": len(self.states), "supports_ack": False}


class ConsoleSubtitleSink:
    sink_type = "console"

    def __init__(self, *, stream: Any | None = None) -> None:
        self.stream = stream
        self.published = 0
        self.closed = False

    async def open(self) -> None:
        self.closed = False

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        del internal_latency
        self.published += 1
        line = "\n".join(item for item in (state.upper_line, state.lower_line) if item)
        print(line, file=self.stream, flush=True)

    async def clear(self) -> None:
        print("", file=self.stream, flush=True)

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    def telemetry(self) -> dict[str, Any]:
        return {"type": self.sink_type, "published": self.published, "supports_ack": False}


class OctagonUdpSubtitleSink:
    sink_type = "octagon_udp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        message_format: str = "json",
        max_datagram_bytes: int = 4096,
    ) -> None:
        if not host:
            raise BackendConfigError("subtitle sink 'octagon_udp' host must not be empty")
        if not (1 <= port <= 65535):
            raise BackendConfigError("subtitle sink 'octagon_udp' port must be between 1 and 65535")
        if message_format not in {"json", "text"}:
            raise BackendConfigError("subtitle sink 'octagon_udp' format must be 'json' or 'text'")
        if max_datagram_bytes <= 0:
            raise BackendConfigError("subtitle sink 'octagon_udp' max_datagram_bytes must be positive")
        self.host = host
        self.port = port
        self.message_format = message_format
        self.max_datagram_bytes = max_datagram_bytes
        self.sock: socket.socket | None = None
        self.published = 0
        self.bytes_sent = 0
        self.send_errors = 0
        self.truncated = 0

    async def open(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        message = self._format_state(state, internal_latency)
        self._send(message)
        self.published += 1

    async def clear(self) -> None:
        self._send({"type": "clear", "timestamp": time.time()})

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.sink_type,
            "host": self.host,
            "port": self.port,
            "format": self.message_format,
            "published": self.published,
            "bytes_sent": self.bytes_sent,
            "send_errors": self.send_errors,
            "truncated": self.truncated,
            "supports_ack": False,
        }

    def _format_state(self, state: SubtitleState, internal_latency: dict[str, Any] | None) -> dict[str, Any] | str:
        if self.message_format == "text":
            return "\n".join(item for item in (state.upper_line, state.lower_line) if item)
        payload = state.to_browser_message()
        payload["type"] = "octagon_subtitle"
        payload["upper_line"] = state.upper_line
        payload["lower_line"] = state.lower_line
        if internal_latency:
            payload["internal_latency"] = internal_latency
        return payload

    def _send(self, message: dict[str, Any] | str) -> None:
        if self.sock is None:
            raise RuntimeError("OctagonUdpSubtitleSink.open() was not called")
        if isinstance(message, str):
            data = message.encode("utf-8")
        else:
            data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > self.max_datagram_bytes:
            data = data[: self.max_datagram_bytes]
            self.truncated += 1
        try:
            sent = self.sock.sendto(data, (self.host, self.port))
            self.bytes_sent += sent
        except OSError:
            self.send_errors += 1




def _summarise_ms(samples: list[float]) -> dict[str, float | int] | None:
    """A small distribution, or nothing. Averages alone hide the tail."""
    if not samples:
        return None
    ordered = sorted(samples)
    def at(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 3)
    return {"count": len(ordered), "p50_ms": at(0.5), "p90_ms": at(0.9),
            "p95_ms": at(0.95), "max_ms": round(ordered[-1], 3)}


def _media_position_ms(state: SubtitleState) -> float | None:
    """Position within the programme, preferring the source's own figure.

    A source that seeks knows where it is; `audio_processed` only counts from the
    start of the ASR stream and would report the wrong place after a seek.
    """
    position = (state.metadata or {}).get("source_media_position_ms")
    if isinstance(position, (int, float)):
        return float(position)
    if state.audio_processed is not None:
        return state.audio_processed * 1000.0
    return None


class SubtitleProtocolSink:
    """Persistent WebSocket transport for SubtitleState, speaking protocol v1.

    Nothing in here is Enigma2-specific: it sends two display lines to whatever
    renders them. The SF8008 plugin is one such renderer and the desktop player is
    another, which is why it is registered under two names — `enigma2` for the
    frozen set-top contract, `subtitle_ws` for everything else. Both are this same
    class; only the label reported in telemetry differs, so an artifact still says
    which side of the project produced it.

    Three properties matter more than throughput here:

    * `publish()` never blocks and never awaits the renderer. It swaps the single
      pending state for the newest one, so a slow renderer costs freshness, not
      backpressure into the ASR pipeline.
    * Exactly one subtitle state is ever pending. There is no queue to grow.
    * A subtitle-transport failure is its own failure domain: the connection
      retries in the background and the producer keeps running throughout.

    Only the two display lines go on the wire; the cumulative transcript never
    does. See `docs/enigma2_hearable_protocol.md`.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sink_type: str = "subtitle_ws",
        path: str = "/hearable",
        scheme: str = "ws",
        source_id: str = "unknown",
        heartbeat_ms: int = 2000,
        reconnect_initial_ms: int = 250,
        reconnect_max_ms: int = 5000,
        connect_timeout_sec: float = 5.0,
        control_queue_limit: int = 32,
    ) -> None:
        self.sink_type = sink_type
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.scheme = scheme
        self.source_id = source_id
        self.heartbeat_ms = heartbeat_ms
        self.reconnect_initial_ms = reconnect_initial_ms
        self.reconnect_max_ms = reconnect_max_ms
        self.connect_timeout_sec = connect_timeout_sec
        self.control_queue_limit = control_queue_limit

        self.source_epoch = 0
        self.seq = 0
        self.connected = False

        # Counters. Named so the stress artifact can be read without the source.
        self.generated = 0
        self.sent = 0
        self.coalesced = 0
        self.acks_received = 0
        self.reconnects = 0
        self.send_errors = 0
        self.protocol_errors = 0
        self.heartbeats_sent = 0
        self.latest_acked_seq = -1
        self.max_pending = 0
        self.seq_lag_samples: list[int] = []
        self.ack_latency_ms: list[float] = []

        # One subtitle state may be superseded by a newer one; control messages
        # may not. A clear must not swallow the source_changed that precedes it,
        # so control travels in its own bounded queue.
        self._pending: dict[str, Any] | None = None
        self._control: deque[dict[str, Any]] = deque(maxlen=control_queue_limit)
        self.control_dropped = 0
        self._sent_seq_high_water = -1
        self._pending_generated_ms: dict[int, float] = {}
        self._wake = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._session: Any = None
        self._ws: Any = None

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.path}"

    async def open(self) -> None:
        self._task = asyncio.create_task(self._run(), name="enigma2-subtitle-sink")

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _queue_state(self, message: dict[str, Any]) -> None:
        """Replace the pending subtitle state. This is the coalescing point."""
        if self._pending is not None:
            self.coalesced += 1
        self._pending = message
        self.max_pending = max(self.max_pending, 1)
        self._wake.set()

    def _queue_control(self, message: dict[str, Any]) -> None:
        """Enqueue a control message. Bounded, and never coalesced away."""
        if len(self._control) == self._control.maxlen:
            self.control_dropped += 1
        self._control.append(message)
        self._wake.set()

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        del internal_latency
        self.generated += 1
        message = e2.subtitle_state(
            seq=self._next_seq(),
            source_id=self.source_id,
            source_epoch=self.source_epoch,
            upper_line=state.upper_line,
            lower_line=state.lower_line,
            clear=state.clear,
            audio_position_ms=_media_position_ms(state),
        )
        self._pending_generated_ms[message["seq"]] = message["generated_monotonic_ms"]
        self._queue_state(message)

    async def clear(self, reason: str = "clear") -> None:
        # A clear must reach the renderer promptly and must never be superseded
        # by a subtitle state, so it goes through the control queue.
        self._queue_control(
            e2.clear_subtitles(seq=self._next_seq(), source_epoch=self.source_epoch, reason=reason)
        )
        # Anything still pending belongs to the source we are leaving.
        self._pending = None

    async def source_changed(self, source_id: str, reason: str = "service_changed") -> int:
        """Bump the epoch and tell the renderer to drop what it is showing.

        Clearing must not wait for new speech, so the clear is queued immediately
        rather than riding along with the next subtitle state.
        """
        previous = self.source_id
        self.source_epoch += 1
        self.source_id = source_id
        self._queue_control(
            e2.source_changed(
                seq=self._next_seq(),
                source_id=source_id,
                previous_source_id=previous,
                source_epoch=self.source_epoch,
                reason=reason,
            )
        )
        await self.clear(reason="source_changed")
        return self.source_epoch

    async def flush(self, timeout_sec: float = 2.0) -> None:
        """Drain the single pending state and wait briefly for its ACK.

        Bounded on purpose. Flushing happens at shutdown, where leaving the last
        subtitle unsent would be untidy, but a renderer that has gone away must
        never be able to hold the producer open.
        """
        deadline = time.monotonic() + timeout_sec
        self._wake.set()
        while time.monotonic() < deadline:
            if (
                self._pending is None
                and not self._control
                and self.latest_acked_seq >= self.sent_seq_high_water
            ):
                return
            await asyncio.sleep(0.02)

    @property
    def sent_seq_high_water(self) -> int:
        return self._sent_seq_high_water

    async def close(self) -> None:
        self._closing.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        await self._disconnect()

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.sink_type,
            "url": self.url,
            "connected": self.connected,
            "source_id": self.source_id,
            "source_epoch": self.source_epoch,
            "generated": self.generated,
            "sent": self.sent,
            "coalesced": self.coalesced,
            "control_pending": len(self._control),
            "control_dropped": self.control_dropped,
            "acks_received": self.acks_received,
            "latest_acked_seq": self.latest_acked_seq,
            # Already collected; it was simply never reported, which left the
            # leg between "state sent" and "state on the screen" unmeasurable
            # from a run's own artifacts.
            "ack_latency_ms": _summarise_ms(self.ack_latency_ms),
            "pending_now": (0 if self._pending is None else 1) + len(self._control),
            "max_pending": self.max_pending,
            "reconnects": self.reconnects,
            "send_errors": self.send_errors,
            "protocol_errors": self.protocol_errors,
            "heartbeats_sent": self.heartbeats_sent,
            "supports_ack": True,
        }

    async def _disconnect(self) -> None:
        self.connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _connect(self) -> bool:
        import aiohttp

        try:
            self._session = aiohttp.ClientSession()
            self._ws = await asyncio.wait_for(
                self._session.ws_connect(self.url, heartbeat=None),
                timeout=self.connect_timeout_sec,
            )
        except Exception:
            await self._disconnect()
            return False
        try:
            await self._ws.send_json(e2.hello(role=e2.ROLE_PRODUCER, session_id=self.source_id))
            await self._ws.send_json(
                e2.capabilities(role=e2.ROLE_PRODUCER, supports_render_ack=True)
            )
        except Exception:
            await self._disconnect()
            return False
        self.connected = True
        return True

    async def _reader(self) -> None:
        import aiohttp

        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    payload = e2.validate(json.loads(msg.data))
                except (ValueError, e2.ProtocolError):
                    self.protocol_errors += 1
                    continue
                if payload["type"] == "render_ack":
                    self._on_ack(payload)
        except Exception:
            pass

    def _on_ack(self, payload: dict[str, Any]) -> None:
        seq = payload["seq"]
        if payload.get("source_epoch") != self.source_epoch:
            return
        self.acks_received += 1
        self.latest_acked_seq = max(self.latest_acked_seq, seq)
        self.seq_lag_samples.append(max(0, self.seq - seq))
        generated = self._pending_generated_ms.pop(seq, None)
        if generated is not None:
            self.ack_latency_ms.append(e2.monotonic_ms() - generated)
        # Only the newest generation timestamps are useful; keep the map bounded.
        if len(self._pending_generated_ms) > 256:
            for stale in sorted(self._pending_generated_ms)[:-64]:
                self._pending_generated_ms.pop(stale, None)

    async def _run(self) -> None:
        backoff_ms = self.reconnect_initial_ms
        last_heartbeat = e2.monotonic_ms()
        reader: asyncio.Task | None = None
        while not self._closing.is_set():
            if not self.connected:
                if not await self._connect():
                    await asyncio.sleep(backoff_ms / 1000.0)
                    backoff_ms = min(backoff_ms * 2, self.reconnect_max_ms)
                    continue
                backoff_ms = self.reconnect_initial_ms
                self.reconnects += 1
                reader = asyncio.create_task(self._reader(), name="enigma2-sink-reader")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.heartbeat_ms / 1000.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

            # Control first: ordering between a source change and its clear is
            # part of the contract.
            if self._control:
                message = self._control.popleft()
            else:
                message, self._pending = self._pending, None
            now = e2.monotonic_ms()
            try:
                if message is not None:
                    await self._ws.send_json(message)
                    self.sent += 1
                    self._sent_seq_high_water = max(self._sent_seq_high_water, message["seq"])
                    last_heartbeat = now
                elif now - last_heartbeat >= self.heartbeat_ms:
                    await self._ws.send_json(e2.heartbeat(seq=self._next_seq()))
                    self.heartbeats_sent += 1
                    last_heartbeat = now
            except Exception:
                self.send_errors += 1
                # The state we failed to send is obsolete by definition: whatever
                # comes next is fresher. Reconnect and carry on, never replay.
                await self._disconnect()
                if reader is not None:
                    reader.cancel()
                    reader = None
        if reader is not None:
            reader.cancel()


class SubtitleSinkRegistry:
    _registry: dict[str, Callable[..., SubtitleSink]] = {}

    @classmethod
    def register(cls, type_name: str, factory: Callable[..., SubtitleSink]) -> None:
        cls._registry[type_name] = factory

    @classmethod
    def available_types(cls) -> list[str]:
        return sorted(cls._registry)

    @classmethod
    def create(cls, type_name: str, options: dict[str, Any], *, root: Path, **kwargs: Any) -> SubtitleSink:
        del root
        if type_name not in cls._registry:
            available = ", ".join(cls.available_types()) or "(none)"
            raise BackendConfigError(f"unknown subtitle sink type '{type_name}'; available: {available}")
        if not isinstance(options, dict):
            raise BackendConfigError(f"subtitle sink '{type_name}' options must be an object")
        return cls._registry[type_name](options, **kwargs)


def _create_null(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    del kwargs
    if options:
        raise BackendConfigError("subtitle sink 'null' has no options")
    return NullSubtitleSink()


def _create_console(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    del kwargs
    unknown = sorted(set(options) - {"prefix"})
    if unknown:
        raise BackendConfigError(f"subtitle sink 'console' unknown option(s): {', '.join(unknown)}")
    return ConsoleSubtitleSink()


def _create_octagon_udp(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    del kwargs
    unknown = sorted(set(options) - {"host", "port", "format", "max_datagram_bytes"})
    if unknown:
        raise BackendConfigError(f"subtitle sink 'octagon_udp' unknown option(s): {', '.join(unknown)}")
    return OctagonUdpSubtitleSink(
        host=str(options.get("host", "127.0.0.1")),
        port=int(options.get("port", 18081)),
        message_format=str(options.get("format", "json")),
        max_datagram_bytes=int(options.get("max_datagram_bytes", 4096)),
    )


SubtitleSinkRegistry.register("null", _create_null)
SubtitleSinkRegistry.register("console", _create_console)
#: The class the Phase 7 contract names. Kept so the SF8008 side keeps the name
#: it was specified with, while the shared implementation has a neutral one.
Enigma2SubtitleSink = SubtitleProtocolSink


def _create_subtitle_protocol(options: dict[str, Any], sink_type: str) -> SubtitleSink:
    known = {
        "host", "port", "path", "scheme", "source_id", "heartbeat_ms",
        "reconnect_initial_ms", "reconnect_max_ms", "connect_timeout_sec",
        "control_queue_limit",
    }
    unknown = sorted(set(options) - known)
    if unknown:
        raise BackendConfigError(
            f"subtitle sink '{sink_type}' unknown option(s): {', '.join(unknown)}")
    host = str(options.get("host", "")).strip()
    if not host:
        raise BackendConfigError(f"subtitle sink '{sink_type}' requires a non-empty host")
    return SubtitleProtocolSink(
        sink_type=sink_type,
        host=host,
        port=int(options.get("port", 8790)),
        path=str(options.get("path", "/hearable")),
        scheme=str(options.get("scheme", "ws")),
        source_id=str(options.get("source_id", "unknown")),
        heartbeat_ms=int(options.get("heartbeat_ms", 2000)),
        reconnect_initial_ms=int(options.get("reconnect_initial_ms", 250)),
        reconnect_max_ms=int(options.get("reconnect_max_ms", 5000)),
        connect_timeout_sec=float(options.get("connect_timeout_sec", 5.0)),
        control_queue_limit=int(options.get("control_queue_limit", 32)),
    )


def _create_enigma2(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    del kwargs
    return _create_subtitle_protocol(options, "enigma2")


def _create_subtitle_ws(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    del kwargs
    return _create_subtitle_protocol(options, "subtitle_ws")


SubtitleSinkRegistry.register("octagon_udp", _create_octagon_udp)
SubtitleSinkRegistry.register("enigma2", _create_enigma2)
SubtitleSinkRegistry.register("subtitle_ws", _create_subtitle_ws)
