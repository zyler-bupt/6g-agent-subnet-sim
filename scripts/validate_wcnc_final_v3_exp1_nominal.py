"""Read-only completeness and event audit for the canonical nominal Exp1 run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from experiments.exp1_netns_verified_formation import (
    build_experiment_schedule,
    counterbalanced_method_order,
)
from scripts.normalize_wcnc_final_v3_exp1 import exp1_canonical_grid_errors


FORMAL_METHODS = ("proposed", "cspf", "global_sfc_embedding")
FORMAL_SIZES = (4, 8, 12, 16, 20)
FORMAL_SEEDS = tuple(range(50))
TERMINAL_STAGES = frozenset({"DATA_PLANE_VERIFIED", "FORMATION_FAILED"})


@dataclass(frozen=True)
class NominalArtifactValidation:
    complete: bool
    raw_rows: int
    event_rows: int
    event_runs: int
    configuration_sha256: str
    runs_sha256: str
    events_sha256: str
    scope_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configuration_sha256(path: Path) -> str:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"nominal event line {line_number} is invalid JSON") from error
            if not isinstance(event, dict):
                raise ValueError(f"nominal event line {line_number} is not an object")
            events.append(event)
    if not events:
        raise ValueError("nominal event provenance is empty")
    return events


def _validate_scope(
    scope: object,
    configuration_hash: str,
    expected_schedule: list[dict[str, object]],
) -> None:
    if not isinstance(scope, dict):
        raise ValueError("nominal measurement scope is not an object")
    expected = {
        "configuration_sha256": configuration_hash,
        "seeds": list(FORMAL_SEEDS),
        "task_sizes": list(FORMAL_SIZES),
        "methods": list(FORMAL_METHODS),
        "schedule": expected_schedule,
        "schedule_policy": "five-seed blocks with Latin task-size rotation",
        "method_schedule_policy": "paired-seed deterministic Latin rotation",
    }
    for field, value in expected.items():
        if scope.get(field) != value:
            raise ValueError(f"nominal measurement scope {field} does not match frozen formal invocation")


def _validate_events(events: list[dict[str, Any]], run_ids: set[str]) -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    closed: set[str] = set()
    previous_run_id: str | None = None
    for event in events:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or run_id not in run_ids:
            raise ValueError("nominal event references an unknown or missing run_id")
        if previous_run_id is not None and run_id != previous_run_id:
            closed.add(previous_run_id)
        if run_id in closed:
            raise ValueError("nominal event stream is split or reordered")
        previous_run_id = run_id
        grouped.setdefault(run_id, []).append(event)
    if set(grouped) != run_ids:
        raise ValueError("nominal event provenance does not cover the exact 750-row grid")
    for run_id, stream in grouped.items():
        timestamps: list[float] = []
        for event in stream:
            try:
                timestamp = float(event["timestamp"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"nominal event for {run_id} has an invalid timestamp") from error
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError(f"nominal event for {run_id} has an invalid timestamp")
            timestamps.append(timestamp)
            if not isinstance(event.get("details"), dict):
                raise ValueError(f"nominal event for {run_id} has invalid details")
        if timestamps != sorted(timestamps):
            raise ValueError(f"nominal events for {run_id} are not monotonic")
        stages = [event.get("stage") for event in stream]
        if stages.count("TASK_RECEIVED") != 1:
            raise ValueError(f"nominal run {run_id} lacks exactly one TASK_RECEIVED event")
        terminals = [stage for stage in stages if stage in TERMINAL_STAGES]
        if len(terminals) != 1:
            raise ValueError(f"nominal run {run_id} lacks exactly one terminal event")
    return len(grouped)


def validate_completed_nominal_artifacts(
    runs_path: Path,
    events_path: Path,
    scope_path: Path,
    config_path: Path,
) -> NominalArtifactValidation:
    """Prove that a nominal runner invocation is complete without changing it."""
    paths = (runs_path, events_path, scope_path, config_path)
    if any(not path.is_file() or path.stat().st_size == 0 for path in paths):
        raise ValueError("nominal validation requires nonempty runs, events, scope, and config files")
    with runs_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    grid_errors = exp1_canonical_grid_errors(rows)
    if grid_errors:
        raise ValueError("; ".join(grid_errors))
    configuration_hash = _configuration_sha256(config_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("nominal frozen formal config is not a mapping")
    if {row.get("configuration_sha256", "") for row in rows} != {configuration_hash}:
        raise ValueError("nominal row configuration hash differs from frozen formal config")
    if {row.get("result_mode", "") for row in rows} != {
        "real_linux_netns_veth_tc_data_plane"
    }:
        raise ValueError("nominal rows are not measured Linux netns artifacts")
    run_ids = [row.get("run_id", "") for row in rows]
    if any(not run_id for run_id in run_ids) or len(run_ids) != len(set(run_ids)):
        raise ValueError("nominal grid has missing or duplicate run_id values")
    schedule = build_experiment_schedule(config, FORMAL_SEEDS)
    expected_invocations: dict[tuple[int, int, str], tuple[int, int, int, int]] = {}
    for scheduled in schedule:
        methods = counterbalanced_method_order(FORMAL_METHODS, scheduled.seed)
        for method_position, method_id in enumerate(methods):
            expected_invocations[(
                scheduled.num_agents, scheduled.seed, method_id,
            )] = (
                (scheduled.run_sequence - 1) * len(FORMAL_METHODS)
                + method_position + 1,
                scheduled.block_index,
                scheduled.order_position,
                method_position,
            )
    for row in rows:
        key = (int(row["num_agents"]), int(row["seed"]), row["method_id"])
        try:
            observed_invocation = tuple(int(row[field]) for field in (
                "run_sequence", "block_index", "order_position",
                "method_order_position",
            ))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("nominal row has invalid invocation sequence evidence") from error
        if observed_invocation != expected_invocations[key]:
            raise ValueError("nominal row invocation sequence differs from frozen schedule")
    scenario_groups: dict[tuple[int, int], set[str]] = {}
    for row in rows:
        key = (int(row["num_agents"]), int(row["seed"]))
        fingerprint = row.get("scenario_fingerprint", "")
        if not fingerprint:
            raise ValueError("nominal row has no scenario fingerprint")
        scenario_groups.setdefault(key, set()).add(fingerprint)
    if any(len(fingerprints) != 1 for fingerprints in scenario_groups.values()):
        raise ValueError("nominal paired methods do not share one scenario fingerprint")
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    _validate_scope(scope, configuration_hash, [asdict(item) for item in schedule])
    events = _read_jsonl(events_path)
    event_runs = _validate_events(events, set(run_ids))
    return NominalArtifactValidation(
        complete=True,
        raw_rows=len(rows),
        event_rows=len(events),
        event_runs=event_runs,
        configuration_sha256=configuration_hash,
        runs_sha256=_sha256(runs_path),
        events_sha256=_sha256(events_path),
        scope_sha256=_sha256(scope_path),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument(
        "--config", default="configs/exp1_netns_verified_formation_v3.yaml"
    )
    args = parser.parse_args()
    result = validate_completed_nominal_artifacts(
        Path(args.runs), Path(args.events), Path(args.scope), Path(args.config)
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
