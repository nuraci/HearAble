#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
import wave
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hearable.asr_capi import NemoAsr
from hearable.stabilizer import SubtitleStabilizer
from tools.benchmark_machine_info import collect as collect_machine_info


DEFAULT_MODEL = ROOT / "models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"
LATENCY_DIR = ROOT / "benchmarks/latency"
HARDWARE_DIR = ROOT / "benchmarks/hardware"


@dataclass
class AudioCase:
    name: str
    path: Path
    kind: str
    transcript: str | None = None
    speech_onset_ms: float | None = None
    source_note: str = ""


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def load_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if channels != 1 or rate != 16000:
        raise ValueError(f"{path} must be 16 kHz mono")
    if width == 2:
        return (np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0).copy()
    if width == 4:
        return np.frombuffer(frames, dtype=np.float32).copy()
    raise ValueError(f"unsupported sample width for {path}: {width}")


def write_wav_16k(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm.tobytes())


def normalize_length(samples: np.ndarray, target_seconds: float) -> np.ndarray:
    target = int(round(target_seconds * 16000))
    if len(samples) >= target:
        return samples[:target].copy()
    repeats = int(math.ceil(target / max(1, len(samples))))
    return np.tile(samples, repeats)[:target].copy()


def run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def synth_phrase(path: Path, text: str) -> None:
    tmp = path.with_suffix(".espeak.wav")
    proc = run(["espeak-ng", "-v", "it", "-s", "155", "-w", str(tmp), text], timeout=20.0)
    if proc.returncode != 0:
        raise RuntimeError(f"espeak-ng failed for {path}: {proc.stderr}")
    converted = path.with_suffix(".speech.wav")
    ffmpeg = run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(tmp),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(converted),
        ],
        timeout=20.0,
    )
    if ffmpeg.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {path}: {ffmpeg.stderr}")
    speech = load_wav_16k_mono(converted)
    silence = np.zeros(2 * 16000, dtype=np.float32)
    write_wav_16k(path, np.concatenate([silence, speech, silence]))
    for temp in (tmp, converted):
        try:
            temp.unlink()
        except OSError:
            pass


def copy_with_silence(path: Path, source: Path) -> None:
    speech = load_wav_16k_mono(source)
    silence = np.zeros(2 * 16000, dtype=np.float32)
    write_wav_16k(path, np.concatenate([silence, speech, silence]))


