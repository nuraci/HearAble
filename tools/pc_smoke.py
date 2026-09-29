#!/usr/bin/env python3
"""Deterministic single-file smoke test for one HearAble PC backend.

Writes the benchmarks/pc/smoke_<backend>.json artifact described in the PC
baseline work order. This is a correctness gate, not a performance measurement:
`wall_seconds` includes model load and is therefore NOT an RTF_compute.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BUILD_DIRS = {
    "cpu": ROOT / "upstream/NeMo-Speech.cpp/build/cpu-asr-make",
    "vulkan": ROOT / "upstream/NeMo-Speech.cpp/build/vulkan-asr-make",
}
# The CLI device string. Vulkan is pinned to physical device 0 so a run can never
# silently land on the llvmpipe software device enumerated alongside the RX 6600.
DEVICE_ARG = {"cpu": "cpu", "vulkan": "vulkan:0"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getframerate() != 16000:
            raise SystemExit(f"{path} must be canonical 16 kHz mono PCM")
        return wav.getnframes() / float(wav.getframerate())


def detect_device(stderr: str, backend: str) -> str:
    """Report the device the runtime actually used, from its own log lines."""
    match = re.search(r"^\[asr\].*backend=(\S+)", stderr, flags=re.MULTILINE)
    reported = match.group(1) if match else "unknown"
    ggml = re.search(r"ggml_vulkan: *\d+ *= *([^(]+?) *\(", stderr)
    if ggml:
        return f"{reported} / {ggml.group(1).strip()}"
    return reported


def resolve_vulkan_physical_device(model: Path, env: dict, gpu: int = 0) -> dict:
    """Name the Vulkan physical device ggml will bind to.

    The nemo-speech CLI reports only the logical label `Vulkan0`, which alone
    cannot distinguish the RX 6600 from the llvmpipe software device this host
    also enumerates. `check_backend_coverage` prints ggml's own device list, so
    the physical name is taken from there.
    """
    probe = BUILD_DIRS["vulkan"] / "bin/check_backend_coverage"
    if not probe.exists():
        return {"name": None, "error": f"missing probe binary: {probe}"}
    proc = subprocess.run(
        [str(probe), str(model), "--gpu", str(gpu)],
        capture_output=True, text=True, env=env,
    )
    devices = re.findall(r"^ggml_vulkan: *(\d+) *= *(.+)$", proc.stdout + proc.stderr, flags=re.MULTILINE)
    selected = next((line for idx, line in devices if int(idx) == gpu), None)
    return {
        "name": selected.split("|")[0].strip() if selected else None,
        "enumerated": [f"{idx} = {line.split('|')[0].strip()}" for idx, line in devices],
        "probe_exit_code": proc.returncode,
    }


def amdgpu_counters() -> dict | None:
    """Direct amdgpu evidence that work actually reached the discrete GPU."""
    for device in sorted(Path("/sys/class/drm").glob("card*/device")):
        try:
            if (device / "vendor").read_text().strip() != "0x1002":
                continue
            return {
                "sysfs": str(device),
                "gpu_busy_percent": int((device / "gpu_busy_percent").read_text().strip()),
                "vram_used_bytes": int((device / "mem_info_vram_used").read_text().strip()),
                "vram_total_bytes": int((device / "mem_info_vram_total").read_text().strip()),
            }
        except (OSError, ValueError):
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=sorted(BUILD_DIRS), required=True)
    parser.add_argument("--input", default=str(ROOT / "benchmarks/audio/italian_latency_001_smoke30s.wav"))
    parser.add_argument("--model", default=str(ROOT / "models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"))
    parser.add_argument("--language", default="it-IT")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    build_dir = BUILD_DIRS[args.backend]
    binary = build_dir / "bin/nemo-speech"
    lib_dir = build_dir / "bin"
    model = Path(args.model)
    audio = Path(args.input)

    for required in (binary, model, audio):
        if not required.exists():
            raise SystemExit(f"missing required path: {required}")

    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{lib_dir}{os.pathsep}{env.get('LD_LIBRARY_PATH', '')}"

    cmd = [
        str(binary), "transcribe", str(audio),
        "--model", str(model),
        "--device", DEVICE_ARG[args.backend],
        "--language", args.language,
        "--stream",
        "--format", "json",
    ]

    physical_device = None
    if args.backend == "vulkan":
        physical_device = resolve_vulkan_physical_device(model, env)

    baseline_counters = amdgpu_counters() if args.backend == "vulkan" else None
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    peak_busy = 0
    peak_vram = 0
    if args.backend == "vulkan":
        while proc.poll() is None:
            sampled = amdgpu_counters()
            if sampled:
                peak_busy = max(peak_busy, sampled["gpu_busy_percent"])
                peak_vram = max(peak_vram, sampled["vram_used_bytes"])
            time.sleep(0.05)
    stdout, stderr = proc.communicate()
    wall = time.perf_counter() - started
    proc = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    transcript = ""
    if proc.returncode == 0:
        try:
            transcript = json.loads(proc.stdout)["text"]
        except (json.JSONDecodeError, KeyError):
            transcript = ""

    device = detect_device(proc.stderr, args.backend)
    if physical_device and physical_device.get("name"):
        device = f"{device} ({physical_device['name']})"
    fell_back = args.backend == "vulkan" and "vulkan" not in device.lower()
    passed = proc.returncode == 0 and bool(transcript.strip()) and not fell_back

    report = {
        "backend": args.backend,
        "device": device,
        "model": str(model),
        "model_sha256": sha256(model),
        "language": args.language,
        "input": str(audio),
        "audio_seconds": wav_seconds(audio),
        "wall_seconds": wall,
        "wall_seconds_note": "includes model load; not an RTF_compute measurement",
        "transcript": transcript,
        "exit_code": proc.returncode,
        "pass": passed,
        "vulkan_fallback_to_cpu": fell_back if args.backend == "vulkan" else None,
        "vulkan_physical_device": physical_device,
        "amdgpu": None if args.backend != "vulkan" else {
            "baseline": baseline_counters,
            "peak_gpu_busy_percent": peak_busy,
            "peak_vram_used_bytes": peak_vram,
            "sampling_interval_ms": 50,
        },
        "command": cmd,
        "stderr": proc.stderr.strip().splitlines(),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    output = Path(args.output or ROOT / f"benchmarks/pc/smoke_{args.backend}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(output)
    print(f"pass={passed} device={device} exit={proc.returncode}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
