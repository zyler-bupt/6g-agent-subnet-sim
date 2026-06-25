from __future__ import annotations

import unicodedata
from typing import Sequence

# Lightweight, dependency-free terminal reporting helpers.
# Goal: a human reading the terminal should understand the result immediately,
# without parsing raw dicts.


def display_width(text: str) -> int:
    """Terminal column width of a string, counting CJK characters as 2."""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    text = str(text)
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def rule(char: str = "─", width: int = 66) -> str:
    return char * width


def banner(title: str, width: int = 66) -> str:
    return "\n".join([rule("═", width), "  " + title, rule("═", width)])


def section(title: str) -> str:
    return f"\n【{title}】"


def kv_block(pairs: Sequence[tuple[str, object]], indent: int = 2) -> str:
    if not pairs:
        return ""
    label_w = max(display_width(label) for label, _ in pairs)
    return "\n".join(" " * indent + pad(label, label_w) + "  " + str(value) for label, value in pairs)


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    indent: int = 2,
    aligns: Sequence[str] | None = None,
) -> str:
    cols = len(headers)
    aligns = list(aligns) if aligns else ["left"] * cols
    widths = [display_width(h) for h in headers]
    str_rows = [[str(cell) for cell in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], display_width(cell))

    def fmt(cells: Sequence[str]) -> str:
        return " " * indent + "   ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(cells))

    lines = [fmt([str(h) for h in headers])]
    lines.append(" " * indent + "   ".join("─" * widths[i] for i in range(cols)))
    for row in str_rows:
        lines.append(fmt(row))
    return "\n".join(lines)


def bullet(text: str, indent: int = 2, mark: str = "·") -> str:
    return " " * indent + f"{mark} {text}"


def pct_change(old: float, new: float) -> str:
    """Human-friendly relative change from old to new."""
    if old == 0:
        return "—" if new == 0 else "+∞"
    delta = (new - old) / old * 100.0
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.1f}%"
