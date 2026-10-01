#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import signal
import statistics
import sys
import time
from typing import Any

from aiohttp import WSMsgType, web

from hearable import __version__
from hearable.audio_sources import AudioSource
from hearable.audio_sources import AudioSourceRegistry
from hearable.asr_capi import NemoAsr
from hearable.io_config import BackendConfig
from hearable.io_config import BackendConfigError
from hearable.io_config import load_backend_config
from hearable.io_config import resolve_path
from hearable.led_subtitles.asr_commit import AsrCommitter
from hearable.led_subtitles.config import load_config
from hearable.led_subtitles.layout import tokenize
from hearable.led_subtitles.rollup import RollUpRenderer
from hearable.stabilizer import SubtitleStabilizer
from hearable.subtitle_sinks import SubtitleSink
from hearable.subtitle_sinks import SubtitleSinkRegistry
from hearable.subtitle_sinks import SubtitleState
from tools.benchmark_machine_info import collect as collect_machine_info
from tools.latency_harness import drift_growth_indicator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CONFIG_PATH = ROOT / "config" / "realtime.json"
DEFAULT_AUDIO_SOURCE_CONFIG_PATH = ROOT / "config" / "audio_source.json"
DEFAULT_SUBTITLE_SINK_CONFIG_PATH = ROOT / "config" / "subtitle_sink.json"
PATH_CONFIG_KEYS = {
    "model",
    "lib_dir",
    "input_wav",
    "led_config",
    "metrics_output",
    "events_output",
    "audio_source_config",
    "subtitle_sink_config",
}
RUNTIME_CONFIG_KEYS = {
    "model",
    "lib_dir",
    "language",
    "target",
    "input_wav",
    "host",
    "port",
    "duration",
    "chunk_ms",
    "gpu",
    "rnnt_right_context",
    "capture_latency",
    "max_words",
    "stabilizer_history_size",
    "stabilizer_min_confirmations",
    "stabilizer_timeout_sec",
    "max_chunks",
    "flush_on_stop",
    "metrics_output",
    "events_output",
    "include_latency_records",
    "ui_max_fps",
    "ui_final_text_chars",
    "led_config",
    "pending_render_limit",
    "final_ack_wait",
    "mode",
    "require_browser_ready",
    "browser_ready_timeout",
    "verbose_pipeline",
    "audio_source_config",
    "subtitle_sink_config",
}


class LedSubtitleBridge:
    def __init__(self, config_path: str) -> None:
        self.config = load_config(config_path)
        self.committer = AsrCommitter(self.config)
        self.renderer = RollUpRenderer(self.config)
        self._last_input_text = ""
        self._last_input_ms: int | None = None
        self._tail_flushed = True
        self._last_payload = self.payload()
        self.last_commit_events: list[dict[str, Any]] = []
        # What the tail flush actually does, as opposed to what it is meant to
        # do. The field reported zero TAIL_FLUSH_TIMEOUT commits in a
        # half-hour session, and nothing recorded whether the deadline never
        # arrived or the committer refused the words when it did.
        self.tail_flush_due = 0
        self.tail_flush_committed = 0
        self.tail_flush_refused = 0
        self.tail_refusal_kinds: dict[str, int] = {}
        self.last_tail_refusal: dict = {}

    def reset(self) -> None:
        """Drop committed and displayed text, keeping the frozen C_tail1 config."""
        self.committer.reset()
        self.renderer = RollUpRenderer(self.config)
        self._last_input_text = ""
        self._last_input_ms = None
        self._tail_flushed = True
        self.last_commit_events = []
        self._last_payload = self.payload()

    def update(self, text: str, is_final: bool, now_ms: int) -> tuple[dict, bool]:
        incoming = " ".join(tokenize(text))
        # The tail-flush timer measures quiet, and quiet means "the hypothesis
        # stopped changing" — not "no event arrived".
        #
        # This used to stamp `now_ms` on every call, which looked equivalent and
        # was not: a cache-aware streaming recogniser emits a result for every
        # chunk, including while nobody is speaking, and each of those identical
        # results reset the 450 ms timer. The held last word therefore never
        # flushed on silence. It waited for the next utterance and was painted
        # at the head of it, so a sentence ended one word short and the next one
        # began with a word that belonged to the sentence before.
        #
        # Reported from real use: "l'ultima parola di una frase non viene
        # mandata fuori subito ma se c'e' del silenzio viene mandata fuori per
        # prima con la frase successiva".
        changed = incoming != self._last_input_text
        self._last_input_text = incoming
        if changed or self._last_input_ms is None:
            self._last_input_ms = now_ms
        if changed or is_final:
            self._tail_flushed = is_final or not self._has_held_tail(incoming)
        if is_final:
            result = self.committer.force_ingest(self._last_input_text)
        else:
            result = self.committer.ingest(self._commit_safe_text(self._last_input_text, is_final))
        self.last_commit_events = result.commit_events
        self.renderer.ingest_words(result.committed_words, now_ms=now_ms)
        if is_final:
            self.committer.reset()
        return self._payload_with_changed()

    def tick(self, now_ms: int) -> tuple[dict, bool]:
        if self._should_flush_tail(now_ms):
            self.tail_flush_due += 1
            before = self.committer.refusals
            # Which of the two the switch chose is recorded in the metrics, so a
            # run can always say which behaviour produced its numbers.
            if self.config.commit_tail_release == "off":
                # Unchanged behaviour, and it refuses: kept so a run in "off"
                # still records what the strict guard does.
                result = self.committer.force_ingest(self._last_input_text,
                                                    reason="TAIL_FLUSH_TIMEOUT")
            else:
                result = self.committer.release_tail(self._last_input_text)
            if result.committed_words:
                self.tail_flush_committed += 1
            else:
                self.tail_flush_refused += 1
                if self.committer.refusals > before:
                    kind = str(self.committer.last_refusal.get("kind", "UNKNOWN"))
                    first = kind not in self.tail_refusal_kinds
                    self.tail_refusal_kinds[kind] = self.tail_refusal_kinds.get(kind, 0) + 1
                    self.last_tail_refusal = dict(self.committer.last_refusal)
                    if first:
                        # Once per kind, not per occurrence: the journal should
                        # name a new failure mode, not count an old one.
                        print(f"[tail] flush refused: {kind} "
                              f"{self.committer.last_refusal}", file=sys.stderr, flush=True)
            self.last_commit_events = result.commit_events
            self.renderer.ingest_words(result.committed_words, now_ms=now_ms)
            self._tail_flushed = True
        else:
            self.last_commit_events = []
        self.renderer.tick(now_ms)
        return self._payload_with_changed()

    def tail_diagnostics(self) -> dict:
        """Measured, so the next conversation about this starts from numbers."""
        return {
            "tail_flush_due": self.tail_flush_due,
            "tail_flush_committed": self.tail_flush_committed,
            "tail_flush_refused": self.tail_flush_refused,
            "refusal_kinds": dict(sorted(self.tail_refusal_kinds.items(),
                                         key=lambda kv: -kv[1])),
            "last_refusal": self.last_tail_refusal,
            "commit_tail_hold_words": self.config.commit_tail_hold_words,
            "commit_tail_flush_ms": self.config.commit_tail_flush_ms,
            "commit_tail_release": self.config.commit_tail_release,
        }

    def payload(self) -> dict:
        frame = self.renderer.frame()
        return {
            "mode": "rollup",
            "previous_line": frame.top,
            "current_line": frame.bottom,
            "status": frame.status,
            "max_chars": self.config.max_chars,
            "scroll_ms": self.config.scroll_ms,
            "min_line_hold_ms": self.config.min_line_hold_ms,
            "idle_clear_ms": self.config.idle_clear_ms,
            "commit_tail_hold_words": self.config.commit_tail_hold_words,
            "commit_tail_flush_ms": self.config.commit_tail_flush_ms,
        }

    def _payload_with_changed(self) -> tuple[dict, bool]:
        payload = self.payload()
        changed = payload != self._last_payload
        self._last_payload = payload
        return payload, changed

    def _commit_safe_text(self, text: str, is_final: bool) -> str:
        if is_final:
            return text
        words = tokenize(text)
        hold_words = max(0, self.config.commit_tail_hold_words)
        if len(words) <= hold_words:
            return ""
        return " ".join(words[:-hold_words])

    def _has_held_tail(self, text: str) -> bool:
        return len(tokenize(text)) > len(tokenize(self._commit_safe_text(text, False)))

    # A full stop, a question or an exclamation mark on the held word: the
    # model saying the sentence is over, rather than us noticing it paused.
    TERMINAL_PUNCTUATION = (".", "!", "?", "\u2026")

    def _should_flush_tail(self, now_ms: int) -> bool:
        if self._tail_flushed or not self._last_input_text or self._last_input_ms is None:
            return False
        mode = self.config.commit_tail_release
        if mode == "off":
            # The timer still runs, so the counters keep saying what the other
            # modes would have done. Nothing is released: see _release_tail.
            return now_ms - self._last_input_ms >= self.config.commit_tail_flush_ms
        if mode == "punctuation":
            words = tokenize(self._last_input_text)
            return bool(words) and words[-1].endswith(self.TERMINAL_PUNCTUATION)
        return now_ms - self._last_input_ms >= self.config.commit_tail_flush_ms


