from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
import os
from pathlib import Path
import select
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol
import wave

import numpy as np

from hearable.io_config import BackendConfigError, resolve_path
from tools.latency_harness import absolute_deadline_ns, pacing_sleep_seconds, source_clock_drift_ms


CANONICAL_SAMPLE_RATE = 16000
CANONICAL_CHANNELS = 1
CANONICAL_DTYPE = "float32"


@dataclass
class CanonicalAudioFrame:
    seq: int
    samples: np.ndarray
    sample_rate: int
    channels: int
    source_start_time: float | None
    source_end_time: float | None
    monotonic_available_time_ns: int
    wall_available_time_ns: int
    expected_feed_monotonic_ns: int | None = None
    source_clock_drift_ms: float | None = None
    dropped_chunks: int = 0


class AudioSource(Protocol):
    source_type: str
    eof: bool

    def open(self) -> None:
        ...

    async def start(self) -> None:
        ...

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        ...

    async def stop(self) -> None:
        ...

    async def close(self) -> None:
        ...


def load_wav_16k_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wav:
        if wav.getframerate() != CANONICAL_SAMPLE_RATE or wav.getnchannels() != CANONICAL_CHANNELS:
            raise ValueError("input WAV replay requires 16 kHz mono; convert with ffmpeg first")
        sampwidth = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if sampwidth == 2:
        return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0).copy()
    if sampwidth == 4:
        return np.frombuffer(raw, dtype=np.float32).copy()
    raise ValueError(f"unsupported WAV sample width: {sampwidth}")


def read_pcm_chunk(reader: Any, wanted: int, wait_sec: float = 0.5) -> bytes | None:
    fd = reader.fileno()
    chunks: list[bytes] = []
    remaining = wanted
    deadline = time.monotonic() + wait_sec
    while remaining > 0:
        timeout = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b"".join(chunks) if chunks else None
        try:
            data = os.read(fd, remaining)
        except BlockingIOError:
            continue
        if not data:
            return b"".join(chunks)
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


class WavAudioSource:
    source_type = "wav"

    def __init__(self, *, path: str, chunk_ms: int, mode: str = "realtime", root: Path | None = None) -> None:
        if not path:
            raise BackendConfigError("audio source wav option 'path' is required")
        self.path = resolve_path(path, root=root) if root else path
        self.chunk_ms = chunk_ms
        self.mode = mode
        self.samples_per_chunk = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000)
        self.samples: np.ndarray | None = None
        self.source_start_ns = 0
        self.chunk_index = 0
        self.eof = False

    def open(self) -> None:
        self.samples = load_wav_16k_mono(self.path)

    async def start(self) -> None:
        self.source_start_ns = time.monotonic_ns()

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if self.samples is None:
            raise RuntimeError("WavAudioSource.open() was not called")
        offset = self.chunk_index * self.samples_per_chunk
        if offset >= len(self.samples):
            self.eof = True
            return None
        chunk_samples = min(self.samples_per_chunk, len(self.samples) - offset)
        chunk_audio_start_ms = offset / 16.0
        chunk_audio_end_ms = (offset + chunk_samples) / 16.0
        expected_feed_ns = absolute_deadline_ns(self.source_start_ns, chunk_audio_start_ms)
        delay_sec = pacing_sleep_seconds(self.mode, self.source_start_ns, chunk_audio_start_ms, time.monotonic_ns())
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        t0_mono_ns = time.monotonic_ns()
        self.chunk_index += 1
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=self.samples[offset : offset + self.samples_per_chunk].copy(),
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=chunk_audio_start_ms / 1000.0,
            source_end_time=chunk_audio_end_ms / 1000.0,
            monotonic_available_time_ns=t0_mono_ns,
            wall_available_time_ns=time.time_ns(),
            expected_feed_monotonic_ns=expected_feed_ns,
            source_clock_drift_ms=source_clock_drift_ms(t0_mono_ns, expected_feed_ns),
        )

    async def stop(self) -> None:
        self.eof = True

    async def close(self) -> None:
        self.samples = None


class PipeWireAudioSource:
    source_type = "pipewire"

    def __init__(self, *, target: str = "", capture_latency: str = "20ms", chunk_ms: int) -> None:
        self.target = target
        self.capture_latency = capture_latency
        self.chunk_ms = chunk_ms
        self.bytes_per_chunk = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000) * 4
        self.procs: list[subprocess.Popen] = []
        self.reader: Any | None = None
        self.chunk_index = 0
        self.eof = False

    def open(self) -> None:
        pw_cmd = [
            "pw-record",
            "--rate",
            "48000",
            "--channels",
            "2",
            "--format",
            "s16",
            "--latency",
            self.capture_latency,
            "-P",
            "stream.capture.sink=true",
        ]
        if self.target:
            pw_cmd += ["--target", self.target]
        pw_cmd += ["-"]
        pw = subprocess.Popen(pw_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                str(CANONICAL_SAMPLE_RATE),
                "-f",
                "f32le",
                "pipe:1",
            ],
            stdin=pw.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if pw.stdout:
            pw.stdout.close()
        self.procs = [pw, ffmpeg]
        self.reader = ffmpeg.stdout

    async def start(self) -> None:
        if self.reader is None:
            raise RuntimeError("PipeWireAudioSource.open() was not called")
        os.set_blocking(self.reader.fileno(), False)

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if self.reader is None:
            raise RuntimeError("PipeWireAudioSource.open() was not called")
        data = await asyncio.to_thread(read_pcm_chunk, self.reader, self.bytes_per_chunk)
        if data is None:
            return None
        if data == b"":
            self.eof = True
            return None
        dropped = 0
        if len(data) % 4:
            dropped = 1
            data = data[: len(data) - (len(data) % 4)]
        if not data:
            return None
        self.chunk_index += 1
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=np.frombuffer(data, dtype=np.float32).copy(),
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=None,
            source_end_time=None,
            monotonic_available_time_ns=time.monotonic_ns(),
            wall_available_time_ns=time.time_ns(),
            dropped_chunks=dropped,
        )

    async def stop(self) -> None:
        self.eof = True
        stop_processes(self.procs)

    async def close(self) -> None:
        await self.stop()
        self.reader = None
        self.procs = []


