#!/usr/bin/env python3
"""Read one PID off the demux of the service Enigma2 is already showing.

Runs on the box. The hypothesis it exists to test is that a PID filter on an
already-tuned service does not hold the tuner the way a streamserver client
does — which is a hypothesis, not a fact, and the gate that needs it says so.

Linux DVB, userspace only: open the demux, set a filter for one PID, read what
comes out. Nothing is tuned, nothing is decoded, no second service is requested.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import select
import sys
import time

# Linux DVB demux, from linux/dvb/dmx.h.
DMX_START = 0x6F29
DMX_STOP = 0x6F2A
DMX_SET_PES_FILTER = 0x40146F2C          # _IOW('o', 44, struct dmx_pes_filter_params)
DMX_SET_BUFFER_SIZE = 0x6F2D             # _IO('o', 45)

DMX_IN_FRONTEND = 0
DMX_OUT_TAP = 1
DMX_OUT_TS_TAP = 2
DMX_OUT_TSDEMUX_TAP = 3
DMX_PES_OTHER = 20
DMX_IMMEDIATE_START = 4

OUTPUTS = {"pes": DMX_OUT_TAP, "ts": DMX_OUT_TS_TAP, "tsdemux": DMX_OUT_TSDEMUX_TAP}


class PesFilterParams(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint16),
                ("input", ctypes.c_int),
                ("output", ctypes.c_int),
                ("pes_type", ctypes.c_int),
                ("flags", ctypes.c_uint32)]


def open_tap(device: str, pid: int, output: int, buffer_bytes: int) -> int:
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, DMX_SET_BUFFER_SIZE, buffer_bytes)
    except OSError:
        pass                       # not every implementation allows it; not fatal
    params = PesFilterParams(pid=pid, input=DMX_IN_FRONTEND, output=output,
                             pes_type=DMX_PES_OTHER, flags=DMX_IMMEDIATE_START)
    fcntl.ioctl(fd, DMX_SET_PES_FILTER, params)
    return fd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=lambda v: int(v, 0), required=True)
    parser.add_argument("--device", default="/dev/dvb/adapter0/demux0")
    parser.add_argument("--output", default="ts", choices=sorted(OUTPUTS))
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--buffer", type=int, default=1 << 20)
    parser.add_argument("--write", default="", help="scrive i byte letti su questo file")
    parser.add_argument("--stdout", action="store_true", help="scrive i byte su stdout")
    parser.add_argument("--to", default="", help="host:porta a cui inviare i pacchetti")
    args = parser.parse_args()

    report = {"device": args.device, "pid": args.pid, "output": args.output,
              "seconds_asked": args.seconds}
    try:
        fd = open_tap(args.device, args.pid, OUTPUTS[args.output], args.buffer)
    except OSError as error:
        report.update({"opened": False, "error": f"{errno.errorcode.get(error.errno, error.errno)}: {error}"})
        print(json.dumps(report, ensure_ascii=False))
        return 1

    sink = open(args.write, "wb") if args.write else None
    # Straight to the receiver, with no ffmpeg in between. There is nothing to
    # remux: what comes off the filter is already transport packets carrying the
    # broadcaster's own timestamps, and the receiver's decoder probes a
    # single-PID stream without a programme table perfectly well — measured.
    stream = None
    if args.to:
        import socket
        host, _, port = args.to.rpartition(":")
        try:
            stream = socket.create_connection((host, int(port)), timeout=10)
            stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as error:
            report.update({"opened": False, "error": f"connessione a {args.to} fallita: {error}"})
            print(json.dumps(report, ensure_ascii=False))
            os.close(fd)
            return 1
    total, first_at, reads, empty = 0, None, 0, 0
    started = time.time()
    try:
        while time.time() - started < args.seconds:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                empty += 1
                continue
            try:
                block = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as error:
                report["read_error"] = f"{errno.errorcode.get(error.errno, error.errno)}: {error}"
                break
            if not block:
                continue
            if first_at is None:
                first_at = time.time()
            total += len(block)
            reads += 1
            if sink:
                sink.write(block)
            if args.stdout:
                sys.stdout.buffer.write(block)
                sys.stdout.buffer.flush()
            if stream is not None:
                try:
                    stream.sendall(block)
                except OSError as error:
                    report["send_error"] = str(error)
                    break
    finally:
        try:
            fcntl.ioctl(fd, DMX_STOP)
        except OSError:
            pass
        os.close(fd)
        if sink:
            sink.close()
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    span = time.time() - started
    report.update({
        "opened": True,
        "bytes": total,
        "reads": reads,
        "seconds": round(span, 2),
        "kbit_s": round(total * 8 / span / 1000, 1) if span > 0 else 0,
        "first_data_after_s": round(first_at - started, 2) if first_at else None,
        "select_timeouts": empty,
        "delivers_data": total > 10_000,
    })
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["delivers_data"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
