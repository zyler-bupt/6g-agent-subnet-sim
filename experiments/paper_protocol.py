from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RunMode(str, Enum):
    PILOT = "pilot"
    PAPER = "paper"


@dataclass(frozen=True)
class ModeSpec:
    topology_seeds: int
    events_per_seed: int
    rate_events_per_seed: int

    def trials_per_point(self, *, rate_metric: bool) -> int:
        event_count = self.rate_events_per_seed if rate_metric else self.events_per_seed
        return self.topology_seeds * event_count


_MODE_SPECS = {
    RunMode.PILOT: ModeSpec(
        topology_seeds=5,
        events_per_seed=2,
        rate_events_per_seed=2,
    ),
    RunMode.PAPER: ModeSpec(
        topology_seeds=30,
        events_per_seed=1,
        rate_events_per_seed=5,
    ),
}


def mode_spec(mode: str | RunMode) -> ModeSpec:
    return _MODE_SPECS[RunMode(mode)]


@dataclass(frozen=True)
class MethodMetadata:
    method_id: str
    label: str
    source: str
    adapted: bool


def _method(
    method_id: str,
    label: str,
    source: str,
    *,
    adapted: bool = False,
) -> MethodMetadata:
    return MethodMetadata(method_id, label, source, adapted)


METHODS: Mapping[str, MethodMetadata] = {
    "proposed": _method("proposed", "Proposed", "This work"),
    "proposed_without_batch": _method(
        "proposed_without_batch",
        "Proposed w/o Batch",
        "This work",
    ),
    "cspf": _method("cspf", "CSPF", "CSPF"),
    "a1_agent_embedded": _method(
        "a1_agent_embedded",
        "A1-Agent-Embedded*",
        "A1 Agent",
        adapted=True,
    ),
    "sanet_dw": _method("sanet_dw", "SANet-DW*", "SANet", adapted=True),
    "adjacent_layer": _method(
        "adjacent_layer",
        "Adjacent-Layer",
        "Adjacent-layer coordination",
    ),
    "independent": _method(
        "independent",
        "Independent",
        "Independent layer optimization",
    ),
    "netren": _method("netren", "NetRen*", "NetRen", adapted=True),
    "local_only": _method("local_only", "Local-Only", "Local repair"),
    "full_rebuild": _method(
        "full_rebuild",
        "Full-Rebuild",
        "Global reconstruction",
    ),
    "netkeeper": _method(
        "netkeeper",
        "NetKeeper*",
        "NetKeeper",
        adapted=True,
    ),
}


EXPERIMENT_METHODS: Mapping[str, tuple[str, ...]] = {
    "exp1": (
        "proposed",
        "proposed_without_batch",
        "cspf",
        "a1_agent_embedded",
    ),
    "exp2": ("proposed", "sanet_dw", "adjacent_layer", "independent"),
    "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
    "exp4": ("proposed", "netkeeper", "cspf", "full_rebuild"),
}


def stable_fingerprint(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
