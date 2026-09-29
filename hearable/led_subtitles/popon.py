from __future__ import annotations

from dataclasses import dataclass

from .config import LedSubtitleConfig
from .layout import PackedLines, pack_popon_text


@dataclass(frozen=True)
class PopOnBlock:
    text: str
    start_ms: int
    end_ms: int | None = None


@dataclass(frozen=True)
class PopOnEvent:
    kind: str
    time_ms: int
    lines: PackedLines


class PopOnScheduler:
    def __init__(self, config: LedSubtitleConfig):
        self.config = config

    def events(self, blocks: list[PopOnBlock]) -> list[PopOnEvent]:
        events: list[PopOnEvent] = []
        next_allowed_on_ms: int | None = None
        dark = PackedLines("", "")

        for block in sorted(blocks, key=lambda item: item.start_ms):
            start_ms = block.start_ms + self.config.global_offset_ms
            if next_allowed_on_ms is not None:
                start_ms = max(start_ms, next_allowed_on_ms)

            duration_ms = self._duration_ms(block)
            end_ms = start_ms + duration_ms
            events.append(PopOnEvent("on", start_ms, pack_popon_text(block.text, self.config)))
            events.append(PopOnEvent("off", end_ms, dark))
            next_allowed_on_ms = end_ms + self.config.popon_gap_ms

        return events

    def _duration_ms(self, block: PopOnBlock) -> int:
        if block.end_ms is not None:
            return max(0, block.end_ms - block.start_ms)
        calculated = int(round((len(block.text) / self.config.reading_cps) * 1000))
        return max(self.config.popon_min_ms, min(self.config.popon_max_ms, calculated))