class Broadcaster:
    def __init__(self) -> None:
        self.clients: set[web.WebSocketResponse] = set()
        self.last: dict | None = None
        self.connections = 0
        self.disconnects = 0
        self.browser_ready = asyncio.Event()
        self.browser_ready_messages: list[dict[str, Any]] = []

    async def add(self, ws: web.WebSocketResponse) -> None:
        self.clients.add(ws)
        self.connections += 1
        if self.last:
            await ws.send_json(self.last)

    def remove(self, ws: web.WebSocketResponse) -> None:
        if ws in self.clients:
            self.disconnects += 1
            self.clients.discard(ws)

    async def publish(self, message: dict) -> None:
        self.last = message
        stale = []
        sent = 0
        for ws in self.clients:
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.remove(ws)
        return sent

    def mark_browser_ready(self, message: dict[str, Any]) -> None:
        self.browser_ready_messages.append(message)
        self.browser_ready.set()


def ms_between(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000.0


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_ms(values: list[float]) -> dict | None:
    if not values:
        return None
    sorted_values = sorted(values)

    def percentile(pct: float) -> float:
        idx = min(len(sorted_values) - 1, max(0, int((len(sorted_values) - 1) * pct)))
        return sorted_values[idx]

    return {
        "count": len(values),
        "mean_ms": statistics.fmean(values),
        "min_ms": min(values),
        "p50_ms": statistics.median(values),
        "p90_ms": percentile(0.90),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": max(values),
    }


class LatencyTracker:
    def __init__(self, pending_limit: int = 128) -> None:
        self.next_seq = 1
        self.pending_limit = pending_limit
        self.pending: dict[int, dict] = {}
        self.generated: list[dict] = []
        self.sent: list[dict] = []
        self.completed: list[dict] = []
        self.subtitle_states_generated = 0
        self.ui_updates_sent = 0
        self.render_acks_received = 0
        self.stale_updates_dropped = 0
        self.pending_overflow_dropped = 0
        self.render_states_obsoleted_by_ack = 0
        self.duplicate_acks = 0
        self.out_of_order_acks = 0
        self.stale_acks = 0
        self.max_pending_render_states = 0
        self.latest_generated_seq = 0
        self.latest_sent_seq = 0
        self.latest_rendered_seq = 0
        self.latest_seq_lag_samples: list[float] = []
        self.render_freshness_ms: list[float] = []
        self.browser_metrics = {
            "ui_updates_received": 0,
            "ui_frames_rendered": 0,
            "updates_coalesced_browser": 0,
            "latest_received_seq": 0,
            "latest_rendered_seq": 0,
            "display_state_changes": 0,
            "dom_updates": 0,
            "sentence_promotions": 0,
            "current_line_rewrites": 0,
            "duplicate_visible_states_skipped": 0,
        }

    def create(
        self,
        *,
        chunk: int,
        event_index: int,
        t0_mono_ns: int,
        t0_wall_ns: int,
        t1_mono_ns: int,
        t2_mono_ns: int,
        t3_mono_ns: int,
        t4_mono_ns: int,
        t4_wall_ns: int,
        is_final: bool,
    ) -> tuple[int, dict, dict]:
        seq = self.next_seq
        self.next_seq += 1
        self.subtitle_states_generated += 1
        self.latest_generated_seq = seq
        server_latency = {
            "seq": seq,
            "chunk": chunk,
            "event": event_index,
            "capture_latency_ms": None,
            "dsp_latency_ms": ms_between(t0_mono_ns, t1_mono_ns),
            "asr_latency_ms": ms_between(t2_mono_ns, t3_mono_ns),
            "stabilizer_latency_ms": ms_between(t3_mono_ns, t4_mono_ns),
            "app_to_subtitle_ms": ms_between(t0_mono_ns, t4_mono_ns),
            "render_latency_ms": None,
            "total_latency_ms": None,
        }
        internal = {
            "t0_wall_ns": t0_wall_ns,
            "t4_wall_ns": t4_wall_ns,
            "is_final": is_final,
        }
        self.generated.append(server_latency)
        return seq, server_latency, internal

    def mark_sent(self, seq: int, latency_payload: dict, internal: dict) -> None:
        if seq <= self.latest_sent_seq:
            return
        while len(self.pending) >= self.pending_limit:
            oldest = next(iter(self.pending))
            self.pending.pop(oldest, None)
            self.pending_overflow_dropped += 1
        self.latest_sent_seq = seq
        self.ui_updates_sent += 1
        self.pending[seq] = {
            **latency_payload,
            **internal,
            "t4_sent_mono_ns": time.monotonic_ns(),
        }
        self.max_pending_render_states = max(self.max_pending_render_states, len(self.pending))
        self.sent.append(latency_payload)

    def ack(self, message: dict) -> None:
        try:
            seq = int(message.get("rendered_seq", message.get("seq", 0)))
        except (TypeError, ValueError):
            return
        if seq == self.latest_rendered_seq:
            self.duplicate_acks += 1
        elif seq < self.latest_rendered_seq:
            self.out_of_order_acks += 1
        for key in (
            "ui_updates_received",
            "ui_frames_rendered",
            "updates_coalesced_browser",
            "latest_received_seq",
            "latest_rendered_seq",
            "display_state_changes",
            "dom_updates",
            "sentence_promotions",
            "current_line_rewrites",
            "duplicate_visible_states_skipped",
        ):
            value = message.get(key)
            if isinstance(value, (int, float)):
                self.browser_metrics[key] = max(self.browser_metrics[key], int(value))
        obsolete = [pending_seq for pending_seq in self.pending if pending_seq < seq]
        for pending_seq in obsolete:
            self.pending.pop(pending_seq, None)
            self.render_states_obsoleted_by_ack += 1
        record = self.pending.pop(seq, None)
        if record is None:
            self.stale_updates_dropped += 1
            self.stale_acks += 1
            return
        t6_mono_ns = int(message.get("_server_received_mono_ns", time.monotonic_ns()))
        server_ack_latency = ms_between(record["t4_sent_mono_ns"], t6_mono_ns)
        browser_receive_to_dom_ms = safe_float(message.get("browser_receive_to_dom_ms"))
        browser_receive_to_render_ms = safe_float(message.get("browser_receive_to_render_ms"))
        browser_render_to_ack_send_ms = safe_float(message.get("browser_render_to_ack_send_ms"))
        record["render_latency_ms"] = browser_receive_to_render_ms
        record["total_latency_ms"] = None
        record["browser_receive_to_dom_ms"] = browser_receive_to_dom_ms
        record["browser_receive_to_render_ms"] = browser_receive_to_render_ms
        record["browser_render_to_ack_send_ms"] = browser_render_to_ack_send_ms
        record["server_t4_to_t6_ack_ms"] = server_ack_latency
        record["clock_domain"] = "browser timings use performance.now only; server ACK latency uses server monotonic only"
        record["latest_seq_lag"] = max(0, self.latest_generated_seq - seq)
        record.pop("t0_wall_ns", None)
        record.pop("t4_wall_ns", None)
        record.pop("t4_sent_mono_ns", None)
        self.completed.append(record)
        self.render_acks_received += 1
        self.latest_rendered_seq = max(self.latest_rendered_seq, seq)
        self.latest_seq_lag_samples.append(record["latest_seq_lag"])
        if browser_receive_to_render_ms is not None:
            self.render_freshness_ms.append(browser_receive_to_render_ms)

    def drain(self, queue: asyncio.Queue) -> None:
        while True:
            try:
                message = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self.ack(message)

    def summary(self) -> dict:
        server_fields = [
            "dsp_latency_ms",
            "asr_latency_ms",
            "stabilizer_latency_ms",
            "app_to_subtitle_ms",
        ]
        browser_fields = [
            "render_latency_ms",
            "total_latency_ms",
            "browser_receive_to_dom_ms",
            "browser_receive_to_render_ms",
            "browser_render_to_ack_send_ms",
            "server_t4_to_t6_ack_ms",
        ]
        return {
            "schema": "T0 app audio available, T1 audio preprocessed, T2 ASR push, T3 ASR partial received, T4 server display-state send, T5 browser render observation, T6 server render ACK receipt",
            "capture_latency_ms": None,
            "capture_latency_note": "PipeWire source timestamp is not available yet; T0 starts when a decoded PCM chunk is available to HearAble.",
            "emitted_subtitle_count": len(self.generated),
            "render_ack_count": self.render_acks_received,
            "pending_render_acks": len(self.pending),
            "clock_domain_note": "T4->T6 uses server monotonic timestamps only. Browser receive/render durations use browser performance.now() only. Server and browser clocks are not subtracted from each other.",
            "stats": {
                **{
                    field: summarize_ms([r[field] for r in self.generated if r.get(field) is not None])
                    for field in server_fields
                },
                **{
                    field: summarize_ms([r[field] for r in self.completed if r.get(field) is not None])
                    for field in browser_fields
                },
            },
            "coalescing": self.coalescing_summary(),
            "ack_anomalies": {
                "duplicate_acks": self.duplicate_acks,
                "out_of_order_acks": self.out_of_order_acks,
                "stale_acks": self.stale_acks,
            },
        }

    def coalescing_summary(self) -> dict:
        ui_received = self.browser_metrics["ui_updates_received"]
        rendered = self.browser_metrics["ui_frames_rendered"]
        return {
            "subtitle_states_generated": self.subtitle_states_generated,
            "ui_updates_sent": self.ui_updates_sent,
            "ui_updates_received": ui_received,
            "ui_frames_rendered": rendered,
            "render_acks_received": self.render_acks_received,
            "updates_coalesced_browser": self.browser_metrics["updates_coalesced_browser"],
            "render_states_obsoleted_by_ack": self.render_states_obsoleted_by_ack,
            "stale_updates_dropped": self.stale_updates_dropped + self.pending_overflow_dropped,
            "pending_render_states": len(self.pending),
            "max_pending_render_states": self.max_pending_render_states,
            "pending_render_state_limit": self.pending_limit,
            "latest_generated_seq": self.latest_generated_seq,
            "latest_sent_seq": self.latest_sent_seq,
            "latest_rendered_seq": self.latest_rendered_seq,
            "display_state_changes": self.browser_metrics["display_state_changes"],
            "dom_updates": self.browser_metrics["dom_updates"],
            "sentence_promotions": self.browser_metrics["sentence_promotions"],
            "current_line_rewrites": self.browser_metrics["current_line_rewrites"],
            "duplicate_visible_states_skipped": self.browser_metrics["duplicate_visible_states_skipped"],
            "ack_rate_sent": self.render_acks_received / self.ui_updates_sent if self.ui_updates_sent else None,
            "render_rate_received": rendered / ui_received if ui_received else None,
            "latest_seq_lag": summarize_ms(self.latest_seq_lag_samples),
            "render_freshness_ms": summarize_ms(self.render_freshness_ms),
            "duplicate_acks": self.duplicate_acks,
            "out_of_order_acks": self.out_of_order_acks,
            "stale_acks": self.stale_acks,
        }

    def records(self) -> dict:
        return {
            "generated": self.generated,
            "sent": self.sent,
            "completed": self.completed,
            "pending": list(self.pending.values()),
        }


class UiCoalescingTransport:
    def __init__(self, broadcaster: Broadcaster, latency: LatencyTracker, max_fps: float) -> None:
        self.broadcaster = broadcaster
        self.latency = latency
        self.min_interval = 1.0 / max(1.0, max_fps)
        self.latest: tuple[dict, dict] | None = None
        self.sent_seq = 0
        self.updates_coalesced_server = 0
        self.closed = False
        self.wake = asyncio.Event()
        self.last_send = 0.0

    def offer(self, message: dict, internal_latency: dict) -> None:
        if self.latest and self.latest[0].get("seq", 0) > self.sent_seq:
            self.updates_coalesced_server += 1
        self.latest = (message, internal_latency)
        self.wake.set()

    async def _send_latest(self, *, force: bool = False) -> bool:
        if not self.latest:
            return False
        message, internal_latency = self.latest
        seq = int(message.get("seq") or 0)
        if seq <= self.sent_seq:
            return False
        now = time.monotonic()
        if not force and now - self.last_send < self.min_interval:
            return False
        if self.broadcaster.clients:
            self.latency.mark_sent(seq, message["latency"], internal_latency)
        sent_clients = await self.broadcaster.publish(message)
        if sent_clients:
            self.sent_seq = seq
            self.last_send = time.monotonic()
        return bool(sent_clients)

    async def run(self) -> None:
        while not self.closed:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.min_interval)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            delay = self.min_interval - (time.monotonic() - self.last_send)
            if delay > 0 and self.latest and int(self.latest[0].get("seq") or 0) > self.sent_seq:
                await asyncio.sleep(delay)
            await self._send_latest()
        await self._send_latest(force=True)

    async def close(self) -> None:
        self.closed = True
        self.wake.set()