class Enigma2AudioSource:
    """Live audio from an Enigma2 receiver over the LAN.

    Uses Enigma2's own service stream rather than any bespoke relay: the receiver
    already serves the live MPEG-TS, so nothing new has to run on the decoder.
    ffmpeg demuxes it, throws the video away without decoding it (`-vn`), decodes
    only the chosen audio track and hands back canonical 16 kHz mono.

    Service tracking is what makes this more than a URL reader. OpenWebif is
    polled for the current service reference; when it changes, the stream is torn
    down and rebuilt, and the caller is told so it can reset the ASR context. A
    word from the previous channel must never appear over the new one.
    """

    source_type = "enigma2"

    def __init__(
        self,
        *,
        host: str,
        chunk_ms: int,
        openwebif_scheme: str = "http",
        openwebif_port: int = 80,
        stream_port: int = 8001,
        stream_scheme: str = "http",
        service_ref: str = "",
        preferred_audio_languages: list[str] | None = None,
        audio_track_index: int | None = None,
        username: str = "",
        password: str = "",
        service_poll_ms: int = 500,
        reconnect_initial_ms: int = 250,
        reconnect_max_ms: int = 5000,
        probe_timeout_sec: float = 10.0,
        probe_size_bytes: int = 500_000,
        analyze_duration_us: int = 500_000,
    ) -> None:
        self.host = host
        self.chunk_ms = chunk_ms
        self.openwebif_scheme = openwebif_scheme
        self.openwebif_port = openwebif_port
        self.stream_port = stream_port
        self.stream_scheme = stream_scheme
        self.configured_service_ref = service_ref
        self.preferred_audio_languages = [
            lang.lower() for lang in (preferred_audio_languages or ["ita", "it"])
        ]
        self.audio_track_index = audio_track_index
        self.username = username
        self.password = password
        self.service_poll_ms = service_poll_ms
        self.reconnect_initial_ms = reconnect_initial_ms
        self.reconnect_max_ms = reconnect_max_ms
        self.probe_timeout_sec = probe_timeout_sec
        self.probe_size_bytes = probe_size_bytes
        self.analyze_duration_us = analyze_duration_us

        self.bytes_per_chunk = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000) * 4
        self.procs: list[subprocess.Popen] = []
        self.reader: Any | None = None
        self.chunk_index = 0
        self.eof = False

        self.service_ref = service_ref
        self.service_name = ""
        self.source_epoch = 0
        self.selected_track: dict[str, Any] | None = None
        self.track_policy = "not selected"
        self.stream_connects = 0
        self.reconnects = 0
        self.service_changes = 0
        self.probe_failures = 0
        self.read_failures = 0
        self.pending_source_change: dict[str, Any] | None = None
        self._last_service_poll = 0.0
        self._backoff_ms = reconnect_initial_ms
        self._poll_task: asyncio.Task | None = None
        self._stopping = False
        self._pending_change_lock = False

    # ---- OpenWebif -------------------------------------------------------

    @property
    def openwebif_base(self) -> str:
        return f"{self.openwebif_scheme}://{self.host}:{self.openwebif_port}"

    def _auth_handler(self) -> Any:
        if not self.username:
            return None
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.openwebif_base, self.username, self.password)
        return urllib.request.HTTPBasicAuthHandler(manager)

    def openwebif_json(self, path: str, timeout: float = 5.0) -> dict[str, Any] | None:
        url = f"{self.openwebif_base}{path}"
        handler = self._auth_handler()
        opener = urllib.request.build_opener(*(h for h in (handler,) if h))
        try:
            with opener.open(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            self.probe_failures += 1
            return None

    def current_service(self) -> tuple[str, str]:
        """Return (service_reference, service_name) as the receiver reports them."""
        payload = self.openwebif_json("/api/getcurrent")
        if not payload:
            return "", ""
        info = payload.get("info") or {}
        return str(info.get("sref") or ""), str(info.get("name") or "")

    def stream_url(self, service_ref: str) -> str:
        # Enigma2 serves the live service as <scheme>://host:port/<service reference>.
        credentials = ""
        if self.username:
            credentials = f"{urllib.parse.quote(self.username)}:{urllib.parse.quote(self.password)}@"
        return f"{self.stream_scheme}://{credentials}{self.host}:{self.stream_port}/{service_ref}"

    # ---- audio track selection -------------------------------------------

    def probe_audio_tracks(self, url: str) -> list[dict[str, Any]]:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index,codec_name,channels,sample_rate:stream_tags=language,title",
                 "-probesize", str(self.probe_size_bytes),
                 "-analyzeduration", str(self.analyze_duration_us),
                 "-of", "json", url],
                capture_output=True, text=True, timeout=self.probe_timeout_sec,
            )
        except (OSError, subprocess.SubprocessError):
            self.probe_failures += 1
            return []
        if result.returncode != 0:
            self.probe_failures += 1
            return []
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except json.JSONDecodeError:
            self.probe_failures += 1
            return []
        tracks = []
        for order, stream in enumerate(streams):
            tags = stream.get("tags") or {}
            tracks.append({
                "audio_order": order,
                "stream_index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
                "language": str(tags.get("language", "")).lower(),
                "title": str(tags.get("title", "")),
            })
        return tracks

    def select_audio_track(self, tracks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        """Pick a track by policy. A PID is never hardcoded.

        Audio-description tracks are skipped while a normal programme track in the
        preferred language exists, because they narrate the picture rather than
        carry the dialogue.
        """
        if not tracks:
            return None, "no audio track found"
        if self.audio_track_index is not None:
            for track in tracks:
                if track["audio_order"] == self.audio_track_index:
                    return track, f"configured audio_track_index={self.audio_track_index}"
            return tracks[0], f"configured audio_track_index={self.audio_track_index} not present; first track"

        def is_description(track: dict[str, Any]) -> bool:
            title = track["title"].lower()
            return any(marker in title for marker in ("audio description", "descriz", "ad)", "visually"))

        for language in self.preferred_audio_languages:
            matching = [t for t in tracks if t["language"].startswith(language)]
            normal = [t for t in matching if not is_description(t)]
            if normal:
                return normal[0], f"preferred language '{language}'"
            if matching:
                return matching[0], f"preferred language '{language}' (only description track available)"
        normal = [t for t in tracks if not is_description(t)]
        if normal:
            return normal[0], "first non-description programme track"
        return tracks[0], "first available track"

    # ---- streaming -------------------------------------------------------

    def _spawn(self, url: str, track: dict[str, Any] | None) -> None:
        # -vn discards video at the demuxer: the picture is never decoded here.
        mapping = ["-vn"]
        if track is not None:
            mapping += ["-map", f"0:a:{track['audio_order']}"]
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-fflags", "+nobuffer", "-flags", "low_delay",
            # A live TS trickles in at broadcast rate. ffmpeg's default 5 MB /
            # 5 s probe would stall startup for tens of seconds before the first
            # PCM sample appears, so the probe window is bounded explicitly.
            "-probesize", str(self.probe_size_bytes),
            "-analyzeduration", str(self.analyze_duration_us),
            "-i", url, *mapping,
            "-ac", str(CANONICAL_CHANNELS),
            "-ar", str(CANONICAL_SAMPLE_RATE),
            "-f", "f32le", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.procs = [proc]
        self.reader = proc.stdout
        if self.reader is not None:
            os.set_blocking(self.reader.fileno(), False)
        self.stream_connects += 1

    def _teardown(self) -> None:
        stop_processes(self.procs)
        self.procs = []
        self.reader = None

    def open(self) -> None:
        service_ref, service_name = ("", "")
        if not self.configured_service_ref:
            service_ref, service_name = self.current_service()
        self.service_ref = self.configured_service_ref or service_ref
        self.service_name = service_name
        if not self.service_ref:
            raise RuntimeError(
                "Enigma2AudioSource could not determine a service reference; "
                "set 'service_ref' in the config or check OpenWebif reachability"
            )
        url = self.stream_url(self.service_ref)
        tracks = self.probe_audio_tracks(url)
        self.selected_track, self.track_policy = self.select_audio_track(tracks)
        self._spawn(url, self.selected_track)

    async def start(self) -> None:
        if self.reader is None:
            raise RuntimeError("Enigma2AudioSource.open() was not called")
        self._last_service_poll = time.monotonic()
        if not self.configured_service_ref:
            self._poll_task = asyncio.create_task(
                self._service_poll_loop(), name="enigma2-service-poll"
            )

    async def _service_poll_loop(self) -> None:
        """Watch for a zap in the background.

        Deliberately not inline in read_chunk: OpenWebif is a blocking HTTP call
        over the LAN, and a receiver that stops answering would otherwise stall
        the audio path for the whole timeout.
        """
        try:
            while not self._stopping:
                await asyncio.sleep(self.service_poll_ms / 1000.0)
                try:
                    service_ref, service_name = await asyncio.to_thread(self.current_service)
                except Exception:
                    self.probe_failures += 1
                    continue
                self._apply_service_change(service_ref, service_name)
        except asyncio.CancelledError:
            pass

    def _apply_service_change(self, service_ref: str, service_name: str) -> None:
        if service_ref and service_ref != self.service_ref:
            detected_ns = time.monotonic_ns()
            previous = self.service_ref
            self.service_ref = service_ref
            self.service_name = service_name
            self.service_changes += 1
            self.source_epoch += 1
            # Tear the old stream down before anything else: every byte still in
            # those buffers belongs to the previous channel.
            self._teardown()
            self._pending_change_lock = True
            self.pending_source_change = {
                "previous_service_ref": previous,
                "service_ref": service_ref,
                "service_name": service_name,
                "source_epoch": self.source_epoch,
                "detected_monotonic_ns": detected_ns,
                "audio_resumed_monotonic_ns": None,
            }

    def _reconnect(self) -> None:
        url = self.stream_url(self.service_ref)
        tracks = self.probe_audio_tracks(url)
        self.selected_track, self.track_policy = self.select_audio_track(tracks)
        self._spawn(url, self.selected_track)

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if self.reader is None:
            if self.eof:
                return None
            await asyncio.sleep(self._backoff_ms / 1000.0)
            try:
                await asyncio.to_thread(self._reconnect)
                self._backoff_ms = self.reconnect_initial_ms
                self.reconnects += 1
            except Exception:
                self._backoff_ms = min(self._backoff_ms * 2, self.reconnect_max_ms)
                return None
        data = await asyncio.to_thread(read_pcm_chunk, self.reader, self.bytes_per_chunk)
        if data is None:
            return None
        if data == b"":
            # The stream ended or the receiver went away: drop it and let the
            # next call reconnect. A stalled source must not feed stale audio.
            self.read_failures += 1
            self._teardown()
            return None
        dropped = 0
        if len(data) % 4:
            dropped = 1
            data = data[: len(data) - (len(data) % 4)]
        if not data:
            return None
        self.chunk_index += 1
        if self.pending_source_change and self.pending_source_change["audio_resumed_monotonic_ns"] is None:
            self.pending_source_change["audio_resumed_monotonic_ns"] = time.monotonic_ns()
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=np.frombuffer(data, dtype=np.float32).copy(),
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=None,
            source_end_time=None,
            monotonic_available_time_ns=time.monotonic_ns(),
            wall_available_time_ns=time.time_ns(),
            dropped_chunks=dropped,
        )

    def take_source_change(self) -> dict[str, Any] | None:
        """Hand the pending change to the pipeline exactly once."""
        change, self.pending_source_change = self.pending_source_change, None
        return change

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.source_type,
            "host": self.host,
            "service_ref": self.service_ref,
            "service_name": self.service_name,
            "source_epoch": self.source_epoch,
            "selected_track": self.selected_track,
            "track_policy": self.track_policy,
            "stream_connects": self.stream_connects,
            "reconnects": self.reconnects,
            "service_changes": self.service_changes,
            "probe_failures": self.probe_failures,
            "read_failures": self.read_failures,
            "chunks": self.chunk_index,
        }

    async def stop(self) -> None:
        self.eof = True
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        self._teardown()

    async def close(self) -> None:
        await self.stop()


