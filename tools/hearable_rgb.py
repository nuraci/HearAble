#!/usr/bin/env python3
"""The ring on the T9's case, telling the room what the machine is doing.

The controller is a CH340 bridge to a small MCU that accepts five-byte frames.
It has no notion of colour: it offers five canned modes plus brightness and
speed, so the vocabulary has to be built from those rather than from a palette.

    0xFA  mode  brightness  speed  checksum        checksum = sum & 0xFF
    modes: rainbow 0x01, breathing 0x02, cycle 0x03, off 0x04, auto 0x05
    brightness and speed run 1..5 and are inverted: 0x01 is brightest/fastest

Protocol from two independent implementations, kept as references:
  https://github.com/cwt/LED               (frame, checksum, mode codes)
  https://github.com/thekief/s1t-ledcontrol (the baud rate question)
The official driver uses 10000 baud, which the CH340N does not officially
support; that tool defaults to 9600 and works, so this tries 10000 first and
falls back.

This process only ever *reads* HearAble's state. It cannot stop a subtitle, and
if the ring disappears it says so once and keeps running: a missing decoration
must never take the captions with it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

START = 0xFA
MODES = {"rainbow": 0x01, "breathing": 0x02, "cycle": 0x03, "off": 0x04, "auto": 0x05}

# What each state looks like.
#
# These are not chosen: they are measured. The first mapping was built on what
# the patterns seemed to mean to their author, and a blind test with a viewer
# scored **zero out of five** — three of the six were read as the exact opposite
# of their intent. What follows is that viewer's own reading, which was
# perfectly consistent; the table records what each pattern actually says to
# someone who has not been told.
LOOKS = {
    # Read as "sta partendo": bright rainbow.
    "BOOTING":        ("rainbow", 1, 2),
    # Read as "accesa e in attesa": colours marching round, unhurried.
    "READY":          ("cycle", 2, 3),
    # Read as "sta lavorando": the controller's own `auto` pattern, and the only
    # thing in its vocabulary that says work rather than waiting.
    "STREAMING":      ("auto", 1, 3),
    # Read as "si sta riagganciando": a hard blink, driven from here.
    "RECONNECTING":   ("__blink__", 1, 1),
    # Read as "c'è un problema": fast bright breathing.
    "ERROR":          ("breathing", 1, 1),
    # The one state with no blind reading of its own: the waiting pattern with
    # the brightness walked down as the countdown runs, so it visibly fades
    # towards the dark rather than differing from waiting by a constant.
    "IDLE_COUNTDOWN": ("__fade__", 2, 3),
    "SHUTDOWN":       ("off", 5, 5),
}


def frame(mode: str, brightness: int, speed: int) -> bytes:
    code = MODES[mode]
    body = [START, code, brightness, speed]
    return bytes(body + [sum(body) & 0xFF])


class Ring:
    """The controller, or nothing at all if it is not there."""

    def __init__(self, device: str, bauds=(10000, 9600), byte_delay: float = 0.005):
        self.device, self.bauds, self.byte_delay = device, bauds, byte_delay
        self.baud = None
        self.port = None
        self.missing_logged = False

    def open(self) -> bool:
        try:
            import serial                                   # pyserial
        except ImportError:
            if not self.missing_logged:
                print("[rgb] pyserial non installato: il ring resta spento, "
                      "i sottotitoli no", flush=True)
                self.missing_logged = True
            return False
        if self.port is not None:
            return True
        for baud in self.bauds:
            try:
                self.port = serial.Serial(self.device, baud, timeout=1)
                self.baud = baud
                print(f"[rgb] aperto {self.device} a {baud} baud", flush=True)
                self.missing_logged = False
                return True
            except Exception:
                self.port = None
        if not self.missing_logged:
            print(f"[rgb] {self.device} non apribile: continuo senza ring", flush=True)
            self.missing_logged = True
        return False

    def send(self, mode: str, brightness: int, speed: int) -> bool:
        if not self.open():
            return False
        try:
            for byte in frame(mode, brightness, speed):
                self.port.write(bytes([byte]))
                self.port.flush()
                time.sleep(self.byte_delay)
            return True
        except Exception as error:
            print(f"[rgb] scrittura fallita ({type(error).__name__}): "
                  f"riprovo alla prossima transizione", flush=True)
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None
            return False


def fade_brightness(watchdog_file: str) -> int:
    """Brightness 2..5 as the countdown runs out, so the ring visibly fades."""
    try:
        w = json.loads(Path(watchdog_file).read_text())
        remaining = w.get("seconds_to_poweroff")
        total = (w.get("timeout_minutes") or 20) * 60
    except Exception:
        return 3
    if not remaining or not total:
        return 3
    fraction = max(0.0, min(1.0, remaining / total))
    return int(round(5 - 3 * fraction))          # full countdown 2, nearly out 5


def read_state(watchdog_file: str) -> str:
    """What HearAble is doing, from files it already writes."""
    try:
        w = json.loads(Path(watchdog_file).read_text())
    except Exception:
        w = {}
    active = subprocess.run(["systemctl", "is-active", "hearable-t9.service"],
                            capture_output=True, text=True).stdout.strip()
    if active == "failed":
        return "ERROR"
    if active in ("activating", "deactivating"):
        return "RECONNECTING"
    if active != "active":
        return "ERROR"
    if w.get("sessions", 0) > 0:
        return "STREAMING"
    if w.get("idle"):
        remaining = w.get("seconds_to_poweroff")
        if remaining is not None and remaining < 120:
            return "SHUTDOWN"
        return "IDLE_COUNTDOWN"
    return "READY"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyUSB0")
    parser.add_argument("--watchdog-file", default="/run/hearable-idle.json")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--demo", default="", help="mostra uno stato e esce, per il test visivo")
    parser.add_argument("--seconds", type=float, default=0.0, help="con --demo: quanto tenerlo")
    args = parser.parse_args()

    ring = Ring(args.device)

    if args.demo:
        state = args.demo.upper()
        if state not in LOOKS:
            print(f"stati: {', '.join(LOOKS)}")
            return 2
        mode, brightness, speed = LOOKS[state]
        end = time.monotonic() + (args.seconds or 8.0)
        print(f"[rgb] {state}: {mode} luminosità {brightness} velocità {speed}", flush=True)
        while time.monotonic() < end:
            if mode == "__fade__":
                # In a demo there is no countdown to follow: walk it down so the
                # fade can be seen in a minute instead of twenty.
                span = max(1.0, (args.seconds or 8.0))
                left = (end - time.monotonic()) / span
                ring.send("cycle", int(round(5 - 3 * max(0.0, min(1.0, left)))), speed)
                time.sleep(2.0)
            elif mode == "__blink__":
                ring.send("rainbow", brightness, speed)
                time.sleep(0.35)
                ring.send("off", brightness, speed)
                time.sleep(0.35)
            else:
                ring.send(mode, brightness, speed)
                time.sleep(1.0)
        return 0

    # Boot pattern first, before anything is known.
    ring.send(*LOOKS["BOOTING"])
    current = "BOOTING"
    blink_on = False
    last_sent = 0.0
    while True:
        state = read_state(args.watchdog_file)
        mode, brightness, speed = LOOKS[state]
        if mode == "__blink__":
            blink_on = not blink_on
            ring.send("rainbow" if blink_on else "off", brightness, speed)
            current = state
            time.sleep(0.4)
            continue
        if mode == "__fade__":
            brightness = fade_brightness(args.watchdog_file)
            ring.send("cycle", brightness, speed)
            if state != current:
                print(f"[rgb] {current} -> {state}", flush=True)
            current = state
            time.sleep(max(2.0, args.interval))
            continue
        now = time.monotonic()
        # Resend periodically as well as on change: the controller forgets if it
        # is power-cycled with the machine still up.
        if state != current or now - last_sent > 30:
            if state != current:
                print(f"[rgb] {current} -> {state}", flush=True)
            ring.send(mode, brightness, speed)
            current, last_sent = state, now
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
