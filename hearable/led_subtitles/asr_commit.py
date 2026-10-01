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
        # Diagnostic counters: a refusal is silent by construction.
        self.refusals = 0
        self.last_refusal: dict = {}

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
            # Diagnostic only, and the reason this exists: a refusal here
            # returns no words and no events, so from the outside it is
            # indistinguishable from a flush that never fired. The field said
            # TAIL_FLUSH_TIMEOUT appeared zero times in 1353 commits; whether
            # that is because the deadline never came or because this guard
            # refused is not answerable without recording it.
            self.last_refusal = self._classify_refusal(words)
            self.refusals += 1
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

    def _classify_refusal(self, words: list[str]) -> dict:
        """Where the committed prefix and the hypothesis part company, and how.

        The distinction that matters: the recogniser *appending punctuation* to
        a word already on screen is not it changing its mind about what was
        said, while a different word is. Measured, not assumed.
        """
        committed = self.committed_words
        at = 0
        for mine, theirs in zip(committed, words):
            if mine != theirs:
                break
            at += 1
        detail = {"committed": len(committed), "hypothesis": len(words),
                  "agreed_up_to": at}
        if at >= len(words):
            detail["kind"] = "HYPOTHESIS_SHORTER"
            return detail
        if at >= len(committed):
            detail["kind"] = "OTHER"
            return detail
        mine, theirs = committed[at], words[at]
        bare = lambda w: w.rstrip(".,;:!?\u2026\u00bb\u00ab\"'")
        if bare(mine) == bare(theirs):
            detail["kind"] = "PUNCTUATION_ONLY"
        elif mine.lower() == theirs.lower():
            detail["kind"] = "CASE_ONLY"
        elif bare(mine).lower() == bare(theirs).lower():
            detail["kind"] = "CASE_AND_PUNCTUATION"
        elif at == 0:
            # Disagreeing at word zero while both lists are long is not the
            # recogniser starting a new sentence: it is the two accumulations
            # having drifted out of index alignment and never resynchronised.
            # Naming it a restart would have been a comfortable guess.
            detail["kind"] = ("RESTARTED_FROM_SCRATCH" if len(words) <= 4
                              else "MISALIGNED_FROM_WORD_ZERO")
        else:
            detail["kind"] = "DIFFERENT_WORD"
        detail["committed_word"] = mine
        detail["hypothesis_word"] = theirs
        return detail

    def release_tail(self, text: str, reason: str = "TAIL_FLUSH_TIMEOUT") -> CommitResult:
        """Let the held tail onto the screen, by appending and nothing else.

        Holding the last word back is an operation that only ever *adds*, so
        releasing it is too. `force_ingest` refuses unless the whole committed
        prefix still matches the hypothesis, and in the field that refused 202
        times out of 203: the recogniser had committed the partial word
        `offer`, completed it to `offermi`, and the prefix never agreed again
        for the remaining 1346 words of the session — nothing resets the
        committer, because `is_final` never fires on this model.

        This appends `words[len(committed):]`, which is exactly what `ingest`
        does for every other word. No word already on screen is touched;
        measured over 9517 field events, zero rewrites.

        `force_ingest` keeps its strict guard, because an ASR final is a
        different claim from a tail release.
        """
        words = tokenize(text)
        start = len(self.committed_words)
        new_words = words[start:]
        if not new_words:
            self._update_stability(words)
            self._last_words = words
            return CommitResult([], [])
        self.committed_words.extend(new_words)
        self._update_stability(words)
        self._last_words = words
        return CommitResult(new_words, [],
                            [self._commit_event(i, words, reason)
                             for i in range(start, len(words))])

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