class DesktopMediaAudioSource:
    """Programme audio for the desktop player, taken ahead of the presentation.

    The player presents picture and sound delayed by D so that a subtitle, which
    can only exist after the words have been spoken and stabilised, arrives in
    time to be shown with the scene it belongs to. For that to work the ASR must
    read the file at a position D *ahead* of what the viewer is watching, so this
    source decodes the media itself rather than tapping the presentation path.

    It follows the player the same way Enigma2AudioSource follows a receiver: a
    small control endpoint is polled in the background, and a change of epoch --
    a seek, an audio-track change, a new file -- tears the decoder down and
    rebuilds it, handing the pipeline a source change so the ASR and C_tail1
    contexts are reset. No word from before a seek can survive it.
    """

    source_type = "desktop_media"

    def __init__(
        self,
        *,
        chunk_ms: int,
        media: str = "",
        control_url: str = "",
        audio_track: int = 0,
        start_position_ms: float = 0.0,
        control_poll_ms: int = 150,
        realtime_pacing: bool = True,
        root: Path | None = None,
    ) -> None:
        self.chunk_ms = chunk_ms
        self.media = str(resolve_path(media, root=root)) if media and root else media
        self.control_url = control_url
        self.audio_track = audio_track
        self.start_position_ms = start_position_ms
        self.control_poll_ms = control_poll_ms
        self.realtime_pacing = realtime_pacing

        self.bytes_per_chunk = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000) * 4
        self.procs: list[subprocess.Popen] = []
        self.reader: Any | None = None
        self.chunk_index = 0
        self.eof = False

        self.epoch = 0
        self.state = "playing"
        self.position_ms = start_position_ms
        self.decoder_starts = 0
        self.control_failures = 0
        self.samples_emitted = 0
        self.pending_source_change: dict[str, Any] | None = None

        self._poll_task: asyncio.Task | None = None
        self._stopping = False
        self._decoder_start_ms = 0.0

    # ---- control ---------------------------------------------------------

    def read_control(self) -> dict[str, Any] | None:
        if not self.control_url:
            return None
        try:
            with urllib.request.urlopen(self.control_url, timeout=2.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            self.control_failures += 1
            return None

    async def _control_loop(self) -> None:
        """Poll off the audio path: a stalled player must not stall decoding."""
        try:
            while not self._stopping:
                await asyncio.sleep(self.control_poll_ms / 1000.0)
                payload = await asyncio.to_thread(self.read_control)
                if payload:
                    self._apply_control(payload)
        except asyncio.CancelledError:
            pass

    def _apply_control(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state", self.state))
        if state != self.state:
            self.state = state
        epoch = payload.get("epoch")
        if not isinstance(epoch, int) or epoch == self.epoch:
            return
        detected_ns = time.monotonic_ns()
        previous_epoch = self.epoch
        self.epoch = epoch
        self.media = str(payload.get("media", self.media))
        self.audio_track = int(payload.get("audio_track", self.audio_track))
        self.position_ms = float(payload.get("asr_position_ms", 0.0))
        # Everything still in the decoder belongs to where we were, not where we
        # are going.
        self._teardown()
        self.pending_source_change = {
            "reason": str(payload.get("reason", "seek")),
            "previous_epoch": previous_epoch,
            "source_epoch": epoch,
            "service_ref": f"{self.media}#t={self.position_ms / 1000.0:.3f}",
            "service_name": Path(self.media).name if self.media else "",
            "media": self.media,
            "audio_track": self.audio_track,
            "position_ms": self.position_ms,
            "detected_monotonic_ns": detected_ns,
            "audio_resumed_monotonic_ns": None,
        }

    # ---- decoding --------------------------------------------------------

    def probe_audio_tracks(self, media: str = "") -> list[dict[str, Any]]:
        path = media or self.media
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries",
                 "stream=index,codec_name,channels,sample_rate,channel_layout:"
                 "stream_tags=language,title:stream_disposition=default",
                 "-of", "json", path],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except json.JSONDecodeError:
            return []
        tracks = []
        for order, stream in enumerate(streams):
            tags = stream.get("tags") or {}
            disposition = stream.get("disposition") or {}
            tracks.append({
                "audio_order": order,
                "stream_index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "sample_rate": stream.get("sample_rate"),
                "language": str(tags.get("language", "")).lower(),
                "title": str(tags.get("title", "")),
                "default": bool(disposition.get("default")),
            })
        return tracks

    def _spawn(self) -> None:
        if not self.media or not Path(self.media).exists():
            raise FileNotFoundError(f"media not found: {self.media}")
        # Seek to where the player is *now*, not to where it was when the epoch
        # changed. Starting a decoder takes time, and because reading is paced at
        # playback speed that time is never made up: after a pause it left the
        # recogniser permanently behind the picture by the restart latency.
        if self.control_url:
            latest = self.read_control()
            if latest and int(latest.get("epoch", self.epoch)) == self.epoch:
                position = latest.get("asr_position_ms")
                if isinstance(position, (int, float)):
                    self.position_ms = float(position)
                    self.samples_emitted = 0
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.realtime_pacing:
            # Consume at playback speed. Without this the ASR would race to the
            # end of the file instead of tracking the viewer.
            cmd += ["-re"]
        if self.position_ms > 0:
            cmd += ["-ss", f"{self.position_ms / 1000.0:.3f}"]
        cmd += [
            "-i", self.media,
            "-vn", "-map", f"0:a:{self.audio_track}",
            "-ac", str(CANONICAL_CHANNELS), "-ar", str(CANONICAL_SAMPLE_RATE),
            "-f", "f32le", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.procs = [proc]
        self.reader = proc.stdout
        if self.reader is not None:
            os.set_blocking(self.reader.fileno(), False)
        self.decoder_starts += 1
        self._decoder_start_ms = time.monotonic() * 1000.0

    def _teardown(self) -> None:
        stop_processes(self.procs)
        self.procs = []
        self.reader = None

    def open(self) -> None:
        payload = self.read_control()
        if payload:
            self.epoch = int(payload.get("epoch", 0))
            self.state = str(payload.get("state", "playing"))
            self.media = str(payload.get("media", self.media))
            self.audio_track = int(payload.get("audio_track", self.audio_track))
            self.position_ms = float(payload.get("asr_position_ms", self.start_position_ms))
        self._spawn()

    async def start(self) -> None:
        if self.control_url:
            self._poll_task = asyncio.create_task(self._control_loop(), name="desktop-control")

    @property
    def asr_position_ms(self) -> float:
        """Where in the programme the audio handed to the ASR has reached."""
        return self.position_ms + (self.samples_emitted / CANONICAL_SAMPLE_RATE) * 1000.0

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if self.state in {"paused", "stopped"}:
            # Stop pulling. ffmpeg blocks on a full pipe, so look-ahead is bounded
            # by the pipe buffer rather than growing for as long as the pause lasts.
            await asyncio.sleep(0.05)
            return None
        if self.reader is None:
            if self._stopping:
                return None
            try:
                await asyncio.to_thread(self._spawn)
            except Exception:
                await asyncio.sleep(0.2)
                return None
        data = await asyncio.to_thread(read_pcm_chunk, self.reader, self.bytes_per_chunk)
        if data is None:
            return None
        if data == b"":
            self.eof = True
            self._teardown()
            return None
        dropped = 0
        if len(data) % 4:
            dropped = 1
            data = data[: len(data) - (len(data) % 4)]
        if not data:
            return None
        self.chunk_index += 1
        samples = np.frombuffer(data, dtype=np.float32).copy()
        self.samples_emitted += len(samples)
        if self.pending_source_change and self.pending_source_change["audio_resumed_monotonic_ns"] is None:
            self.pending_source_change["audio_resumed_monotonic_ns"] = time.monotonic_ns()
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=samples,
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=None,
            source_end_time=None,
            monotonic_available_time_ns=time.monotonic_ns(),
            wall_available_time_ns=time.time_ns(),
            dropped_chunks=dropped,
        )

    def take_source_change(self) -> dict[str, Any] | None:
        change, self.pending_source_change = self.pending_source_change, None
        if change is not None:
            self.samples_emitted = 0
            self.eof = False
        return change

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.source_type,
            "media": self.media,
            "audio_track": self.audio_track,
            "epoch": self.epoch,
            "state": self.state,
            "asr_position_ms": self.asr_position_ms,
            "decoder_starts": self.decoder_starts,
            "control_failures": self.control_failures,
            "chunks": self.chunk_index,
        }

    async def stop(self) -> None:
        self.eof = True
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        self._teardown()

    async def close(self) -> None:
        await self.stop()


