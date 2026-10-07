"""Runtime paths and timing for the live prototype.

All periods scale with ``TCANET_TIME_SCALE`` (default 1.0) so tests can run
the full pipeline faster; paths follow ``TCANET_RUN_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path

FLOW_PORT_BASE = 47000
PACKET_BYTES = 1200
K_MAX = 3  # paper Alg. 1 attempt bound


def run_dir() -> Path:
    return Path(os.environ.get("TCANET_RUN_DIR", "/tmp/tcanet-demo"))


def bus_path() -> Path:
    return run_dir() / "bus.sock"


def events_path() -> Path:
    return run_dir() / "events.jsonl"


def mode_path() -> Path:
    return run_dir() / "mode"


def pids_dir() -> Path:
    return run_dir() / "pids"


def logs_dir() -> Path:
    return run_dir() / "logs"


def captures_dir() -> Path:
    return run_dir() / "captures"


def time_scale() -> float:
    return float(os.environ.get("TCANET_TIME_SCALE", "1.0"))


def heartbeat_s() -> float:
    return 0.5 * time_scale()


def heartbeat_timeout_s() -> float:
    return 1.5 * time_scale()


def sample_period_s() -> float:
    return 0.5 * time_scale()


def radio_period_s() -> float:
    return 1.0 * time_scale()


def assess_window_s() -> float:
    return 1.0 * time_scale()


def assess_settle_s() -> float:
    return 0.3 * time_scale()


def detect_debounce_s() -> float:
    return 0.7 * time_scale()


def ack_timeout_s() -> float:
    return 2.0 * time_scale()


def read_mode() -> str:
    """``"netns"`` or ``"sim"`` as written by ``root up`` (default netns)."""
    try:
        return mode_path().read_text(encoding="utf-8").strip() or "netns"
    except FileNotFoundError:
        return "netns"
