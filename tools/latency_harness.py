from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import time
from typing import Any
import unicodedata
import wave


WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def normalize_token(text: str) -> str:
    value = unicodedata.normalize("NFC", str(text or "").lower())
    value = value.replace("'", "'")
    value = re.sub(r"[^\w']+", "", value, flags=re.UNICODE)
    return value.strip("_")


def normalize_words(text: str) -> list[str]:
    return [token for token in (normalize_token(match.group(0)) for match in WORD_RE.finditer(text or "")) if token]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audio_duration_ms(path: str | Path) -> int:
    with wave.open(str(path), "rb") as wav:
        return int(round((wav.getnframes() / wav.getframerate()) * 1000))


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output


def absolute_deadline_ns(source_start_ns: int, chunk_audio_start_ms: float) -> int:
    return source_start_ns + int(round(chunk_audio_start_ms * 1_000_000.0))


def source_clock_drift_ms(actual_feed_ns: int, expected_feed_ns: int) -> float:
    return (actual_feed_ns - expected_feed_ns) / 1_000_000.0


def pacing_sleep_seconds(mode: str, source_start_ns: int, chunk_audio_start_ms: float, now_ns: int) -> float:
    if mode != "realtime":
        return 0.0
    deadline_ns = absolute_deadline_ns(source_start_ns, chunk_audio_start_ms)
    return max(0.0, (deadline_ns - now_ns) / 1_000_000_000.0)