class FakeAudioSource:
    source_type = "fake"

    def __init__(self, frames: list[CanonicalAudioFrame] | None = None) -> None:
        self.frames = list(frames or [])
        self.eof = False

    def open(self) -> None:
        pass

    async def start(self) -> None:
        pass

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if not self.frames:
            self.eof = True
            return None
        return self.frames.pop(0)

    async def stop(self) -> None:
        self.eof = True

    async def close(self) -> None:
        self.frames = []


class UdpPcm16leAudioSource:
    source_type = "udp"

    def __init__(
        self,
        *,
        listen_address: str,
        listen_port: int,
        audio_format: str,
        sample_rate: int,
        channels: int,
        chunk_ms: int,
        source_timeout_ms: int = 1000,
        max_datagram_bytes: int = 65535,
        rx_buffer_bytes: int = 1048576,
    ) -> None:
        if audio_format != "pcm_s16le":
            raise BackendConfigError("audio source 'udp' currently supports only audio_format='pcm_s16le'")
        if sample_rate != CANONICAL_SAMPLE_RATE:
            raise BackendConfigError("audio source 'udp' currently requires sample_rate=16000")
        if channels != CANONICAL_CHANNELS:
            raise BackendConfigError("audio source 'udp' currently requires channels=1")
        if not (0 <= listen_port <= 65535):
            raise BackendConfigError("audio source 'udp' listen_port must be between 0 and 65535")
        self.listen_address = listen_address
        self.listen_port = listen_port
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_ms = chunk_ms
        self.source_timeout_ms = source_timeout_ms
        self.max_datagram_bytes = max_datagram_bytes
        self.rx_buffer_bytes = rx_buffer_bytes
        self.samples_per_chunk = int(CANONICAL_SAMPLE_RATE * chunk_ms / 1000)
        self.sock: socket.socket | None = None
        self.buffer = np.empty(0, dtype=np.float32)
        self.chunk_index = 0
        self.total_samples = 0
        self.eof = False
        self.last_packet_mono_ns: int | None = None
        self._timed_out_reported = False
        self.state = "NO_PACKETS"
        self.metrics = {
            "rx_packets": 0,
            "rx_bytes": 0,
            "rx_truncated": 0,
            "rx_errors": 0,
            "packet_size_min": None,
            "packet_size_max": None,
            "rx_gap_last_us": None,
            "rx_gap_min_us": None,
            "rx_gap_max_us": None,
            "rx_gap_mean_us": None,
            "queue_depth_samples": 0,
            "max_queue_depth_samples": 0,
            "inserted_silence_samples": 0,
            "source_timeouts": 0,
            "source_restarts": 0,
            "partial_samples_dropped_on_timeout": 0,
        }
        self._gap_sum_us = 0.0
        self._gap_count = 0

    def open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rx_buffer_bytes)
        sock.bind((self.listen_address, self.listen_port))
        sock.setblocking(False)
        self.sock = sock
        self.listen_port = int(sock.getsockname()[1])
        self.effective_rx_buffer_bytes = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

    async def start(self) -> None:
        if self.sock is None:
            raise RuntimeError("UdpPcm16leAudioSource.open() was not called")

    async def read_chunk(self) -> CanonicalAudioFrame | None:
        if self.sock is None:
            raise RuntimeError("UdpPcm16leAudioSource.open() was not called")
        while len(self.buffer) < self.samples_per_chunk:
            data = await asyncio.to_thread(self._recv_datagram)
            if data is None:
                if self._timed_out():
                    self._mark_source_timeout()
                return None
            samples = self._decode_datagram(data)
            if samples is None:
                continue
            self.buffer = np.concatenate((self.buffer, samples))
            self.metrics["queue_depth_samples"] = int(len(self.buffer))
            self.metrics["max_queue_depth_samples"] = max(
                int(self.metrics["max_queue_depth_samples"]),
                int(len(self.buffer)),
            )
        now_ns = time.monotonic_ns()
        chunk = self.buffer[: self.samples_per_chunk].copy()
        self.buffer = self.buffer[self.samples_per_chunk :].copy()
        start_sample = self.total_samples
        self.total_samples += len(chunk)
        self.chunk_index += 1
        self.metrics["queue_depth_samples"] = int(len(self.buffer))
        return CanonicalAudioFrame(
            seq=self.chunk_index,
            samples=chunk,
            sample_rate=CANONICAL_SAMPLE_RATE,
            channels=CANONICAL_CHANNELS,
            source_start_time=start_sample / CANONICAL_SAMPLE_RATE,
            source_end_time=self.total_samples / CANONICAL_SAMPLE_RATE,
            monotonic_available_time_ns=now_ns,
            wall_available_time_ns=time.time_ns(),
        )

    async def stop(self) -> None:
        self.eof = True

    async def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self.eof = True
        self.buffer = np.empty(0, dtype=np.float32)

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.source_type,
            "state": self.state,
            "listen_address": self.listen_address,
            "listen_port": self.listen_port,
            "audio_format": self.audio_format,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "effective_rx_buffer_bytes": getattr(self, "effective_rx_buffer_bytes", None),
            **self.metrics,
        }

    def _recv_datagram(self) -> bytes | None:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], 0.05)
        if not ready:
            return None
        try:
            data, _ancillary, flags, _addr = self.sock.recvmsg(self.max_datagram_bytes)
        except BlockingIOError:
            return None
        except OSError:
            self.metrics["rx_errors"] = int(self.metrics["rx_errors"]) + 1
            return None
        now_ns = time.monotonic_ns()
        if flags & getattr(socket, "MSG_TRUNC", 0):
            self.metrics["rx_truncated"] = int(self.metrics["rx_truncated"]) + 1
            self.state = "ERROR"
            return None
        self._record_packet(len(data), now_ns)
        if self._timed_out_reported:
            self.metrics["source_restarts"] = int(self.metrics["source_restarts"]) + 1
            self._timed_out_reported = False
        self.state = "CONNECTED"
        return data

    def _decode_datagram(self, data: bytes) -> np.ndarray | None:
        if len(data) % 2:
            self.metrics["rx_errors"] = int(self.metrics["rx_errors"]) + 1
            self.state = "ERROR"
            return None
        return (np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0).copy()

    def _record_packet(self, size: int, now_ns: int) -> None:
        self.metrics["rx_packets"] = int(self.metrics["rx_packets"]) + 1
        self.metrics["rx_bytes"] = int(self.metrics["rx_bytes"]) + size
        current_min = self.metrics["packet_size_min"]
        current_max = self.metrics["packet_size_max"]
        self.metrics["packet_size_min"] = size if current_min is None else min(int(current_min), size)
        self.metrics["packet_size_max"] = size if current_max is None else max(int(current_max), size)
        if self.last_packet_mono_ns is not None:
            gap_us = (now_ns - self.last_packet_mono_ns) / 1000.0
            self.metrics["rx_gap_last_us"] = gap_us
            current_gap_min = self.metrics["rx_gap_min_us"]
            current_gap_max = self.metrics["rx_gap_max_us"]
            self.metrics["rx_gap_min_us"] = gap_us if current_gap_min is None else min(float(current_gap_min), gap_us)
            self.metrics["rx_gap_max_us"] = gap_us if current_gap_max is None else max(float(current_gap_max), gap_us)
            self._gap_sum_us += gap_us
            self._gap_count += 1
            self.metrics["rx_gap_mean_us"] = self._gap_sum_us / self._gap_count
        self.last_packet_mono_ns = now_ns

    def _timed_out(self) -> bool:
        if self.source_timeout_ms <= 0:
            return False
        if self.last_packet_mono_ns is None:
            return False
        return (time.monotonic_ns() - self.last_packet_mono_ns) / 1_000_000.0 >= self.source_timeout_ms

    def _mark_source_timeout(self) -> None:
        if self._timed_out_reported:
            return
        self.state = "NO_PACKETS"
        self.metrics["source_timeouts"] = int(self.metrics["source_timeouts"]) + 1
        if len(self.buffer):
            self.metrics["partial_samples_dropped_on_timeout"] = (
                int(self.metrics["partial_samples_dropped_on_timeout"]) + int(len(self.buffer))
            )
            self.buffer = np.empty(0, dtype=np.float32)
            self.metrics["queue_depth_samples"] = 0
        self._timed_out_reported = True


