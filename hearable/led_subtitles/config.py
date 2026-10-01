from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "led_subtitles.json"


def _release_mode(values: dict[str, Any], default: str) -> str:
    """The mode, accepting the boolean key this replaced.

    An installation configured before the three modes existed said
    COMMIT_TAIL_FLUSH_APPENDS: true, which meant what is now called "time".
    Reading it still works rather than silently reverting someone's choice.
    """
    mode = values.get("COMMIT_TAIL_RELEASE")
    if mode is None:
        legacy = values.get("COMMIT_TAIL_FLUSH_APPENDS")
        if legacy is None:
            return default
        return "time" if bool(legacy) else "off"
    mode = str(mode).strip().lower()
    return mode if mode in ("off", "time", "punctuation") else default


@dataclass(frozen=True)
class LedSubtitleConfig:
    max_chars: int = 40
    soft_break_min: int = 28
    commit_stable_updates: int = 2
    commit_lookahead: int = 2
    commit_tail_hold_words: int = 6
    commit_tail_flush_ms: int = 1200
    # What, if anything, is allowed to let the held last word onto the screen
    # before another word arrives behind it.
    #
    #   "off"           nothing does. The last word of a sentence waits for the
    #                   next sentence. This is the shipped behaviour, and the
    #                   default, because the two alternatives were both tried
    #                   on real television and only one of them survived.
    #
    #   "time"          release after COMMIT_TAIL_FLUSH_MS of the hypothesis not
    #                   changing. Measured over two sessions: 186 and 240
    #                   releases, of which 55% and 48% were words the model
    #                   then changed. The viewer's verdict was "e' peggiorato".
    #                   Kept only so the comparison can be repeated.
    #
    #   "punctuation"   release when the model has put a full stop, question or
    #                   exclamation mark on the held word — a claim that the
    #                   sentence is finished, rather than an observation that it
    #                   paused. Measured on the same two sessions: 114 and 172
    #                   releases, 4.4% and 0.0% later changed.
    #
    # Raising the time threshold does not rescue the time rule: at three seconds
    # it is still one word in five, by which point the word is late enough that
    # waiting for the next sentence costs no more.
    commit_tail_release: str = "off"
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
            commit_tail_release=_release_mode(values, cls.commit_tail_release),
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
