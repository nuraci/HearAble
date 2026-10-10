"""Send the selected audio off the box, from ahead of where the viewer is.

The decoder will not tell the network where it is, and it will not stream a
local file — both measured, both closed. From inside enigma2 neither is a
problem: the service interface answers the position exactly, and the file is
right there. So the relay is a *second reader* of the same media, started at the
viewer's position plus the intended lead, forwarding only the selected audio
track, compressed, with its own timestamps.

Three properties matter more than throughput, and all three come from it being a
**separate process** rather than anything inside the playback pipeline:

* enigma2 cannot be blocked by it. A slow consumer fills a TCP buffer that
  belongs to `ffmpeg`, not to the process drawing the television picture.
* A receiver that is absent, slow or killed is harmless. The relay waits, or
  dies and is restarted, and the viewer sees nothing either way.
* If it fails outright, playback is untouched. Nothing here is on the path
  between the file and the screen.

No transcoding: `-c copy`. No video: one `-map`. The audio the viewer is about
to hear, and nothing else.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

# Enough that an ordinary decode wobble is not read as a seek, small enough that
# a real one is caught within a poll. A viewer's skip is 15 s at the smallest.
SEEK_THRESHOLD_S = 4.0
# Playback advances by about the poll interval; a position that has not moved
# for this long is a pause rather than a slow poll.
PAUSE_CONFIRM_S = 1.5
# ffmpeg's own complaints go here. A relay that dies silently is a relay nobody
# can fix from the far end.
FFMPEG_LOG = "/tmp/hearable_relay_ffmpeg.log"

# Waking the machine that does the listening.
#
# The receiver lives on the other end of the private cable and switches itself
# off twenty minutes after the last session. F4 is the viewer asking for
# subtitles, so F4 is where the magic packet belongs: without it the panel says
# «Avvio...» to a decoder that is waiting for a machine nobody has woken.
#
# A packet is sent only while there is no receiver, at most once every
# WOL_INTERVAL_S and at most WOL_MAX_ATTEMPTS times per switch-on. A machine
# that is already awake ignores it, and one that is not there is not worth
# shouting at for ever.
WOL_CONFIG = "/etc/enigma2/hearable_wol.json"
# The MAC is a fact about one household's machine, not about HearAble, so it is
# not written here. It lives in WOL_CONFIG on the box, which the installer
# provisions from config/site.json. An empty MAC disables waking with a note
# that says so, rather than failing somewhere less obvious.
WOL_DEFAULTS = {"enabled": True,
                "mac": "",
                "broadcast": "10.77.0.255",
                "ports": [9, 7]}
WOL_INTERVAL_S = 20.0
WOL_MAX_ATTEMPTS = 15

# The other half of the same courtesy: when the decoder goes away on purpose,
# say so, instead of leaving the receiver to work it out from a silence that
# takes twenty minutes to become conclusive.
#
# The datagram is a hint, never an order. Enigma2 calls the same hook when it
# restarts for an image update, and a restart must not take the appliance down
# with it — so the receiver shortens its countdown rather than switching off,
# and a decoder that comes back within the grace cancels it like any other
# returning session. A power cut sends nothing, and the twenty minutes are
# still there underneath.
BYE_PORT = 9011
BYE_MESSAGE = b"hearable-decoder-off"

# A receiver is considered present for this long after it last asked for the
# control state. It polls far more often than that, so a receiver that is killed
# stops being served within a few seconds and the relay simply stops.
RECEIVER_TTL_S = 8.0
RESPAWN_DELAY_S = 2.0
# A receiver that has announced itself but is not actually listening had the
# relay retrying every couple of seconds for as long as it took anyone to
# notice — seven hundred times in one sitting. Harmless to the television and
# useless to everyone, so a run of failures backs off.
MAX_RESPAWN_DELAY_S = 30.0
# The relay reads at playback speed, so once it is in the wrong place it stays
# there: nothing brings it back on its own. Wide enough that ordinary jitter
# does not restart the stream, narrow enough that a viewer never waits on it.
LEAD_TOLERANCE_S = 2.0
# A fixed head start, not a learned one.
#
# Everything between spawning ffmpeg and the receiver having the first sample in
# hand — the seek, the connection, the probe, the decoder filling — comes off the
# lead, and it is several seconds. That was first answered with an allowance the
# relay learned from what the receiver reported, which meant two regulators, one
# at each end, correcting the same quantity in opposite directions. They fought:
# the relay tore down streams the allowance had just set up, the allowance
# oscillated into its own clamp, and the receiver sat deadlocked behind a full
# queue.
#
# One regulator is enough, and it belongs at the receiving end where the
# measurement is exact. So the relay simply aims generously ahead and never
# adjusts; the receiver refuses to hand on anything further ahead than the
# configured delay. The surplus waits in a buffer, which is what a buffer is for.
HEAD_START_S = 5.0
# Only a discontinuity nobody caught should restart a healthy stream.
IMPLAUSIBLE_LEAD_S = 12.0
# Where the live audio comes from.
#
# The demux tap is a PID filter on the service Enigma2 is already showing. It
# does not hold the tuner: measured against its controls, twelve zaps of twelve
# and eight of eight across multiplexes, the same as with nobody listening at
# all, where a streamserver client managed four of eight and left the screen
# blank forty-four times. The streamserver path is kept as a fallback because it
# is the one the previous gate qualified, and because a box whose demux refuses
# a second filter would otherwise have nothing.
DEMUX_TAP = "/usr/lib/enigma2/python/Plugins/Extensions/HearAbleOSD/dvb_demux_tap.py"
LIVE_INPUT = os.environ.get("HEARABLE_LIVE_INPUT", "demux")     # demux | streamserver
# A lead measured before this much of a new stream has reached the receiver
# still describes the old one. Without the wait the first report after a
# re-prime restarts the stream that was about to fix it, and the report after
# that restarts the next: a slow consumer produced seventy spawns in ninety
# seconds, each one cancelling its predecessor before any audio arrived.
LEAD_SETTLE_S = 12.0
# How long to leave a stream alone after re-priming it for an implausible lead.
LEAD_REPRIME_INTERVAL_S = 20.0
# Re-priming corrects a relay reading from the wrong place. It cannot make a
# receiver faster, so after this many attempts that did not bring the lead back
# the relay stops trying and keeps streaming: a consumer that cannot keep up is
# the consumer's problem, and the television must not pay for it.
LEAD_REPRIME_LIMIT = 3


def source_kind(reference: str | None) -> str:
    """What the viewer is watching, from the reference Enigma2 gives us.

    Reported so the receiver can refuse what it cannot yet handle instead of
    being handed audio it would mislabel. Only `file` is implemented: the relay
    seeks into the media with `-ss`, which a live service has no equivalent of,
    so a channel must fail loudly rather than quietly.

    The first field of a service reference is its type: `1` is a broadcast
    service, `4097` is servicemp3 — which covers both a file on the stick and a
    URL, told apart by what follows.

    A type is not enough on its own. A recording played back is type `1` too —
    Enigma2 plays its own `.ts` files through the same DVB service as a
    channel — and is told apart only by the path in its eleventh field:
    `1:0:0:0:0:0:0:0:0:0:/media/hdd/movie/<name>.ts:<title>`. Read as a channel,
    it sent the relay to the tuner for an audio PID that only exists in the
    file, and the subtitles stayed blank for the whole recording.
    """
    if not reference:
        return "unknown"
    head, _, rest = reference.partition(":")
    if head == "4097":
        return "stream" if "http" in rest.lower() else "file"
    if head == "1":
        return "file" if recording_path(reference) else "dvb"
    return "unknown"


def recording_path(reference: str | None) -> str:
    """The file behind a type-1 reference, or "" for a live channel.

    The path is the eleventh field, and a title may follow it. Enigma2 writes a
    colon inside a field as `%3a`, so a title with a colon in it cannot be
    mistaken for the end of the path.
    """
    fields = (reference or "").split(":")
    if len(fields) > 10 and fields[0] == "1" and fields[10].startswith("/"):
        return fields[10].replace("%3a", ":").replace("%3A", ":")
    return ""


def live_without_a_reference(info: dict) -> bool:
    """A broadcast whose reference Enigma2 will not name.

    Restart the plugin while a channel is already on the screen and
    `getCurrentlyPlayingServiceReference()` answers None until the viewer zaps,
    even though the decoder is plainly playing. Measured on Cartoonito HD:
    sixty seconds of no reference, then one zap and it was there again.

    The reference is not the only thing that can be read. A file can be sought
    into and a broadcast cannot, so a source that is playing, is not seekable
    and has no reference is live — and treating it as a file, which is what
    happened, applied the wrong delay and refused to carry the audio at all.
    """
    return (bool(info.get("available"))
            and not info.get("seekable")
            and not info.get("service_reference"))


class AudioRelay:
    """Keeps one ffmpeg alive, pointed at where the viewer is."""

    def __init__(self, playback, *, port: int = 9010, lead_ms: int = 1500,
                 log=lambda message: None) -> None:
        self.playback = playback            # callable returning the plugin's playback dict
        self.port = port
        self.lead_ms = lead_ms
        self.log = log

        self.epoch = 0
        # Switched off by the viewer, the relay lets go of everything rather
        # than merely going quiet: the demux filter is closed and the tuner
        # sees nobody.
        self.enabled = True
        self.state = "idle"
        self.proc: subprocess.Popen | None = None
        self.started_at_media_s: float | None = None
        self.started_at_wall: float | None = None
        self.spawns = 0
        self.exits = 0
        self._wol_unconfigured_said = False
        self.wol = dict(WOL_DEFAULTS)
        try:
            with open(WOL_CONFIG) as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.wol.update({k: v for k, v in loaded.items() if k in WOL_DEFAULTS})
        except Exception:
            pass
        self.wol_sent = 0
        self.wol_attempts = 0
        self._wol_last = 0.0
        self.consecutive_failures = 0
        self.last_reason = ""
        self.events: list[dict] = []

        self._media = ""
        self._track = 0
        self.live_pid: int | None = None
        self._last_position: float | None = None
        self._last_seen_wall = 0.0
        self._still_since: float | None = None
        self._paused = False
        self._respawn_after = 0.0
        self._spawned_at = 0.0
        self.receiver: str | None = None
        self._receiver_seen = 0.0
        self.reported_lead_s: float | None = None
        self._pending_lead_restart = False
        self._lead_valid_after = 0.0
        self._lead_reprimes = 0

    # Below this, a change in the wanted lead is not worth a splice in the audio.
    LEAD_CHANGE_WORTH_A_RESTART_MS = 100

    def set_lead_ms(self, lead_ms: int) -> None:
        """The receiver says how far ahead of the viewer it wants to be.

        It regulates the lead itself — it simply refuses to hand on audio that is
        further ahead than this — so the relay needs the figure only to decide
        where to start reading. A stream already running was started from the old
        one, though, so a real change has to re-prime or it would take effect
        only at the next discontinuity, which might be an hour away.
        """
        if not 0 <= lead_ms <= 30_000 or lead_ms == self.lead_ms:
            return
        changed_by = abs(lead_ms - self.lead_ms)
        self.note("lead_changed", from_ms=self.lead_ms, to_ms=lead_ms)
        self.lead_ms = lead_ms
        if changed_by >= self.LEAD_CHANGE_WORTH_A_RESTART_MS and self.proc is not None:
            self._pending_lead_restart = True

    def report_lead(self, lead_s: float) -> None:
        """The receiver says how far ahead of the viewer it actually ended up.

        Recorded, and acted on only when it is absurd — which means a
        discontinuity slipped past the checks above, not that the lead needs
        trimming. Trimming is the receiver's job and it does it without asking.
        """
        self.reported_lead_s = round(lead_s, 3)

    def announce(self, receiver: str | None) -> None:
        """The receiver says where to send, and that it is still there.

        The relay *connects out* rather than listening, which is the difference
        between a stream that starts where the viewer is and one that starts
        wherever they happened to be when the process was spawned. Waiting on a
        listening socket meant deciding the seek position up to thirty seconds
        before anyone arrived to read it.

        It also means an absent receiver costs nothing at all: no process is
        started, no port is held, and the television never knows.
        """
        if receiver:
            self.receiver = receiver
            self._receiver_seen = time.time()

    def receiver_present(self) -> bool:
        return bool(self.receiver) and (time.time() - self._receiver_seen) <= RECEIVER_TTL_S

    # -- what the receiver is told -----------------------------------------
    def control(self) -> dict:
        info = self.playback()
        return {
            "protocol": "hearable-audio-relay/1",
            "source_type": source_kind(info.get("service_reference")),
            "service_reference": info.get("service_reference"),
            "media": self._media,
            "source_epoch": self.epoch,
            "state": self.state,
            "selected_audio_track": info.get("audio_track"),
            "selected_audio_pid": info.get("audio_pid"),
            "seekable": bool(info.get("seekable")),
            "selected_audio_language": info.get("audio_track_language"),
            "audio_track_count": info.get("audio_track_count"),
            "position_s": info.get("position_s"),
            "length_s": info.get("length_s"),
            "sampled_wall_epoch": info.get("sampled_wall_epoch"),
            "relay_head_start_s": HEAD_START_S,
            "relay_reported_lead_s": self.reported_lead_s,
            "relay_receiver": self.receiver,
            "relay_receiver_present": self.receiver_present(),
            "relay_audio_pid": self.live_pid,
            "live_input": LIVE_INPUT,
            "relay_lead_ms": self.lead_ms,
            "relay_running": self.proc is not None and self.proc.poll() is None,
            "relay_started_at_media_s": self.started_at_media_s,
            "relay_spawns": self.spawns,
            "wol_sent": self.wol_sent,
            "wol_attempts": self.wol_attempts,
            "relay_exits": self.exits,
            "relay_consecutive_failures": self.consecutive_failures,
            "last_reason": self.last_reason,
            "wire": {"container": "mpegts", "codec": "aac", "transcoded": False,
                     "timestamps": "source media PTS (-copyts)"},
            "events": self.events[-20:],
        }

    def note(self, event: str, **detail) -> None:
        entry = {"wall_epoch": round(time.time(), 3), "event": event}
        entry.update(detail)
        self.events.append(entry)
        if len(self.events) > 200:
            del self.events[:100]
        self.log(f"relay: {event} {detail}")

    # -- the process --------------------------------------------------------
    def _media_path(self, reference: str) -> str:
        # A 4097: reference carries the path as its last field; a recording
        # carries it in the eleventh, with its title after it.
        return recording_path(reference) or (reference.split(":")[-1] if reference else "")

    def release(self, reason: str) -> None:
        """Let go of the stream now, without waiting for anything.

        This box has one terrestrial tuner. A live relay holds the service it is
        copying, and when the viewer changes to a channel on another multiplex
        Enigma2 has to retune — with the stream still attached it cannot, and
        measured on air the zap failed and left the television with no service
        at all, which is the one thing that must never happen.

        So the relay lets go the instant Enigma2 says the service is ending,
        rather than a poll later when the damage is done. No terminate-and-wait
        here: this runs inside the main loop, and half a second spent being
        polite is half a second of frozen television.
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        self.state = "released"
        self.live_pid = None
        self._media = ""
        self.note("released", reason=reason)

    def _kill(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            for _ in range(10):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        except Exception as error:
            self.log(f"relay: kill failed: {type(error).__name__}: {error}")

    def wake_receiver(self) -> None:
        """A magic packet at the receiver, while it is absent and not for ever.

        Broadcast rather than addressed: a machine that is switched off cannot
        answer an ARP request, so there is nothing to resolve. On a cable with
        one listener the broadcast reaches exactly it.
        """
        if not self.wol.get("enabled") or self.receiver_present():
            return
        if not str(self.wol.get("mac") or "").strip():
            # Said once, not every twenty seconds: a box with no MAC configured
            # is a deployment that is not finished, and the log should read that
            # way instead of repeating a hex parsing error.
            if not self._wol_unconfigured_said:
                self._wol_unconfigured_said = True
                self.note("wol_unconfigured", config=WOL_CONFIG)
            return
        if self.wol_attempts >= WOL_MAX_ATTEMPTS:
            return
        now = time.time()
        if now - self._wol_last < WOL_INTERVAL_S:
            return
        self._wol_last = now
        self.wol_attempts += 1
        try:
            raw = bytes(bytearray.fromhex(str(self.wol["mac"]).replace(":", "")))
            if len(raw) != 6:
                raise ValueError(self.wol["mac"])
            payload = b"\xff" * 6 + raw * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in self.wol.get("ports") or [9]:
                sock.sendto(payload, (self.wol["broadcast"], int(port)))
            sock.close()
            self.wol_sent += 1
            self.note("wol_sent", mac=self.wol["mac"],
                      broadcast=self.wol["broadcast"], attempt=self.wol_attempts)
        except Exception as error:
            self.note("wol_failed", detail=f"{type(error).__name__}: {error}")

    def announce_going_away(self) -> None:
        """Tell the receiver the decoder is leaving, once, without insisting."""
        if not self.wol.get("enabled"):
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(BYE_MESSAGE, (self.wol["broadcast"], BYE_PORT))
            sock.close()
            self.note("bye_sent", broadcast=self.wol["broadcast"], port=BYE_PORT)
        except Exception as error:
            self.note("bye_failed", detail=f"{type(error).__name__}: {error}")

    def reset_wake(self) -> None:
        """A fresh switch-on gets a fresh budget of attempts."""
        self.wol_attempts = 0
        self._wol_last = 0.0

    def _spawn(self, position_s: float, track: int, media: str, reason: str) -> None:
        self._kill()
        if not self.receiver_present():
            self.state = "waiting_for_receiver"
            self.wake_receiver()
            return
        start_at = max(0.0, position_s + self.lead_ms / 1000.0 + HEAD_START_S)
        url = f"tcp://{self.receiver}"
        command = [
            "ffmpeg", "-v", "error", "-nostdin",
            # Paced at playback speed: the relay must not race ahead of the
            # viewer, and a stalled consumer must show up as backpressure rather
            # than as a growing buffer somewhere.
            "-re", "-copyts",
            # The stamps must count from the start of the file, because that is
            # what the position they are compared with counts from. A film does
            # already; a recording does not — it carries the broadcast's clock,
            # and one starting at 43 560 s looked twelve hours ahead of the
            # picture, so the receiver held every sample and nothing was ever
            # recognised. A no-op for a file that already starts at zero.
            "-start_at_zero",
            "-ss", f"{start_at:.3f}",
            "-i", media,
            "-map", f"0:a:{track}",
            "-c", "copy", "-f", "mpegts", url,
        ]
        try:
            errors = open(FFMPEG_LOG, "a")
            errors.write(f"\n=== {time.strftime('%H:%M:%S')} {' '.join(command)}\n")
            errors.flush()
            self.proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=errors,
                stdin=subprocess.DEVNULL, close_fds=True,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None)
            errors.close()
        except Exception as error:
            self.proc = None
            self.note("spawn_failed", detail=f"{type(error).__name__}: {error}")
            return
        self.spawns += 1
        self._spawned_at = time.time()
        # Whatever the receiver last reported was about the stream just killed.
        self.reported_lead_s = None
        self._lead_valid_after = self._spawned_at + LEAD_SETTLE_S
        self.started_at_media_s = round(start_at, 3)
        self.started_at_wall = time.time()
        self.last_reason = reason
        self.state = "streaming"
        self.note("relay_started", at_media_s=self.started_at_media_s,
                  track=track, reason=reason, epoch=self.epoch)

    # -- live ----------------------------------------------------------------
    def _poll_live(self, info: dict, reference: str) -> None:
        """A broadcast service, carried off the decoder's own demux tap.

        Almost nothing the file path does applies. There is no position to read,
        nothing to seek into, and no reading ahead: the future has not been
        transmitted. The whole advance HearAble gets is what already sits
        between the demux and the screen — measured at 599.8 ms — so the audio
        goes out as it arrives and nothing is held back anywhere.

        What does apply, unchanged, is the barrier: a zap or a track change is a
        discontinuity like any other, the epoch rises, and the receiver throws
        away what it was holding.
        """
        pid = info.get("audio_pid")
        # A transport stream PID is thirteen bits, and 0x0000-0x000F and 0x1FFF
        # are reserved. In the second after a zap the decoder answers with a
        # number that is none of these — 7628149, 30225547, 36807132 were all
        # seen in one half-hour run — because the programme table has not been
        # parsed yet. Spawning a tap on it costs a failed ioctl, a wasted epoch
        # and a line in the log every single time the viewer changes channel.
        if not isinstance(pid, int) or not 0x10 <= pid <= 0x1FFE:
            if self.state != "no_audio_pid":
                self.state = "no_audio_pid"
                self._kill()
                self.note("live_without_an_audio_pid", reference=reference)
            return

        now = time.time()
        if not self.receiver_present():
            if self.proc is not None:
                self._kill()
                self.note("receiver_gone", receiver=self.receiver)
            # Set whether or not there was a process to kill: the label is read
            # by the gates and by the status file, and leaving the previous one
            # standing made a relay that was switched on and simply waiting
            # report itself as off.
            self.state = "waiting_for_receiver"
            self.wake_receiver()
            return

        changed = None
        if reference != self._media:
            changed = "service_changed"
        elif pid != self.live_pid:
            changed = "audio_pid_changed"
        if changed is not None:
            previous_reference, self._media = self._media, reference
            previous_pid, self.live_pid = self.live_pid, pid
            self.epoch += 1
            self.note(changed, epoch=self.epoch, reference=reference, audio_pid=pid,
                      previous_reference=previous_reference, previous_audio_pid=previous_pid)
            self._spawn_live(reference, pid, changed)
            return

        if self.proc is not None and self.proc.poll() is not None:
            self.exits += 1
            self.proc = None
            self.state = "waiting"
            if now - self._spawned_at < 3.0:
                self.consecutive_failures += 1
            else:
                self.consecutive_failures = 0
            delay = min(MAX_RESPAWN_DELAY_S,
                        RESPAWN_DELAY_S * (2 ** min(self.consecutive_failures, 4)))
            self._respawn_after = now + delay
            self.note("relay_exited", spawns=self.spawns, exits=self.exits,
                      failures=self.consecutive_failures, retry_in_s=round(delay, 1))
        if self.proc is None and now >= self._respawn_after:
            first_time = not self.spawns
            if not first_time:
                # A respawn is a hole in the audio, not a continuation. The
                # stream was down for as long as it took to notice and come
                # back, and live television does not wait: the seconds in
                # between were broadcast and lost. Told nothing, the recogniser
                # stitches across the gap and invents a sentence out of two.
                # Measured over half an hour, this was five media
                # discontinuities that nobody announced.
                self.epoch += 1
                self.note("stream_resumed", epoch=self.epoch, reference=reference,
                          audio_pid=pid)
            self._spawn_live(reference, pid, "start" if first_time else "respawn")

    def _spawn_live(self, reference: str, pid: int, reason: str) -> None:
        """One ffmpeg, copying the selected audio PID off port 8001.

        `-map 0:i:<pid>` selects by transport stream id, which is the PID. An
        index would be whatever order ffmpeg happened to find the tracks in, and
        the viewer chose a PID; on the service this was written against the
        Italian track is 1157 and also happens to be the first, which is exactly
        the sort of coincidence that hides a bug for months.

        No `-ss`: there is nowhere to seek to. No `-re`: the stream already
        arrives at the speed it was broadcast, and pacing it again would only
        add a queue nobody asked for.
        """
        self._kill()
        if not self.receiver_present():
            self.state = "waiting_for_receiver"
            self.wake_receiver()
            return
        if LIVE_INPUT == "demux" and os.path.exists(DEMUX_TAP):
            self._spawn_demux_tap(reference, pid, reason)
            return
        command = [
            "ffmpeg", "-v", "error", "-nostdin",
            # Live means the audio is worth exactly as much as it is fresh, so
            # everything that buffers for its own convenience is turned down.
            # ffmpeg's defaults study the input for seconds before emitting, and
            # its mpegts muxer holds 0.7 s to interleave — on a stream with one
            # copied audio track there is nothing to interleave and nothing to
            # discover. Enough probing is left to read the PMT, because mapping
            # by PID needs the programme table parsed.
            "-fflags", "nobuffer", "-probesize", "1000000", "-analyzeduration", "1000000",
            # The broadcaster's own timestamps, kept. Without this ffmpeg starts
            # each output near zero, so every respawn of the same service would
            # label fresh audio with times it had already used — and the only
            # thing telling old from new would be the epoch. The broadcast PTS
            # is a real clock and it runs through a respawn.
            "-copyts",
            "-i", f"http://127.0.0.1:8001/{reference}",
            "-map", f"0:i:{pid}",
            "-c", "copy", "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", f"tcp://{self.receiver}",
        ]
        try:
            errors = open(FFMPEG_LOG, "a")
            errors.write(f"\n=== {time.strftime('%H:%M:%S')} {' '.join(command)}\n")
            errors.flush()
            self.proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=errors,
                stdin=subprocess.DEVNULL, close_fds=True,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None)
            errors.close()
        except Exception as error:
            self.proc = None
            self.note("spawn_failed", detail=f"{type(error).__name__}: {error}")
            return
        self.spawns += 1
        self._spawned_at = time.time()
        self.started_at_media_s = None
        self.started_at_wall = self._spawned_at
        self.live_pid = pid
        self.last_reason = reason
        self.state = "streaming"
        self.note("live_relay_started", reference=reference, audio_pid=pid,
                  reason=reason, epoch=self.epoch)

    def _spawn_demux_tap(self, reference: str, pid: int, reason: str) -> None:
        """The PID filter, writing transport packets straight at the receiver.

        No ffmpeg anywhere on the box for live: what comes off the filter is
        already MPEG-TS carrying the broadcaster's timestamps, and the
        receiver's decoder reads a single-PID stream without a programme table.
        One process fewer, and none of its buffering.
        """
        command = ["python3", DEMUX_TAP, "--pid", str(pid), "--output", "tsdemux",
                   "--seconds", "86400", "--to", self.receiver]
        try:
            errors = open(FFMPEG_LOG, "a")
            errors.write(f"\n=== {time.strftime('%H:%M:%S')} {' '.join(command)}\n")
            errors.flush()
            self.proc = subprocess.Popen(
                command, stdout=errors, stderr=errors,
                stdin=subprocess.DEVNULL, close_fds=True,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None)
            errors.close()
        except Exception as error:
            self.proc = None
            self.note("spawn_failed", detail=f"{type(error).__name__}: {error}")
            return
        self.spawns += 1
        self._spawned_at = time.time()
        self.started_at_media_s = None
        self.started_at_wall = self._spawned_at
        self.live_pid = pid
        self.last_reason = reason
        self.state = "streaming"
        self.note("demux_tap_started", reference=reference, audio_pid=pid,
                  reason=reason, epoch=self.epoch)

    def _discontinuity(self, position_s: float, track: int, media: str, reason: str,
                       **detail) -> None:
        """Everything before this belongs to somewhere the viewer has left."""
        self.epoch += 1
        self.note(reason, epoch=self.epoch, position_s=round(position_s, 3), **detail)
        self._spawn(position_s, track, media, reason)

    # -- the tick -----------------------------------------------------------
    def poll(self) -> None:
        try:
            self._poll()
        except Exception as error:
            self.note("poll_failed", detail=f"{type(error).__name__}: {error}")

    def _poll(self) -> None:
        if not self.enabled:
            if self.proc is not None:
                self.release("hearable_off")
            self.state = "off"
            return
        info = self.playback()
        if not info.get("available"):
            if self.state != "idle":
                self.state = "idle"
                self._kill()
                self.note("media_gone", reason=info.get("reason"))
            return

        reference = info.get("service_reference") or ""
        if source_kind(reference) == "dvb" or live_without_a_reference(info):
            self._poll_live(info, reference)
            return

        # Anything else that cannot be sought into is not something this relay
        # knows how to carry. Say so plainly, once, and leave the television
        # alone rather than half-trying.
        if not info.get("seekable"):
            if self.state != "unsupported_source":
                self.state = "unsupported_source"
                self._kill()
                self.note("source_not_relayable",
                          reference=reference, reason=info.get("reason"))
            return

        position = info["position_s"]
        track = int(info.get("audio_track") or 0)
        media = self._media_path(info.get("service_reference") or "")
        now = time.time()

        if not media:
            return
        if media != self._media:
            previous, self._media = self._media, media
            self._track = track
            self._last_position = position
            self._discontinuity(position, track, media, "source_changed",
                                previous_media=previous)
            return

        # --- pause: the position stops advancing ---------------------------
        if self._last_position is not None and abs(position - self._last_position) < 0.05:
            if self._still_since is None:
                self._still_since = now
            elif not self._paused and now - self._still_since >= PAUSE_CONFIRM_S:
                self._paused = True
                self.state = "paused"
                self._kill()
                self.note("paused", position_s=round(position, 3))
        else:
            if self._paused:
                self._paused = False
                self._still_since = None
                self._last_position = position
                self._discontinuity(position, track, media, "resumed")
                return
            self._still_since = None

        if self._paused:
            self._last_position = position
            return

        # --- track change ---------------------------------------------------
        if track != self._track:
            previous, self._track = self._track, track
            self._last_position = position
            self._discontinuity(position, track, media, "track_changed",
                                previous_track=previous, new_track=track)
            return

        # --- seek -------------------------------------------------------------
        # `_last_seen_wall` of zero would make the expectation an epoch's worth of
        # seconds away and every first poll a seek.
        if self._last_position is not None and self._last_seen_wall:
            expected = self._last_position + max(0.0, now - self._last_seen_wall)
            if abs(position - expected) > SEEK_THRESHOLD_S:
                jumped = position - self._last_position
                self._last_position = position
                self._last_seen_wall = now
                self._discontinuity(position, track, media, "seeked",
                                    jump_s=round(jumped, 3))
                return

        self._last_position = position
        self._last_seen_wall = now

        # --- the receiver asked for a different lead ---------------------------
        if self._pending_lead_restart:
            self._pending_lead_restart = False
            self._discontinuity(position, track, media, "lead_changed",
                                lead_ms=self.lead_ms)
            return

        # --- drift: the relay is somewhere it should not be --------------------
        # It reads at playback speed, so it cannot catch up or fall back on its
        # own. Started too early — or left running across something that moved
        # the viewer — it simply stays wrong, which is how one run ended up
        # twenty-two seconds behind the picture and content with it.
        # Drift is judged on what the receiver reports, not on elapsed time.
        #
        # "Started at media X, so it must be at X plus however long it has been
        # running" is only true of a relay nothing is holding back. This one is
        # held back deliberately: the receiver refuses audio further ahead than
        # the configured delay, which stalls ffmpeg through the socket, which is
        # exactly the backpressure the design wants. Modelled by elapsed time the
        # relay then appears to drift further ahead every second and tears itself
        # down — four splices in twenty seconds of stream.
        if (self.proc is not None and self.proc.poll() is None
                and self.reported_lead_s is not None
                and now >= self._lead_valid_after):
            wanted = self.lead_ms / 1000.0
            if abs(self.reported_lead_s - wanted) <= IMPLAUSIBLE_LEAD_S:
                self._lead_reprimes = 0
            elif self._lead_reprimes < LEAD_REPRIME_LIMIT:
                self._lead_reprimes += 1
                self.note("lead_implausible", reported_lead_s=self.reported_lead_s,
                          wanted_s=wanted, attempt=self._lead_reprimes)
                self._spawn(position, track, media, "implausible_lead")
                self._lead_valid_after = max(self._lead_valid_after,
                                             now + LEAD_REPRIME_INTERVAL_S)
                return
            else:
                # Three re-primes did not fix it, so the relay is not what is
                # wrong. Say so once and leave the stream running.
                self.note("receiver_cannot_keep_up",
                          reported_lead_s=self.reported_lead_s, wanted_s=wanted)
                self._lead_valid_after = now + LEAD_REPRIME_INTERVAL_S

        # --- keep it alive ----------------------------------------------------
        if self.proc is not None and self.proc.poll() is not None:
            self.exits += 1
            self.proc = None
            self.state = "waiting"
            # A stream that ran for a while and then ended is a different thing
            # from one that never started; only the second should back off.
            if now - self._spawned_at < 3.0:
                self.consecutive_failures += 1
            else:
                self.consecutive_failures = 0
            delay = min(MAX_RESPAWN_DELAY_S,
                        RESPAWN_DELAY_S * (2 ** min(self.consecutive_failures, 4)))
            self._respawn_after = now + delay
            self.note("relay_exited", spawns=self.spawns, exits=self.exits,
                      failures=self.consecutive_failures, retry_in_s=round(delay, 1))
        if not self.receiver_present():
            if self.proc is not None:
                self._kill()
                self.note("receiver_gone", receiver=self.receiver)
            # Set whether or not there was a process to kill: the label is read
            # by the gates and by the status file, and leaving the previous one
            # standing made a relay that was switched on and simply waiting
            # report itself as off.
            self.state = "waiting_for_receiver"
            self.wake_receiver()
            return
        if self.proc is None and now >= self._respawn_after:
            self._spawn(position, track, media, "start" if not self.spawns else "respawn")