def drift_growth_indicator(records: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [
        (float(record["chunk_audio_end_ms"]), float(record["source_clock_drift_ms"]))
        for record in records
        if record.get("chunk_audio_end_ms") is not None and record.get("source_clock_drift_ms") is not None
    ]
    if len(pairs) < 2:
        return {"sample_count": len(pairs), "slope_ms_per_audio_second": None, "correlation": None, "flag": "UNRESOLVED"}
    xs = [x / 1000.0 for x, _y in pairs]
    ys = [y for _x, y in pairs]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    x_var = sum((x - x_mean) ** 2 for x in xs)
    y_var = sum((y - y_mean) ** 2 for y in ys)
    cov = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    slope = cov / x_var if x_var else None
    corr = cov / math.sqrt(x_var * y_var) if x_var and y_var else 0.0
    flag = "LATENCY_DRIFT_DETECTED" if slope is not None and slope > 10.0 and corr > 0.70 else None
    return {
        "sample_count": len(pairs),
        "slope_ms_per_audio_second": slope,
        "correlation": corr,
        "flag": flag,
    }


def latency_growth_indicator(rows: list[dict[str, Any]], field: str = "display_state_latency_from_end") -> dict[str, Any]:
    records = [
        {
            "chunk_audio_end_ms": row.get("T0b_word_end_ms"),
            "source_clock_drift_ms": row.get(field),
        }
        for row in rows
        if row.get(field) is not None and row.get("T0b_word_end_ms") is not None
    ]
    return drift_growth_indicator(records)


def git_metadata(root: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str:
        proc = subprocess.run(args, cwd=root, check=False, text=True, capture_output=True)
        return proc.stdout.strip()

    commit = run(["git", "rev-parse", "--short", "HEAD"]) or None
    dirty = bool(run(["git", "status", "--short"]))
    return {"commit": commit, "dirty": dirty}


def machine_metadata(root: Path) -> dict[str, Any]:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git": git_metadata(root),
        "os": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
    }


def make_reference(
    *,
    sample_id: str,
    language: str,
    audio_file: str,
    transcript_file: str,
    transcript_text: str,
    alignment_engine: str,
    alignment_version: str,
    words: list[dict[str, Any]],
    authoritative: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sample_id": sample_id,
        "language": language,
        "audio_file": audio_file,
        "audio_sha256": sha256_file(audio_file),
        "transcript_file": transcript_file,
        "transcript_sha256": sha256_text(transcript_text),
        "alignment_engine": alignment_engine,
        "alignment_version": alignment_version,
        "authoritative": authoritative,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "normalization": {
            "case": "lowercase",
            "unicode": "NFC",
            "punctuation": "removed except apostrophes inside tokens",
            "whitespace": "collapsed",
            "numbers": "kept as written",
            "hyphens": "treated as separators",
            "non_speech_tokens": "not allowed in transcript/reference tokens",
        },
        "words": words,
    }


def synthetic_even_split_words(transcript_text: str, duration_ms: int) -> list[dict[str, Any]]:
    tokens = normalize_words(transcript_text)
    if not tokens:
        raise ValueError("transcript has no normalized words")
    step = duration_ms / len(tokens)
    words = []
    for index, token in enumerate(tokens):
        start_ms = int(round(index * step))
        end_ms = int(round((index + 1) * step))
        words.append({"index": index, "word": token, "start_ms": start_ms, "end_ms": end_ms})
    return words


def validate_reference(reference: dict[str, Any], *, audio_duration_ms_value: int, tolerance_ms: int = 50) -> dict[str, Any]:
    errors: list[str] = []
    words = reference.get("words")
    if reference.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(words, list) or not words:
        errors.append("words must be a non-empty list")
        words = []

    previous_start = -1
    previous_end = -1
    empty_tokens = 0
    negative_durations = 0
    overlapping = 0
    out_of_order = 0
    outside_audio = 0

    for expected_index, word in enumerate(words):
        if word.get("index") != expected_index:
            errors.append(f"word index mismatch at {expected_index}")
        token = normalize_token(word.get("word", ""))
        if not token:
            empty_tokens += 1
        try:
            start_ms = int(word["start_ms"])
            end_ms = int(word["end_ms"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"invalid timestamps at word {expected_index}")
            continue
        if start_ms < 0 or end_ms < start_ms:
            negative_durations += 1
        if start_ms < previous_start or end_ms < previous_end:
            out_of_order += 1
        if start_ms < previous_end:
            overlapping += 1
        if end_ms > audio_duration_ms_value + tolerance_ms:
            outside_audio += 1
        previous_start = start_ms
        previous_end = end_ms

    last_end = int(words[-1]["end_ms"]) if words else None
    checks = {
        "word_count": len(words),
        "audio_duration_ms": audio_duration_ms_value,
        "first_word_timestamp_ms": int(words[0]["start_ms"]) if words else None,
        "last_word_timestamp_ms": last_end,
        "negative_durations": negative_durations,
        "overlapping_timestamps": overlapping,
        "out_of_order_timestamps": out_of_order,
        "words_outside_audio_duration": outside_audio,
        "empty_tokens": empty_tokens,
    }
    for key in ("negative_durations", "overlapping_timestamps", "out_of_order_timestamps", "words_outside_audio_duration", "empty_tokens"):
        if checks[key]:
            errors.append(f"{key}: {checks[key]}")
    return {"valid": not errors, "errors": errors, "checks": checks}


def review_markdown(reference: dict[str, Any], validation: dict[str, Any]) -> str:
    words = reference.get("words", [])
    middle_start = max(0, (len(words) // 2) - 15)
    sections = [
        ("first_30", words[:30]),
        ("middle_30", words[middle_start:middle_start + 30]),
        ("last_30", words[-30:]),
    ]
    lines = [
        "# HearAble Latency Reference Review",
        "",
        f"sample_id: `{reference.get('sample_id')}`",
        f"valid: `{validation['valid']}`",
        f"word_count: `{validation['checks']['word_count']}`",
        f"audio_duration_ms: `{validation['checks']['audio_duration_ms']}`",
        "",
    ]
    for title, rows in sections:
        lines.extend([f"## {title}", "", "| index | word | start_ms | end_ms |", "| --- | --- | ---: | ---: |"])
        for row in rows:
            lines.append(f"| {row['index']} | {row['word']} | {row['start_ms']} | {row['end_ms']} |")
        lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class AlignmentItem:
    reference_index: int | None
    reference_word: str | None
    hypothesis_index: int | None
    hypothesis_word: str | None
    match_type: str


def align_words(reference: list[str], hypothesis: list[str]) -> list[AlignmentItem]:
    return list(_align_words_cached(tuple(reference), tuple(hypothesis)))


@lru_cache(maxsize=256)
def _align_words_cached(reference: tuple[str, ...], hypothesis: tuple[str, ...]) -> tuple[AlignmentItem, ...]:
    n = len(reference)
    m = len(hypothesis)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    back: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
        back[i][0] = "D"
    for j in range(1, m + 1):
        dp[0][j] = j
        back[0][j] = "I"
    rank = {"M": 0, "S": 1, "D": 2, "I": 3}
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match = "M" if reference[i - 1] == hypothesis[j - 1] else "S"
            if match == "M":
                # On repeated words, prefer the left-most reference match so
                # a later duplicate is deleted before an earlier duplicate.
                candidates = [
                    (dp[i - 1][j] + 1, "D"),
                    (dp[i - 1][j - 1], "M"),
                    (dp[i][j - 1] + 1, "I"),
                ]
                local_rank = {"D": 0, "M": 1, "I": 2}
            else:
                candidates = [
                    (dp[i - 1][j - 1] + 1, "S"),
                    (dp[i - 1][j] + 1, "D"),
                    (dp[i][j - 1] + 1, "I"),
                ]
                local_rank = rank
            dp[i][j], back[i][j] = min(candidates, key=lambda item: (item[0], local_rank[item[1]]))
    items: list[AlignmentItem] = []
    i, j = n, m
    while i or j:
        op = back[i][j]
        if op in ("M", "S"):
            items.append(AlignmentItem(i - 1, reference[i - 1], j - 1, hypothesis[j - 1], "exact" if op == "M" else "substitution"))
            i -= 1
            j -= 1
        elif op == "D":
            items.append(AlignmentItem(i - 1, reference[i - 1], None, None, "deletion"))
            i -= 1
        else:
            items.append(AlignmentItem(None, None, j - 1, hypothesis[j - 1], "insertion"))
            j -= 1
    return tuple(reversed(items))


def wer_counts(reference: list[str], hypothesis: list[str]) -> dict[str, Any]:
    alignment = align_words(reference, hypothesis)
    substitutions = sum(1 for item in alignment if item.match_type == "substitution")
    deletions = sum(1 for item in alignment if item.match_type == "deletion")
    insertions = sum(1 for item in alignment if item.match_type == "insertion")
    n = len(reference)
    return {
        "n": n,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "wer": (substitutions + deletions + insertions) / n if n else None,
    }


def cer(reference_text: str, hypothesis_text: str) -> float | None:
    ref = normalize_token(reference_text.replace(" ", ""))
    hyp = normalize_token(hypothesis_text.replace(" ", ""))
    if not ref:
        return None
    return _levenshtein_distance_banded(ref, hyp) / len(ref)


@lru_cache(maxsize=128)
def _levenshtein_distance_banded(ref: str, hyp: str) -> int:
    max_len = max(len(ref), len(hyp))
    band = max(abs(len(ref) - len(hyp)) + 32, int(max_len * 0.10), 64)
    while True:
        distance = _levenshtein_distance_with_band(ref, hyp, band)
        if distance is not None:
            return distance
        if band >= max_len:
            return _levenshtein_distance_with_band(ref, hyp, max_len) or max_len
        band = min(max_len, band * 2)


def _levenshtein_distance_with_band(ref: str, hyp: str, band: int) -> int | None:
    n = len(ref)
    m = len(hyp)
    if abs(n - m) > band:
        return None
    inf = n + m + 1
    previous = {j: j for j in range(0, min(m, band) + 1)}
    for i, ref_char in enumerate(ref, start=1):
        current: dict[int, int] = {}
        lo = max(0, i - band)
        hi = min(m, i + band)
        for j in range(lo, hi + 1):
            if j == 0:
                current[j] = i
                continue
            current[j] = min(
                previous.get(j, inf) + 1,
                current.get(j - 1, inf) + 1,
                previous.get(j - 1, inf) + (0 if ref_char == hyp[j - 1] else 1),
            )
        previous = current
    return previous.get(m)


def percentiles(values: list[float]) -> dict[str, Any] | None:
    clean = sorted(value for value in values if value is not None and math.isfinite(value))
    if not clean:
        return None
    def pct(p: float) -> float:
        idx = min(len(clean) - 1, max(0, int((len(clean) - 1) * p)))
        return clean[idx]
    return {
        "sample_count": len(clean),
        "mean": sum(clean) / len(clean),
        "min": clean[0],
        "p50": pct(0.50),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": clean[-1],
    }


def event_words(event: dict[str, Any], field: str) -> list[str]:
    if field == "raw":
        text = str(event.get("raw", ""))
    elif field == "led":
        led = event.get("led") or {}
        text = " ".join(part for part in (led.get("previous_line", ""), led.get("current_line", "")) if part)
    else:
        text = str(event.get(field, ""))
    return normalize_words(text)


def reconstruct_visible_stream(events: list[dict[str, Any]]) -> dict[str, Any]:
    stream: list[str] = []
    token_times_ms: dict[int, float] = {}
    rewrites = 0
    visible_updates = 0
    previous_display: list[str] = []
    led_cache: dict[tuple[str, str], list[str]] = {}
    for event in events:
        led = event.get("led") or {}
        led_key = (str(led.get("previous_line", "")), str(led.get("current_line", "")))
        display = led_cache.setdefault(led_key, normalize_words(" ".join(part for part in led_key if part)))
        if not display or display == previous_display:
            previous_display = display
            continue
        visible_updates += 1
        max_overlap = min(len(stream), len(display))
        overlap = 0
        for candidate in range(max_overlap, 0, -1):
            if stream[-candidate:] == display[:candidate]:
                overlap = candidate
                break
        if stream and overlap == 0:
            suffix_len = min(len(display), len(stream))
            if display != stream[-suffix_len:]:
                rewrites += 1
        timestamp = float(event.get("wall_since_start_ms", 0.0))
        for token in display[overlap:]:
            token_times_ms[len(stream)] = timestamp
            stream.append(token)
        previous_display = display
    return {
        "words": stream,
        "token_times_ms": token_times_ms,
        "visible_rewrites": rewrites,
        "visible_updates": visible_updates,
    }


def exact_hypothesis_to_reference(reference_words: list[str], hypothesis_words: list[str]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for item in align_words(reference_words, hypothesis_words):
        if item.match_type == "exact" and item.reference_index is not None and item.hypothesis_index is not None:
            mapping[item.hypothesis_index] = item.reference_index
    return mapping


def first_raw_word_times(
    reference_words: list[str],
    events: list[dict[str, Any]],
    final_raw: list[str],
) -> tuple[dict[int, dict[str, Any]], dict[int, int]]:
    hyp_to_ref = exact_hypothesis_to_reference(reference_words, final_raw)
    hyp_token_times: dict[tuple[int, str], float] = {}
    raw_cache: dict[str, list[str]] = {}
    previous_hypothesis: list[str] = []
    previous_raw_text = ""
    for event in events:
        timestamp = float(event.get("wall_since_start_ms", 0.0))
        raw_text = str(event.get("raw", ""))
        if raw_text == previous_raw_text:
            continue
        hypothesis = raw_cache.setdefault(raw_text, normalize_words(raw_text))
        common = 0
        max_common = min(len(previous_hypothesis), len(hypothesis))
        while common < max_common and previous_hypothesis[common] == hypothesis[common]:
            common += 1
        for hyp_index in range(common, len(hypothesis)):
            token = hypothesis[hyp_index]
            hyp_token_times.setdefault((hyp_index, token), timestamp)
        previous_hypothesis = hypothesis
        previous_raw_text = raw_text

    first_seen: dict[int, dict[str, Any]] = {}
    for hyp_index, reference_index in hyp_to_ref.items():
        token = final_raw[hyp_index]
        timestamp = hyp_token_times.get((hyp_index, token))
        if timestamp is not None:
            first_seen[reference_index] = {"word": reference_words[reference_index], "time_ms": timestamp}
    return first_seen, hyp_to_ref


def first_display_word_times_from_stream(
    reference_words: list[str],
    visible_words: list[str],
    visible_token_times_ms: dict[int, float],
) -> dict[int, dict[str, Any]]:
    first_seen: dict[int, dict[str, Any]] = {}
    for item in align_words(reference_words, visible_words):
        if item.match_type != "exact" or item.reference_index is None or item.hypothesis_index is None:
            continue
        timestamp = visible_token_times_ms.get(item.hypothesis_index)
        if timestamp is not None:
            first_seen.setdefault(item.reference_index, {"word": item.reference_word, "time_ms": timestamp})
    return first_seen


def reference_alignment_by_index(reference_words: list[str], hypothesis_words: list[str]) -> dict[int, AlignmentItem]:
    return {
        item.reference_index: item
        for item in align_words(reference_words, hypothesis_words)
        if item.reference_index is not None
    }


def classify_visible_loss(reference_words: list[str], raw_words: list[str], visible_words: list[str]) -> dict[str, Any]:
    raw_alignment = reference_alignment_by_index(reference_words, raw_words)
    visible_alignment = reference_alignment_by_index(reference_words, visible_words)
    reasons: dict[str, int] = {}
    per_word: list[dict[str, Any]] = []
    for index, word in enumerate(reference_words):
        visible_item = visible_alignment.get(index)
        if visible_item and visible_item.match_type == "exact":
            reason = None
            stage = "represented_in_visible_committed_stream"
        else:
            raw_item = raw_alignment.get(index)
            stage = "dropped_or_unmatched"
            if raw_item is None or raw_item.match_type == "deletion":
                reason = "NOT_RECOGNIZED"
            elif raw_item.match_type == "substitution":
                reason = "ASR_SUBSTITUTION"
            elif raw_item.match_type == "exact":
                reason = "DISPLAY_POLICY_DROP"
            else:
                reason = "UNKNOWN"
            reasons[reason] = reasons.get(reason, 0) + 1
        per_word.append({"reference_index": index, "word": word, "stage": stage, "loss_reason": reason})
    total = len(reference_words)
    return {
        "reason_counts": reasons,
        "reason_percentages": {key: value / total if total else None for key, value in reasons.items()},
        "unknown_count": reasons.get("UNKNOWN", 0),
        "per_word": per_word,
    }


def eof_audit(events: list[dict[str, Any]], reference_words: list[str], visible_words: list[str]) -> dict[str, Any]:
    last = events[-1] if events else {}
    led = last.get("led") or {}
    raw_words = event_words(last, "raw")
    visible_exact = {
        item.reference_index
        for item in align_words(reference_words, visible_words)
        if item.match_type == "exact" and item.reference_index is not None
    }
    raw_exact = {
        item.reference_index
        for item in align_words(reference_words, raw_words)
        if item.match_type == "exact" and item.reference_index is not None
    }
    raw_exact_not_visible = sorted(raw_exact - visible_exact)
    tail_start = max(0, len(reference_words) - 20)
    final_tail_missing = [index for index in raw_exact_not_visible if index >= tail_start]
    return {
        "unstable_tail_words": event_words(last, "unstable"),
        "stable_words": event_words(last, "stable"),
        "current_lower_line": led.get("current_line", ""),
        "current_upper_line": led.get("previous_line", ""),
        "raw_exact_not_visible_count": len(raw_exact_not_visible),
        "final_tail_raw_exact_not_visible_count": len(final_tail_missing),
        "pending_display_state": bool(led),
        "final_flush_loss_evidenced": bool(event_words(last, "unstable")) or bool(final_tail_missing),
    }


def latency_rows(reference: dict[str, Any], events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ref_words = [normalize_token(word["word"]) for word in reference["words"]]
    final_raw = event_words(events[-1], "raw") if events else []
    raw_times, _hyp_to_ref = first_raw_word_times(ref_words, events, final_raw)
    visible_stream = reconstruct_visible_stream(events)
    final_display = visible_stream["words"]
    display_times = first_display_word_times_from_stream(ref_words, final_display, visible_stream["token_times_ms"])

    rows: list[dict[str, Any]] = []
    for word in reference["words"]:
        idx = int(word["index"])
        t0 = float(word["start_ms"])
        t0b = float(word["end_ms"])
        t2 = raw_times.get(idx, {}).get("time_ms")
        t4 = display_times.get(idx, {}).get("time_ms")
        t3 = t4
        row = {
            "reference_index": idx,
            "reference_word": word["word"],
            "match_type": "exact" if t2 is not None else "unmatched",
            "T0_word_start_ms": t0,
            "T0b_word_end_ms": t0b,
            "T2_first_asr_seen_ms": t2,
            "T3_committed_ms": t3,
            "T4_display_state_ms": t4,
            "T5_browser_render_ms": None,
            "T6_ack_received_ms": None,
            "asr_latency_from_start": t2 - t0 if t2 is not None else None,
            "asr_latency_from_end": t2 - t0b if t2 is not None else None,
            "commit_latency_from_start": t3 - t0 if t3 is not None else None,
            "commit_latency_from_end": t3 - t0b if t3 is not None else None,
            "display_state_latency_from_start": t4 - t0 if t4 is not None else None,
            "display_state_latency_from_end": t4 - t0b if t4 is not None else None,
            "browser_display_latency_from_start": None,
            "browser_display_latency_from_end": None,
            "stability_penalty": t3 - t2 if t2 is not None and t3 is not None else None,
            "display_publish_penalty": t4 - t3 if t3 is not None and t4 is not None else None,
            "browser_render_penalty": None,
            "ack_penalty": None,
        }
        rows.append(row)

    visible_text = " ".join(final_display)
    reference_text = " ".join(ref_words)
    raw_text = " ".join(final_raw)
    visible_accuracy = wer_counts(ref_words, final_display) | {"cer": cer(reference_text, visible_text)}
    raw_alignment = wer_counts(ref_words, final_raw) | {"cer": cer(reference_text, raw_text)}
    loss = classify_visible_loss(ref_words, final_raw, final_display)
    summary = {
        "raw_alignment": raw_alignment,
        "visible_accuracy": visible_accuracy,
        "words_seen_by_asr": len(final_raw),
        "words_committed": len(final_display),
        "words_displayed": len(final_display),
        "words_never_displayed": max(0, len(ref_words) - len(final_display)),
        "words_dropped_by_display_policy": max(0, len(final_raw) - len(final_display)),
        "display_updates": len(events),
        "visible_stream": visible_stream,
        "visible_loss": loss,
        "eof_audit": eof_audit(events, ref_words, final_display),
    }
    return rows, summary


def summarize_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = [
        "asr_latency_from_end",
        "commit_latency_from_end",
        "display_state_latency_from_end",
        "browser_display_latency_from_end",
        "stability_penalty",
        "display_publish_penalty",
        "browser_render_penalty",
        "ack_penalty",
    ]
    return {field: percentiles([row.get(field) for row in rows if row.get("match_type") == "exact"]) for field in fields}


def aggregate_repeated(runs: list[dict[str, Any]], field: str = "display_state_latency_from_end") -> dict[str, Any]:
    p95s = [
        run.get("latency_summary", {}).get(field, {}).get("p95")
        for run in runs
        if run.get("latency_summary", {}).get(field)
    ]
    clean = sorted(float(value) for value in p95s if value is not None)
    if not clean:
        return {"count": 0, "median": None, "min": None, "max": None, "variability": None, "flag": None}
    median = clean[len(clean) // 2]
    variability = clean[-1] - clean[0]
    return {
        "count": len(clean),
        "median": median,
        "min": clean[0],
        "max": clean[-1],
        "variability": variability,
        "flag": "BENCHMARK_NOISY" if median and variability / median > 0.10 else None,
    }


def gate_result(candidate: dict[str, Any], baseline: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    cand_p95 = candidate.get("latency_summary", {}).get("display_state_latency_from_end", {}).get("p95")
    base_p95 = baseline.get("latency_summary", {}).get("display_state_latency_from_end", {}).get("p95")
    if cand_p95 is not None and base_p95 is not None:
        limit = base_p95 * float(gates.get("display_latency_p95_multiplier", 1.10))
        if cand_p95 > limit:
            failures.append(f"display_state_latency_from_end p95 {cand_p95:.3f} > {limit:.3f}")
    cand_wer = candidate.get("visible_accuracy", {}).get("wer")
    base_wer = baseline.get("visible_accuracy", {}).get("wer")
    if cand_wer is not None and base_wer is not None:
        limit = base_wer + float(gates.get("visible_wer_abs_tolerance", 0.02))
        if cand_wer > limit:
            failures.append(f"visible WER {cand_wer:.3f} > {limit:.3f}")
    return {"pass": not failures, "failures": failures}
