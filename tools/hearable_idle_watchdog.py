#!/usr/bin/env python3
"""Power the T9 down when the decoder stops sending audio — and only then.

The causal signal is the audio *session*, not the speech. A channel can be
silent, or carry twenty minutes of music, and the viewer is still watching with
HearAble on; deciding from the recogniser's output or from the PCM level would
switch the machine off in the middle of a film. So this watches the one thing
that means "the decoder has let go": the TCP connection the direct PID tap opens
to the receiver port.

It is deliberately separate from the recogniser and from the RGB ring. If this
process dies, subtitles carry on; if the ring is missing, the countdown still
runs. It reads /proc and writes one state file, nothing else.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import threading
import urllib.request
import time
from pathlib import Path

# The decoder says goodbye on this port when enigma2 stops on purpose. It is a
# hint and not an order: enigma2 says the same thing when it restarts for an
# image update, so hearing it shortens the countdown rather than ending it.
BYE_PORT = 9011
BYE_MESSAGE = b"hearable-decoder-off"


def sessions_on(port: int) -> int:
    """How many established TCP connections are landing on our receiver port.

    /proc/net/tcp rather than ss or lsof: no process to spawn twice a second,
    and no dependency to install on an appliance.
    """
    wanted = f"{port:04X}"
    count = 0
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as handle:
                next(handle, None)
                for line in handle:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local_port = parts[1].rsplit(":", 1)[-1]
                    state = parts[3]
                    if local_port == wanted and state == "01":      # ESTABLISHED
                        count += 1
        except OSError:
            continue
    return count


def decoder_state(host: str, timeout: float = 2.0) -> str:
    """"watching", "standby" or "gone" — what the decoder is doing.

    Three states because the viewer has three ways of stopping, and they do not
    look alike from here:

      * the remote's power button puts the box in **standby**. It stays on the
        network and answers a ping perfectly happily, which is why reachability
        alone is not the question. It does say so: OpenWebif reports
        `instandby`, and that is the honest signal.
      * deep standby, the plug pulled, or the cable out leave it **gone**, and
        no message could ever have been sent from it.
      * enigma2 restarting for an image update is still **watching** as far as
        this is concerned: the machine answers within seconds and the long wait
        is the right one.

    The plugin's own session-stop hook would have been tidier, but on
    openATV 7.5.1 it is never called — measured with `init 4` and with
    OpenWebif's clean restart, neither of which reaches it.
    """
    try:
        with urllib.request.urlopen(f"http://{host}/api/powerstate",
                                    timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return "standby" if payload.get("instandby") else "watching"
    except Exception:
        pass
    # No answer from the web interface: is the machine there at all?
    reachable = subprocess.run(["ping", "-c1", "-W", "2", host],
                               capture_output=True).returncode == 0
    return "watching" if reachable else "gone"


class Farewell:
    """Listens for the decoder's goodbye, and remembers the last one heard."""

    def __init__(self, port: int = BYE_PORT):
        self.port = port
        self.heard_at: float | None = None
        self.count = 0
        self._thread = threading.Thread(target=self._listen, daemon=True)

    def start(self) -> "Farewell":
        self._thread.start()
        return self

    def _listen(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", self.port))
        except OSError as error:
            print(f"[watchdog] non posso ascoltare su {self.port}: {error}; "
                  "resta solo il timeout lungo", flush=True)
            return
        while True:
            try:
                data, _ = sock.recvfrom(256)
            except OSError:
                return
            if data.strip() == BYE_MESSAGE:
                self.heard_at = time.monotonic()
                self.count += 1
                print("[watchdog] il decoder ha salutato: passo alla grazia breve",
                      flush=True)

    def recent(self, since: float | None, within: float = 120.0) -> bool:
        """Was a goodbye heard around the time the session ended?

        Bounded on purpose: a goodbye from an hour ago says nothing about the
        session that has just stopped.
        """
        if self.heard_at is None or since is None:
            return False
        return abs(self.heard_at - since) <= within


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=20.0,
                        help="quanto aspettare senza sessione audio prima di spegnere")
    parser.add_argument("--port", type=int, default=9010)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--state-file", default="/run/hearable-idle.json")
    parser.add_argument("--grace-minutes", type=float, default=3.0,
                        help="quanto aspettare quando il decoder non c'è più")
    parser.add_argument("--decoder", default="10.77.0.1",
                        help="chi interrogare per sapere se il decoder è ancora acceso")
    parser.add_argument("--decoder-gone-after", type=float, default=60.0,
                        help="per quanto deve restare in standby o assente prima di contarlo")
    parser.add_argument("--dry-run", action="store_true",
                        help="non spegnere: registra soltanto che lo avrebbe fatto")
    args = parser.parse_args()

    timeout = args.minutes * 60
    grace = args.grace_minutes * 60
    farewell = Farewell().start()
    idle_since: float | None = None
    decoder_silent_since: float | None = None
    last_probe = 0.0
    decoder_here = "watching"
    last_seen = 0.0
    state_path = Path(args.state_file)
    print(f"[watchdog] porta {args.port}, timeout {args.minutes} min, "
          f"{'prova a vuoto' if args.dry_run else 'spegnimento reale'}", flush=True)

    while True:
        now = time.monotonic()
        live = sessions_on(args.port)
        if live > 0:
            if idle_since is not None:
                print(f"[watchdog] sessione tornata dopo {now - idle_since:.0f} s: "
                      f"timer annullato", flush=True)
            idle_since = None
            last_seen = now
        elif idle_since is None:
            idle_since = now
            print("[watchdog] nessuna sessione audio: timer avviato", flush=True)

        # Only ask about the decoder while there is nothing to lose by asking:
        # with a session running it is plainly there.
        if idle_since is None:
            decoder_silent_since = None
            decoder_here = "watching"
        elif now - last_probe >= 5.0:
            last_probe = now
            decoder_here = decoder_state(args.decoder)
            if decoder_here == "watching":
                decoder_silent_since = None
            elif decoder_silent_since is None:
                decoder_silent_since = now

        decoder_gone = (decoder_silent_since is not None
                        and now - decoder_silent_since >= args.decoder_gone_after)
        # Either the decoder said it was leaving, or it stopped answering long
        # enough to have left. Both mean the short wait; neither means at once,
        # because a decoder rebooting is a decoder coming back.
        said_goodbye = farewell.recent(idle_since)
        limit = grace if (said_goodbye or decoder_gone) else timeout
        remaining = None if idle_since is None else max(0.0, limit - (now - idle_since))
        try:
            state_path.write_text(json.dumps({
                "sessions": live,
                "idle": idle_since is not None,
                "idle_seconds": None if idle_since is None else round(now - idle_since, 1),
                "seconds_to_poweroff": None if remaining is None else round(remaining, 1),
                "timeout_minutes": args.minutes,
                "grace_minutes": args.grace_minutes,
                "decoder_said_goodbye": said_goodbye,
                "decoder": decoder_here,
                "decoder_away_s": (None if decoder_silent_since is None
                                   else round(now - decoder_silent_since, 1)),
                "decoder_considered_away": decoder_gone,
                "goodbyes_heard": farewell.count,
                "limit_in_force_minutes": round(limit / 60, 2),
                "last_session_monotonic": round(last_seen, 1),
                "dry_run": args.dry_run,
                "updated": round(time.time(), 1),
            }) + "\n")
        except OSError:
            pass

        if idle_since is not None and (now - idle_since) >= limit:
            why = (" dopo il saluto del decoder" if said_goodbye
                   else (f" con il decoder in {decoder_here}" if decoder_gone else ""))
            print(f"[watchdog] {round(limit / 60, 2)} minuti senza sessione audio"
                  f"{why}: spengo", flush=True)
            if args.dry_run:
                print("[watchdog] prova a vuoto: non spengo davvero", flush=True)
                idle_since = now          # start counting again rather than spin
            else:
                # Stop the pipeline first so it can close its files and its
                # sockets, then let systemd take the machine down the usual way.
                subprocess.run(["systemctl", "stop", "hearable-t9.service"],
                               capture_output=True, timeout=60)
                subprocess.run(["systemctl", "poweroff"], capture_output=True, timeout=60)
                return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
