"""Programme audio from the SF8008's relay, without a copy of the programme.

The `/file` source that came before this works and stays, and it asks a great
deal of whatever is running HearAble: the whole container crosses the network,
video included, and the receiving node has to hold a bit-identical copy of the
media so that the decoder's position can be recovered by matching a frame off
its screen. On a 4 GB single-board computer none of that is affordable.

This asks for almost nothing. The decoder sends **only the selected audio track,
still compressed**, from ahead of where the viewer is, and says exactly where
that is over a small control endpoint. What arrives is 26 kB/s of MPEG-TS. What
is needed to use it is one AAC decode and a resample.

  no local media  ·  no video decode  ·  no frame matching  ·  no /grab

Two things are worth knowing about the shape:

*The relay connects out to us.* We listen and announce ourselves in the control
poll, which doubles as a heartbeat. That is what lets the stream start exactly
where the viewer is — a listening relay has to decide its seek position before
anyone arrives to read it — and it is why an absent receiver costs the decoder
nothing at all: no process is started and no port is held.

*The stream says where it starts.* `-c copy` cannot seek to a sample, only to a
container boundary, so the relay always begins a little *before* the position it
was asked for — measured at about 0.65 s. The first packets of each stream are
probed for their timestamp, once per epoch, and the audio ahead of the intended
start is discarded here.

That trim is what makes the pre-delay exactly what was asked for instead of
approximately. The alternative was a correction fed back to the relay, and a
feedback loop is the wrong instrument when the measurement is already exact:
both ends know a real timestamp, so the difference can simply be dropped.
"""
from __future__ import annotations

import asyncio
import json
import queue
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

CANONICAL_SAMPLE_RATE = 16000
CANONICAL_CHANNELS = 1
BYTES_PER_FLOAT = 4

CONTROL_POLL_S = 0.5
# The two ends regulate the lead from opposite sides, and deliberately so.
#
# Here: never hand on audio that is further ahead of the viewer than the
# configured delay. The relay is paced at playback speed and cannot outrun the
# viewer in steady state, but the first packets of a stream arrive in a burst,
# and a consumer that drains them keeps that head start for the rest of the run.
# Nothing is dropped — only held, and the queue behind it backpressures the relay.
#
# There: the relay aims a little further ahead than asked, by an allowance it
# learns from what the receiver reports, so that the reader is never *starved*.
#
# One side sets the ceiling and the other stops the floor being hit. A tolerance
# on this side as well only moves the ceiling up: the lead then settles at the
# tolerance rather than at the delay, which is what a quarter of a second here
# did — 1.896 s for a configured 1.5.
# Ten transport packets carry a timestamp; sixty-four kilobytes carry two and a
# half seconds of waiting before the decoder is even started, and the viewer
# walks away from the reader by exactly that much. Measured: 1880 bytes is
# enough, and this is a safe multiple of it.
# How long a connection may deliver nothing before it is treated as gone. The
# tap sends about 26 kB/s without pause, so this is far longer than any real gap
# and far shorter than TCP's own idea of a dead peer, which is minutes.
STREAM_SILENCE_LIMIT_S = 15.0
PROBE_BYTES = 4096
# Short: a full queue blocks the socket pump, which fills the TCP window, which
# is how backpressure reaches a relay paced at playback speed instead of audio
# piling up in a process nobody is reading.
# Deep enough that the relay's generous head start sits here rather than
# stalling the socket every second. Six seconds of 16 kHz mono float is 384 kB —
# nothing, next to never blocking the decoder's ffmpeg.
DEFAULT_QUEUE_CHUNKS = 40


