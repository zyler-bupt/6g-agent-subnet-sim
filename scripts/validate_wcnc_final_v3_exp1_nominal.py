"""Read-only completeness and event audit for the canonical nominal Exp1 run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from experiments.exp1_netns_verified_formation import (
    NetnsFormationRun,
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
FULL_IDENTITY_STAGES = (
    frozenset(MANDATORY_STAGE_TIMESTAMPS)
    | TERMINAL_STAGES
    | {"BACKGROUND_PREPARED", "BACKGROUND_PREPARATION_FAILED"}
)


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


_SELECTION_RECEIPT_VERSION = "wcnc-v3-nominal-selection-v1"


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


def _strict_truth(value: object, description: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"nominal {description} boolean domain is invalid")
    return normalized == "true"


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
        "run_id", "run_sequence", "block_index", "order_position", "seed", "num_agents",
    ):
        if field not in event:
            raise ValueError(f"nominal event identity field {field} is missing")
        if str(event[field]) != str(row[field]):
            raise ValueError(
                f"nominal event identity field {field} disagrees with terminal row"
            )
    for field in ("method_id", "method_order_position"):
        if event.get("stage") in FULL_IDENTITY_STAGES and field not in event:
            raise ValueError(f"nominal primary event identity field {field} is missing")
        if field in event and str(event[field]) != str(row[field]):
            raise ValueError(
                f"nominal event identity field {field} disagrees with terminal row"
            )


def _numeric_equal(observed: object, expected: object) -> bool:
    return math.isclose(
        _as_finite_float(observed, "derived event summary"),
        _as_finite_float(expected, "row summary"),
        rel_tol=1e-9, abs_tol=1e-9,
    )


def _validate_command_results(run_id: str, stream: list[dict[str, Any]], row: dict[str, str]) -> None:
    preparation_failed = row["failure_stage"] == "PREPARATION"
    domains = (
        ("PING_COMMAND_RESULT", "ping", int(row["ping_edges_total"]), int(row["ping_attempts"]), int(row["ping_retried_edges"])),
        ("IPERF3_COMMAND_RESULT", "iperf", int(row["iperf_flows_total"]), int(row["iperf_attempts"]), int(row["iperf_retried_flows"])),
    )
    final_by_domain: dict[str, list[dict[str, Any]]] = {}
    expected_backoffs: list[str] = []
    for stage, domain, total, attempts, retried in domains:
        results = [event for event in stream if event.get("stage") == stage]
        if preparation_failed:
            if results or attempts != 0 or retried != 0:
                raise ValueError(f"nominal run {run_id} preparation failure has command evidence")
            final_by_domain[domain] = []
            continue
        if attempts not in {0, 1, 2} or (attempts == 0 and results):
            raise ValueError(f"nominal run {run_id} invalid {stage} attempt domain")
        expected_count = 0 if attempts == 0 else total + (retried if attempts == 2 else 0)
        if len(results) != expected_count:
            raise ValueError(f"nominal run {run_id} {stage} command row count contradicts row")
        keyed: dict[tuple[str, int], dict[str, Any]] = {}
        first: dict[str, dict[str, Any]] = {}
        second: dict[str, dict[str, Any]] = {}
        for event in results:
            details = event["details"]
            edge_id = details.get("edge_id")
            attempt = details.get("attempt")
            if not isinstance(edge_id, str) or not edge_id or attempt not in {1, 2}:
                raise ValueError(f"nominal run {run_id} {stage} edge/attempt identity is invalid")
            key = (edge_id, int(attempt))
            if key in keyed:
                raise ValueError(f"nominal run {run_id} duplicate {stage} edge attempt")
            if not isinstance(details.get("passed"), bool) or not isinstance(details.get("timeout"), bool):
                raise ValueError(f"nominal run {run_id} {stage} boolean domain is invalid")
            if not isinstance(details.get("source_index"), int) or not isinstance(details.get("target_index"), int):
                raise ValueError(f"nominal run {run_id} {stage} endpoint domain is invalid")
            if not isinstance(details.get("command"), list) or not all(
                isinstance(item, str) for item in details["command"]
            ):
                raise ValueError(f"nominal run {run_id} {stage} command evidence is invalid")
            for field in ("duration_s", "packet_loss_percent") if domain == "ping" else (
                "duration_s", "reported_duration_s", "throughput_mbps",
                "required_throughput_mbps",
            ):
                value = _as_finite_float(details.get(field), f"{stage} {field}")
                if value < 0:
                    raise ValueError(f"nominal run {run_id} {stage} {field} is negative")
            if domain == "ping" and details.get("average_rtt_ms") is not None:
                if _as_finite_float(details["average_rtt_ms"], f"{stage} average RTT") < 0:
                    raise ValueError(f"nominal run {run_id} {stage} average RTT is negative")
            if domain == "iperf" and (
                not isinstance(details.get("retransmissions"), int)
                or int(details["retransmissions"]) < 0
            ):
                raise ValueError(f"nominal run {run_id} {stage} retransmissions is invalid")
            keyed[key] = details
            (first if attempt == 1 else second)[edge_id] = details
        if attempts:
            if len(first) != total or len(second) != retried:
                raise ValueError(f"nominal run {run_id} {stage} attempt cardinality contradicts row")
            failed_first = {edge for edge, details in first.items() if not details["passed"]}
            if attempts == 2 and set(second) != failed_first:
                raise ValueError(f"nominal run {run_id} {stage} retry edges contradict first attempt")
        final = dict(first); final.update(second)
        final_by_domain[domain] = list(final.values())
        if retried:
            expected_backoffs.append("PING" if domain == "ping" else "IPERF3")

    ping_final = final_by_domain["ping"]
    iperf_final = final_by_domain["iperf"]
    ping_rtts = [float(item["average_rtt_ms"]) for item in ping_final if item.get("average_rtt_ms") is not None]
    summaries = (
        (sum(bool(item["passed"]) for item in ping_final), row["ping_edges_passed"], "ping passed summary"),
        ((sum(ping_rtts) / len(ping_rtts)) if ping_rtts else 0.0, row["ping_mean_rtt_ms"], "ping RTT summary"),
        (max((float(item["packet_loss_percent"]) for item in ping_final), default=100.0), row["ping_max_packet_loss_percent"], "ping loss summary"),
        (sum(bool(event["details"]["timeout"]) for event in stream if event.get("stage") == "PING_COMMAND_RESULT"), row["ping_timeouts"], "ping timeout summary"),
        (sum(bool(item["passed"]) for item in iperf_final), row["iperf_flows_passed"], "iperf passed summary"),
        (sum(int(item["retransmissions"]) for item in iperf_final), row["iperf_retransmissions"], "iperf retransmission summary"),
        (max((float(item["reported_duration_s"]) for item in iperf_final), default=0.0), row["iperf_reported_duration_s"], "iperf duration summary"),
        (sum(float(item["throughput_mbps"]) for item in iperf_final), row["aggregate_receiver_throughput_mbps"], "iperf throughput summary"),
    )
    for observed, expected, description in summaries:
        if not _numeric_equal(observed, expected):
            raise ValueError(f"nominal run {run_id} {description} contradicts command evidence")
    backoffs = [event for event in stream if event.get("stage") == "VERIFICATION_RETRY_BACKOFF"]
    if len(backoffs) != int(row["retry_count"]) or sorted(
        str(event["details"].get("verification_type")) for event in backoffs
    ) != sorted(expected_backoffs):
        raise ValueError(f"nominal run {run_id} retry backoff count/type contradicts row")
    if any(int(event["details"].get("configured_backoff_ms", -1)) != int(row["retry_backoff_ms"]) for event in backoffs):
        raise ValueError(f"nominal run {run_id} retry backoff duration contradicts row")


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
    batches = route_details.get("deployment_batches")
    commands = route_details.get("commands")
    if not isinstance(batches, list) or not isinstance(commands, list):
        raise ValueError(f"nominal run {run_id} deployment evidence shape is invalid")
    if any(
        not isinstance(batch, dict)
        or not isinstance(batch.get("label"), str)
        or not isinstance(batch.get("command_count"), int)
        or int(batch["command_count"]) < 0
        for batch in batches
    ):
        raise ValueError(f"nominal run {run_id} planned deployment batch shape is invalid")
    if any(
        not isinstance(command, dict)
        or not isinstance(command.get("command"), list)
        or not isinstance(command.get("namespace"), str)
        for command in commands
    ):
        raise ValueError(f"nominal run {run_id} deployment command shape is invalid")
    planned_rules = sum(int(batch["command_count"]) for batch in batches)
    returned_messages = int(row["control_messages"])
    returned_rules = int(row["rules_installed"])
    if (
        returned_messages < 0 or returned_rules < 0
        or returned_messages > len(batches) or returned_rules > planned_rules
        or len(commands) > planned_rules
    ):
        raise ValueError(f"nominal run {run_id} deployment evidence exceeds planned work")
    failure_stage = row["failure_stage"]
    if failure_stage == "PREPARATION":
        if returned_messages or returned_rules or commands:
            raise ValueError(f"nominal run {run_id} preparation failure issued deployment work")
    elif failure_stage == "ROUTE_INSTALLATION":
        # The installer assigns returned counters only after the whole plan
        # returns.  Completed earlier batches may therefore be evidenced even
        # while row counters remain zero or partial.
        pass
    elif (
        len(batches) != returned_messages
        or planned_rules != returned_rules
        or len(commands) != returned_rules
    ):
        raise ValueError(f"nominal run {run_id} completed deployment evidence contradicts row")
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
    timings = (
        (float(row["mapping_finished_at"]) - float(row["task_received_at"]), row["mapping_latency_s"], "mapping latency"),
        (float(row["traffic_control_finished_at"]) - float(row["traffic_control_started_at"]), row["traffic_control_latency_s"], "traffic-control latency"),
        (float(row["route_install_finished_at"]) - float(row["route_install_started_at"]), row["route_install_latency_s"], "route-install latency"),
        (float(row["ping_verify_finished_at"]) - float(row["ping_verify_started_at"]), row["ping_verification_latency_s"], "ping latency"),
        (float(row["data_plane_verified_at"]) - float(row["iperf_verify_started_at"]), row["iperf3_verification_latency_s"], "iperf latency"),
        (float(row["data_plane_verified_at"]) - float(row["verification_started_at"]), row["verification_latency_s"], "verification latency"),
    )
    for observed, expected, description in timings:
        if observed < 0 or not _numeric_equal(observed, expected):
            raise ValueError(f"nominal run {run_id} {description} contradicts event timing")
    formation_timing_valid = _strict_truth(row["formation_timing_valid"], "formation_timing_valid")
    expected_formation_valid = row["failure_stage"] != "PREPARATION"
    if formation_timing_valid != expected_formation_valid:
        raise ValueError(f"nominal run {run_id} formation timing validity contradicts failure stage")
    formation_latency = row["verified_formation_latency_s"]
    if expected_formation_valid:
        observed = float(row["data_plane_verified_at"]) - float(row["task_received_at"])
        if not _numeric_equal(observed, formation_latency):
            raise ValueError(f"nominal run {run_id} verified formation latency contradicts timing")
    elif formation_latency != "":
        raise ValueError(f"nominal run {run_id} preparation failure has formation latency")
    _validate_command_results(run_id, stream, row)


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
        reader = csv.DictReader(handle)
        expected_fields = [field.name for field in __import__("dataclasses").fields(NetnsFormationRun)]
        if reader.fieldnames != expected_fields:
            raise ValueError("nominal runs.csv schema is not the exact NetnsFormationRun producer schema")
        rows = list(reader)
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


def _confined_nominal_candidate(protocol_root: Path, candidate: Path) -> Path:
    root = protocol_root.resolve()
    resolved = candidate.resolve()
    base = (root / "exp1_netns_staging_v3").resolve()
    attempts = (root / "exp1_netns_staging_v3_attempts").resolve()
    allowed = resolved == base or (
        resolved.parent == attempts
        and re.fullmatch(r"attempt-[0-9]{3,}", resolved.name) is not None
    )
    if not allowed:
        raise ValueError(f"nominal candidate is outside confined staging roots: {resolved}")
    return resolved


def _validated_execution_commit(candidate: Path, repo: Path) -> tuple[str, str]:
    path = candidate / "execution_commit.txt"
    if not path.is_file():
        raise ValueError("nominal candidate lacks execution commit provenance")
    commit = path.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("nominal candidate execution commit is not 40-hex")
    resolved = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo,
        text=True, capture_output=True,
    )
    if resolved.returncode != 0:
        raise ValueError("nominal candidate execution commit is not git-resolvable")
    return commit, _sha256(path)


def _selection_receipt_payload(
    protocol_root: Path, candidate: Path, config_path: Path, repo: Path,
) -> dict[str, object]:
    selected = _confined_nominal_candidate(protocol_root, candidate)
    validation = validate_completed_nominal_artifacts(
        selected / "raw" / "runs.csv",
        selected / "raw" / "events.jsonl",
        selected / "raw" / "measurement_scope.json",
        config_path,
    )
    commit, commit_file_hash = _validated_execution_commit(selected, repo)
    return {
        "receipt_version": _SELECTION_RECEIPT_VERSION,
        "selected_path": str(selected.relative_to(protocol_root.resolve())),
        "execution_commit": commit,
        "execution_commit_sha256": commit_file_hash,
        "runs_sha256": validation.runs_sha256,
        "events_sha256": validation.events_sha256,
        "scope_sha256": validation.scope_sha256,
        "configuration_sha256": validation.configuration_sha256,
        "config_file_sha256": _sha256(config_path),
        "raw_rows": validation.raw_rows,
        "event_rows": validation.event_rows,
        "event_runs": validation.event_runs,
    }


def select_nominal_candidate(
    protocol_root: Path,
    candidates: tuple[Path, ...] | list[Path],
    config_path: Path,
    receipt_path: Path,
    *,
    repo: Path = Path("."),
) -> Path:
    """Select the first valid confined candidate and atomically bind its bytes."""
    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = _selection_receipt_payload(
                protocol_root, candidate, config_path, repo,
            )
        except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
            errors.append(f"{candidate}: {error}")
            continue
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = receipt_path.with_name(receipt_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, receipt_path)
        return (protocol_root.resolve() / str(payload["selected_path"])).resolve()
    detail = "; ".join(errors) if errors else "no candidates supplied"
    raise ValueError(f"no valid confined nominal candidate: {detail}")


def verify_nominal_selection_receipt(
    protocol_root: Path,
    receipt_path: Path,
    config_path: Path,
    *,
    repo: Path = Path("."),
) -> Path:
    """Re-audit a selection receipt before normalization or publication."""
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("nominal selection receipt is unreadable") from error
    if not isinstance(payload, dict) or payload.get("receipt_version") != _SELECTION_RECEIPT_VERSION:
        raise ValueError("nominal selection receipt version is invalid")
    selected_path = payload.get("selected_path")
    if not isinstance(selected_path, str) or Path(selected_path).is_absolute():
        raise ValueError("nominal selection receipt path is invalid")
    candidate = _confined_nominal_candidate(protocol_root, protocol_root / selected_path)
    actual = _selection_receipt_payload(protocol_root, candidate, config_path, repo)
    if payload != actual:
        raise ValueError("nominal selection receipt hash/provenance drift")
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs")
    parser.add_argument("--events")
    parser.add_argument("--scope")
    parser.add_argument(
        "--config", default="configs/exp1_netns_verified_formation_v3.yaml"
    )
    parser.add_argument("--protocol-root")
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--receipt")
    parser.add_argument("--verify-selection", action="store_true")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    if args.protocol_root or args.candidate or args.receipt or args.verify_selection:
        if not args.protocol_root or not args.receipt:
            parser.error("selection mode requires --protocol-root and --receipt")
        if args.verify_selection:
            selected = verify_nominal_selection_receipt(
                Path(args.protocol_root), Path(args.receipt), Path(args.config),
                repo=Path(args.repo),
            )
        else:
            selected = select_nominal_candidate(
                Path(args.protocol_root), tuple(Path(item) for item in args.candidate),
                Path(args.config), Path(args.receipt), repo=Path(args.repo),
            )
        print(str(selected))
        return
    if not args.runs or not args.events or not args.scope:
        parser.error("artifact mode requires --runs, --events, and --scope")
    result = validate_completed_nominal_artifacts(
        Path(args.runs), Path(args.events), Path(args.scope), Path(args.config)
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
