from __future__ import annotations

from dataclasses import dataclass

from .config import LedSubtitleConfig


@dataclass(frozen=True)
class PackedLines:
    top: str
    bottom: str


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def tokenize(text: str) -> list[str]:
    return normalize_text(text).split()


def line_text(words: list[str]) -> str:
    return " ".join(words)


def candidate_fits(words: list[str], word: str, config: LedSubtitleConfig) -> bool:
    candidate = words + [word]
    return len(line_text(candidate)) <= config.max_chars


def is_soft_break(line: str, config: LedSubtitleConfig) -> bool:
    clean = normalize_text(line)
    return len(clean) >= config.soft_break_min and clean.endswith((".", ",", ";", ":", "!", "?"))


def is_orphan_word(word: str, config: LedSubtitleConfig) -> bool:
    return normalize_text(word).lower() in config.orphan_words


def center_line(line: str, config: LedSubtitleConfig) -> str:
    clean = normalize_text(line)
    if len(clean) >= config.max_chars:
        return clean
    return clean.center(config.max_chars).rstrip()


def pack_popon_text(text: str, config: LedSubtitleConfig) -> PackedLines:
    words = tokenize(text)
    lines: list[list[str]] = [[]]
    for word in words:
        if len(word) > config.max_chars:
            raise ValueError(f"word exceeds display width: {word}")
        if lines[-1] and not candidate_fits(lines[-1], word, config):
            if len(lines) == 2:
                break
            lines.append([])
        lines[-1].append(word)

    rendered = [line_text(line) for line in lines if line]
    if not rendered:
        return PackedLines("", "")
    if len(rendered) == 1:
        return PackedLines("", center_line(rendered[0], config))

    first, second = rendered[0], rendered[1]
    if len(first) < len(second) and not is_soft_break(first, config):
        first, second = second, first
    return PackedLines(center_line(first, config), center_line(second, config))
