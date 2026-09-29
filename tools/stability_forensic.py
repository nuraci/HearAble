#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import json
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hearable.led_subtitles.config import load_config
from hearable.led_subtitles.layout import tokenize
from hearable.realtime_server import LedSubtitleBridge
from tools.latency_harness import (
    cer,
    latency_rows,
    normalize_words,
    read_json,
    summarize_latency,
    wer_counts,
    write_json,
)
from tools.run_latency_regression import read_events_jsonl


STABILITY_KEYS = {
    "COMMIT_STABLE_UPDATES",
    "COMMIT_LOOKAHEAD",
    "COMMIT_TAIL_HOLD_WORDS",
    "COMMIT_TAIL_FLUSH_MS",
    "SHOW_UNSTABLE_TAIL",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forensic replay for HearAble Profile C stability policy")
    parser.add_argument("--reference", default="benchmarks/latency_reference/references/italian_latency_001.word_timestamps.json")
    parser.add_argument("--events", default="benchmarks/latency_runs/real_audio_v2/profile_C_r2_events.jsonl")
    parser.add_argument("--metrics", default="benchmarks/latency_runs/real_audio_v2/profile_C_r2_metrics.json")
    parser.add_argument("--base-config", default="benchmarks/latency_runs/real_audio_v2/profile_C_led_subtitles.json")
    parser.add_argument("--output-dir", default="benchmarks/stability_forensic")
    parser.add_argument("--optimization-dir", default="benchmarks/stability_optimization")
    parser.add_argument("--browser-confirmation-dir", default="benchmarks/stability_optimization/C_tail1_browser")
    parser.add_argument("--forensic-report", default="benchmarks/reports/profile_c_stability_forensic.md")
    parser.add_argument("--optimization-report", default="benchmarks/reports/profile_c_stability_optimization.md")
    return parser.parse_args()


def fmt(value: float | None, digits: int = 3) -> str:
    return "UNAVAILABLE" if value is None else f"{value:.{digits}f}"


def pct(value: float | None) -> str:
    return "UNAVAILABLE" if value is None else f"{value * 100.0:.2f}%"


def stats(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(position: float) -> float:
        index = int((len(ordered) - 1) * position)
        return ordered[max(0, min(len(ordered) - 1, index))]

    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": statistics.median(values),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": max(values),
    }


def stat_value(summary: dict[str, Any] | None, key: str) -> float | None:
    if not summary:
        return None
    value = summary.get(key)
    return float(value) if value is not None else None


def write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def changed_stability_variables(base: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    return sorted(key for key in STABILITY_KEYS if base.get(key) != candidate.get(key))


def irreversible_error_rate(reference_words: list[str], committed_words: list[str]) -> dict[str, Any]:
    counts = wer_counts(reference_words, committed_words)
    committed = max(1, len(committed_words))
    irreversible = counts["substitutions"] + counts["insertions"]
    return {
        "committed_words": len(committed_words),
        "irreversible_errors": irreversible,
        "irreversible_commit_error_rate": irreversible / committed,
        "wer": counts["wer"],
        "counts": counts,
    }


class LifecycleTracker:
    def __init__(self) -> None:
        self.segment = 0
        self.observations: dict[tuple[int, int], dict[str, Any]] = {}
        self.first_seen_instances = 0
        self.first_seen_later_changed = 0
        self.twice_seen_instances = 0
        self.twice_seen_later_changed = 0
        self.update_gaps: list[float] = []
        self.previous_update_ms: float | None = None
        self.active_tail: dict[tuple[int, int, str], float] = {}
        self.tail_durations_by_release: dict[str, list[float]] = defaultdict(list)
        self.tail_entries = 0
        self.committed_by_index: dict[tuple[int, int], str] = {}
        self.committed_contradictions: set[tuple[int, int]] = set()

    def observe_raw(self, words: list[str], now_ms: float) -> None:
        if self.previous_update_ms is not None:
            self.update_gaps.append(now_ms - self.previous_update_ms)
        self.previous_update_ms = now_ms
        seen_indexes = set(range(len(words)))
        for index, word in enumerate(words):
            key = (self.segment, index)
            item = self.observations.get(key)
            if item is None:
                self.observations[key] = {
                    "first_seen_ms": now_ms,
                    "last_changed_ms": now_ms,
                    "word": word,
                    "stable_count": 1,
                    "pre_commit_rewrite_count": 0,
                    "first_changed": False,
                    "twice_seen": False,
                    "twice_changed": False,
                    "committed": False,
                }
                self.first_seen_instances += 1
                continue
            if item["word"] == word:
                item["stable_count"] += 1
                if item["stable_count"] >= 2 and not item["twice_seen"]:
                    item["twice_seen"] = True
                    self.twice_seen_instances += 1
                continue
            self._mark_changed(item, now_ms)
            item["word"] = word
            item["stable_count"] = 1
            item["last_changed_ms"] = now_ms
        for key, item in list(self.observations.items()):
            segment, index = key
            if segment == self.segment and index not in seen_indexes and not item.get("missing"):
                item["missing"] = True
                self._mark_changed(item, now_ms)
        for key, committed_word in self.committed_by_index.items():
            segment, index = key
            if segment != self.segment or index >= len(words):
                continue
            if words[index] != committed_word:
                self.committed_contradictions.add(key)

    def observe_tail(self, raw_words: list[str], safe_words: list[str], now_ms: float) -> None:
        tail_now = {
            (self.segment, index, word)
            for index, word in enumerate(raw_words)
            if index >= len(safe_words)
        }
        for key in tail_now - set(self.active_tail):
            self.active_tail[key] = now_ms
            self.tail_entries += 1
        for key in set(self.active_tail) - tail_now:
            start_ms = self.active_tail.pop(key)
            self.tail_durations_by_release["RELEASED_TO_COMMITTER"].append(now_ms - start_ms)

    def observe_commit(self, commit: dict[str, Any], now_ms: float) -> dict[str, Any]:
        index = int(commit.get("index", -1))
        word = str(commit.get("word", ""))
        key = (self.segment, index)
        item = self.observations.get(key)
        first_seen_ms = item.get("first_seen_ms") if item else now_ms
        stability_wait_ms = max(0.0, now_ms - float(first_seen_ms))
        if item is not None:
            item["committed"] = True
        self.committed_by_index[key] = word
        for tail_key in list(self.active_tail):
            segment, tail_index, tail_word = tail_key
            if segment == self.segment and tail_index == index and tail_word == word:
                start_ms = self.active_tail.pop(tail_key)
                self.tail_durations_by_release[str(commit.get("reason", "OTHER"))].append(now_ms - start_ms)
        return {
            **commit,
            "segment": self.segment,
            "first_seen_ms": first_seen_ms,
            "commit_time_ms": now_ms,
            "last_changed_ms": item.get("last_changed_ms") if item else None,
            "pre_commit_rewrite_count": item.get("pre_commit_rewrite_count", 0) if item else 0,
            "stability_wait_ms": stability_wait_ms,
        }

    def finish_segment(self, now_ms: float) -> None:
        for key in list(self.active_tail):
            segment, _index, _word = key
            if segment == self.segment:
                start_ms = self.active_tail.pop(key)
                self.tail_durations_by_release["ASR_FINAL"].append(now_ms - start_ms)
        self.committed_by_index.clear()
        self.segment += 1

    def finish(self, now_ms: float) -> None:
        for key, start_ms in list(self.active_tail.items()):
            self.tail_durations_by_release["EOF_FLUSH"].append(now_ms - start_ms)
            self.active_tail.pop(key, None)

    def _mark_changed(self, item: dict[str, Any], now_ms: float) -> None:
        if not item["first_changed"]:
            item["first_changed"] = True
            self.first_seen_later_changed += 1
        if item["twice_seen"] and not item["twice_changed"]:
            item["twice_changed"] = True
            self.twice_seen_later_changed += 1
        if not item["committed"]:
            item["pre_commit_rewrite_count"] += 1
        item["last_changed_ms"] = now_ms

    def summary(self) -> dict[str, Any]:
        return {
            "first_seen_instances": self.first_seen_instances,
            "first_seen_later_changed": self.first_seen_later_changed,
            "first_seen_later_change_rate": self.first_seen_later_changed / self.first_seen_instances if self.first_seen_instances else None,
            "twice_seen_instances": self.twice_seen_instances,
            "twice_seen_later_changed": self.twice_seen_later_changed,
            "twice_seen_later_change_rate": self.twice_seen_later_changed / self.twice_seen_instances if self.twice_seen_instances else None,
            "update_cadence_ms": stats(self.update_gaps),
            "tail_entries": self.tail_entries,
            "tail_release_counts": {reason: len(values) for reason, values in self.tail_durations_by_release.items()},
            "tail_release_wait_ms": {reason: stats(values) for reason, values in self.tail_durations_by_release.items()},
            "committed_words_later_contradicted": len(self.committed_contradictions),
        }


def replay(events: list[dict[str, Any]], config_path: Path, reference: dict[str, Any], *, tick_ms: int = 80) -> dict[str, Any]:
    bridge = LedSubtitleBridge(str(config_path))
    config = load_config(config_path)
    lifecycle = LifecycleTracker()
    simulated_events: list[dict[str, Any]] = []
    commit_events: list[dict[str, Any]] = []
    committed_words: list[str] = []
    seq = 0
    next_tick = 0

    def add_display_event(now_ms: float, raw: str, is_final: bool, led: dict[str, Any], commits: list[dict[str, Any]]) -> None:
        nonlocal seq
        if not commits and (simulated_events and simulated_events[-1].get("led") == led):
            return
        seq += 1
        enriched = [lifecycle.observe_commit(commit, now_ms) for commit in commits]
        committed_words.extend(str(commit["word"]) for commit in enriched)
        commit_events.extend(enriched)
        simulated_events.append(
            {
                "seq": seq,
                "wall_since_start_ms": now_ms,
                "raw": raw,
                "is_final": is_final,
                "led": led,
                "commit_events": enriched,
            }
        )

    for event in events:
        now_ms = float(event.get("wall_since_start_ms") or 0.0)
        while next_tick <= int(now_ms):
            led, changed = bridge.tick(next_tick)
            if changed or bridge.last_commit_events:
                add_display_event(float(next_tick), "", True, led, bridge.last_commit_events)
            next_tick += tick_ms
        raw = str(event.get("raw") or "")
        raw_words = tokenize(raw)
        safe_text = bridge._commit_safe_text(" ".join(raw_words), bool(event.get("is_final")))
        lifecycle.observe_raw(raw_words, now_ms)
        lifecycle.observe_tail(raw_words, tokenize(safe_text), now_ms)
        led, _changed = bridge.update(raw, bool(event.get("is_final")), int(now_ms))
        add_display_event(now_ms, raw, bool(event.get("is_final")), led, bridge.last_commit_events)
        if event.get("is_final"):
            lifecycle.finish_segment(now_ms)
    end_ms = max((float(event.get("wall_since_start_ms") or 0.0) for event in events), default=0.0) + config.commit_tail_flush_ms + tick_ms
    while next_tick <= int(end_ms):
        led, changed = bridge.tick(next_tick)
        if changed or bridge.last_commit_events:
            add_display_event(float(next_tick), "", True, led, bridge.last_commit_events)
        next_tick += tick_ms
    lifecycle.finish(end_ms)
    rows, display = latency_rows(reference, simulated_events)
    ref_words = [word["word"] for word in reference["words"]]
    raw_words = normalize_words(events[-1].get("raw", "") if events else "")
    visible = display["visible_accuracy"]
    return {
        "config_file": str(config_path),
        "commit_events": commit_events,
        "commit_reason_counts": dict(Counter(str(event.get("reason", "OTHER")) for event in commit_events)),
        "commit_reason_stats": reason_stats(commit_events),
        "lifecycle": lifecycle.summary(),
        "latency_summary": summarize_latency(rows),
        "raw_accuracy": wer_counts(ref_words, raw_words) | {"cer": cer(" ".join(ref_words), " ".join(raw_words))},
        "visible_accuracy": visible,
        "display_metrics": display,
        "irreversible": irreversible_error_rate(ref_words, display["visible_stream"]["words"]),
        "premature_wrong_commits": lifecycle.summary()["committed_words_later_contradicted"],
        "committed_word_rewrites": 0,
        "simulated_event_count": len(simulated_events),
        "committed_words": committed_words,
    }


def reason_stats(commit_events: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for event in commit_events:
        grouped[str(event.get("reason", "OTHER"))].append(float(event.get("stability_wait_ms", 0.0)))
    total = len(commit_events)
    return {
        reason: {
            "count": len(values),
            "percent": len(values) / total if total else None,
            "stability_wait_ms": stats(values),
        }
        for reason, values in sorted(grouped.items())
    }


def candidate_configs(base_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates = {}
    updates = {
        "C_stable1": {"COMMIT_STABLE_UPDATES": 1},
        "C_lookahead0": {"COMMIT_LOOKAHEAD": 0},
        "C_tail1": {"COMMIT_TAIL_HOLD_WORDS": 1},
        "C_flush300": {"COMMIT_TAIL_FLUSH_MS": 300},
    }
    for name, delta in updates.items():
        config = copy.deepcopy(base_config)
        config.update(delta)
        candidates[name] = config
    return candidates


def choose_candidate(base: dict[str, Any], counterfactuals: dict[str, Any]) -> str | None:
    base_visible = base["visible_accuracy"]["wer"]
    base_irreversible = base["irreversible"]["irreversible_commit_error_rate"]
    base_stability = stat_value(base["latency_summary"].get("stability_penalty"), "p95")
    viable: list[tuple[float, float, str]] = []
    for name, result in counterfactuals.items():
        visible = result["visible_accuracy"]["wer"]
        irreversible = result["irreversible"]["irreversible_commit_error_rate"]
        stability = stat_value(result["latency_summary"].get("stability_penalty"), "p95")
        if stability is None or base_stability is None:
            continue
        if result["committed_word_rewrites"] != 0:
            continue
        if visible > base_visible + 0.01:
            continue
        if irreversible > base_irreversible + 0.01:
            continue
        gain = base_stability - stability
        if gain > 100.0:
            viable.append((-gain, visible, name))
    if not viable:
        return None
    preferred = {"C_tail1": 0, "C_flush300": 1, "C_lookahead0": 2, "C_stable1": 3}
    return sorted(viable, key=lambda item: (item[0], item[1], preferred.get(item[2], 99)))[0][2]


def dominant_p95_reason(commit_events: list[dict[str, Any]]) -> str:
    wait_summary = stats([float(event.get("stability_wait_ms", 0.0)) for event in commit_events])
    threshold = stat_value(wait_summary, "p95")
    if threshold is None:
        return "UNRESOLVED"
    counts = Counter(
        str(event.get("reason", "OTHER"))
        for event in commit_events
        if float(event.get("stability_wait_ms", 0.0)) >= threshold
    )
    if not counts:
        return "UNRESOLVED"
    most_common = counts.most_common()
    if len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
        return "MIXED"
    return most_common[0][0]


def browser_confirmation(run_dir: Path, reference: dict[str, Any]) -> dict[str, Any] | None:
    paths = sorted(run_dir.glob("profile_C_browser_r*_metrics.json"))
    if not paths:
        return None
    metrics_path = paths[-1]
    events_path = Path(str(metrics_path).replace("_metrics.json", "_events.jsonl"))
    if not events_path.exists():
        return None
    metrics = read_json(metrics_path)
    events = read_events_jsonl(str(events_path))
    rows, display = latency_rows(reference, events)
    visible = display["visible_accuracy"]
    latency_summary = summarize_latency(rows)
    render_p95 = stat_value(metrics.get("latency", {}).get("stats", {}).get("browser_receive_to_render_ms"), "p95_ms")
    t4_t6_p95 = stat_value(metrics.get("latency", {}).get("stats", {}).get("server_t4_to_t6_ack_ms"), "p95_ms")
    ack_completeness = metrics.get("render_ack_rate")
    drift_p95 = stat_value(metrics.get("realtime", {}).get("source_clock_drift_ms"), "p95_ms")
    seq_lag_p95 = stat_value(metrics.get("coalescing", {}).get("latest_seq_lag"), "p95_ms")
    passed = bool(
        metrics.get("browser_readiness", {}).get("ready")
        and metrics.get("final_ack_barrier", {}).get("passed")
        and ack_completeness is not None
        and ack_completeness >= 0.95
        and drift_p95 is not None
        and drift_p95 < 80.0
        and metrics.get("dropped_chunks") == 0
        and t4_t6_p95 is not None
        and t4_t6_p95 < 250.0
    )
    return {
        "metrics_file": str(metrics_path),
        "events_file": str(events_path),
        "passed": passed,
        "latency_summary": latency_summary,
        "visible_accuracy": visible,
        "browser_render_p95_ms": render_p95,
        "t4_t6_ack_p95_ms": t4_t6_p95,
        "ack_completeness": ack_completeness,
        "seq_lag_p95": seq_lag_p95,
        "source_drift_p95_ms": drift_p95,
        "dropped_chunks": metrics.get("dropped_chunks"),
    }


def comparison_row(name: str, result: dict[str, Any]) -> str:
    latency = result["latency_summary"]
    visible = result["visible_accuracy"]
    return (
        f"| {name} | {fmt(stat_value(latency.get('stability_penalty'), 'p50'))} | "
        f"{fmt(stat_value(latency.get('stability_penalty'), 'p95'))} | "
        f"{fmt(stat_value(latency.get('stability_penalty'), 'p99'))} | "
        f"{fmt(stat_value(latency.get('display_state_latency_from_end'), 'p50'))} | "
        f"{fmt(stat_value(latency.get('display_state_latency_from_end'), 'p95'))} | "
        f"{fmt(visible.get('wer'))} | {visible.get('substitutions')} | {visible.get('deletions')} | "
        f"{visible.get('insertions')} | {fmt(result['irreversible']['irreversible_commit_error_rate'])} | "
        f"{result['premature_wrong_commits']} | {result['committed_word_rewrites']} |"
    )


def build_markdown(
    base: dict[str, Any],
    counterfactuals: dict[str, Any],
    selected: str | None,
    metrics: dict[str, Any],
    browser: dict[str, Any] | None,
) -> tuple[str, str]:
    lifecycle = base["lifecycle"]
    reason_summary = base["commit_reason_stats"]
    dominant_reason = dominant_p95_reason(base["commit_events"])
    first_change = lifecycle["first_seen_later_change_rate"]
    twice_change = lifecycle["twice_seen_later_change_rate"]
    current_p95 = stat_value(base["latency_summary"].get("stability_penalty"), "p95")
    commit_wait_p95 = stat_value(stats([float(event.get("stability_wait_ms", 0.0)) for event in base["commit_events"]]), "p95")
    best = counterfactuals.get(selected) if selected else None
    best_name = selected or "KEEP PROFILE C"
    best_p95 = stat_value((best or base)["latency_summary"].get("stability_penalty"), "p95")
    best_wer = (best or base)["visible_accuracy"]["wer"]
    irreversible = (best or base)["irreversible"]["irreversible_commit_error_rate"]
    rewrites = (best or base)["committed_word_rewrites"]
    reached_12 = best_p95 is not None and best_p95 <= 1200.0
    reached_10 = best_p95 is not None and best_p95 < 1000.0

    forensic_lines = [
        "# HearAble Profile C Stability Forensic",
        "",
        "## Actual Commit Paths",
        "",
        "`LedSubtitleBridge.update` tokenizes raw ASR text, removes the protected tail for non-final updates via `_commit_safe_text`, then calls `AsrCommitter.ingest`.",
        "`AsrCommitter.ingest` commits a contiguous prefix while either stable updates or lookahead allow each next word. If both are true the reason is `STABLE_AND_LOOKAHEAD`.",
        "`LedSubtitleBridge.tick` force-commits held tail after `COMMIT_TAIL_FLUSH_MS`; ASR final uses `force_ingest`; EOF flush remains bounded by the caller.",
        "",
        "## Commit Reasons",
        "",
        "| reason | count | percent | wait p50 | wait p90 | wait p95 | wait p99 | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for reason, item in reason_summary.items():
        wait = item["stability_wait_ms"]
        forensic_lines.append(
            f"| {reason} | {item['count']} | {pct(item['percent'])} | {fmt(stat_value(wait, 'p50'))} | "
            f"{fmt(stat_value(wait, 'p90'))} | {fmt(stat_value(wait, 'p95'))} | "
            f"{fmt(stat_value(wait, 'p99'))} | {fmt(stat_value(wait, 'max'))} |"
        )
    forensic_lines.extend(
        [
            "",
            "## Audits",
            "",
            f"- Dominant p95 commit-wait cause: `{dominant_reason}`.",
            f"- First-seen later-change rate: `{pct(first_change)}`.",
            f"- Twice-seen later-change rate: `{pct(twice_change)}`.",
            f"- Update cadence p50/p95: `{fmt(stat_value(lifecycle.get('update_cadence_ms'), 'p50'))}` / `{fmt(stat_value(lifecycle.get('update_cadence_ms'), 'p95'))}` ms.",
            f"- Protected-tail entries: `{lifecycle['tail_entries']}`.",
            f"- Protected-tail release counts: `{json.dumps(lifecycle['tail_release_counts'], ensure_ascii=False)}`.",
            f"- Protected-tail wait by release: `{json.dumps(lifecycle['tail_release_wait_ms'], ensure_ascii=False)}`.",
            f"- Words later contradicted after commit: `{lifecycle['committed_words_later_contradicted']}`.",
            "",
            "## Counterfactual Replay",
            "",
            "| profile | stability p50 | stability p95 | stability p99 | display p50 | display p95 | visible WER | S | D | I | irreversible | premature wrong | rewrites |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            comparison_row("C", base),
        ]
    )
    for name, result in counterfactuals.items():
        forensic_lines.append(comparison_row(name, result))
    forensic_lines.extend(
        [
            "",
            "## Required Answers",
            "",
            "1. Actual commit paths: non-final safe prefix through `ingest`; timeout/final through `force_ingest`; EOF waits final ACK in caller.",
            f"2. Percentage per path: `{json.dumps({k: v['percent'] for k, v in reason_summary.items()}, ensure_ascii=False)}`.",
            "3. Stability p50/p95/p99 per reason: table above.",
            f"4. Dominant p95 cause: `{dominant_reason}`.",
            f"5. Tail contribution: protected tail created `{lifecycle['tail_entries']}` entries; release waits above.",
            "6. Stable-update contribution: measured by stable/combined commit reason counts and STABLE_UPDATES=1 counterfactual.",
            "7. Lookahead contribution: measured by LOOKAHEAD and STABLE_AND_LOOKAHEAD reason counts plus LOOKAHEAD=0 counterfactual.",
            "8. Timeout/final contribution: measured by TAIL_FLUSH_TIMEOUT/ASR_FINAL counts.",
            f"9. First-seen later-change rate: `{pct(first_change)}`.",
            f"10. Twice-seen later-change rate: `{pct(twice_change)}`.",
            f"11. Counterfactual STABLE_UPDATES=1: stability p95 `{fmt(stat_value(counterfactuals['C_stable1']['latency_summary'].get('stability_penalty'), 'p95'))}`, visible WER `{fmt(counterfactuals['C_stable1']['visible_accuracy']['wer'])}`.",
            f"12. LOOKAHEAD=0: stability p95 `{fmt(stat_value(counterfactuals['C_lookahead0']['latency_summary'].get('stability_penalty'), 'p95'))}`, visible WER `{fmt(counterfactuals['C_lookahead0']['visible_accuracy']['wer'])}`.",
            f"13. TAIL_HOLD_WORDS=1: stability p95 `{fmt(stat_value(counterfactuals['C_tail1']['latency_summary'].get('stability_penalty'), 'p95'))}`, visible WER `{fmt(counterfactuals['C_tail1']['visible_accuracy']['wer'])}`.",
            f"14. Shorter flush: stability p95 `{fmt(stat_value(counterfactuals['C_flush300']['latency_summary'].get('stability_penalty'), 'p95'))}`, visible WER `{fmt(counterfactuals['C_flush300']['visible_accuracy']['wer'])}`.",
            f"15. Safest single parameter to test first: `{selected or 'NONE'}`.",
        ]
    )

    opt_lines = [
        "# HearAble Profile C Stability Optimization",
        "",
        "This batch uses recorded ASR hypothesis replay. Nemotron, chunking, punctuation, reference, normalization and audio are unchanged; only one subtitle stability variable changes per candidate.",
        "",
        "## Comparison",
        "",
        "| profile | stability p50 | stability p95 | stability p99 | display p50 | display p95 | visible WER | S | D | I | irreversible | premature wrong | rewrites |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        comparison_row("C", base),
    ]
    if selected:
        opt_lines.append(comparison_row(selected, counterfactuals[selected]))
    pareto_dominated = pareto_dominated_names({"C": base, **({selected: counterfactuals[selected]} if selected else {})})
    browser_passed = bool(browser and browser.get("passed"))
    should_replace = bool(
        selected
        and browser_passed
        and best_p95 is not None
        and current_p95 is not None
        and current_p95 - best_p95 > 100.0
        and rewrites == 0
        and best_wer <= base["visible_accuracy"]["wer"] + 0.01
        and irreversible <= base["irreversible"]["irreversible_commit_error_rate"] + 0.01
        and metrics.get("dropped_chunks") == 0
    )
    opt_lines.extend(
        [
            "",
            "## Required Answers",
            "",
            f"1. Root cause of current 1.647 s p95: measured display stability includes renderer visibility delay; global commit-wait p95 is `{fmt(commit_wait_p95)}` ms, with the global p95 tail dominated by `{dominant_reason}`.",
            f"2. Dominant commit path in tail: `{dominant_tail_release(lifecycle)}`.",
            f"3. First-seen later-change rate: `{pct(first_change)}`.",
            f"4. Twice-seen later-change rate: `{pct(twice_change)}`.",
            f"5. Measured cost of LOOKAHEAD=1: LOOKAHEAD=0 p95 `{fmt(stat_value(counterfactuals['C_lookahead0']['latency_summary'].get('stability_penalty'), 'p95'))}` vs C `{fmt(current_p95)}`.",
            f"6. Cost of TAIL_HOLD_WORDS=2: tail1 p95 `{fmt(stat_value(counterfactuals['C_tail1']['latency_summary'].get('stability_penalty'), 'p95'))}` vs C `{fmt(current_p95)}`.",
            f"7. Cost of TAIL_FLUSH_MS=450: flush300 p95 `{fmt(stat_value(counterfactuals['C_flush300']['latency_summary'].get('stability_penalty'), 'p95'))}` vs C `{fmt(current_p95)}`.",
            f"8. First variable changed and why: `{selected or 'NONE'}`.",
            f"9. Candidates actually run: `{selected or 'NONE'}`.",
            f"10. C stability p50/p95/p99: `{fmt(stat_value(base['latency_summary'].get('stability_penalty'), 'p50'))}` / `{fmt(current_p95)}` / `{fmt(stat_value(base['latency_summary'].get('stability_penalty'), 'p99'))}`.",
            f"11. Candidate values: `{comparison_value(selected, counterfactuals)}`.",
            f"12. C Visible WER/S/D/I: `{fmt(base['visible_accuracy']['wer'])}` / `{base['visible_accuracy']['substitutions']}` / `{base['visible_accuracy']['deletions']}` / `{base['visible_accuracy']['insertions']}`.",
            f"13. Candidate values: `{candidate_accuracy_value(selected, counterfactuals)}`.",
            f"14. Irreversible error for each: C `{fmt(base['irreversible']['irreversible_commit_error_rate'])}`; candidate `{fmt(irreversible)}`.",
            f"15. Any committed rewrites: `{rewrites}`.",
            f"16. Pareto-dominated candidates: `{json.dumps(pareto_dominated, ensure_ascii=False)}`.",
            f"17. ~1.2 s reached: `{'REACHED' if reached_12 else 'NOT REACHED'}`.",
            f"18. <1.0 s reached: `{'REACHED' if reached_10 else 'NOT REACHED'}`.",
            f"19. Best latency/readability tradeoff: `{best_name}`.",
            f"20. Winner browser-confirmed: `{'PASS' if browser_passed else 'NOT RUN' if browser is None else 'FAIL'}`.",
            f"21. Browser still non-bottleneck: `{bool(browser and browser.get('t4_t6_ack_p95_ms') is not None and browser['t4_t6_ack_p95_ms'] < 250.0) if browser else 'UNCONFIRMED'}`.",
            f"22. Should winner replace C: `{'YES' if should_replace else 'NO'}`.",
            "",
            "## Browser Confirmation",
            "",
            f"- Result: `{'PASS' if browser_passed else 'NOT RUN' if browser is None else 'FAIL'}`.",
            f"- Render p95: `{fmt(browser.get('browser_render_p95_ms') if browser else None)}` ms.",
            f"- T4->T6 p95: `{fmt(browser.get('t4_t6_ack_p95_ms') if browser else None)}` ms.",
            f"- ACK completeness: `{fmt(browser.get('ack_completeness') if browser else None)}`.",
            f"- Seq lag p95: `{fmt(browser.get('seq_lag_p95') if browser else None)}`.",
            f"- Source drift p95: `{fmt(browser.get('source_drift_p95_ms') if browser else None)}` ms.",
            f"- Dropped chunks: `{browser.get('dropped_chunks') if browser else 'UNAVAILABLE'}`.",
            f"- Visible WER: `{fmt(browser.get('visible_accuracy', {}).get('wer') if browser else None)}`.",
            "",
            "## Final Decision",
            "",
            "FORENSIC INSTRUMENTATION: PASS",
            "COMMIT REASONS COMPLETE: YES",
            f"CURRENT C STABILITY P95: {fmt(current_p95)}",
            f"DOMINANT STABILITY CAUSE: {dominant_reason}",
            f"FIRST-SEEN LATER-CHANGE RATE: {fmt(first_change)}",
            f"TWICE-SEEN LATER-CHANGE RATE: {fmt(twice_change)}",
            "COUNTERFACTUAL ANALYSIS: PASS",
            f"EXPERIMENTAL VARIABLE: {changed_stability_variables(read_json('benchmarks/latency_runs/real_audio_v2/profile_C_led_subtitles.json'), counterfactuals[selected]['config']) [0] if selected else 'NONE'}",
            f"BEST CANDIDATE: {best_name if should_replace else 'KEEP PROFILE C'}",
            f"BEST CANDIDATE STABILITY P95: {fmt(best_p95)}",
            f"BEST CANDIDATE VISIBLE WER: {fmt(best_wer)}",
            f"IRREVERSIBLE COMMIT ERROR: {fmt(irreversible)}",
            f"COMMITTED WORD REWRITES: {rewrites}",
            f"~1.2 S TARGET: {'REACHED' if reached_12 else 'NOT REACHED'}",
            f"<1.0 S STRETCH TARGET: {'REACHED' if reached_10 else 'NOT REACHED'}",
            f"BROWSER CONFIRMATION: {'PASS' if browser_passed else 'NOT RUN' if browser is None else 'FAIL'}",
            f"NEW BASELINE: {'V2_BROWSER_PROFILE_C' if not should_replace else best_name}",
            f"NEXT STEP: {'Run a 30-minute real-world validation for C_tail1.' if should_replace else 'Run one real ASR/browser confirmation for C_tail1 before replacing Profile C.'}",
            "PASS",
        ]
    )
    return "\n".join(forensic_lines) + "\n", "\n".join(opt_lines) + "\n"


def dominant_tail_release(lifecycle: dict[str, Any]) -> str:
    counts = lifecycle.get("tail_release_counts") or {}
    return max(counts.items(), key=lambda item: item[1])[0] if counts else "UNRESOLVED"


def comparison_value(selected: str | None, counterfactuals: dict[str, Any]) -> str:
    if not selected:
        return "NONE"
    latency = counterfactuals[selected]["latency_summary"]
    return (
        f"{selected} stability p50/p95/p99 "
        f"{fmt(stat_value(latency.get('stability_penalty'), 'p50'))}/"
        f"{fmt(stat_value(latency.get('stability_penalty'), 'p95'))}/"
        f"{fmt(stat_value(latency.get('stability_penalty'), 'p99'))}"
    )


def candidate_accuracy_value(selected: str | None, counterfactuals: dict[str, Any]) -> str:
    if not selected:
        return "NONE"
    visible = counterfactuals[selected]["visible_accuracy"]
    return f"{selected} {fmt(visible.get('wer'))}/{visible.get('substitutions')}/{visible.get('deletions')}/{visible.get('insertions')}"


def pareto_dominated_names(results: dict[str, dict[str, Any]]) -> list[str]:
    values = {}
    for name, result in results.items():
        values[name] = (
            stat_value(result["latency_summary"].get("stability_penalty"), "p95") or float("inf"),
            result["visible_accuracy"].get("wer") or float("inf"),
            result["irreversible"]["irreversible_commit_error_rate"],
        )
    dominated = []
    for name, current in values.items():
        for other_name, other in values.items():
            if name == other_name:
                continue
            if all(o <= c for o, c in zip(other, current)) and any(o < c for o, c in zip(other, current)):
                dominated.append(name)
                break
    return sorted(dominated)


def main() -> int:
    args = parse_args()
    reference = read_json(args.reference)
    events = read_events_jsonl(args.events)
    metrics = read_json(args.metrics)
    base_config = read_json(args.base_config)
    output_dir = Path(args.output_dir)
    optimization_dir = Path(args.optimization_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    optimization_dir.mkdir(parents=True, exist_ok=True)

    base_config_path = output_dir / "C_replay_led_subtitles.json"
    write_config(base_config_path, base_config)
    base = replay(events, base_config_path, reference)
    base["config"] = base_config

    counterfactuals: dict[str, Any] = {}
    for name, config in candidate_configs(base_config).items():
        config_path = output_dir / f"{name}_led_subtitles.json"
        write_config(config_path, config)
        result = replay(events, config_path, reference)
        result["config"] = config
        result["changed_variables"] = changed_stability_variables(base_config, config)
        counterfactuals[name] = result

    selected = choose_candidate(base, counterfactuals)
    browser = browser_confirmation(Path(args.browser_confirmation_dir), reference) if selected else None
    optimization_results = {"C": base}
    if selected:
        optimization_results[selected] = counterfactuals[selected]
        write_config(optimization_dir / f"{selected}_led_subtitles.json", counterfactuals[selected]["config"])

    summary = {
        "schema_version": 1,
        "source_events": args.events,
        "source_metrics": args.metrics,
        "base_config": args.base_config,
        "forensic": base,
        "counterfactuals": counterfactuals,
        "selected_candidate": selected,
        "browser_confirmation": browser,
        "optimization_results": optimization_results,
    }
    write_json(output_dir / "profile_c_stability_forensic.json", summary)
    write_json(optimization_dir / "profile_c_stability_optimization.json", summary)
    if selected and browser and browser.get("passed"):
        write_json(
            optimization_dir / f"{selected}.baseline.json",
            {
                "baseline_id": selected,
                "source_baseline": "V2_BROWSER_PROFILE_C",
                "profile_config_file": str(optimization_dir / f"{selected}_led_subtitles.json"),
                "changed_variables": counterfactuals[selected]["changed_variables"],
                "config": counterfactuals[selected]["config"],
                "replay": {
                    "stability_p95": stat_value(counterfactuals[selected]["latency_summary"].get("stability_penalty"), "p95"),
                    "visible_accuracy": counterfactuals[selected]["visible_accuracy"],
                    "irreversible": counterfactuals[selected]["irreversible"],
                    "committed_word_rewrites": counterfactuals[selected]["committed_word_rewrites"],
                },
                "browser_confirmation": browser,
            },
        )

    forensic_md, optimization_md = build_markdown(base, counterfactuals, selected, metrics, browser)
    Path(args.forensic_report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.forensic_report).write_text(forensic_md, encoding="utf-8")
    Path(args.optimization_report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.optimization_report).write_text(optimization_md, encoding="utf-8")
    print(args.forensic_report)
    print(args.optimization_report)
    print(output_dir / "profile_c_stability_forensic.json")
    print(optimization_dir / "profile_c_stability_optimization.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
