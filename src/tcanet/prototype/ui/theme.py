"""Validated dark chart palette (dataviz reference instance, dark mode)."""
from __future__ import annotations

SURFACE = "#1a1a19"
PAGE = "#0d0d0d"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

# Categorical slots 1-4 (dark), validated adjacent CVD ΔE ≥ 8.4.
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")

# Status palette — always paired with a text label.
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"


def series_color(index: int) -> str:
    return SERIES[index % len(SERIES)]
