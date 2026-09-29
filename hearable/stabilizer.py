from __future__ import annotations

from dataclasses import dataclass, field
import time


def _words(text: str) -> list[str]:
    return text.strip().split()


def _common_prefix(items: list[list[str]]) -> list[str]:
    if not items:
        return []
    prefix = items[0]
    for words in items[1:]:
        n = 0
        for a, b in zip(prefix, words):
            if a != b:
                break
            n += 1
        prefix = prefix[:n]
        if not prefix:
            break
    return prefix


@dataclass
class SubtitleState:
    stable: str = ""
    unstable: str = ""
    final_text: str = ""
    is_final: bool = False
    changed: bool = False


@dataclass
class SubtitleStabilizer:
    history_size: int = 4
    min_confirmations: int = 3
    max_words: int = 18
    timeout_sec: float = 1.2
    _history: list[str] = field(default_factory=list)
    _stable_words: list[str] = field(default_factory=list)
    _last_update: float = field(default_factory=time.monotonic)
    _final_segments: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Forget everything about the previous source.

        Called on a channel change: partial history, confirmed words and finalized
        segments all belong to the old service, and carrying any of them across
        would put the previous channel's words on the new channel's picture.
        """
        self._history.clear()
        self._stable_words.clear()
        self._final_segments.clear()
        self._last_update = time.monotonic()

    def update(self, text: str, is_final: bool = False) -> SubtitleState:
        text = " ".join(text.strip().split())
        previous = self.render()
        now = time.monotonic()
        self._last_update = now

        if is_final:
            if text:
                self._final_segments.append(text)
            self._history.clear()
            self._stable_words.clear()
            state = SubtitleState(
                stable=text,
                unstable="",
                final_text=" ".join(self._final_segments),
                is_final=True,
            )
            state.changed = previous != self.render_from_state(state)
            return state

        if text:
            self._history.append(text)
            self._history = self._history[-self.history_size :]

        candidate: list[str] = []
        if len(self._history) >= self.min_confirmations:
            candidate = _common_prefix([_words(item) for item in self._history[-self.min_confirmations :]])

        if len(candidate) > len(self._stable_words):
            self._stable_words = candidate

        all_words = _words(text)
        stable_len = min(len(self._stable_words), len(all_words))
        stable = " ".join(all_words[:stable_len])
        unstable = " ".join(all_words[stable_len:])

        stable, unstable = self._fit_lines(stable, unstable)
        state = SubtitleState(
            stable=stable,
            unstable=unstable,
            final_text=" ".join(self._final_segments),
            is_final=False,
        )
        state.changed = previous != self.render_from_state(state)
        return state

    def tick(self) -> SubtitleState | None:
        if not self._history:
            return None
        if time.monotonic() - self._last_update < self.timeout_sec:
            return None
        text = self._history[-1]
        return self.update(text, is_final=True)

    def render(self) -> str:
        stable = " ".join(self._stable_words)
        unstable = ""
        if self._history:
            words = _words(self._history[-1])
            unstable = " ".join(words[len(self._stable_words) :])
        stable, unstable = self._fit_lines(stable, unstable)
        return self.render_from_state(SubtitleState(stable=stable, unstable=unstable))

    @staticmethod
    def render_from_state(state: SubtitleState) -> str:
        return " ".join(part for part in (state.stable, state.unstable) if part).strip()

    def _fit_lines(self, stable: str, unstable: str) -> tuple[str, str]:
        all_words = _words(" ".join(part for part in (stable, unstable) if part))
        if len(all_words) <= self.max_words:
            return stable, unstable
        stable_words = _words(stable)
        clipped_from_left = len(all_words) - self.max_words
        words = all_words[-self.max_words :]
        stable_count = max(0, len(stable_words) - clipped_from_left)
        stable_count = min(stable_count, len(words))
        return " ".join(words[:stable_count]), " ".join(words[stable_count:])