def _first_packet_pts(payload: bytes) -> float | None:
    """The media timestamp the stream actually begins at."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", "-"],
        input=payload, capture_output=True, timeout=30)
    for line in probe.stdout.decode("utf-8", "replace").splitlines():
        value = line.strip().rstrip(",")
        try:
            return float(value)
        except ValueError:
            continue
    return None


class SF8008AudioRelaySource:
    """`AudioSource` over the decoder's pre-presentation audio relay."""

    source_type = "sf8008_relay"

    def __init__(self, *, chunk_ms: int, host: str, control_port: int = 8770,
                 receiver_port: int = 9010, receiver_host: str = "",
                 queue_chunks: int = DEFAULT_QUEUE_CHUNKS,
                 lead_ms: int | None = None,
                 control_path: str = "/control") -> None:
        self.chunk_ms = chunk_ms
        self.host = host
        self.control_port = control_port
        self.receiver_port = receiver_port
        self.receiver_host = receiver_host or self._local_address(host)
        self.control_path = control_path
        self.chunk_bytes = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000) * BYTES_PER_FLOAT

        self.eof = False
        self.chunk_index = 0
        self.samples_emitted = 0
        self.source_epoch = 0
        self.stream_start_media_s: float | None = None
        self.control: dict[str, Any] = {}
        self.pending_source_change: dict[str, Any] | None = None

        self.connections = 0
        self.control_failures = 0
        self.unsupported_control: str | None = None
        self.bytes_received = 0
        self.queue_flushes = 0
        self.chunks_dropped_on_flush = 0
        self.probe_failures = 0
        self.stream_timeouts = 0
        self.trimmed_seconds = 0.0
        self.control_start_disagreement_s: float | None = None
        self.decoder_errors: list[str] = []
        self._trim_samples = 0

        self.queue: queue.Queue = queue.Queue(maxsize=queue_chunks)
        # None means "whatever the box is already set to", which is how this
        # behaved before the figure was configurable.
        self.lead_ms = lead_ms
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._decoder: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._epoch_at_connect = 0

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _local_address(peer: str) -> str:
        """The address this machine has on the route to the decoder."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer, 9))
            return probe.getsockname()[0]
        finally:
            probe.close()

    def _control_url(self) -> str:
        fields = {"receiver": f"{self.receiver_host}:{self.receiver_port}"}
        # How far ahead of the viewer we want to be. Sent every poll rather than
        # once, because the box may have been restarted since the last one, and
        # because it is the receiver that regulates this: the relay only needs it
        # to know where to start reading.
        #
        # Meaningless for a broadcast service, where there is nothing to read
        # ahead of, so it is not sent: asking for a lead nobody can give would
        # only put a number in the telemetry that describes nothing.
        if self.lead_ms is not None and self.control.get("source_type") != "dvb":
            fields["want_lead_ms"] = str(int(self.lead_ms))
        # The relay chooses its seek position before anything downstream exists,
        # so everything the startup costs comes off the lead and is invisible to
        # it. This is the measurement that closes that gap.
        measured = self.measured_lead_s()
        if measured is not None:
            fields["lead_s"] = f"{measured:.3f}"
        return (f"http://{self.host}:{self.control_port}{self.control_path}?"
                + urllib.parse.urlencode(fields))

    # A lead measured in the first seconds of a stream is not the lead: the
    # decoder is still filling and the reader has not settled into the relay's
    # pacing. Reported early, it latched the correction at a quarter of a second
    # when three were needed.
    SETTLE_SAMPLES = CANONICAL_SAMPLE_RATE * 5

    def measured_lead_s(self) -> float | None:
        box = self.box_position_now()
        if (box is None or self.stream_start_media_s is None
                or self.samples_emitted < self.SETTLE_SAMPLES):
            return None
        reader = self.stream_start_media_s + self.samples_emitted / CANONICAL_SAMPLE_RATE
        return reader - box

    def box_position_now(self) -> float | None:
        """Where the viewer is, right now.

        The control state is up to a poll old, and it carries the instant it was
        sampled, so it can be carried forward rather than used stale. Paused,
        it is not carried forward at all.
        """
        control = self.control
        position = control.get("position_s")
        sampled = control.get("sampled_wall_epoch")
        if position is None or not sampled:
            return None
        if control.get("state") == "paused":
            return float(position)
        return float(position) + (time.time() - float(sampled))

    # What this receiver knows how to be sent. The control plane names both, and
    # a receiver that quietly accepts a protocol it does not understand is the
    # failure the wire format exists to prevent: it would hand the pipeline audio
    # it cannot place in the timeline. DVB and IPTV will report a different
    # source_type and must be refused here until they are actually supported.
    SUPPORTED_PROTOCOL = "hearable-audio-relay/1"
    SUPPORTED_SOURCE_TYPES = ("file", "dvb")

    def _unsupported(self, control: dict[str, Any]) -> str | None:
        protocol = control.get("protocol")
        if protocol and protocol != self.SUPPORTED_PROTOCOL:
            return f"protocol {protocol!r}, this receiver speaks {self.SUPPORTED_PROTOCOL!r}"
        source_type = control.get("source_type")
        if source_type and source_type not in self.SUPPORTED_SOURCE_TYPES:
            return (f"source_type {source_type!r}, this receiver handles "
                    f"{', '.join(self.SUPPORTED_SOURCE_TYPES)}")
        return None

    def read_control(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(self._control_url(), timeout=3.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            self.control_failures += 1
            return None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", self.receiver_port))
        listener.listen(1)
        listener.settimeout(1.0)
        self._listener = listener

        control = self.read_control()
        if control is None:
            raise RuntimeError(
                f"no control plane at {self.host}:{self.control_port}{self.control_path}; "
                "is the HearAbleOSD plugin installed and the decoder playing?")
        unsupported = self._unsupported(control)
        if unsupported is not None:
            listener.close()
            self._listener = None
            raise RuntimeError(f"the box is offering {unsupported}")
        self.control = control
        self.source_epoch = int(control.get("source_epoch") or 0)

        for target in (self._control_loop, self._accept_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    async def start(self) -> None:
        return None

    # -- control -----------------------------------------------------------
    def _control_loop(self) -> None:
        while not self._stop.wait(CONTROL_POLL_S):
            control = self.read_control()
            if control is None:
                continue
            unsupported = self._unsupported(control)
            if unsupported is not None:
                # Mid-run this means the viewer moved to something else entirely.
                # Say so and stop taking audio rather than mislabel it.
                if self.unsupported_control != unsupported:
                    self.unsupported_control = unsupported
                    self._flush_queue()
                continue
            self.unsupported_control = None
            self.control = control
            epoch = int(control.get("source_epoch") or 0)
            if epoch != self.source_epoch:
                previous, self.source_epoch = self.source_epoch, epoch
                self._flush_queue()
                self.pending_source_change = {
                    "previous_service_ref": control.get("service_reference"),
                    "service_ref": control.get("service_reference") or "",
                    "service_name": Path(control.get("media") or "relay").name,
                    "reason": control.get("last_reason") or "discontinuity",
                    "previous_epoch": previous,
                    "source_epoch": epoch,
                    "audio_track": control.get("selected_audio_track"),
                    "box_position_s": control.get("position_s"),
                    "detected_monotonic_ns": time.monotonic_ns(),
                    "audio_resumed_monotonic_ns": None,
                }

    def _flush_queue(self) -> int:
        """Drop audio read from where the decoder no longer is."""
        dropped = 0
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
        self.queue_flushes += 1
        self.chunks_dropped_on_flush += dropped
        self.stream_start_media_s = None
        self.samples_emitted = 0
        return dropped

    # -- media -------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            self._epoch_at_connect = self.source_epoch
            try:
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for option, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5),
                                      ("TCP_KEEPCNT", 3)):
                    if hasattr(socket, option):
                        connection.setsockopt(socket.IPPROTO_TCP,
                                              getattr(socket, option), value)
            except OSError:
                pass
            try:
                self._serve(connection)
            except Exception:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _serve(self, connection: socket.socket) -> None:
        # A new connection is a new stream with its own media origin, whether or
        # not the epoch moved — the relay restarts for drift and for its own
        # exits too. Anything still queued belongs to the previous origin, and
        # keeping it would have the new origin's timestamps attached to the old
        # stream's audio. That mislabelled three seconds of it.
        self._flush_queue()
        connection.settimeout(5.0)
        head = bytearray()
        while len(head) < PROBE_BYTES and not self._stop.is_set():
            try:
                block = connection.recv(16384)
            except socket.timeout:
                break
            if not block:
                return
            head += block

        start = _first_packet_pts(bytes(head))
        if start is None:
            self.probe_failures += 1
        self.stream_start_media_s = start

        # The timestamp in the stream is the authority, and nothing else is.
        #
        # This used to be overwritten with the position the control plane said
        # the relay had been asked for, so that the lead would come out at
        # exactly the configured value after trimming the difference. That is a
        # guess wearing a measurement's clothes: with the relay restarting every
        # few seconds, the control snapshot in hand belongs to whichever spawn
        # happened to be current, not to the stream that just connected. It
        # labelled audio from 504 s as 91.4 s — four hundred seconds of the
        # programme, confidently mis-stamped, and every timing number downstream
        # would have inherited it.
        #
        # The lead does not need the trim: the receiver already refuses to hand
        # on audio further ahead than the configured delay, which regulates it
        # from this side without touching what the audio claims to be.
        self._trim_samples = 0
        intended = self.control.get("relay_started_at_media_s")
        if start is not None and isinstance(intended, (int, float)):
            self.control_start_disagreement_s = round(intended - start, 3)

        decoder = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-nostdin",
             # Without these ffmpeg studies the stream before emitting anything,
             # and its defaults are sized for a full-rate transport stream: at
             # the 26 kB/s this carries, the analysis window is minutes long and
             # no audio ever comes out. There is one known stream in one known
             # codec here; there is nothing to discover.
             "-probesize", self._probe_bytes(), "-analyzeduration", "0",
             "-fflags", "nobuffer",
             "-f", "mpegts", "-i", "pipe:0",
             "-map", "0:a:0", "-f", "f32le", "-ar", str(CANONICAL_SAMPLE_RATE),
             "-ac", str(CANONICAL_CHANNELS), "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_decoder_errors, args=(decoder,),
                         daemon=True).start()
        with self._lock:
            self._decoder = decoder

        pump = threading.Thread(target=self._pump_decoded, args=(decoder,), daemon=True)
        pump.start()
        silent_for = 0.0
        try:
            decoder.stdin.write(bytes(head))
            self.bytes_received += len(head)
            while not self._stop.is_set():
                try:
                    block = connection.recv(32768)
                except socket.timeout:
                    # A read timeout used to mean "carry on waiting", forever.
                    # Pull the network cable and that is exactly what happens:
                    # the peer is gone but TCP does not know it, so this loop
                    # holds a dead connection while the accept loop — which is
                    # serial — never gets back to take the live one. Measured on
                    # a real unplug: the decoder's tap could no longer connect,
                    # respawned eighteen times, and three half-open sockets sat
                    # on the receiver with eighty kilobytes unread apiece.
                    #
                    # A live tap delivers continuously; silence this long is not
                    # a quiet passage, it is an absent peer.
                    silent_for += connection.gettimeout() or 5.0
                    if silent_for >= STREAM_SILENCE_LIMIT_S:
                        self.stream_timeouts += 1
                        break
                    continue
                if not block:
                    break
                silent_for = 0.0
                self.bytes_received += len(block)
                decoder.stdin.write(block)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                decoder.stdin.close()
            except OSError:
                pass
            pump.join(timeout=5)
            decoder.terminate()
            try:
                decoder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                decoder.kill()
            with self._lock:
                if self._decoder is decoder:
                    self._decoder = None

    def _probe_bytes(self) -> str:
        """How much ffmpeg may read before it starts producing.

        It is latency, not caution: whatever it holds at the start it keeps for
        the life of the stream, because everything downstream runs at the speed
        of the broadcast and nothing ever catches up. Measured on air, dropping
        this from 32768 to 2048 took the decoder's contribution from 2852 ms to
        about 1000.

        A file is left alone at the value it was qualified with. It is reading
        ahead of the viewer anyway, so the same bytes cost it nothing, and its
        numbers are frozen.
        """
        return "2048" if self.control.get("source_type") == "dvb" else "32768"

    def _drain_decoder_errors(self, decoder: subprocess.Popen) -> None:
        """Keep the decoder's complaints instead of discarding them.

        A decoder that quietly produces nothing is the hardest kind of failure to
        find from the far end, and this one did exactly that.

        Each complaint is stamped with how far into the stream it arrived. Live
        broadcast is joined mid-frame, so a burst of decode errors in the first
        second is the join and not a fault, while the same text arriving two
        minutes in is a fault — and without the time on it the two are the same
        line of text.
        """
        started = time.time()
        try:
            for line in decoder.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.decoder_errors.append(
                        {"at_stream_second": round(time.time() - started, 2), "text": text})
                    del self.decoder_errors[:-40]
        except Exception:
            pass

    def _pump_decoded(self, decoder: subprocess.Popen) -> None:
        while not self._stop.is_set():
            block = decoder.stdout.read(self.chunk_bytes)
            if not block:
                return
            if self._trim_samples > 0:
                have = len(block) // BYTES_PER_FLOAT
                if have <= self._trim_samples:
                    self._trim_samples -= have
                    continue
                block = block[self._trim_samples * BYTES_PER_FLOAT:]
                self._trim_samples = 0
            while not self._stop.is_set():
                try:
                    self.queue.put(block, timeout=0.25)
                    break
                except queue.Full:
                    continue

    # -- the contract ------------------------------------------------------
    async def read_chunk(self):
        from hearable.audio_sources import CanonicalAudioFrame

        if self._stop.is_set():
            return None

        # Do not hand on audio the viewer is not near yet. This is the whole of
        # the look-ahead bound: it needs no feedback to the decoder, because both
        # sides of the comparison are real timestamps.
        box = self.box_position_now()
        if box is not None and self.stream_start_media_s is not None:
            reader = self.stream_start_media_s + self.samples_emitted / CANONICAL_SAMPLE_RATE
            target = (self.lead_ms
                      if self.lead_ms is not None
                      else (self.control.get("relay_lead_ms") or 1500)) / 1000.0
            if reader - box > target:
                await asyncio.sleep(0.02)
                return None

        try:
            data = await asyncio.to_thread(self.queue.get, True, 0.5)
        except queue.Empty:
            return None
        remainder = len(data) % BYTES_PER_FLOAT
        dropped = 0
        if remainder:
            dropped = 1
            data = data[: len(data) - remainder]
        if not data:
            return None

        self.chunk_index += 1
        samples = np.frombuffer(data, dtype=np.float32).copy()
        start = self.stream_start_media_s
        source_start = None if start is None else start + self.samples_emitted / CANONICAL_SAMPLE_RATE
        self.samples_emitted += len(samples)
        if (self.pending_source_change
                and self.pending_source_change.get("audio_resumed_monotonic_ns") is None):
            self.pending_source_change["audio_resumed_monotonic_ns"] = time.monotonic_ns()
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=samples,
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=source_start,
            source_end_time=(None if source_start is None
                             else source_start + len(samples) / CANONICAL_SAMPLE_RATE),
            monotonic_available_time_ns=time.monotonic_ns(),
            wall_available_time_ns=time.time_ns(),
            dropped_chunks=dropped,
        )

    def take_source_change(self) -> dict[str, Any] | None:
        change, self.pending_source_change = self.pending_source_change, None
        if change is not None:
            self.eof = False
        return change

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.source_type,
            "host": self.host,
            "control_url": f"http://{self.host}:{self.control_port}{self.control_path}",
            "receiver": f"{self.receiver_host}:{self.receiver_port}",
            "source_epoch": self.source_epoch,
            "stream_start_media_s": self.stream_start_media_s,
            "reader_media_s": (None if self.stream_start_media_s is None else
                               round(self.stream_start_media_s
                                     + self.samples_emitted / CANONICAL_SAMPLE_RATE, 3)),
            "chunks": self.chunk_index,
            "audio_seconds_emitted": round(self.samples_emitted / CANONICAL_SAMPLE_RATE, 3),
            "bytes_received": self.bytes_received,
            "connections": self.connections,
            "configured_lead_ms": (None if self.control.get("source_type") == "dvb"
                                   else self.lead_ms),
            "source_type": self.control.get("source_type"),
            "control_failures": self.control_failures,
            "unsupported_control": self.unsupported_control,
            "probe_failures": self.probe_failures,
            "stream_timeouts": self.stream_timeouts,
            "trimmed_seconds": self.trimmed_seconds,
            "control_start_disagreement_s": self.control_start_disagreement_s,
            "decoder_errors": self.decoder_errors[-8:],
            "decoder_errors_after_the_join": [
                e for e in self.decoder_errors
                if isinstance(e, dict) and e["at_stream_second"] > 2.0][-5:],
            "queue_flushes": self.queue_flushes,
            "chunks_dropped_on_flush": self.chunks_dropped_on_flush,
            "queue_depth": self.queue.qsize(),
            "control": {k: v for k, v in self.control.items() if k != "events"},
        }

    async def stop(self) -> None:
        self.eof = True
        self._stop.set()
        with self._lock:
            decoder = self._decoder
        if decoder is not None:
            decoder.terminate()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        for thread in self._threads:
            thread.join(timeout=5)

    async def close(self) -> None:
        await self.stop()
