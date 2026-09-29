from __future__ import annotations

from dataclasses import dataclass

from .config import LedSubtitleConfig
from .rollup import SubtitleFrame


@dataclass(frozen=True)
class TerminalDisplay:
    config: LedSubtitleConfig

    def render(self, frame: SubtitleFrame, centered: bool = False) -> str:
        top = self._format_line(frame.top, centered)
        bottom = self._format_line(frame.bottom, centered)
        return f"+{'-' * self.config.max_chars}+\n|{top}|\n|{bottom}|\n+{'-' * self.config.max_chars}+"

    def _format_line(self, line: str, centered: bool) -> str:
        clean = " ".join(str(line or "").split())
        if len(clean) > self.config.max_chars:
            clean = clean[:self.config.max_chars]
        return clean.center(self.config.max_chars) if centered else clean.ljust(self.config.max_chars)
