#!/usr/bin/env python3
"""Inventory and integrity-check the audio assets this repository depends on.

Two jobs:
1. Re-verify the frozen latency-reference manifest (audio, transcript, word
   alignment). A drifted copy would silently invalidate every latency number.
2. Write docs/pc_audio_assets.md so each asset's provenance, hash and role are
   recorded rather than remembered.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIO_SUFFIXES = {".wav", ".mp3"}

# Why each asset is kept. Anything not listed is reported as unclassified
# rather than silently blessed.
ROLES = {
    "benchmarks/latency_reference/audio/italian_latency_001.wav":
        "**Compute dataset (Mode A)** and latency ground truth; registered sample `italian_latency_001`",
    "benchmarks/audio/italian_latency_001.source.mp3":
        "Original MP3 the registered WAV was canonicalized from (`audio.mp3` in the parent project)",
    "benchmarks/audio/italian_latency_001_smoke30s.wav":
        "First 30 s of the registered sample; input for `tools/pc_smoke.py`",
    "benchmarks/audio/italian_latency_001_loop5x.wav":
        "Registered sample concatenated 5x (2061 s); feeds the 30-minute Phase 5 run. Regenerable, untracked",
    "benchmarks/audio/italian_lingualibre_parole.wav":
        "Known-good short Italian word; latency case `validated_short_word`",
    "benchmarks/audio/italian_clean_tts.wav":
        "Local Italian TTS sentence; latency case `validated_sentence`",
    "benchmarks/audio/browser_capture_16k_mono.wav":
        "Real browser capture; default Mode A source when `--compute-audio` is not given",
    "benchmarks/audio/baseline_cpu/italian_lingualibre_parole.wav":
        "Input directory for `scripts/run_vulkan_benchmark.sh` / `tools/asr_benchmark.py`",
    "benchmarks/accuracy/golden_it/source/Italian.mp3":
        "Source of the golden WER set (accuracy work, not this milestone)",
}
ROLE_PREFIXES = [
    ("benchmarks/desktop/fixtures/",
     "Desktop player test media, generated locally by tools/make_desktop_fixtures.py. "
     "Regenerable, untracked"),
    ("benchmarks/latency/audio/", "Generated controlled latency suite (espeak-ng + real samples)"),
    ("benchmarks/sf8008_usb_mkv/samples/",
     "Evidence for the SF8008 USB-MKV gate: the head of the audio the PC read off "
     "the decoder, kept so the claim that it matches the selected track sample for "
     "sample can be rechecked. Cut from the project's own fixture"),
    ("benchmarks/accuracy/golden_it/", "Golden Italian WER set (accuracy work, not this milestone)"),
    ("benchmarks/nemotron_sweeps/golden_rc_chunk/empty_repro_audio/",
     "Reproduction audio for the empty-hypothesis trailing-silence investigation"),
    ("benchmarks/audio/pipewire_", "PipeWire capture debugging residue; kept for provenance, not used by any test"),
    ("benchmarks/audio/test_tone_", "Synthetic tone used to debug capture routing; not speech"),
    ("benchmarks/audio/browser_capture.wav", "Pre-canonicalization original of the browser capture"),
    ("benchmarks/audio/italian_clean_tts.raw.wav", "Pre-canonicalization original of the TTS sentence"),
    ("benchmarks/audio/italian_lingualibre_parole.original.wav", "Pre-canonicalization original from LinguaLibre"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def role_for(rel: str) -> str:
    if rel in ROLES:
        return ROLES[rel]
    for prefix, text in ROLE_PREFIXES:
        if rel.startswith(prefix):
            return text
    return "unclassified"


def probe(path: Path) -> tuple[str, float | None]:
    if path.suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                rate, channels = wav.getframerate(), wav.getnchannels()
                return f"{rate} Hz / {channels} ch", wav.getnframes() / float(rate)
        except (wave.Error, OSError):
            return "unreadable", None
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=sample_rate,channels:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "unreadable", None
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = data.get("format", {}).get("duration")
    return f"{stream.get('sample_rate')} Hz / {stream.get('channels')} ch", float(duration) if duration else None


def verify_manifest() -> list[str]:
    manifest_path = ROOT / "benchmarks/latency_reference/manifest.json"
    if not manifest_path.exists():
        return ["- `benchmarks/latency_reference/manifest.json` is MISSING."]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lines = []
    for sample in manifest.get("samples", []):
        lines.append(f"`{sample['sample_id']}` — aligner {sample['alignment_engine']} "
                     f"{sample['alignment_version']}, authoritative: {sample['authoritative']}")
        for key in ("audio", "transcript", "reference"):
            path = ROOT / sample[f"{key}_file"]
            if not path.exists():
                lines.append(f"  - {key}: **MISSING** `{sample[f'{key}_file']}`")
                continue
            ok = sha256(path) == sample[f"{key}_sha256"]
            lines.append(f"  - {key}: {'OK' if ok else '**SHA-256 MISMATCH**'} `{path.name}`")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "docs/pc_audio_assets.md"))
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    manifest_lines = verify_manifest()
    failed = any("MISMATCH" in line or "MISSING" in line for line in manifest_lines)

    assets = sorted(
        p for p in (ROOT / "benchmarks").rglob("*")
        if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file()
    )

    rows = []
    unclassified = 0
    total_bytes = 0
    for path in assets:
        rel = str(path.relative_to(ROOT))
        fmt, seconds = probe(path)
        role = role_for(rel)
        unclassified += role == "unclassified"
        total_bytes += path.stat().st_size
        rows.append((rel, fmt, seconds, path.stat().st_size, sha256(path), role))

    if args.check_only:
        print("\n".join(manifest_lines))
        print(f"{len(rows)} audio assets, {unclassified} unclassified")
        return 1 if failed else 0

    lines = [
        "# HearAble PC Reference — Audio Assets",
        "",
        f"Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} by `tools/audio_inventory.py`.",
        "Re-run it after adding or replacing any audio; `--check-only` exits non-zero on a",
        "manifest mismatch and is safe to use as a gate.",
        "",
        "## Frozen latency-reference integrity",
        "",
    ]
    lines += manifest_lines
    lines += [
        "",
        "## All audio assets",
        "",
        f"{len(rows)} files, {total_bytes / (1024 * 1024):.1f} MiB total.",
        "",
        "| File | Format | Duration | Role |",
        "|---|---|---:|---|",
    ]
    for rel, fmt, seconds, _size, _digest, role in rows:
        duration = f"{seconds:.2f} s" if seconds is not None else "—"
        lines.append(f"| `{rel}` | {fmt} | {duration} | {role} |")

    lines += ["", "## SHA-256", "", "```text"]
    lines += [f"{digest}  {rel}" for rel, _f, _s, _sz, digest, _r in rows]
    lines += ["```", ""]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(output)
    if unclassified:
        print(f"warning: {unclassified} unclassified asset(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
