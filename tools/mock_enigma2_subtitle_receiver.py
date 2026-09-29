#!/usr/bin/env python3
"""Stand-in for the SF8008 HearAble renderer plugin.

Exists so the PC side can be developed and proven without the decoder. It speaks
the same version 1 contract the real plugin will: handshake, render ACKs, epoch
rejection. It can also be told to render slowly or to drop the connection, which
is how the producer's coalescing and reconnect behaviour get tested.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hearable import enigma2_protocol as e2


class MockRenderer:
    def __init__(self, *, render_fps: float, ack_delay_ms: float, quiet: bool) -> None:
        self.render_interval = 1.0 / render_fps if render_fps > 0 else 0.0
        self.ack_delay_ms = ack_delay_ms
        self.quiet = quiet
        self.guard = e2.EpochGuard()
        self.received = 0
        self.rendered = 0
        self.acked = 0
        self.clears = 0
        self.source_changes = 0
        self.heartbeats = 0
        self.protocol_errors = 0
        self.connections = 0
        self.disconnects = 0
        self.upper_line = ""
        self.lower_line = ""
        self.last_render = 0.0
        self.rendered_seqs: list[int] = []
        # Newest state that arrived inside the current frame interval. A real OSD
        # repaints it on the next tick; dropping it would leave stale text on
        # screen forever once the producer goes quiet.
        self.pending_state: dict | None = None
        self.skipped = 0

    def counters(self) -> dict:
        return {
            "connections": self.connections,
            "disconnects": self.disconnects,
            "received": self.received,
            "rendered": self.rendered,
            "acked": self.acked,
            "clears": self.clears,
            "source_changes": self.source_changes,
            "heartbeats": self.heartbeats,
            "protocol_errors": self.protocol_errors,
            "skipped_superseded": self.skipped,
            "latest_rendered_seq": self.rendered_seqs[-1] if self.rendered_seqs else -1,
            "display": {"upper_line": self.upper_line, "lower_line": self.lower_line},
            **{f"epoch_{k}": v for k, v in self.guard.counters().items()},
        }

    def show(self) -> None:
        if self.quiet:
            return
        print(f"\r\033[K{self.upper_line}\n\033[K{self.lower_line}\033[1A", end="", flush=True)


async def handler(request: web.Request) -> web.WebSocketResponse:
    renderer: MockRenderer = request.app["renderer"]
    ws = web.WebSocketResponse(heartbeat=None)
    await ws.prepare(request)
    renderer.connections += 1
    await ws.send_json(e2.hello(role=e2.ROLE_RENDERER, session_id="mock"))
    await ws.send_json(
        e2.capabilities(role=e2.ROLE_RENDERER, supports_render_ack=True, extra={"renderer": "mock"})
    )

    repaint = asyncio.create_task(repaint_loop(ws, renderer))
    async for msg in ws:
        if msg.type is not WSMsgType.TEXT:
            continue
        try:
            payload = e2.validate(json.loads(msg.data))
        except (ValueError, e2.ProtocolError):
            renderer.protocol_errors += 1
            continue

        kind = payload["type"]
        if kind == "heartbeat":
            renderer.heartbeats += 1
            continue
        if kind in {"hello", "capabilities"}:
            continue

        renderer.received += 1
        if not renderer.guard.accept(payload):
            continue

        if kind == "source_changed":
            renderer.source_changes += 1
            renderer.upper_line = renderer.lower_line = ""
        elif kind == "clear_subtitles":
            renderer.clears += 1
            renderer.upper_line = renderer.lower_line = ""
        elif kind == "subtitle_state":
            # A real OSD repaints at a fixed rate. States arriving inside the
            # frame interval are held, not discarded, and the newest one is
            # painted on the next tick.
            now = time.monotonic()
            if renderer.render_interval and now - renderer.last_render < renderer.render_interval:
                if renderer.pending_state is not None:
                    renderer.skipped += 1
                renderer.pending_state = payload
                continue
            await render_and_ack(ws, renderer, payload)
            continue

        await render_and_ack(ws, renderer, payload)

    repaint.cancel()
    renderer.disconnects += 1
    return ws


async def render_and_ack(ws, renderer: "MockRenderer", payload: dict) -> None:
    if payload["type"] == "subtitle_state":
        renderer.last_render = time.monotonic()
        renderer.upper_line = payload["upper_line"]
        renderer.lower_line = payload["lower_line"]
        renderer.show()
    renderer.rendered += 1
    renderer.rendered_seqs.append(payload["seq"])
    if renderer.ack_delay_ms:
        await asyncio.sleep(renderer.ack_delay_ms / 1000.0)
    await ws.send_json(e2.render_ack(seq=payload["seq"], source_epoch=payload["source_epoch"]))
    renderer.acked += 1


async def repaint_loop(ws, renderer: "MockRenderer") -> None:
    """Paint the newest held state once its frame interval has elapsed."""
    if not renderer.render_interval:
        return
    try:
        while True:
            await asyncio.sleep(renderer.render_interval / 2.0)
            state = renderer.pending_state
            if state is None:
                continue
            if time.monotonic() - renderer.last_render < renderer.render_interval:
                continue
            renderer.pending_state = None
            await render_and_ack(ws, renderer, state)
    except (asyncio.CancelledError, ConnectionResetError):
        pass


async def counters_handler(request: web.Request) -> web.Response:
    return web.json_response(request.app["renderer"].counters())


def build_app(*, render_fps: float, ack_delay_ms: float, quiet: bool) -> web.Application:
    app = web.Application()
    app["renderer"] = MockRenderer(render_fps=render_fps, ack_delay_ms=ack_delay_ms, quiet=quiet)
    app.router.add_get("/hearable", handler)
    app.router.add_get("/counters", counters_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--render-fps", type=float, default=25.0,
                        help="repaint rate; 0 renders every state")
    parser.add_argument("--ack-delay-ms", type=float, default=0.0,
                        help="artificial delay before acknowledging a render")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="exit after N seconds; 0 runs until interrupted")
    parser.add_argument("--counters-output", default="")
    parser.add_argument("--quiet", action="store_true", help="do not paint the two lines")
    args = parser.parse_args()

    app = build_app(render_fps=args.render_fps, ack_delay_ms=args.ack_delay_ms, quiet=args.quiet)

    async def run() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()
        print(f"mock enigma2 renderer on ws://{args.host}:{args.port}/hearable", file=sys.stderr)
        try:
            if args.duration:
                await asyncio.sleep(args.duration)
            else:
                await asyncio.Event().wait()
        finally:
            await runner.cleanup()
            counters = app["renderer"].counters()
            if args.counters_output:
                Path(args.counters_output).write_text(
                    json.dumps(counters, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            print(json.dumps(counters, ensure_ascii=False))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