def stop_processes(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


class AudioSourceRegistry:
    _registry: dict[str, Callable[..., AudioSource]] = {}

    @classmethod
    def register(cls, type_name: str, factory: Callable[..., AudioSource]) -> None:
        cls._registry[type_name] = factory

    @classmethod
    def available_types(cls) -> list[str]:
        return sorted(cls._registry)

    @classmethod
    def create(cls, type_name: str, options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
        if type_name not in cls._registry:
            available = ", ".join(cls.available_types()) or "(none)"
            raise BackendConfigError(f"unknown audio source type '{type_name}'; available: {available}")
        if not isinstance(options, dict):
            raise BackendConfigError(f"audio source '{type_name}' options must be an object")
        return cls._registry[type_name](options, root=root, chunk_ms=chunk_ms, mode=mode)


def _create_wav(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    unknown = sorted(set(options) - {"path"})
    if unknown:
        raise BackendConfigError(f"audio source 'wav' unknown option(s): {', '.join(unknown)}")
    return WavAudioSource(path=str(options.get("path", "")), chunk_ms=chunk_ms, mode=mode, root=root)


def _create_pipewire(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    del root, mode
    unknown = sorted(set(options) - {"target", "capture_latency"})
    if unknown:
        raise BackendConfigError(f"audio source 'pipewire' unknown option(s): {', '.join(unknown)}")
    return PipeWireAudioSource(
        target=str(options.get("target", "")),
        capture_latency=str(options.get("capture_latency", "20ms")),
        chunk_ms=chunk_ms,
    )


def _create_udp(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    del root, mode
    unknown = sorted(
        set(options)
        - {
            "listen_address",
            "listen_port",
            "audio_format",
            "sample_rate",
            "channels",
            "source_timeout_ms",
            "max_datagram_bytes",
            "rx_buffer_bytes",
        }
    )
    if unknown:
        raise BackendConfigError(f"audio source 'udp' unknown option(s): {', '.join(unknown)}")
    return UdpPcm16leAudioSource(
        listen_address=str(options.get("listen_address", "0.0.0.0")),
        listen_port=int(options.get("listen_port", 0)),
        audio_format=str(options.get("audio_format", "pcm_s16le")),
        sample_rate=int(options.get("sample_rate", CANONICAL_SAMPLE_RATE)),
        channels=int(options.get("channels", CANONICAL_CHANNELS)),
        chunk_ms=chunk_ms,
        source_timeout_ms=int(options.get("source_timeout_ms", 1000)),
        max_datagram_bytes=int(options.get("max_datagram_bytes", 65535)),
        rx_buffer_bytes=int(options.get("rx_buffer_bytes", 1048576)),
    )


AudioSourceRegistry.register("wav", _create_wav)
AudioSourceRegistry.register("pipewire", _create_pipewire)
def _create_enigma2(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    del root, mode
    known = {
        "host", "openwebif_scheme", "openwebif_port", "stream_port", "stream_scheme",
        "service_ref", "preferred_audio_languages", "audio_track_index",
        "username_env", "password_env", "service_poll_ms",
        "reconnect_initial_ms", "reconnect_max_ms", "probe_timeout_sec",
        "probe_size_bytes", "analyze_duration_us",
    }
    unknown = sorted(set(options) - known)
    if unknown:
        raise BackendConfigError(f"audio source 'enigma2' unknown option(s): {', '.join(unknown)}")
    host = str(options.get("host", "")).strip()
    if not host:
        raise BackendConfigError("audio source 'enigma2' requires a non-empty host")
    languages = options.get("preferred_audio_languages", ["ita", "it"])
    if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
        raise BackendConfigError("audio source 'enigma2' preferred_audio_languages must be a list of strings")
    track_index = options.get("audio_track_index")
    if track_index is not None and not isinstance(track_index, int):
        raise BackendConfigError("audio source 'enigma2' audio_track_index must be an integer")
    # Credentials are read from the environment, never from the config file.
    username = os.environ.get(str(options.get("username_env", "HEARABLE_E2_USERNAME")), "")
    password = os.environ.get(str(options.get("password_env", "HEARABLE_E2_PASSWORD")), "")
    return Enigma2AudioSource(
        host=host,
        chunk_ms=chunk_ms,
        openwebif_scheme=str(options.get("openwebif_scheme", "http")),
        openwebif_port=int(options.get("openwebif_port", 80)),
        stream_port=int(options.get("stream_port", 8001)),
        stream_scheme=str(options.get("stream_scheme", "http")),
        service_ref=str(options.get("service_ref", "")),
        preferred_audio_languages=list(languages),
        audio_track_index=track_index,
        username=username,
        password=password,
        service_poll_ms=int(options.get("service_poll_ms", 500)),
        reconnect_initial_ms=int(options.get("reconnect_initial_ms", 250)),
        reconnect_max_ms=int(options.get("reconnect_max_ms", 5000)),
        probe_timeout_sec=float(options.get("probe_timeout_sec", 10.0)),
        probe_size_bytes=int(options.get("probe_size_bytes", 500_000)),
        analyze_duration_us=int(options.get("analyze_duration_us", 500_000)),
    )


AudioSourceRegistry.register("udp", _create_udp)
def _create_desktop_media(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    del mode
    known = {"media", "control_url", "audio_track", "start_position_ms",
             "control_poll_ms", "realtime_pacing"}
    unknown = sorted(set(options) - known)
    if unknown:
        raise BackendConfigError(f"audio source 'desktop_media' unknown option(s): {', '.join(unknown)}")
    if not options.get("media") and not options.get("control_url"):
        raise BackendConfigError(
            "audio source 'desktop_media' requires 'media' or 'control_url'")
    return DesktopMediaAudioSource(
        chunk_ms=chunk_ms,
        media=str(options.get("media", "")),
        control_url=str(options.get("control_url", "")),
        audio_track=int(options.get("audio_track", 0)),
        start_position_ms=float(options.get("start_position_ms", 0.0)),
        control_poll_ms=int(options.get("control_poll_ms", 150)),
        realtime_pacing=bool(options.get("realtime_pacing", True)),
        root=root,
    )



AudioSourceRegistry.register("enigma2", _create_enigma2)
def _create_sf8008_relay(options: dict[str, Any], *, root: Path, chunk_ms: int, mode: str) -> AudioSource:
    """The decoder's own pre-presentation audio relay.

    Where `sf8008_usb` needs a local copy of the media and recovers the position
    by matching a frame off the decoder's screen, this needs neither: the
    decoder sends the selected audio track, compressed, from ahead of where the
    viewer is, and says where that is over a small control endpoint.

    Imported lazily for the same reason as its sibling — the receiver's
    particulars stay out of the module HearAble Desktop depends on.
    """
    del root, mode
    from hearable.sf8008.relay_source import SF8008AudioRelaySource

    unknown = sorted(set(options) - {"host", "control_port", "receiver_port",
                                     "receiver_host", "queue_chunks", "control_path",
                                     "lead_ms"})
    if unknown:
        raise BackendConfigError(f"audio source 'sf8008_relay' unknown option(s): {', '.join(unknown)}")
    host = str(options.get("host", "")).strip()
    if not host:
        raise BackendConfigError("audio source 'sf8008_relay' requires a 'host'")
    return SF8008AudioRelaySource(
        chunk_ms=chunk_ms,
        host=host,
        control_port=int(options.get("control_port", 8770)),
        receiver_port=int(options.get("receiver_port", 9010)),
        receiver_host=str(options.get("receiver_host", "")),
        queue_chunks=int(options.get("queue_chunks", 8)),
        control_path=str(options.get("control_path", "/control")),
        lead_ms=(int(options["lead_ms"]) if options.get("lead_ms") is not None else None),
    )


AudioSourceRegistry.register("sf8008_relay", _create_sf8008_relay)
AudioSourceRegistry.register("desktop_media", _create_desktop_media)