class BrowserSubtitleSink:
    sink_type = "browser"
    supports_ack = True

    def __init__(self, broadcaster: Broadcaster, latency: LatencyTracker, max_fps: float) -> None:
        self.broadcaster = broadcaster
        self.latency = latency
        self.transport = UiCoalescingTransport(broadcaster, latency, max_fps)
        self.task: asyncio.Task | None = None
        self.closed = False

    async def open(self) -> None:
        self.closed = False
        self.task = asyncio.create_task(self.transport.run())

    async def publish(self, state: SubtitleState, internal_latency: dict[str, Any] | None = None) -> None:
        self.transport.offer(state.to_browser_message(), internal_latency or {})

    async def publish_status(self, status: str, *, source: str, metrics: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"type": "status", "status": status, "source": source}
        if metrics is not None:
            message["metrics"] = metrics
        await self.broadcaster.publish(message)

    async def clear(self) -> None:
        await self.broadcaster.publish({"type": "clear"})

    async def flush(self) -> None:
        await self.transport.close()
        if self.task:
            await self.task
            self.task = None

    async def close(self) -> None:
        await self.flush()
        self.closed = True

    def telemetry(self) -> dict[str, Any]:
        return {
            "type": self.sink_type,
            "supports_ack": True,
            "connections": self.broadcaster.connections,
            "disconnects": self.broadcaster.disconnects,
            "connected_at_stop": len(self.broadcaster.clients),
            "updates_coalesced_server": self.transport.updates_coalesced_server,
        }


