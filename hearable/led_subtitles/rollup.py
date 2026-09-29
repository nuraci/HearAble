from __future__ import annotations

from dataclasses import dataclass

from .config import LedSubtitleConfig
from .layout import candidate_fits, is_orphan_word, is_soft_break, line_text


@dataclass(frozen=True)
class SubtitleFrame:
    top: str = ""
    bottom: str = ""
    status: str = "listening"


class RollUpRenderer:
    def __init__(self, config: LedSubtitleConfig):
        self.config = config
        self.top_words: list[str] = []
        self.bottom_words: list[str] = []
        self.pending_prefix_words: list[str] = []
        self.last_word_ms: int | None = None
        self.top_since_ms: int | None = None
        self.scroll_count = 0
        self.scroll_times_ms: list[int] = []
        self.discarded_words: list[str] = []
        self.status = "listening"

    def frame(self) -> SubtitleFrame:
        return SubtitleFrame(
            top=line_text(self.top_words),
            bottom=line_text(self.bottom_words),
            status=self.status,
        )

    def clear(self) -> None:
        self.top_words.clear()
        self.bottom_words.clear()
        self.pending_prefix_words.clear()
        self.status = "listening"

    def scene_change(self) -> None:
        self.clear()
        self.last_word_ms = None
        self.top_since_ms = None

    def set_audio_error(self) -> None:
        self.clear()
        self.status = "audio_error"
        self.bottom_words = ["---"]

    def ingest_words(self, words: list[str], now_ms: int, scene_change: bool = False) -> None:
        if scene_change:
            self.scene_change()
            return
        for word in words:
            self._ingest_word(word, now_ms)
        if words and not self.pending_prefix_words:
            self.last_word_ms = now_ms

    def tick(self, now_ms: int) -> None:
        if self.last_word_ms is not None and now_ms - self.last_word_ms >= self.config.idle_clear_ms:
            self.clear()

    def _ingest_word(self, word: str, now_ms: int) -> None:
        if len(word) > self.config.max_chars:
            self.discarded_words.append(word)
            return

        if is_orphan_word(word, self.config):
            self.pending_prefix_words.append(word)
            return

        if self.pending_prefix_words:
            group = [*self.pending_prefix_words, word]
            self.pending_prefix_words.clear()
            self._ingest_group(group, now_ms)
            return

        self._ingest_group([word], now_ms)

    def _ingest_group(self, group: list[str], now_ms: int) -> None:
        if len(line_text(group)) > self.config.max_chars:
            self.discarded_words.extend(group)
            return

        if not self.bottom_words:
            self.bottom_words.extend(group)
            if is_soft_break(line_text(self.bottom_words), self.config):
                self._try_scroll(now_ms)
            return

        if len(line_text([*self.bottom_words, *group])) <= self.config.max_chars:
            self.bottom_words.extend(group)
            if is_soft_break(line_text(self.bottom_words), self.config):
                self._try_scroll(now_ms)
            return

        moved_orphan: str | None = None
        if self.bottom_words and is_orphan_word(self.bottom_words[-1], self.config):
            moved_orphan = self.bottom_words.pop()

        if not self._try_scroll(now_ms):
            if moved_orphan is not None:
                self.bottom_words.append(moved_orphan)
            self.discarded_words.extend(group)
            return

        if moved_orphan is not None:
            self.bottom_words.append(moved_orphan)

        if len(line_text([*self.bottom_words, *group])) <= self.config.max_chars:
            self.bottom_words.extend(group)
        else:
            self.discarded_words.extend(group)

    def _try_scroll(self, now_ms: int) -> bool:
        if not self.bottom_words:
            return True
        if not self._can_replace_top(now_ms):
            return False
        self.top_words = list(self.bottom_words)
        self.bottom_words.clear()
        self.top_since_ms = now_ms
        self.scroll_count += 1
        self.scroll_times_ms.append(now_ms)
        return True

    def _can_replace_top(self, now_ms: int) -> bool:
        if not self.top_words:
            return True
        if self.top_since_ms is None:
            return True
        return now_ms - self.top_since_ms >= self.config.min_line_hold_ms
