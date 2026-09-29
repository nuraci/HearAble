from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "led_subtitles.json"


@dataclass(frozen=True)
class LedSubtitleConfig:
    max_chars: int = 40
    soft_break_min: int = 28
    commit_stable_updates: int = 2
    commit_lookahead: int = 2
    commit_tail_hold_words: int = 6
    commit_tail_flush_ms: int = 1200
    scroll_ms: int = 120
    min_line_hold_ms: int = 1500
    idle_clear_ms: int = 5500
    popon_gap_ms: int = 120
    popon_min_ms: int = 1000
    popon_max_ms: int = 6000
    reading_cps: int = 18
    brightness_min: int = 5
    brightness_max: int = 100
    show_unstable_tail: bool = False
    global_offset_ms: int = 0
    diarization_confidence_min: float = 0.65
    orphan_words: tuple[str, ...] = field(default_factory=lambda: (
        "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
        "di", "a", "da", "in", "con", "su", "per", "tra", "fra",
        "del", "della", "dei", "delle", "al", "alla", "ai", "alle",
        "dal", "dalla", "nel", "nella", "che", "e", "ed", "ma",
        "o", "se", "non",
    ))

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "LedSubtitleConfig":
        return cls(
            max_chars=int(values.get("MAX_CHARS", cls.max_chars)),
            soft_break_min=int(values.get("SOFT_BREAK_MIN", cls.soft_break_min)),
            commit_stable_updates=int(values.get("COMMIT_STABLE_UPDATES", cls.commit_stable_updates)),
            commit_lookahead=int(values.get("COMMIT_LOOKAHEAD", cls.commit_lookahead)),
            commit_tail_hold_words=int(values.get("COMMIT_TAIL_HOLD_WORDS", cls.commit_tail_hold_words)),
            commit_tail_flush_ms=int(values.get("COMMIT_TAIL_FLUSH_MS", cls.commit_tail_flush_ms)),
            scroll_ms=min(250, int(values.get("SCROLL_MS", cls.scroll_ms))),
            min_line_hold_ms=int(values.get("MIN_LINE_HOLD_MS", cls.min_line_hold_ms)),
            idle_clear_ms=int(values.get("IDLE_CLEAR_MS", cls.idle_clear_ms)),
            popon_gap_ms=int(values.get("POPON_GAP_MS", cls.popon_gap_ms)),
            popon_min_ms=int(values.get("POPON_MIN_MS", cls.popon_min_ms)),
            popon_max_ms=int(values.get("POPON_MAX_MS", cls.popon_max_ms)),
            reading_cps=int(values.get("READING_CPS", cls.reading_cps)),
            brightness_min=int(values.get("BRIGHTNESS_MIN", cls.brightness_min)),
            brightness_max=int(values.get("BRIGHTNESS_MAX", cls.brightness_max)),
            show_unstable_tail=bool(values.get("SHOW_UNSTABLE_TAIL", cls.show_unstable_tail)),
            global_offset_ms=int(values.get("GLOBAL_OFFSET_MS", cls.global_offset_ms)),
            diarization_confidence_min=float(values.get("DIARIZATION_CONFIDENCE_MIN", cls.diarization_confidence_min)),
            orphan_words=tuple(str(word).lower() for word in values.get("ORPHAN_WORDS", cls().orphan_words)),
        )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> LedSubtitleConfig:
    config_path = Path(path)
    values = json.loads(config_path.read_text(encoding="utf-8"))
    return LedSubtitleConfig.from_mapping(values)
