"""Backend selection and metric identity for the PC reference baseline."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import pc_smoke
from tools.hearable_benchmark import prepare_audio_suite


def write_silence(path: Path, seconds: float = 0.25) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())


class BackendSelection(unittest.TestCase):
    def test_cpu_backend_selects_cpu_build_and_device(self):
        self.assertEqual(pc_smoke.DEVICE_ARG["cpu"], "cpu")
        self.assertTrue(str(pc_smoke.BUILD_DIRS["cpu"]).endswith("build/cpu-asr-make"))

    def test_vulkan_backend_pins_physical_device_zero(self):
        # Must be explicit: this host also enumerates an llvmpipe software device.
        self.assertEqual(pc_smoke.DEVICE_ARG["vulkan"], "vulkan:0")
        self.assertTrue(str(pc_smoke.BUILD_DIRS["vulkan"]).endswith("build/vulkan-asr-make"))

    def test_cpu_and_vulkan_builds_do_not_share_a_tree(self):
        self.assertNotEqual(pc_smoke.BUILD_DIRS["cpu"], pc_smoke.BUILD_DIRS["vulkan"])

    def test_invalid_backend_is_rejected(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools/pc_smoke.py"), "--backend", "rocm"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid choice", proc.stderr)


class DeviceIdentity(unittest.TestCase):
    def test_backend_identity_is_read_from_runtime_log(self):
        stderr = "[asr] model=x head=rnnt backend=Vulkan0\n"
        self.assertEqual(pc_smoke.detect_device(stderr, "vulkan"), "Vulkan0")

    def test_physical_device_name_is_appended_when_ggml_reports_it(self):
        stderr = (
            "ggml_vulkan: 0 = AMD Radeon RX 6600 (RADV NAVI23) (radv) | uma: 0\n"
            "[asr] model=x head=rnnt backend=Vulkan0\n"
        )
        self.assertEqual(
            pc_smoke.detect_device(stderr, "vulkan"),
            "Vulkan0 / AMD Radeon RX 6600",
        )

    def test_unknown_backend_identity_does_not_crash(self):
        self.assertEqual(pc_smoke.detect_device("no marker here", "cpu"), "unknown")

    def test_missing_vulkan_probe_is_reported_not_raised(self):
        original = pc_smoke.BUILD_DIRS["vulkan"]
        pc_smoke.BUILD_DIRS["vulkan"] = ROOT / "does-not-exist"
        try:
            result = pc_smoke.resolve_vulkan_physical_device(Path("model.gguf"), {})
        finally:
            pc_smoke.BUILD_DIRS["vulkan"] = original
        self.assertIsNone(result["name"])
        self.assertIn("missing probe binary", result["error"])

    def test_amdgpu_counters_shape(self):
        counters = pc_smoke.amdgpu_counters()
        if counters is None:
            self.skipTest("no amdgpu device on this host")
        self.assertIn("gpu_busy_percent", counters)
        self.assertGreater(counters["vram_total_bytes"], 0)


class ComputeDataset(unittest.TestCase):
    def test_explicit_compute_audio_is_used_verbatim(self):
        with TemporaryDirectory() as tmp:
            audio = Path(tmp) / "dataset_it.wav"
            write_silence(audio)
            compute, _ = prepare_audio_suite(compute_audio=audio)
        self.assertEqual([case.path for case in compute], [audio])
        self.assertEqual(compute[0].name, "dataset_it")
        self.assertEqual(compute[0].kind, "compute")

    def test_missing_compute_audio_fails_loudly(self):
        with self.assertRaises(SystemExit):
            prepare_audio_suite(compute_audio=ROOT / "benchmarks/audio/nope.wav")


class SmokeArtifact(unittest.TestCase):
    def test_generated_artifacts_carry_backend_identity(self):
        for backend in ("cpu", "vulkan"):
            path = ROOT / f"benchmarks/pc/smoke_{backend}.json"
            if not path.exists():
                self.skipTest(f"{path} not generated yet")
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(report["backend"], backend)
            self.assertTrue(report["device"])
            self.assertIn("model_sha256", report)
            if backend == "vulkan":
                self.assertFalse(report["vulkan_fallback_to_cpu"])
                self.assertIn("RX 6600", report["device"])


if __name__ == "__main__":
    unittest.main()


def _needs(path):
    """Skip, with a reason, when an artefact this clone does not carry is missing.

    The repository carries code, documents and the small data the reports cite.
    It does not carry 97 MB of reference audio or the 707 MB model: those live
    beside it, and `models/README.md` says where. A test that reads one of them
    is not failing when it is absent — it has nothing to judge.
    """
    import unittest as _u
    from pathlib import Path as _P
    if not _P(path).exists():
        raise _u.SkipTest(f"artefatto non presente in questo clone: {path}")


class AudioAssets(unittest.TestCase):
    """The audio this repository depends on must be present and unmodified."""

    def test_frozen_latency_reference_still_matches_its_manifest(self):
        _needs(ROOT / "benchmarks/latency_reference/audio/italian_latency_001.wav")
        from tools.audio_inventory import verify_manifest

        lines = verify_manifest()
        self.assertTrue(lines, "manifest produced no output")
        bad = [line for line in lines if "MISMATCH" in line or "MISSING" in line]
        self.assertEqual(bad, [], f"latency reference integrity broken: {bad}")

    def test_assets_the_pc_scripts_depend_on_exist(self):
        _needs(ROOT / "benchmarks/audio")
        required = [
            "benchmarks/latency_reference/audio/italian_latency_001.wav",
            "benchmarks/latency_reference/transcripts/italian_latency_001.txt",
            "benchmarks/latency_reference/references/italian_latency_001.word_timestamps.json",
            "benchmarks/audio/italian_latency_001_smoke30s.wav",
            "benchmarks/audio/browser_capture_16k_mono.wav",
            "benchmarks/audio/baseline_cpu/italian_lingualibre_parole.wav",
            "benchmarks/latency_profiles.json",
        ]
        missing = [path for path in required if not (ROOT / path).exists()]
        self.assertEqual(missing, [], f"missing audio assets: {missing}")

    def test_smoke_excerpt_is_canonical_pcm(self):
        path = ROOT / "benchmarks/audio/italian_latency_001_smoke30s.wav"
        _needs(path)
        with wave.open(str(path), "rb") as wav:
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)

    def test_every_audio_asset_has_a_declared_role(self):
        from tools.audio_inventory import AUDIO_SUFFIXES, role_for

        unclassified = [
            str(p.relative_to(ROOT))
            for p in (ROOT / "benchmarks").rglob("*")
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
            and role_for(str(p.relative_to(ROOT))) == "unclassified"
        ]
        self.assertEqual(unclassified, [], f"audio with no declared role: {unclassified}")


class RealtimeValidation(unittest.TestCase):
    """Phase 4/5 PASS conditions, asserted against the committed run artifacts."""

    REQUIRED_RUNS = [
        ("cpu", "realtime_412s"),
        ("vulkan", "realtime_412s"),
        ("vulkan", "realtime_1800s"),
    ]

    def load_run(self, backend: str, name: str) -> dict:
        path = ROOT / f"benchmarks/pc/{backend}/{name}/run.json"
        if not path.exists():
            self.skipTest(f"{path} not generated yet")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_no_chunk_was_dropped(self):
        for backend, name in self.REQUIRED_RUNS:
            with self.subTest(run=f"{backend}/{name}"):
                self.assertEqual(self.load_run(backend, name)["dropped_chunks"], 0)

    def test_backlog_returned_to_zero(self):
        for backend, name in self.REQUIRED_RUNS:
            with self.subTest(run=f"{backend}/{name}"):
                run = self.load_run(backend, name)
                self.assertEqual(run["backlog"]["pending_render_acks_at_stop"], 0)
                self.assertEqual(run["websocket"]["disconnects"], 0)

    def test_compute_keeps_up_with_realtime(self):
        for backend, name in self.REQUIRED_RUNS:
            with self.subTest(run=f"{backend}/{name}"):
                self.assertLess(self.load_run(backend, name)["compute"]["compute_rtf"], 1.0)

    def test_pacing_uses_absolute_deadlines_and_drift_is_measured(self):
        # A cumulative-sleep source would report pacing "none" and no drift at all.
        for backend, name in self.REQUIRED_RUNS:
            with self.subTest(run=f"{backend}/{name}"):
                realtime = self.load_run(backend, name)["realtime"]
                self.assertEqual(realtime["pacing_strategy"], "absolute_deadline_from_audio_timeline")
                self.assertIsNotNone(realtime["source_clock_drift_ms"])
                self.assertLess(realtime["source_clock_drift_ms"]["p95_ms"], 50.0)

    def test_drift_does_not_accumulate(self):
        for backend, name in self.REQUIRED_RUNS:
            with self.subTest(run=f"{backend}/{name}"):
                growth = self.load_run(backend, name)["realtime"]["source_clock_drift_growth"]
                self.assertLessEqual(growth["slope_ms_per_audio_second"], 0.0)

    def test_thirty_minute_run_is_long_enough(self):
        run = self.load_run("vulkan", "realtime_1800s")
        self.assertGreaterEqual(run["audio_seconds"], 1800.0)

    def test_word_latency_used_the_authoritative_reference(self):
        for backend in ("cpu", "vulkan"):
            path = ROOT / f"benchmarks/pc/{backend}/latency_words/latest_latency_regression.json"
            if not path.exists():
                self.skipTest(f"{path} not generated yet")
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(backend=backend):
                self.assertTrue(data["manifest_guard"]["valid"])
                self.assertTrue(data["reference"]["authoritative"])
                self.assertEqual(data["reference"]["sample_id"], "italian_latency_001")
                self.assertEqual(data["profiles"][0]["profile"], "C_tail1")
                # The whole reference must be covered, not a prefix of it.
                self.assertEqual(data["profiles"][0]["median_run"]["visible_accuracy"]["n"], 992)

    def test_production_profile_matches_the_loaded_runtime_config(self):
        profile = json.loads((ROOT / "benchmarks/pc/production_profile.json").read_text(encoding="utf-8"))
        config = json.loads((ROOT / "config/led_subtitles.json").read_text(encoding="utf-8"))
        for key, value in profile["profiles"][0]["config"].items():
            with self.subTest(key=key):
                self.assertEqual(config[key], value)