def _create_browser_sink(options: dict[str, Any], **kwargs: Any) -> SubtitleSink:
    unknown = sorted(set(options))
    if unknown:
        raise BackendConfigError(f"subtitle sink 'browser' unknown option(s): {', '.join(unknown)}")
    return BrowserSubtitleSink(
        broadcaster=kwargs["broadcaster"],
        latency=kwargs["latency"],
        max_fps=kwargs["max_fps"],
    )


SubtitleSinkRegistry.register("browser", _create_browser_sink)


def write_metrics(metrics: dict, output: str) -> Path | None:
    path = Path(output)
    if path.exists() and path.is_dir():
        path = path / "metrics.json"
        print(f"[hearable] metrics output is a directory, using {path}", flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def max_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def write_jsonl(path: str, rows: list[dict]) -> Path | None:
    if not path:
        return None
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return output


def trace(args, message: str) -> None:
    if args.verbose_pipeline:
        print(f"[hearable] {message}", flush=True)


def ui_final_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return "... " + text[-max_chars:]


def subtitle_state_from_message(message: dict[str, Any]) -> SubtitleState:
    led = message.get("led") if isinstance(message.get("led"), dict) else {}
    return SubtitleState(
        seq=int(message.get("seq") or 0),
        timestamp=time.monotonic(),
        upper_line=str(led.get("previous_line", "")),
        lower_line=str(led.get("current_line", "")),
        stable=str(message.get("stable", "")),
        unstable=str(message.get("unstable", "")),
        final_text=str(message.get("final_text", "")),
        final_text_truncated=bool(message.get("final_text_truncated", False)),
        raw=str(message.get("raw", "")),
        is_final=bool(message.get("is_final", False)),
        audio_processed=safe_float(message.get("audio_processed")),
        confidence=safe_float(message.get("confidence")),
        led=led,
        latency=message.get("latency", {}) if isinstance(message.get("latency"), dict) else {},
        commit_events=message.get("commit_events", []) if isinstance(message.get("commit_events"), list) else [],
        metrics=message.get("metrics", {}) if isinstance(message.get("metrics"), dict) else {},
        metadata=message.get("metadata", {}) if isinstance(message.get("metadata"), dict) else {},
    )


def _config_path_value(key: str, value: Any) -> Any:
    if key not in PATH_CONFIG_KEYS or value in ("", None):
        return value
    path = Path(str(value))
    return str(path if path.is_absolute() else ROOT / path)


def load_runtime_defaults(path: str) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        return {}
    values = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError(f"runtime config must be a JSON object: {config_path}")
    unknown = sorted(set(values) - RUNTIME_CONFIG_KEYS)
    if unknown:
        raise ValueError(f"unknown runtime config keys in {config_path}: {', '.join(unknown)}")
    return {key: _config_path_value(key, value) for key, value in values.items()}


def load_audio_source_config(args: argparse.Namespace) -> BackendConfig:
    config = load_backend_config(args.audio_source_config, kind="audio source", root=ROOT)
    source_type = config.type
    options = dict(config.options)
    if args.input_wav:
        source_type = "wav"
        options = {"path": args.input_wav}
    elif source_type == "pipewire":
        if args.target:
            options["target"] = args.target
        options.setdefault("target", "")
        options["capture_latency"] = args.capture_latency
    return BackendConfig(path=config.path, type=source_type, options=options)


def load_subtitle_sink_config(args: argparse.Namespace) -> BackendConfig:
    return load_backend_config(args.subtitle_sink_config, kind="subtitle sink", root=ROOT)


def create_audio_source(args: argparse.Namespace) -> tuple[AudioSource, BackendConfig]:
    config = load_audio_source_config(args)
    source = AudioSourceRegistry.create(config.type, config.options, root=ROOT, chunk_ms=args.chunk_ms, mode=args.mode)
    return source, config


def create_subtitle_sink(
    args: argparse.Namespace,
    config: BackendConfig,
    *,
    broadcaster: Broadcaster,
    latency: LatencyTracker,
) -> SubtitleSink:
    return SubtitleSinkRegistry.create(
        config.type,
        config.options,
        root=ROOT,
        broadcaster=broadcaster,
        latency=latency,
        max_fps=args.ui_max_fps,
    )


def source_clock_record_from_frame(frame) -> dict[str, Any] | None:
    if frame.expected_feed_monotonic_ns is None or frame.source_clock_drift_ms is None:
        return None
    return {
        "chunk_index": frame.seq,
        "chunk_audio_start_ms": frame.source_start_time * 1000.0 if frame.source_start_time is not None else None,
        "chunk_audio_end_ms": frame.source_end_time * 1000.0 if frame.source_end_time is not None else None,
        "expected_feed_monotonic_ns": frame.expected_feed_monotonic_ns,
        "actual_feed_monotonic_ns": frame.monotonic_available_time_ns,
        "source_clock_drift_ms": frame.source_clock_drift_ms,
    }


async def pipeline(
    args,
    source: AudioSource,
    source_config: BackendConfig,
    sink: SubtitleSink,
    sink_config: BackendConfig,
    broadcaster: Broadcaster,
    stop: asyncio.Event,
    latency_acks: asyncio.Queue,
    latency: LatencyTracker,
) -> None:
    lib_dir = Path(args.lib_dir)
    model = Path(args.model)
    backend = "vulkan" if args.gpu >= 0 else "cpu"
    source_label = source.source_type
    readiness = {
        "required": bool(args.require_browser_ready),
        "ready": False,
        "timeout_sec": args.browser_ready_timeout,
        "wait_ms": None,
        "messages": [],
    }
    if args.require_browser_ready:
        if not isinstance(sink, BrowserSubtitleSink):
            raise BackendConfigError("--require-browser-ready requires subtitle sink type 'browser'")
        wait_start_ns = time.monotonic_ns()
        await sink.publish_status("waiting_browser_ready", source=source_label)
        try:
            await asyncio.wait_for(broadcaster.browser_ready.wait(), timeout=args.browser_ready_timeout)
            readiness["ready"] = True
        except asyncio.TimeoutError:
            readiness["ready"] = False
            raise TimeoutError(f"browser readiness timeout after {args.browser_ready_timeout}s")
        finally:
            readiness["wait_ms"] = ms_between(wait_start_ns, time.monotonic_ns())
            readiness["messages"] = broadcaster.browser_ready_messages[-5:]
    await sink.open()
    try:
        source.open()
        await source.start()
    except Exception:
        await sink.close()
        raise
    try:
        asr = NemoAsr(
            model=model,
            lib_dir=lib_dir,
            language=args.language,
            gpu=args.gpu,
            chunk_sec=args.chunk_ms / 1000.0,
            rnnt_right_context=args.rnnt_right_context,
        )
    except Exception:
        await source.close()
        await sink.close()
        raise
    stabilizer = SubtitleStabilizer(
        history_size=args.stabilizer_history_size,
        min_confirmations=args.stabilizer_min_confirmations,
        max_words=args.max_words,
        timeout_sec=args.stabilizer_timeout_sec,
    )
    led_bridge = LedSubtitleBridge(args.led_config)
    started = time.monotonic()
    chunks = 0
    dropped = 0
    events = 0
    subtitle_updates = 0
    timeout_updates = 0
    display_timeout_updates = 0
    max_pending_acks = 0
    final_text = ""
    last_raw = ""
    chunk_processing_ms: list[float] = []
    source_clock_records: list[dict[str, Any]] = []
    source_changes: list[dict[str, Any]] = []
    event_records: list[dict] = []

    if isinstance(sink, BrowserSubtitleSink):
        await sink.publish_status("running", source=source_label)
    print("[hearable] pipeline running", flush=True)

    try:
        while not stop.is_set():
            if args.duration and time.monotonic() - started >= args.duration:
                print("[hearable] duration reached", flush=True)
                break
            # A source that can change underneath us (a zap on the receiver) must
            # be handled before its new audio reaches the ASR, or the tail of the
            # previous channel is decoded as the head of the new one.
            if hasattr(source, "take_source_change"):
                change = source.take_source_change()
                if change is not None:
                    source_changes.append(change)
                    asr.reset_stream()
                    stabilizer.reset()
                    led_bridge.reset()
                    if hasattr(sink, "source_changed"):
                        epoch = await sink.source_changed(
                            change["service_ref"], reason="service_changed"
                        )
                        change["sink_source_epoch"] = epoch
                    else:
                        await sink.clear()
                    change["asr_reset_monotonic_ns"] = time.monotonic_ns()
                    print(
                        f"[hearable] source changed -> {change['service_ref']} "
                        f"(epoch {change['source_epoch']})",
                        flush=True,
                    )

            frame = await source.read_chunk()
            if frame is None:
                if source.eof:
                    break
                await asyncio.sleep(0.02)
                continue
            source_clock_record = source_clock_record_from_frame(frame)
            if source_clock_record and args.mode == "realtime":
                source_clock_records.append(source_clock_record)
            dropped += frame.dropped_chunks
            t0_mono_ns = frame.monotonic_available_time_ns
            t0_wall_ns = frame.wall_available_time_ns
            samples = frame.samples
            t1_mono_ns = time.monotonic_ns()
            if args.max_chunks and chunks >= args.max_chunks:
                break
            trace(args, f"push chunk={chunks + 1} samples={len(samples)}")
            t2_mono_ns = time.monotonic_ns()
            asr.push(samples)
            chunks += 1
            trace(args, f"next chunk={chunks}")
            chunk_event_count = 0
            for event in asr.next_events():
                t3_mono_ns = time.monotonic_ns()
                events += 1
                chunk_event_count += 1
                last_raw = event.transcript
                state = stabilizer.update(event.transcript, event.is_final)
                t4_mono_ns = time.monotonic_ns()
                t4_wall_ns = time.time_ns()
                if event.is_final and event.transcript:
                    final_text = state.final_text
                wall_since_start_ms = (time.monotonic() - started) * 1000.0
                led_payload, _ = led_bridge.update(event.transcript, event.is_final, int(wall_since_start_ms))
                seq, latency_payload, latency_internal = latency.create(
                    chunk=chunks,
                    event_index=events,
                    t0_mono_ns=t0_mono_ns,
                    t0_wall_ns=t0_wall_ns,
                    t1_mono_ns=t1_mono_ns,
                    t2_mono_ns=t2_mono_ns,
                    t3_mono_ns=t3_mono_ns,
                    t4_mono_ns=t4_mono_ns,
                    t4_wall_ns=t4_wall_ns,
                    is_final=event.is_final,
                )
                msg = {
                    "type": "subtitle",
                    "seq": seq,
                    "stable": state.stable,
                    "unstable": state.unstable,
                    "final_text": ui_final_text(state.final_text, args.ui_final_text_chars),
                    "final_text_truncated": len(state.final_text) > args.ui_final_text_chars > 0,
                    "raw": event.transcript,
                    "is_final": event.is_final,
                    "audio_processed": event.audio_processed,
                    "confidence": event.confidence,
                    "led": led_payload,
                    "latency": latency_payload | {"wall_since_start_ms": (time.monotonic() - started) * 1000.0},
                    "commit_events": led_bridge.last_commit_events,
                    "metrics": {"chunks": chunks, "events": events, "dropped_chunks": dropped},
                    # Where in the programme this audio came from. audio_processed
                    # counts from the start of the ASR stream, which stops matching
                    # the media position as soon as anything seeks.
                    "metadata": (
                        {"source_media_position_ms": source.asr_position_ms}
                        if hasattr(source, "asr_position_ms") else {}
                    ),
                }
                await sink.publish(subtitle_state_from_message(msg), latency_internal)
                subtitle_updates += 1
                max_pending_acks = max(max_pending_acks, len(latency.pending))
                event_records.append(
                    {
                        "seq": seq,
                        "chunk": chunks,
                        "event_index": events,
                        "wall_since_start_ms": (time.monotonic() - started) * 1000.0,
                        "audio_processed": event.audio_processed,
                        "is_final": event.is_final,
                        "confidence": event.confidence,
                        "raw": event.transcript,
                        "stable": state.stable,
                        "unstable": state.unstable,
                        "final_text": state.final_text,
                        "led": led_payload,
                        "commit_events": led_bridge.last_commit_events,
                        "source_clock": source_clock_record,
                        "latency": latency_payload,
                    }
                )
                latency.drain(latency_acks)
            chunk_processing_ms.append(ms_between(t2_mono_ns, time.monotonic_ns()))
            latency.drain(latency_acks)
            timeout_state = stabilizer.tick()
            if timeout_state and timeout_state.changed:
                now_mono = time.monotonic_ns()
                now_wall = time.time_ns()
                wall_since_start_ms = (time.monotonic() - started) * 1000.0
                led_payload, _ = led_bridge.update(timeout_state.stable, True, int(wall_since_start_ms))
                seq, latency_payload, latency_internal = latency.create(
                    chunk=chunks,
                    event_index=events,
                    t0_mono_ns=now_mono,
                    t0_wall_ns=now_wall,
                    t1_mono_ns=now_mono,
                    t2_mono_ns=now_mono,
                    t3_mono_ns=now_mono,
                    t4_mono_ns=now_mono,
                    t4_wall_ns=now_wall,
                    is_final=True,
                )
                msg = {
                        "type": "subtitle",
                        "seq": seq,
                        "stable": timeout_state.stable,
                        "unstable": timeout_state.unstable,
                        "final_text": ui_final_text(timeout_state.final_text, args.ui_final_text_chars),
                        "final_text_truncated": len(timeout_state.final_text) > args.ui_final_text_chars > 0,
                        "raw": timeout_state.stable,
                        "is_final": True,
                        "audio_processed": chunks * args.chunk_ms / 1000.0,
                        "confidence": 0.0,
                        "led": led_payload,
                        "latency": latency_payload | {"wall_since_start_ms": (time.monotonic() - started) * 1000.0},
                        "commit_events": led_bridge.last_commit_events,
                        "metrics": {"chunks": chunks, "events": events, "dropped_chunks": dropped},
                    }
                await sink.publish(subtitle_state_from_message(msg), latency_internal)
                subtitle_updates += 1
                timeout_updates += 1
            wall_since_start_ms = (time.monotonic() - started) * 1000.0
            led_payload, led_changed = led_bridge.tick(int(wall_since_start_ms))
            if led_changed:
                now_mono = time.monotonic_ns()
                now_wall = time.time_ns()
                seq, latency_payload, latency_internal = latency.create(
                    chunk=chunks,
                    event_index=events,
                    t0_mono_ns=now_mono,
                    t0_wall_ns=now_wall,
                    t1_mono_ns=now_mono,
                    t2_mono_ns=now_mono,
                    t3_mono_ns=now_mono,
                    t4_mono_ns=now_mono,
                    t4_wall_ns=now_wall,
                    is_final=True,
                )
                msg = {
                        "type": "subtitle",
                        "seq": seq,
                        "stable": "",
                        "unstable": "",
                        "final_text": ui_final_text(final_text, args.ui_final_text_chars),
                        "final_text_truncated": len(final_text) > args.ui_final_text_chars > 0,
                        "raw": "",
                        "is_final": True,
                        "audio_processed": chunks * args.chunk_ms / 1000.0,
                        "confidence": 0.0,
                        "led": led_payload,
                        "latency": latency_payload | {"wall_since_start_ms": wall_since_start_ms},
                        "commit_events": led_bridge.last_commit_events,
                        "metrics": {"chunks": chunks, "events": events, "dropped_chunks": dropped},
                    }
                await sink.publish(subtitle_state_from_message(msg), latency_internal)
                subtitle_updates += 1
                display_timeout_updates += 1
            await asyncio.sleep(0)
            latency.drain(latency_acks)
    finally:
        if args.flush_on_stop:
            for event in asr.finish():
                state = stabilizer.update(event.transcript, event.is_final)
                events += 1
                final_text = state.final_text or final_text
                last_raw = event.transcript or last_raw
                wall_since_start_ms = (time.monotonic() - started) * 1000.0
                led_payload, _ = led_bridge.update(event.transcript, event.is_final, int(wall_since_start_ms))
                seq, latency_payload, latency_internal = latency.create(
                    chunk=chunks,
                    event_index=events,
                    t0_mono_ns=time.monotonic_ns(),
                    t0_wall_ns=time.time_ns(),
                    t1_mono_ns=time.monotonic_ns(),
                    t2_mono_ns=time.monotonic_ns(),
                    t3_mono_ns=time.monotonic_ns(),
                    t4_mono_ns=time.monotonic_ns(),
                    t4_wall_ns=time.time_ns(),
                    is_final=event.is_final,
                )
                msg = {
                        "type": "subtitle",
                        "seq": seq,
                        "stable": state.stable,
                        "unstable": state.unstable,
                        "final_text": ui_final_text(state.final_text, args.ui_final_text_chars),
                        "final_text_truncated": len(state.final_text) > args.ui_final_text_chars > 0,
                        "raw": event.transcript,
                        "is_final": event.is_final,
                        "audio_processed": event.audio_processed,
                        "confidence": event.confidence,
                        "led": led_payload,
                        "latency": latency_payload | {"wall_since_start_ms": (time.monotonic() - started) * 1000.0},
                        "commit_events": led_bridge.last_commit_events,
                        "metrics": {"chunks": chunks, "events": events, "dropped_chunks": dropped},
                    }
                await sink.publish(subtitle_state_from_message(msg), latency_internal)
                subtitle_updates += 1
                event_records.append(
                    {
                        "seq": seq,
                        "chunk": chunks,
                        "event_index": events,
                        "wall_since_start_ms": (time.monotonic() - started) * 1000.0,
                        "audio_processed": event.audio_processed,
                        "is_final": event.is_final,
                        "confidence": event.confidence,
                        "raw": event.transcript,
                        "stable": state.stable,
                        "unstable": state.unstable,
                        "final_text": state.final_text,
                        "led": led_payload,
                        "commit_events": led_bridge.last_commit_events,
                        "latency": latency_payload,
                    }
                )
                latency.drain(latency_acks)
        await sink.flush()
        final_ack_barrier = {
            "target_seq": latency.latest_sent_seq,
            "acknowledged_seq": latency.latest_rendered_seq,
            "passed": latency.latest_sent_seq == 0 or latency.latest_rendered_seq >= latency.latest_sent_seq,
            "timeout_sec": args.final_ack_wait,
            "wait_ms": 0.0,
        }
        if getattr(sink, "supports_ack", False) and latency.latest_sent_seq and latency.latest_rendered_seq < latency.latest_sent_seq:
            wait_start_ns = time.monotonic_ns()
            deadline = time.monotonic() + args.final_ack_wait
            while time.monotonic() < deadline and latency.latest_rendered_seq < latency.latest_sent_seq:
                latency.drain(latency_acks)
                await asyncio.sleep(0.01)
            final_ack_barrier["wait_ms"] = ms_between(wait_start_ns, time.monotonic_ns())
            final_ack_barrier["acknowledged_seq"] = latency.latest_rendered_seq
            final_ack_barrier["passed"] = latency.latest_rendered_seq >= latency.latest_sent_seq
        elif getattr(sink, "supports_ack", False):
            await asyncio.sleep(args.final_ack_wait)
        latency.drain(latency_acks)
        asr.close()
        await source.close()
        latency.drain(latency_acks)
        latency_summary = latency.summary()
        coalescing = latency_summary["coalescing"]
        sink_telemetry = sink.telemetry()
        source_telemetry = source.telemetry() if hasattr(source, "telemetry") else {"type": source.source_type}
        coalescing["updates_coalesced_server"] = sink_telemetry.get("updates_coalesced_server", 0)
        render_acks = latency_summary["render_ack_count"]
        audio_seconds = chunks * args.chunk_ms / 1000.0
        compute_seconds = sum(chunk_processing_ms) / 1000.0
        source_drift_values = [record["source_clock_drift_ms"] for record in source_clock_records]
        source_drift_summary = summarize_ms(source_drift_values)
        source_drift_final = source_drift_values[-1] if source_drift_values else None
        metrics = {
            "project": "HearAble",
            "version": __version__,
            "source": source.source_type,
            "audio_source": source_telemetry,
            "source_changes": source_changes,
            "subtitle_sink_telemetry": sink.telemetry() if hasattr(sink, "telemetry") else {},
            "tail_flush": led_bridge.tail_diagnostics(),
            "mode": args.mode,
            "browser_readiness": readiness,
            "configuration": {
                "runtime_config": args.runtime_config,
                "audio_source_config": source_config.path,
                "audio_source": {"type": source_config.type, "options": source_config.options},
                "subtitle_sink_config": sink_config.path,
                "subtitle_sink": {"type": sink_config.type, "options": sink_config.options},
                "model": str(model),
                "lib_dir": str(lib_dir),
                "language": args.language,
                "backend": backend,
                "gpu": args.gpu,
                "chunk_ms": args.chunk_ms,
                "rnnt_right_context": args.rnnt_right_context,
                "punctuation": True,
                "verbatim": False,
                "max_words": args.max_words,
                "stabilizer_history_size": args.stabilizer_history_size,
                "stabilizer_min_confirmations": args.stabilizer_min_confirmations,
                "stabilizer_timeout_sec": args.stabilizer_timeout_sec,
                "capture_latency": args.capture_latency,
                "ui_max_fps": args.ui_max_fps,
                "ui_final_text_chars": args.ui_final_text_chars,
                "pending_render_limit": args.pending_render_limit,
                "led_config": args.led_config,
                "led_max_chars": led_bridge.config.max_chars,
                "led_commit_tail_hold_words": led_bridge.config.commit_tail_hold_words,
                "led_commit_tail_flush_ms": led_bridge.config.commit_tail_flush_ms,
                "mode": args.mode,
            },
            "machine": collect_machine_info(model, backend),
            "chunks": chunks,
            "events": events,
            "subtitle_updates": subtitle_updates,
            "timeout_updates": timeout_updates,
            "display_timeout_updates": display_timeout_updates,
            "dropped_chunks": dropped,
            "backlog": {
                "max_pending_render_acks": coalescing.get("max_pending_render_states", max_pending_acks),
                "pending_render_acks_at_stop": len(latency.pending),
            },
            "websocket": {
                "connections": sink_telemetry.get("connections", 0),
                "disconnects": sink_telemetry.get("disconnects", 0),
                "connected_at_stop": sink_telemetry.get("connected_at_stop", 0),
            },
            "wall_seconds": time.monotonic() - started,
            "audio_seconds": audio_seconds,
            "rtf_estimate": (time.monotonic() - started) / audio_seconds if audio_seconds else None,
            "rtf_estimate_note": "Deprecated compatibility field; use compute.compute_rtf or realtime.wall_rtf.",
            "compute": {
                "audio_duration_seconds": audio_seconds,
                "compute_wall_seconds": compute_seconds,
                "compute_rtf": compute_seconds / audio_seconds if audio_seconds else None,
                "chunk_processing": summarize_ms(chunk_processing_ms),
                "dropped_chunks": dropped,
            },
            "realtime": {
                "mode": args.mode,
                "wall_rtf": (time.monotonic() - started) / audio_seconds if audio_seconds else None,
                "pacing_strategy": "absolute_deadline_from_audio_timeline" if source.source_type == "wav" and args.mode == "realtime" else "none",
                "source_clock_drift_ms": source_drift_summary,
                "source_clock_drift_final_ms": source_drift_final,
                "source_clock_drift_growth": drift_growth_indicator(source_clock_records),
                "source_clock_records": source_clock_records,
            },
            "render_ack_rate": coalescing.get("ack_rate_sent"),
            "raw_state_ack_rate": render_acks / subtitle_updates if subtitle_updates and getattr(sink, "supports_ack", False) else None,
            "chunk_processing": summarize_ms(chunk_processing_ms),
            "peak_ram_bytes": max_rss_bytes(),
            "latency": latency_summary,
            "coalescing": coalescing,
            "subtitle_sink": sink_telemetry,
            "final_ack_barrier": final_ack_barrier,
            "latency_records": latency.records() if args.include_latency_records else None,
            "final_text": final_text,
            "last_raw": last_raw,
        }
        log_metrics = {key: value for key, value in metrics.items() if key not in {"final_text", "latency_records", "last_raw"}}
        if isinstance(log_metrics.get("realtime"), dict):
            log_metrics["realtime"] = {key: value for key, value in log_metrics["realtime"].items() if key != "source_clock_records"}
        log_metrics["final_text_chars"] = len(final_text)
        log_metrics["last_raw_chars"] = len(last_raw)
        log_metrics["latency_records_included"] = bool(args.include_latency_records)
        print(f"[hearable] stopped: {json.dumps(log_metrics, ensure_ascii=False)}", flush=True)
        if args.events_output:
            try:
                saved_events = write_jsonl(args.events_output, event_records)
                if saved_events:
                    print(f"[hearable] events written: {saved_events}", flush=True)
            except OSError as exc:
                print(f"[hearable] warning: events not written: {exc}", flush=True)
        if args.metrics_output:
            try:
                saved = write_metrics(metrics, args.metrics_output)
                if saved:
                    print(f"[hearable] metrics written: {saved}", flush=True)
            except OSError as exc:
                print(f"[hearable] warning: metrics not written: {exc}", flush=True)
        if isinstance(sink, BrowserSubtitleSink):
            await sink.publish_status("stopped", source=source_label, metrics=log_metrics)
        await sink.close()


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "web" / "index.html")


