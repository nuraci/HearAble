"""A WebSocket server small enough to run inside enigma2.

The SF8008 has no WebSocket library — no autobahn, no websockets, no wsproto —
and installing one onto a set-top box's flash to receive two lines of text would
be out of proportion. hashlib, base64 and struct are present, which is all the
handshake and the frame format actually need.

Two constraints come from the host rather than from the protocol:

* **Nothing may block.** This runs inside the enigma2 main loop, where a blocked
  socket read is a frozen television. Every socket is non-blocking and `poll()`
  returns immediately, having done only the work that was ready.
* **Nothing may raise into the caller.** A plugin that throws takes the user's
  picture with it, so a failing client is dropped and recorded, never escalated.

Only what the subtitle protocol uses is implemented: text frames, ping, pong and
close. Fragmented frames are refused rather than half-supported.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import select
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

MAX_FRAME_BYTES = 1 << 20
MAX_HANDSHAKE_BYTES = 1 << 14


class _Client:
    def __init__(self, sock: socket.socket, address) -> None:
        self.sock = sock
        self.address = address
        self.inbox = bytearray()
        self.handshaken = False
        self.closed = False

    def fileno(self) -> int:
        return self.sock.fileno()


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, opcode: int = OPCODE_TEXT) -> bytes:
    """Server-to-client frame. Never masked, per RFC 6455."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


