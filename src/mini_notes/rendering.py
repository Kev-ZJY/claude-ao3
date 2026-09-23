"""Cached character-cell layout, separate from navigation and source content."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import unicodedata

from rich.cells import cell_len


@dataclass(frozen=True, slots=True)
class LayoutLine:
    text: str
    paragraph: int = -1
    offset: int = 0
    kind: str = "body"


NO_START = frozenset("，。！？；：、）》〉】〕〗〙〛’”」』％%,.!?;:)]}")
NO_END = frozenset("（《〈【〔〖〘〚‘“「『([{")


def graphemes(text: str) -> list[str]:
    """Keep combining marks, variation selectors and ZWJ runs together."""
    result: list[str] = []
    for char in text:
        regional_pair = bool(result and len(result[-1]) == 1 and "\U0001f1e6" <= result[-1] <= "\U0001f1ff" and "\U0001f1e6" <= char <= "\U0001f1ff")
        if result and (unicodedata.category(char).startswith("M") or "\U0001f3fb" <= char <= "\U0001f3ff" or regional_pair or char in "\ufe0e\ufe0f\u200d" or result[-1].endswith("\u200d")):
            result[-1] += char
        else:
            result.append(char)
    return result


def wrap_text(text: str, width: int) -> list[tuple[str, int]]:
    """Preserve text and offsets while wrapping Latin words and CJK punctuation.

    A token wider than the entire row may split. Explicit newlines create rows;
    source paragraphs themselves are never changed by this presentation layer.
    """
    width = max(2, width)
    units = graphemes(text)
    if not units:
        return [("", 0)]
    widths = [min(4, width) if u == "\t" else cell_len(u) for u in units]
    offsets: list[int] = []
    total = 0
    for unit in units:
        offsets.append(total)
        total += len(unit)
    def letter(unit: str) -> bool:
        return bool(unit) and unit[0].isalnum() and unicodedata.east_asian_width(unit[0]) not in {"W", "F"} and all(unicodedata.category(c).startswith("M") for c in unit[1:])
    word = [letter(unit) or (unit in "'’_-" and i > 0 and i + 1 < len(units) and letter(units[i-1]) and letter(units[i+1])) for i, unit in enumerate(units)]
    starts = [0] * len(units)
    word_widths = [0] * len(units)
    i = 0
    while i < len(units):
        if not word[i]:
            i += 1
            continue
        first, amount = i, 0
        while i < len(units) and word[i]:
            amount += widths[i]
            starts[i] = first
            i += 1
        for j in range(first, i):
            word_widths[j] = amount
    result: list[tuple[str, int]] = []
    start = 0
    while start < len(units):
        if units[start] in ("\n", "\r", "\r\n"):
            result.append(("", offsets[start]))
            start += 1
            continue
        end, used = start, 0
        while end < len(units) and units[end] not in ("\n", "\r") and used + widths[end] <= width:
            used += widths[end]
            end += 1
        if end == start:
            result.append((units[start], offsets[start]))
            start += 1
            continue
        fitted = end
        if end < len(units) and units[end] not in ("\n", "\r"):
            while end > start:
                if word[end-1] and word[end]:
                    beginning = starts[end]
                    if beginning > start:
                        end = beginning
                        continue
                    if word_widths[end] <= width:
                        end = fitted
                        break
                if units[end] in NO_START or units[end-1] in NO_END:
                    end -= 1
                    continue
                break
            if end == start:
                end = fitted
        result.append(("".join(units[start:end]), offsets[start]))
        start = end
        if start < len(units) and units[start] in ("\n", "\r"):
            start += 1
            if start < len(units) and units[start-1] == "\r" and units[start] == "\n":
                start += 1
    return result


@lru_cache(maxsize=12)
def layout_document(paragraphs: tuple[str, ...], width: int, title: str = "", section: int = 1, decoration: bool = True) -> tuple[LayoutLine, ...]:
    """Reflow only when content, width or visible metadata changes."""
    rows: list[LayoutLine] = []
    if title:
        rows.extend(LayoutLine(part, kind="meta") for part, _ in wrap_text(title, width))
        rows.append(LayoutLine("", kind="meta"))
    if decoration:
        rows.append(LayoutLine(f"> section {section:02d}", kind="command"))
        rows.append(LayoutLine("", kind="meta"))
    for index, paragraph in enumerate(paragraphs):
        rows.extend(LayoutLine(part, index, offset) for part, offset in wrap_text(paragraph, width))
        rows.append(LayoutLine("", index, len(paragraph)))
        if decoration and index > 0 and index % 5 == 0:
            rows.append(LayoutLine(f"> position  {section:02d} / {index + 1:02d}", kind="command"))
            rows.append(LayoutLine("", kind="meta"))
    return tuple(rows)


def line_for_anchor(rows: tuple[LayoutLine, ...], anchor: dict) -> int:
    paragraph = max(0, int(anchor.get("paragraph", 0)))
    offset = max(0, int(anchor.get("offset", 0)))
    match = 0
    for index, row in enumerate(rows):
        if row.paragraph == paragraph and row.offset <= offset:
            match = index
        if row.paragraph > paragraph:
            break
    return match