async def subtitle_display_js(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "web" / "subtitle_display.js")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    broadcaster: Broadcaster = request.app["broadcaster"]
    latency_acks: asyncio.Queue = request.app["latency_acks"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await broadcaster.add(ws)
    try:
        async for ws_msg in ws:
            if ws_msg.type != WSMsgType.TEXT:
                continue
            try:
                message = json.loads(ws_msg.data)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "browser_ready":
                message["_server_received_mono_ns"] = time.monotonic_ns()
                message["_server_received_wall_ns"] = time.time_ns()
                broadcaster.mark_browser_ready(message)
            if message.get("type") == "render_ack":
                message["_server_received_mono_ns"] = time.monotonic_ns()
                message["_server_received_wall_ns"] = time.time_ns()
                await latency_acks.put(message)
    finally:
        broadcaster.remove(ws)
    return ws


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--runtime-config", default=str(DEFAULT_RUNTIME_CONFIG_PATH))
    pre_args, _ = pre_parser.parse_known_args()
    defaults = load_runtime_defaults(pre_args.runtime_config)

    def default(key: str, fallback: Any) -> Any:
        return defaults.get(key, fallback)

    parser = argparse.ArgumentParser(
        description="HearAble realtime local subtitle server",
        parents=[pre_parser],
    )
    parser.add_argument("--version", "-V", action="version", version=f"HearAble {__version__}")
    parser.add_argument("--audio-source-config", default=default("audio_source_config", str(DEFAULT_AUDIO_SOURCE_CONFIG_PATH)))
    parser.add_argument("--subtitle-sink-config", default=default("subtitle_sink_config", str(DEFAULT_SUBTITLE_SINK_CONFIG_PATH)))
    parser.add_argument("--model", default=default("model", str(ROOT / "models/nemotron-3.5-asr-streaming-0.6b.q8_0.gguf")))
    parser.add_argument("--lib-dir", default=default("lib_dir", str(ROOT / "upstream/NeMo-Speech.cpp/build/cpu-asr-make/bin")))
    parser.add_argument("--language", default=default("language", "it-IT"))
    parser.add_argument("--target", default=default("target", ""))
    parser.add_argument("--input-wav", default=default("input_wav", ""))
    parser.add_argument("--mode", choices=("realtime", "throughput"), default=default("mode", "realtime"))
    parser.add_argument("--require-browser-ready", action="store_true", default=default("require_browser_ready", False))
    parser.add_argument("--browser-ready-timeout", type=float, default=default("browser_ready_timeout", 30.0))
    parser.add_argument("--host", default=default("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=default("port", 8765))
    parser.add_argument("--duration", type=float, default=default("duration", 0.0))
    parser.add_argument("--chunk-ms", type=int, default=default("chunk_ms", 160))
    parser.add_argument("--gpu", type=int, default=default("gpu", -1), help="GPU index, or -1 for CPU")
    parser.add_argument("--rnnt-right-context", type=int, default=default("rnnt_right_context", 1))
    parser.add_argument("--capture-latency", default=default("capture_latency", "20ms"))
    parser.add_argument("--max-words", type=int, default=default("max_words", 18))
    parser.add_argument("--stabilizer-history-size", type=int, default=default("stabilizer_history_size", 4))
    parser.add_argument("--stabilizer-min-confirmations", type=int, default=default("stabilizer_min_confirmations", 3))
    parser.add_argument("--stabilizer-timeout-sec", type=float, default=default("stabilizer_timeout_sec", 1.2))
    parser.add_argument("--max-chunks", type=int, default=default("max_chunks", 0))
    parser.add_argument("--flush-on-stop", action="store_true", default=default("flush_on_stop", False))
    parser.add_argument("--metrics-output", default=default("metrics_output", ""))
    parser.add_argument("--events-output", default=default("events_output", ""))
    parser.add_argument("--include-latency-records", action="store_true", default=default("include_latency_records", False))
    parser.add_argument("--ui-max-fps", type=float, default=default("ui_max_fps", 6.0))
    parser.add_argument("--ui-final-text-chars", type=int, default=default("ui_final_text_chars", 4000))
    parser.add_argument("--led-config", default=default("led_config", str(ROOT / "config" / "led_subtitles.json")))
    parser.add_argument("--pending-render-limit", type=int, default=default("pending_render_limit", 128))
    parser.add_argument("--final-ack-wait", type=float, default=default("final_ack_wait", 0.5))
    parser.add_argument("--verbose-pipeline", action="store_true", default=default("verbose_pipeline", False))
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    stop = asyncio.Event()
    broadcaster = Broadcaster()
    latency_acks: asyncio.Queue = asyncio.Queue()
    latency = LatencyTracker(pending_limit=args.pending_render_limit)
    source, source_config = create_audio_source(args)
    sink_config = load_subtitle_sink_config(args)
    sink = create_subtitle_sink(args, sink_config, broadcaster=broadcaster, latency=latency)

    runner: web.AppRunner | None = None
    if isinstance(sink, BrowserSubtitleSink):
        app = web.Application()
        app["broadcaster"] = broadcaster
        app["latency_acks"] = latency_acks
        app.router.add_get("/", index)
        app.router.add_get("/subtitle_display.js", subtitle_display_js)
        app.router.add_get("/ws", ws_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()
        print(f"HearAble UI: http://{args.host}:{args.port}", flush=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(
        pipeline(args, source, source_config, sink, sink_config, broadcaster, stop, latency_acks, latency)
    )
    try:
        await task
    finally:
        if runner is not None:
            await runner.cleanup()


if __name__ == "__main__":
    os.environ.setdefault("LD_LIBRARY_PATH", str(ROOT / "upstream/NeMo-Speech.cpp/build/cpu-asr-make/bin"))
    asyncio.run(main())