class WebSocketServer:
    """Accepts one or more producers and hands their text messages to a callback.

    `poll()` is meant to be called from a timer — every 20 ms on the box. It does
    one non-blocking pass over the listening socket and every client, and
    returns.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8770,
                 path: str = "/hearable", on_message=None, on_event=None,
                 on_http=None) -> None:
        self.host = host
        self.port = port
        self.path = path
        self.on_message = on_message
        self.on_event = on_event or (lambda kind, detail: None)
        # A plain GET on this server answers with JSON instead of being refused,
        # so the control plane needs no second port and no second listener
        # inside enigma2.
        self.on_http = on_http
        self._listener: socket.socket | None = None
        self._clients: list[_Client] = []
        self.connections_accepted = 0
        self.messages_received = 0
        self.protocol_errors = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setblocking(False)
        listener.bind((self.host, self.port))
        listener.listen(4)
        self._listener = listener
        self.on_event("listening", f"{self.host}:{self.port}{self.path}")

    def stop(self) -> None:
        for client in list(self._clients):
            self._drop(client, "server stopping")
        if self._listener is not None:
            try:
                self._listener.close()
            finally:
                self._listener = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- the pass a timer drives -------------------------------------------
    def poll(self) -> None:
        if self._listener is None:
            return
        try:
            readable, _, _ = select.select([self._listener] + self._clients, [], [], 0)
        except (OSError, ValueError):
            # A client died between building the list and selecting on it.
            self._clients = [c for c in self._clients if not c.closed]
            return
        for ready in readable:
            if ready is self._listener:
                self._accept()
            else:
                self._read(ready)

    def broadcast(self, payload: bytes) -> None:
        for client in list(self._clients):
            if not client.handshaken:
                continue
            try:
                client.sock.sendall(encode_frame(payload))
            except OSError as error:
                self._drop(client, f"send failed: {error}")

    # -- internals ---------------------------------------------------------
    def _accept(self) -> None:
        try:
            sock, address = self._listener.accept()
        except OSError:
            return
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._clients.append(_Client(sock, address))
        self.connections_accepted += 1
        self.on_event("connected", str(address))

    def _drop(self, client: _Client, why: str) -> None:
        client.closed = True
        try:
            client.sock.close()
        except OSError:
            pass
        if client in self._clients:
            self._clients.remove(client)
        self.on_event("disconnected", f"{client.address}: {why}")

    def _read(self, client: _Client) -> None:
        try:
            chunk = client.sock.recv(65536)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self._drop(client, f"recv failed: {error}")
            return
        if not chunk:
            self._drop(client, "peer closed")
            return
        client.inbox += chunk
        if not client.handshaken:
            self._try_handshake(client)
        if client.handshaken and not client.closed:
            self._consume_frames(client)

    def _try_handshake(self, client: _Client) -> None:
        if b"\r\n\r\n" not in client.inbox:
            if len(client.inbox) > MAX_HANDSHAKE_BYTES:
                self._drop(client, "handshake too large")
            return
        head, _, rest = bytes(client.inbox).partition(b"\r\n\r\n")
        client.inbox = bytearray(rest)
        lines = head.decode("latin-1").split("\r\n")
        request = lines[0].split(" ") if lines else []
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()

        key = headers.get("sec-websocket-key", "")
        upgrade = headers.get("upgrade", "").lower()
        raw_target = request[1] if len(request) > 1 else ""
        target, _, query = raw_target.partition("?")

        if len(request) >= 2 and request[0] == "GET" and upgrade != "websocket":
            body = None
            if self.on_http is not None:
                try:
                    body = self.on_http(target, query)
                except Exception as error:
                    self.on_event("http_handler_error", f"{type(error).__name__}: {error}")
            if body is None:
                self._refuse(client, "404 Not Found", f"no handler for {target}")
                return
            payload = body.encode("utf-8")
            self._answer(client, "200 OK", payload, "application/json")
            return

        if len(request) < 2 or request[0] != "GET" or upgrade != "websocket" or not key:
            self._refuse(client, "400 Bad Request", "not a WebSocket upgrade")
            return
        if request[1].split("?")[0] not in (self.path, "/"):
            self._refuse(client, "404 Not Found", f"unknown path {request[1]}")
            return

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n\r\n"
        )
        try:
            client.sock.sendall(response.encode("ascii"))
        except OSError as error:
            self._drop(client, f"handshake reply failed: {error}")
            return
        client.handshaken = True
        self.on_event("handshaken", str(client.address))

    def _answer(self, client: _Client, status: str, payload: bytes, content_type: str) -> None:
        head = (f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n").encode("ascii")
        try:
            client.sock.sendall(head + payload)
        except OSError:
            pass
        self._drop(client, "served an HTTP request")

    def _refuse(self, client: _Client, status: str, why: str) -> None:
        try:
            client.sock.sendall(f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))
        except OSError:
            pass
        self.protocol_errors += 1
        self._drop(client, why)

    def _consume_frames(self, client: _Client) -> None:
        while not client.closed:
            frame = self._take_frame(client)
            if frame is None:
                return
            opcode, payload = frame
            if opcode == OPCODE_TEXT:
                self.messages_received += 1
                if self.on_message is not None:
                    try:
                        self.on_message(payload.decode("utf-8", "replace"), client)
                    except Exception as error:  # a bad message is not fatal
                        self.protocol_errors += 1
                        self.on_event("handler_error", f"{type(error).__name__}: {error}")
            elif opcode == OPCODE_PING:
                try:
                    client.sock.sendall(encode_frame(payload, OPCODE_PONG))
                except OSError as error:
                    self._drop(client, f"pong failed: {error}")
            elif opcode == OPCODE_CLOSE:
                self._drop(client, "client closed")
            elif opcode in (OPCODE_PONG, OPCODE_BINARY):
                pass
            else:
                self.protocol_errors += 1
                self._drop(client, f"unsupported opcode {opcode}")

    def _take_frame(self, client: _Client):
        buffer = client.inbox
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        cursor = 2

        if length == 126:
            if len(buffer) < cursor + 2:
                return None
            length = struct.unpack("!H", buffer[cursor:cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if len(buffer) < cursor + 8:
                return None
            length = struct.unpack("!Q", buffer[cursor:cursor + 8])[0]
            cursor += 8

        if length > MAX_FRAME_BYTES:
            self.protocol_errors += 1
            self._drop(client, f"frame of {length} bytes exceeds the limit")
            return None
        if not final or opcode == OPCODE_CONTINUATION:
            self.protocol_errors += 1
            self._drop(client, "fragmented frames are not supported")
            return None

        mask = b""
        if masked:
            if len(buffer) < cursor + 4:
                return None
            mask = bytes(buffer[cursor:cursor + 4])
            cursor += 4
        if len(buffer) < cursor + length:
            return None

        payload = bytes(buffer[cursor:cursor + length])
        del buffer[:cursor + length]
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload
