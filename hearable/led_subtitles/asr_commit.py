from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from .config import LedSubtitleConfig
from .layout import tokenize


@dataclass(frozen=True)
class CommitResult:
    committed_words: list[str]
    unstable_words: list[str]
    commit_events: list[dict] = field(default_factory=list)


class AsrCommitter:
    def __init__(self, config: LedSubtitleConfig):
        self.config = config
        self.committed_words: list[str] = []
        self._last_words: list[str] = []
        self._same_counts: dict[int, int] = {}

    @property
    def committed_text(self) -> str:
        return " ".join(self.committed_words)

    def ingest(self, text: str) -> CommitResult:
        words = tokenize(text)
        self._update_stability(words)
        new_words: list[str] = []
        commit_events: list[dict] = []

        while len(self.committed_words) < len(words):
            index = len(self.committed_words)
            reason = self._commit_reason(index, words)
            if reason is None:
                break
            self.committed_words.append(words[index])
            new_words.append(words[index])
            commit_events.append(self._commit_event(index, words, reason))

        self._last_words = words
        unstable = words[len(self.committed_words):] if self.config.show_unstable_tail else []
        return CommitResult(new_words, unstable, commit_events)

    def force_ingest(self, text: str, reason: str = "ASR_FINAL") -> CommitResult:
        words = tokenize(text)
        if words[:len(self.committed_words)] != self.committed_words:
            self._update_stability(words)
            self._last_words = words
            unstable = words[len(self.committed_words):] if self.config.show_unstable_tail else []
            return CommitResult([], unstable)

        start_index = len(self.committed_words)
        new_words = words[len(self.committed_words):]
        self.committed_words.extend(new_words)
        self._update_stability(words)
        self._last_words = words
        commit_events = [
            self._commit_event(index, words, reason)
            for index in range(start_index, len(words))
        ]
        return CommitResult(new_words, [], commit_events)

    def reset(self) -> None:
        self.committed_words.clear()
        self._last_words.clear()
        self._same_counts.clear()

    def _update_stability(self, words: list[str]) -> None:
        next_counts: dict[int, int] = {}
        for index, word in enumerate(words):
            if index < len(self._last_words) and self._last_words[index] == word:
                next_counts[index] = self._same_counts.get(index, 1) + 1
            else:
                next_counts[index] = 1
        self._same_counts = next_counts

    def _can_commit(self, index: int, words: list[str]) -> bool:
        return self._commit_reason(index, words) is not None

    def _commit_reason(self, index: int, words: list[str]) -> str | None:
        if index < len(self.committed_words):
            return None
        if index >= len(words):
            return None
        if index < len(self._last_words) and index < len(self.committed_words):
            return "OTHER" if words[index] == self._last_words[index] else None
        stable_count = self._same_counts.get(index, 0)
        has_stable_updates = stable_count >= self.config.commit_stable_updates
        has_lookahead = len(words) - index - 1 >= self.config.commit_lookahead
        if has_stable_updates and has_lookahead:
            return "STABLE_AND_LOOKAHEAD"
        if has_stable_updates:
            return "STABLE_UPDATES"
        if has_lookahead:
            return "LOOKAHEAD"
        return None

    def _commit_event(self, index: int, words: list[str], reason: str) -> dict:
        return {
            "index": index,
            "word": words[index],
            "reason": reason,
            "stable_count": self._same_counts.get(index, 0),
            "lookahead_count": max(0, len(words) - index - 1),
            "tail_position": len(words) - index,
        }
