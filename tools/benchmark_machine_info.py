#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_text(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or proc.stderr).strip()
    return text or None


def first_line(text: str | None) -> str | None:
    if not text:
        return None
    return text.splitlines()[0].strip() or None


def read_first(paths: list[Path]) -> str | None:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return None


def cpu_model() -> str | None:
    text = read_first([Path("/proc/cpuinfo")])
    if not text:
        return platform.processor() or None
    for line in text.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or None


def total_ram_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def git_commit(path: Path) -> str | None:
    text = run_text(["git", "-C", str(path), "rev-parse", "HEAD"])
    return first_line(text)


def gpu_lines() -> list[str]:
    text = run_text(["lspci", "-nn"])
    if not text:
        return []
    pattern = re.compile(r"(vga|3d|display|amd|radeon)", re.IGNORECASE)
    return [line for line in text.splitlines() if pattern.search(line)]


def vulkan_devices() -> list[str]:
    text = run_text(["vulkaninfo", "--summary"], timeout=8.0)
    if not text:
        return []
    devices = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("deviceName"):
            devices.append(stripped.split("=", 1)[-1].strip())
    return devices


def collect(model: Path, backend: str) -> dict:
    return {
        "project": "HearAble",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": platform.node(),
        "architecture": platform.machine(),
        "os": platform.platform(),
        "kernel": platform.release(),
        "cpu_model": cpu_model(),
        "cpu_cores_logical": os.cpu_count(),
        "ram_bytes": total_ram_bytes(),
        "gpu": gpu_lines(),
        "vulkan_devices": vulkan_devices(),
        "backend": backend,
        "compiler": {
            "cc": first_line(run_text(["cc", "--version"])),
            "cxx": first_line(run_text(["c++", "--version"])),
            "cmake": first_line(run_text(["cmake", "--version"])),
        },
        "nemo_speech_cpp_commit": git_commit(ROOT / "upstream/NeMo-Speech.cpp"),
        "model": {
            "path": str(model),
            "sha256": sha256(model),
            "size_bytes": model.stat().st_size if model.exists() else None,
        },
        "tools": {
            "rocminfo": first_line(run_text(["which", "rocminfo"])),
            "hipcc": first_line(run_text(["which", "hipcc"])),
            "rocm_smi": first_line(run_text(["which", "rocm-smi"])),
            "radeontop": first_line(run_text(["which", "radeontop"])),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect portable HearAble benchmark machine info")
    parser.add_argument("--model", default=str(ROOT / "models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf"))
    parser.add_argument("--backend", default="cpu")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    info = collect(Path(args.model), args.backend)
    text = json.dumps(info, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(path)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
