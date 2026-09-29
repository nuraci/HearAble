#!/usr/bin/env python3
"""Stand-in for an Enigma2 receiver: OpenWebif API plus a live service stream.

Lets Enigma2AudioSource be exercised end to end with no decoder on the bench: it
answers the same OpenWebif endpoints the real SF8008 does, and streams a
synthetic MPEG-TS carrying video plus two audio tracks, so track selection,
demuxing, resampling and channel-change handling are all really executed.

The media is generated locally by ffmpeg. Nothing broadcast is ever recorded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SERVICES = {
    "1:0:19:2B66:3F3:1:C00000:0:0:0:": "Mock Uno HD",
    "1:0:19:2B67:3F3:1:C00000:0:0:0:": "Mock Due HD",
}


def build_ts_fixture(path: Path, *, seconds: int, speech_wav: Path | None) -> Path:
    """Build a small MPEG-TS with video plus Italian and English audio tracks.

    Video is a colour pattern nobody will decode; its only job is to prove the
    audio source really discards it. The Italian track carries real speech when a
    WAV is supplied, so the fixture can drive the ASR too.
    """
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}"]
    if speech_wav and speech_wav.exists():
        cmd += ["-stream_loop", "-1", "-i", str(speech_wav)]
    else:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}"]
    cmd += [
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-t", str(seconds),
        "-c:v", "mpeg2video", "-b:v", "2500k",
        "-c:a", "mp2", "-b:a", "128k", "-ac", "2", "-ar", "48000",
        "-metadata:s:a:0", "language=ita", "-metadata:s:a:0", "title=Italiano",
        "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English",
        "-f", "mpegts", str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


class MockEnigma2:
    def __init__(self, *, fixture: Path, service_ref: str, on_stream_open=None) -> None:
        self.fixture = fixture
        self.service_ref = service_ref
        self.stream_requests = 0
        self.zaps = 0
        # Fired when a client starts pulling the live service. That instant, not
        # the moment a subtitle socket connects, is when the consumer's audio
        # clock starts running.
        self.on_stream_open = on_stream_open

    def zap(self, service_ref: str) -> None:
        if service_ref != self.service_ref:
            self.service_ref = service_ref
            self.zaps += 1


async def about(request: web.Request) -> web.Response:
    return web.json_response({
        "info": {
            "brand": "Octagon", "model": "SF8008", "chipset": "bcm7252s",
            "imagever": "mock", "enigmaver": "mock-1.0", "webifver": "mock-owif-2.0",
        }
    })


async def getcurrent(request: web.Request) -> web.Response:
    state: MockEnigma2 = request.app["state"]
    return web.json_response({
        "info": {
            "result": True,
            "name": SERVICES.get(state.service_ref, "Unknown"),
            "sref": state.service_ref,
        },
        "now": {"title": "Mock programme", "sname": SERVICES.get(state.service_ref, "Unknown")},
    })


async def subservices(request: web.Request) -> web.Response:
    state: MockEnigma2 = request.app["state"]
    return web.json_response({
        "services": [{"servicereference": state.service_ref,
                      "servicename": SERVICES.get(state.service_ref, "Unknown")}]
    })


async def zap(request: web.Request) -> web.Response:
    state: MockEnigma2 = request.app["state"]
    ref = request.query.get("sRef", "")
    if ref:
        state.zap(ref)
    return web.json_response({"result": True})


async def stream(request: web.Request) -> web.StreamResponse:
    """Serve the fixture on a loop, paced at roughly realtime like a live service."""
    state: MockEnigma2 = request.app["state"]
    state.stream_requests += 1
    if state.on_stream_open is not None:
        state.on_stream_open(state.stream_requests)
    response = web.StreamResponse(status=200, headers={"Content-Type": "video/mpeg"})
    await response.prepare(request)
    data = state.fixture.read_bytes()
    # ~188-byte TS packets; send in small bursts so the reader sees a live trickle.
    chunk = 188 * 100
    bitrate_bytes_per_sec = max(1, len(data) // max(1, request.app["fixture_seconds"]))
    try:
        while True:
            for offset in range(0, len(data), chunk):
                await response.write(data[offset:offset + chunk])
                await asyncio.sleep(chunk / bitrate_bytes_per_sec)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def counters(request: web.Request) -> web.Response:
    state: MockEnigma2 = request.app["state"]
    return web.json_response({
        "service_ref": state.service_ref,
        "stream_requests": state.stream_requests,
        "zaps": state.zaps,
    })


def build_app(fixture: Path, fixture_seconds: int, service_ref: str,
              on_stream_open=None) -> web.Application:
    app = web.Application()
    app["state"] = MockEnigma2(fixture=fixture, service_ref=service_ref,
                               on_stream_open=on_stream_open)
    app["fixture_seconds"] = fixture_seconds
    app.router.add_get("/api/about", about)
    app.router.add_get("/api/getcurrent", getcurrent)
    app.router.add_get("/api/subservices", subservices)
    app.router.add_get("/api/zap", zap)
    app.router.add_get("/counters", counters)
    app.router.add_get("/{sref}", stream)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801, help="OpenWebif port")
    parser.add_argument("--stream-port", type=int, default=8802)
    parser.add_argument("--fixture", default=str(ROOT / "benchmarks/network/fixtures/mock_service.ts"))
    parser.add_argument("--fixture-seconds", type=int, default=30)
    parser.add_argument("--speech-wav", default=str(ROOT / "benchmarks/audio/italian_latency_001_smoke30s.wav"))
    parser.add_argument("--duration", type=float, default=0.0)
    args = parser.parse_args()

    fixture = build_ts_fixture(
        Path(args.fixture), seconds=args.fixture_seconds, speech_wav=Path(args.speech_wav)
    )
    service_ref = next(iter(SERVICES))
    api_app = build_app(fixture, args.fixture_seconds, service_ref)
    stream_app = build_app(fixture, args.fixture_seconds, service_ref)
    stream_app["state"] = api_app["state"]

    async def run() -> None:
        runners = []
        for app, port in ((api_app, args.port), (stream_app, args.stream_port)):
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, args.host, port).start()
            runners.append(runner)
        print(json.dumps({
            "openwebif": f"http://{args.host}:{args.port}",
            "stream": f"http://{args.host}:{args.stream_port}",
            "service_ref": service_ref,
            "fixture": str(fixture),
        }), file=sys.stderr, flush=True)
        try:
            if args.duration:
                await asyncio.sleep(args.duration)
            else:
                await asyncio.Event().wait()
        finally:
            for runner in runners:
                await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
