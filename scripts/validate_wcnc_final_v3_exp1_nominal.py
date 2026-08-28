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
MANDATORY_STAGE_TIMESTAMPS = {
    "TASK_RECEIVED": "task_received_at",
    "MAPPING_FINISHED": "mapping_finished_at",
    "TRAFFIC_CONTROL_STARTED": "traffic_control_started_at",
    "TRAFFIC_CONTROL_FINISHED": "traffic_control_finished_at",
    "ROUTE_INSTALL_STARTED": "route_install_started_at",
    "ROUTE_INSTALL_FINISHED": "route_install_finished_at",
    "ACTIVATION_FINISHED": "activation_finished_at",
    "PING_VERIFY_STARTED": "ping_verify_started_at",
    "PING_VERIFY_FINISHED": "ping_verify_finished_at",
    "IPERF3_VERIFY_STARTED": "iperf_verify_started_at",
}
ALLOWED_STAGES = frozenset(MANDATORY_STAGE_TIMESTAMPS) | TERMINAL_STAGES | frozenset({
    "BACKGROUND_PREPARED", "BACKGROUND_PREPARATION_FAILED",
    "BACKGROUND_TRAFFIC_RESULT", "PING_COMMAND_RESULT",
    "IPERF3_COMMAND_RESULT", "VERIFICATION_RETRY_BACKOFF",
})


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


def _truth(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _as_finite_float(value: object, description: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{description} is not numeric") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{description} is not finite")
    return parsed


def _validate_event_identity(event: dict[str, Any], row: dict[str, str]) -> None:
    for field in (
        "run_sequence", "block_index", "order_position", "method_order_position",
        "seed", "method_id", "num_agents",
    ):
        if field not in event:
            continue
        if str(event[field]) != str(row[field]):
            raise ValueError(
                f"nominal event identity field {field} disagrees with terminal row"
            )


def _validate_run_causality(run_id: str, stream: list[dict[str, Any]], row: dict[str, str]) -> None:
    stages = [str(event.get("stage", "")) for event in stream]
    unknown = sorted(set(stages) - ALLOWED_STAGES)
    if unknown:
        raise ValueError(f"nominal run {run_id} has unknown event stages: {','.join(unknown)}")
    background_prepare = sum(
        stage in {"BACKGROUND_PREPARED", "BACKGROUND_PREPARATION_FAILED"}
        for stage in stages
    )
    if background_prepare != 1 or stages.count("BACKGROUND_TRAFFIC_RESULT") != 1:
        raise ValueError(f"nominal run {run_id} lacks mandatory background causal evidence")
    by_stage: dict[str, dict[str, Any]] = {}
    for stage in MANDATORY_STAGE_TIMESTAMPS:
        matches = [event for event in stream if event.get("stage") == stage]
        if len(matches) != 1:
            raise ValueError(f"nominal run {run_id} lacks exactly one mandatory {stage} event")
        by_stage[stage] = matches[0]
        observed = _as_finite_float(matches[0]["timestamp"], f"{stage} event timestamp")
        expected = _as_finite_float(row[MANDATORY_STAGE_TIMESTAMPS[stage]], f"row {stage} timestamp")
        if observed != expected:
            raise ValueError(f"nominal run {run_id} {stage} timestamp disagrees with row")
    terminals = [event for event in stream if event.get("stage") in TERMINAL_STAGES]
    if len(terminals) != 1:
        raise ValueError(f"nominal run {run_id} lacks exactly one terminal event")
    terminal = terminals[0]
    success = _truth(row["success"])
    expected_terminal = "DATA_PLANE_VERIFIED" if success else "FORMATION_FAILED"
    if terminal.get("stage") != expected_terminal:
        raise ValueError(f"nominal run {run_id} terminal type contradicts success")
    terminal_timestamp = _as_finite_float(terminal["timestamp"], "terminal timestamp")
    if terminal_timestamp != _as_finite_float(row["data_plane_verified_at"], "row terminal timestamp"):
        raise ValueError(f"nominal run {run_id} terminal timestamp disagrees with row")
    if _truth(row["formation_failed"]) == success:
        raise ValueError(f"nominal run {run_id} formation_failed contradicts success")

    mapping_details = by_stage["MAPPING_FINISHED"]["details"]
    route_details = by_stage["ROUTE_INSTALL_STARTED"]["details"]
    ping_details = by_stage["PING_VERIFY_FINISHED"]["details"]
    terminal_details = terminal["details"]
    exact_detail_pairs = (
        (mapping_details.get("business_edges"), row["num_business_edges"], "business edge count"),
        (route_details.get("control_messages"), row["control_messages"], "control message count"),
        (route_details.get("rules_installed"), row["rules_installed"], "installed rule count"),
        (ping_details.get("passed"), row["ping_edges_passed"], "ping passed count"),
        (ping_details.get("total"), row["ping_edges_total"], "ping total count"),
        (terminal_details.get("passed"), row["iperf_flows_passed"], "iperf passed count"),
        (terminal_details.get("total"), row["iperf_flows_total"], "iperf total count"),
    )
    for observed, expected, description in exact_detail_pairs:
        try:
            matches = int(observed) == int(expected)
        except (TypeError, ValueError) as error:
            raise ValueError(f"nominal run {run_id} has invalid {description}") from error
        if not matches:
            raise ValueError(f"nominal run {run_id} {description} contradicts row")
    if str(terminal_details.get("failure_reason", "")) != row["failure_reason"]:
        raise ValueError(f"nominal run {run_id} terminal failure reason contradicts row")

    causal_stages = (
        "TASK_RECEIVED", "MAPPING_FINISHED", "ROUTE_INSTALL_STARTED",
        "ROUTE_INSTALL_FINISHED", "ACTIVATION_FINISHED", "PING_VERIFY_STARTED",
        "PING_VERIFY_FINISHED", "IPERF3_VERIFY_STARTED",
    )
    causal_times = [float(by_stage[stage]["timestamp"]) for stage in causal_stages]
    causal_times.append(terminal_timestamp)
    if causal_times != sorted(causal_times):
        raise ValueError(f"nominal run {run_id} has impossible causal stage ordering")
    tc_times = [
        float(by_stage["TRAFFIC_CONTROL_STARTED"]["timestamp"]),
        float(by_stage["TRAFFIC_CONTROL_FINISHED"]["timestamp"]),
    ]
    if tc_times != sorted(tc_times):
        raise ValueError(f"nominal run {run_id} has impossible traffic-control ordering")
    if float(row["verification_started_at"]) != float(row["ping_verify_started_at"]):
        raise ValueError(f"nominal run {run_id} verification start contradicts ping start")


def _validate_events(events: list[dict[str, Any]], rows: dict[str, dict[str, str]]) -> int:
    run_ids = set(rows)
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
        _validate_event_identity(event, rows[run_id])
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
        _validate_run_causality(run_id, stream, rows[run_id])
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
    event_runs = _validate_events(events, {row["run_id"]: row for row in rows})
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