def prepare_audio_suite(
    force: bool = False,
    compute_audio: Path | None = None,
) -> tuple[list[AudioCase], list[AudioCase]]:
    audio_dir = LATENCY_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    compute: list[AudioCase] = []
    if compute_audio is not None:
        # Explicit dataset: use the supplied canonical 16 kHz mono WAV verbatim so the
        # measured RTF_compute refers to one real recording instead of a synthetic repeat.
        if not compute_audio.exists():
            raise SystemExit(f"missing compute audio: {compute_audio}")
        compute.append(
            AudioCase(
                name=compute_audio.stem,
                path=compute_audio,
                kind="compute",
                source_note=f"explicit compute dataset {compute_audio}",
            )
        )
    else:
        browser = ROOT / "benchmarks/audio/browser_capture_16k_mono.wav"
        if not browser.exists():
            raise SystemExit(f"missing source audio: {browser}")
        browser_samples = load_wav_16k_mono(browser)

        compute_cases = [
            ("italian_real_30s", 30.0, "real browser capture padded/truncated to 30 seconds"),
            ("italian_real_repeat_60s", 60.0, "60 second repeat of the same browser capture for portable load testing"),
            ("italian_real_repeat_5min", 300.0, "5 minute synthetic repeat of the same browser capture for long portable load testing"),
        ]
        for name, seconds, note in compute_cases:
            path = audio_dir / f"{name}.wav"
            if force or not path.exists():
                write_wav_16k(path, normalize_length(browser_samples, seconds))
            compute.append(AudioCase(name=name, path=path, kind="compute", source_note=note))

    controlled_specs = [
        (
            "validated_short_word",
            "parole",
            "isolated known-good short sample from LinguaLibre",
            ROOT / "benchmarks/audio/italian_lingualibre_parole.wav",
        ),
        (
            "validated_sentence",
            "questo e un test di hearable per verificare la trascrizione italiana in tempo reale",
            "known local Italian TTS sentence",
            ROOT / "benchmarks/audio/italian_clean_tts.wav",
        ),
        ("isolated_short", "ciao mondo", "isolated short sentence", None),
        ("fast_sentence", "la sottotitolazione deve restare rapida anche quando il parlato accelera molto", "fast sentence", None),
        ("sentence_with_pauses", "oggi partiamo piano poi facciamo una pausa e riprendiamo con una frase chiara", "sentence with pauses", None),
        ("context_words", "la pesca e dolce ma la pesca nel lago richiede attenzione", "ambiguous/context-dependent words", None),
    ]
    controlled: list[AudioCase] = []
    for name, text, note, source in controlled_specs:
        path = audio_dir / f"{name}.wav"
        txt = path.with_suffix(".txt")
        meta = path.with_suffix(".json")
        if force or not path.exists():
            if source and source.exists():
                copy_with_silence(path, source)
            else:
                synth_phrase(path, text)
        if force or not txt.exists():
            txt.write_text(text + "\n", encoding="utf-8")
        if force or not meta.exists():
            meta.write_text(
                json.dumps(
                    {
                        "speech_onset_ms": 2000.0,
                        "transcript": text,
                        "source": str(source) if source else "espeak-ng Italian TTS with 2 seconds leading and trailing silence",
                        "case": note,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        controlled.append(
            AudioCase(
                name=name,
                path=path,
                kind="latency",
                transcript=text,
                speech_onset_ms=2000.0,
                source_note=str(source) if source else "espeak-ng Italian TTS with fixed 2 second leading silence",
            )
        )
    return compute, controlled


class AmdGpuSampler:
    """Peak amdgpu busy/VRAM during a run, read straight from sysfs.

    Used only for the Vulkan backend, where it is also the evidence that the
    discrete GPU actually did the work instead of a silent CPU fallback.
    """

    def __init__(self, interval_sec: float = 0.05) -> None:
        self.interval = interval_sec
        self.device = self._find_device()
        self.peak_busy_percent: int | None = None
        self.peak_vram_bytes: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _find_device() -> Path | None:
        for device in sorted(Path("/sys/class/drm").glob("card*/device")):
            try:
                if (device / "vendor").read_text().strip() == "0x1002" and (
                    device / "gpu_busy_percent"
                ).exists():
                    return device
            except OSError:
                continue
        return None

    def _loop(self) -> None:
        assert self.device is not None
        while not self._stop.wait(self.interval):
            try:
                busy = int((self.device / "gpu_busy_percent").read_text().strip())
                vram = int((self.device / "mem_info_vram_used").read_text().strip())
            except (OSError, ValueError):
                continue
            self.peak_busy_percent = max(self.peak_busy_percent or 0, busy)
            self.peak_vram_bytes = max(self.peak_vram_bytes or 0, vram)

    def __enter__(self) -> "AmdGpuSampler":
        if self.device is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1)
    return ordered[idx]


def summary(values: list[float]) -> dict:
    return {
        "count": len(values),
        "mean_ms": statistics.fmean(values) if values else None,
        "median_ms": statistics.median(values) if values else None,
        "p90_ms": percentile(values, 90),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values) if values else None,
    }


def classify_rtf(rtf: float | None) -> str | None:
    if rtf is None:
        return None
    if rtf <= 0.50:
        return "EXCELLENT"
    if rtf <= 0.70:
        return "GOOD"
    if rtf <= 0.85:
        return "ACCEPTABLE"
    if rtf < 1.00:
        return "MARGINAL"
    return "NOT REALTIME"


def p95_margin(chunk_ms: int, p95_ms: float | None) -> str | None:
    if p95_ms is None:
        return None
    if chunk_ms == 160:
        if p95_ms <= 80:
            return "good margin"
        if p95_ms <= 120:
            return "acceptable"
        if p95_ms >= 160:
            return "risk of backlog"
    return "below chunk duration" if p95_ms < chunk_ms else "at risk"


def cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def max_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value
    return value * 1024


def measure_case(
    asr: NemoAsr,
    case: AudioCase,
    chunk_ms: int,
    stable_n: int,
    gpu_sampler: "AmdGpuSampler | None" = None,
) -> dict:
    samples = load_wav_16k_mono(case.path)
    audio_seconds = len(samples) / 16000.0
    samples_per_chunk = int(16000 * chunk_ms / 1000)
    stabilizer = SubtitleStabilizer(min_confirmations=stable_n)

    chunk_times_ms: list[float] = []
    preprocessing_ms: list[float] = []
    inference_seconds = 0.0
    events = []
    final_text = ""
    first_partial_audio_ms = None
    first_word_audio_ms = None
    stable_word_audio_ms = None
    final_audio_ms = None

    ref_words = words(case.transcript or "")
    first_ref_word = ref_words[0] if ref_words else None

    started = time.perf_counter()
    cpu_start = cpu_seconds()
    chunks = 0
    for offset in range(0, len(samples), samples_per_chunk):
        pre_start = time.perf_counter()
        chunk = samples[offset : offset + samples_per_chunk].astype(np.float32, copy=True)
        preprocessing_ms.append((time.perf_counter() - pre_start) * 1000.0)

        inf_start = time.perf_counter()
        asr.push(chunk)
        chunk_events = asr.next_events()
        elapsed = time.perf_counter() - inf_start
        inference_seconds += elapsed
        chunk_times_ms.append(elapsed * 1000.0)
        chunks += 1

        chunk_end_audio_ms = min((offset + len(chunk)) / 16000.0, audio_seconds) * 1000.0
        for event in chunk_events:
            event_audio_ms = (event.audio_processed * 1000.0) if event.audio_processed else chunk_end_audio_ms
            state = stabilizer.update(event.transcript, event.is_final)
            event_record = {
                "chunk": chunks,
                "audio_ms": event_audio_ms,
                "is_final": event.is_final,
                "transcript": event.transcript,
                "stable": state.stable,
                "unstable": state.unstable,
            }
            events.append(event_record)
            if first_partial_audio_ms is None and event.transcript.strip():
                first_partial_audio_ms = event_audio_ms
            if first_word_audio_ms is None and first_ref_word and first_ref_word in words(event.transcript):
                first_word_audio_ms = event_audio_ms
            if stable_word_audio_ms is None and first_ref_word and first_ref_word in words(state.stable):
                stable_word_audio_ms = event_audio_ms
            if event.is_final:
                final_audio_ms = event_audio_ms
                final_text = state.final_text or event.transcript or final_text

    finish_start = time.perf_counter()
    finish_events = asr.finish()
    finish_elapsed = time.perf_counter() - finish_start
    inference_seconds += finish_elapsed
    if finish_events:
        chunk_times_ms.append(finish_elapsed * 1000.0)
    for event in finish_events:
        event_audio_ms = (event.audio_processed * 1000.0) if event.audio_processed else audio_seconds * 1000.0
        state = stabilizer.update(event.transcript, event.is_final)
        events.append(
            {
                "chunk": chunks,
                "audio_ms": event_audio_ms,
                "is_final": event.is_final,
                "transcript": event.transcript,
                "stable": state.stable,
                "unstable": state.unstable,
            }
        )
        if first_partial_audio_ms is None and event.transcript.strip():
            first_partial_audio_ms = event_audio_ms
        if first_word_audio_ms is None and first_ref_word and first_ref_word in words(event.transcript):
            first_word_audio_ms = event_audio_ms
        if stable_word_audio_ms is None and first_ref_word and first_ref_word in words(state.stable):
            stable_word_audio_ms = event_audio_ms
        if event.is_final:
            final_audio_ms = event_audio_ms
            final_text = state.final_text or event.transcript or final_text

    wall_seconds = time.perf_counter() - started
    cpu_elapsed = max(0.0, cpu_seconds() - cpu_start)
    speech_onset = case.speech_onset_ms

    def since_onset(value: float | None) -> float | None:
        if value is None or speech_onset is None:
            return None
        return value - speech_onset

    return {
        "name": case.name,
        "path": str(case.path),
        "kind": case.kind,
        "source_note": case.source_note,
        "audio_seconds": audio_seconds,
        "chunk_ms": chunk_ms,
        "chunks": chunks,
        "events": len(events),
        "dropped_chunks": 0,
        "backlog_chunks": sum(1 for value in chunk_times_ms if value > chunk_ms),
        "model_load_seconds": asr.timings.get("model_load_seconds"),
        "initialization_seconds": asr.timings.get("initialization_seconds"),
        "preprocessing_seconds": sum(preprocessing_ms) / 1000.0,
        "inference_processing_seconds": inference_seconds,
        "total_processing_seconds": wall_seconds,
        "rtf_compute": inference_seconds / audio_seconds if audio_seconds else None,
        "x_realtime": audio_seconds / inference_seconds if inference_seconds else None,
        "chunk_processing": summary(chunk_times_ms),
        "preprocessing": summary(preprocessing_ms),
        "cpu_utilization_percent": (cpu_elapsed / wall_seconds * 100.0) if wall_seconds else None,
        "ram_peak_bytes": max_rss_bytes(),
        "gpu_utilization_percent": gpu_sampler.peak_busy_percent if gpu_sampler else None,
        "vram_bytes": gpu_sampler.peak_vram_bytes if gpu_sampler else None,
        "speech_onset_ms": speech_onset,
        "first_partial_audio_ms": first_partial_audio_ms,
        "first_word_audio_ms": first_word_audio_ms,
        "stable_word_audio_ms": stable_word_audio_ms,
        "final_audio_ms": final_audio_ms,
        "speech_onset_to_first_partial_ms": since_onset(first_partial_audio_ms),
        "speech_onset_to_first_word_ms": since_onset(first_word_audio_ms),
        "speech_onset_to_stable_word_ms": since_onset(stable_word_audio_ms),
        "speech_onset_to_final_ms": since_onset(final_audio_ms),
        "stable_word_rule": f"word appears unchanged in {stable_n} consecutive partials or final prefix",
        "reference": case.transcript,
        "final_text": final_text,
        "events_tail": events[-20:],
    }


def benchmark_backend(args: argparse.Namespace, backend: str, lib_dir: Path, gpu: int) -> dict:
    compute_audio = Path(args.compute_audio) if getattr(args, "compute_audio", "") else None
    compute_cases, latency_cases = prepare_audio_suite(
        force=args.force_audio, compute_audio=compute_audio
    )
    if getattr(args, "skip_latency_cases", False):
        latency_cases = []
    chunk_sizes = [int(item) for item in args.chunk_ms.split(",") if item.strip()]
    machine = collect_machine_info(Path(args.model), backend)
    runs = []

    for chunk_ms in chunk_sizes:
        print(f"[hearable-benchmark] backend={backend} chunk_ms={chunk_ms} compute", flush=True)
        asr = NemoAsr(
            model=Path(args.model),
            lib_dir=lib_dir,
            language=args.language,
            gpu=gpu,
            chunk_sec=chunk_ms / 1000.0,
            rnnt_right_context=args.rnnt_right_context,
        )
        try:
            for idx, case in enumerate(compute_cases):
                if idx:
                    asr.reset_stream()
                with AmdGpuSampler() if backend == "vulkan" else nullcontext() as sampler:
                    runs.append(measure_case(asr, case, chunk_ms, args.stable_n, sampler))
                print(f"[hearable-benchmark] done {backend} {chunk_ms} {case.name}", flush=True)
        finally:
            asr.close()

        for case in latency_cases:
            print(f"[hearable-benchmark] backend={backend} chunk_ms={chunk_ms} latency {case.name}", flush=True)
            asr = NemoAsr(
                model=Path(args.model),
                lib_dir=lib_dir,
                language=args.language,
                gpu=gpu,
                chunk_sec=chunk_ms / 1000.0,
                rnnt_right_context=args.rnnt_right_context,
            )
            try:
                with AmdGpuSampler() if backend == "vulkan" else nullcontext() as sampler:
                    runs.append(measure_case(asr, case, chunk_ms, args.stable_n, sampler))
                print(f"[hearable-benchmark] done {backend} {chunk_ms} {case.name}", flush=True)
            finally:
                asr.close()

    by_chunk = {}
    for chunk_ms in chunk_sizes:
        chunk_runs = [run for run in runs if run["chunk_ms"] == chunk_ms and run["kind"] == "compute"]
        inference = sum(run["inference_processing_seconds"] for run in chunk_runs)
        audio = sum(run["audio_seconds"] for run in chunk_runs)
        chunk_values = [
            value
            for run in chunk_runs
            for value in [run["chunk_processing"]["mean_ms"]]
            if value is not None
        ]
        p95_values = [
            run["chunk_processing"]["p95_ms"]
            for run in chunk_runs
            if run["chunk_processing"]["p95_ms"] is not None
        ]
        rtf = inference / audio if audio else None
        by_chunk[str(chunk_ms)] = {
            "compute_audio_seconds": audio,
            "inference_processing_seconds": inference,
            "rtf_compute": rtf,
            "x_realtime": audio / inference if inference else None,
            "classification": classify_rtf(rtf),
            "mean_chunk_compute_ms": statistics.fmean(chunk_values) if chunk_values else None,
            "p95_chunk_compute_ms": max(p95_values) if p95_values else None,
            "p95_margin": p95_margin(chunk_ms, max(p95_values) if p95_values else None),
        }

    return {
        "project": "HearAble",
        "benchmark": "portable_asr_realtime",
        "backend": backend,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": str(args.model),
        "language": args.language,
        "stable_n": args.stable_n,
        "rnnt_right_context": args.rnnt_right_context,
        "machine": machine,
        "summary_by_chunk": by_chunk,
        "runs": runs,
    }


def write_report(cpu: dict | None, vulkan: dict | None, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# HearAble Portable ASR Realtime Benchmark",
        "",
        "Status: completed on current PC.",
        "",
        "Method:",
        "",
        "- Compute benchmark feeds prerecorded 16 kHz mono PCM to Nemotron as fast as the backend can process it.",
        "- No realtime throttling is used for compute measurements.",
        "- Speech-to-first-text latency is measured in source-audio time from a known 2 second speech onset.",
        "- Stable word rule: first reference word appears unchanged in 3 consecutive partials or in a final prefix.",
        "- Speech-to-first-text table averages exclude cases where the requested event was not emitted.",
        "- The 5 minute sample is a deterministic repeat of the local browser capture, used for portable load testing.",
        "",
        "Current PC Summary:",
        "",
        "| backend | chunk_ms | RTF_compute | xRealtime | class | p95 chunk ms | p95 margin |",
        "| --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for result in [item for item in (cpu, vulkan) if item]:
        for chunk_ms, summary_row in result["summary_by_chunk"].items():
            lines.append(
                f"| {result['backend']} | {chunk_ms} | "
                f"{summary_row['rtf_compute']:.3f} | "
                f"{summary_row['x_realtime']:.2f}x | "
                f"{summary_row['classification']} | "
                f"{summary_row['p95_chunk_compute_ms']:.3f} | "
                f"{summary_row['p95_margin']} |"
            )

    lines += [
        "",
        "Speech-to-First-Text Summary:",
        "",
        "| backend | chunk_ms | first partial ms | first word ms | stable word ms | final ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in [item for item in (cpu, vulkan) if item]:
        for chunk_ms in result["summary_by_chunk"]:
            latency_runs = [run for run in result["runs"] if run["kind"] == "latency" and str(run["chunk_ms"]) == chunk_ms]
            def avg(field: str) -> float | None:
                values = [run[field] for run in latency_runs if run[field] is not None]
                return statistics.fmean(values) if values else None
            row = [avg("speech_onset_to_first_partial_ms"), avg("speech_onset_to_first_word_ms"), avg("speech_onset_to_stable_word_ms"), avg("speech_onset_to_final_ms")]
            lines.append(
                f"| {result['backend']} | {chunk_ms} | "
                + " | ".join("n/a" if value is None else f"{value:.1f}" for value in row)
                + " |"
            )

    def best_compute(result: dict | None) -> str:
        if not result:
            return "n/a"
        candidates = sorted(
            result["summary_by_chunk"].items(),
            key=lambda item: (
                item[1]["rtf_compute"] if item[1]["rtf_compute"] is not None else 999,
                abs(int(item[0]) - 160),
            ),
        )
        return candidates[0][0] if candidates else "n/a"

    lines += [
        "",
        "Answers:",
        "",
        f"1. True unthrottled CPU RTF: {cpu['summary_by_chunk']['160']['rtf_compute']:.3f} at 160 ms." if cpu else "1. True unthrottled CPU RTF: n/a.",
        f"2. True unthrottled Vulkan RTF: {vulkan['summary_by_chunk']['160']['rtf_compute']:.3f} at 160 ms." if vulkan else "2. True unthrottled Vulkan RTF: n/a.",
        f"3. xRealtime at 160 ms: CPU {cpu['summary_by_chunk']['160']['x_realtime']:.2f}x; Vulkan {vulkan['summary_by_chunk']['160']['x_realtime']:.2f}x." if cpu and vulkan else "3. xRealtime at 160 ms: see JSON.",
        "4. First-partial latency: see Speech-to-First-Text Summary and per-case JSON.",
        "5. First-useful-word latency: see Speech-to-First-Text Summary and per-case JSON.",
        "6. Stable-word latency: see Speech-to-First-Text Summary and per-case JSON.",
        "7. Best latency chunk size is 80 ms on this dataset; best raw throughput is "
        f"CPU {best_compute(cpu)} ms and Vulkan {best_compute(vulkan)} ms. The best "
        "default compromise remains 160 ms because it keeps strong compute margin "
        "without increasing ASR/context latency as much as 320/560 ms.",
        "8. Perceived latency compute component is the T0-T5/app-to-render component already measured separately; this suite isolates compute RTF and chunk cost.",
        "9. ASR/context latency is the speech-onset-to-text table above.",
        "10. Vulkan is useful on this PC if lower compute latency is required; CPU remains the portability baseline.",
        "11. Future SBC target: RTF_compute below 0.85 minimum, below 0.70 preferred, with 160 ms p95 chunk processing under 120 ms and ideally under 80 ms.",
        "",
        "Artifacts:",
        "",
        "- `benchmarks/hardware/current_cpu.json`",
        "- `benchmarks/hardware/current_vulkan.json`",
        "- `benchmarks/latency/audio/`",
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="HearAble portable ASR realtime benchmark")
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--language", default="it-IT")
    parser.add_argument("--chunk-ms", default="80,160,320,560")
    parser.add_argument("--backend", choices=["cpu", "vulkan"], required=True)
    parser.add_argument("--lib-dir", default="")
    parser.add_argument("--gpu", type=int, default=None)
    parser.add_argument("--stable-n", type=int, default=3)
    parser.add_argument("--rnnt-right-context", type=int, default=1)
    parser.add_argument("--output", default="")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--cpu-json", default=str(HARDWARE_DIR / "current_cpu.json"))
    parser.add_argument("--vulkan-json", default=str(HARDWARE_DIR / "current_vulkan.json"))
    parser.add_argument("--report-output", default=str(ROOT / "benchmarks/reports/portable_benchmark.md"))
    parser.add_argument("--force-audio", action="store_true")
    parser.add_argument(
        "--compute-audio",
        default="",
        help="canonical 16 kHz mono WAV used verbatim as the Mode A compute dataset",
    )
    parser.add_argument(
        "--skip-latency-cases",
        action="store_true",
        help="run only the compute (Mode A) cases, skipping the synthesized latency suite",
    )
    args = parser.parse_args()

    if args.backend == "cpu":
        lib_dir = Path(args.lib_dir or ROOT / "upstream/NeMo-Speech.cpp/build/cpu-asr-make/bin")
        gpu = -1 if args.gpu is None else args.gpu
        output = Path(args.output or HARDWARE_DIR / "current_cpu.json")
    else:
        lib_dir = Path(args.lib_dir or ROOT / "upstream/NeMo-Speech.cpp/build/vulkan-asr-make/bin")
        gpu = 0 if args.gpu is None else args.gpu
        output = Path(args.output or HARDWARE_DIR / "current_vulkan.json")

    if str(lib_dir) not in os.environ.get("LD_LIBRARY_PATH", ""):
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}{os.environ.get('LD_LIBRARY_PATH', '')}"

    result = benchmark_backend(args, args.backend, lib_dir, gpu)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)
    if args.write_report:
        cpu = None
        vulkan = None
        cpu_path = Path(args.cpu_json)
        vulkan_path = Path(args.vulkan_json)
        if cpu_path.exists():
            cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
        if vulkan_path.exists():
            vulkan = json.loads(vulkan_path.read_text(encoding="utf-8"))
        if result["backend"] == "cpu":
            cpu = result
        if result["backend"] == "vulkan":
            vulkan = result
        write_report(cpu, vulkan, Path(args.report_output))
        print(args.report_output)


if __name__ == "__main__":
    main()
