"""Canonicalize measured formal Exp1 transactional trial artifacts.

This normalizer deliberately accepts only the frozen formal arm.  Pilot and
override artifacts remain source data and must not enter the paper raw tree.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.exp1_transactional_formation import (
    ARM_ID,
    CONSTRUCTION_FAULT_TARGET,
    METHODS,
    SCENARIO_CLASSES,
    TASK_SIZES,
    _grid_payload,
    _construction_fault_schedule_fingerprint,
    _logical_scenario_fingerprint,
    build_transactional_schedule,
    load_transactional_config,
)
from experiments.paper_protocol import PROTOCOL_ID, stable_fingerprint


FORMAL_SEEDS = tuple(range(50))
SOURCE_RESULT_MODE = "measured_netns"
CANONICAL_TARGET = Path(
    "results/paper/wcnc_final_v3/raw/exp1_transactional/trials.csv"
)
TERMINAL_STAGE = "TERMINAL_ROW_READY"
TRANSACTION_STAGE_OPERATIONS = {
    "TRANSACTION_PREPARE": "prepare",
    "TRANSACTION_COMMIT": "commit",
    "TRANSACTION_ABORT": "abort",
    "TRANSACTION_ROLLBACK": "rollback",
}
TRANSACTION_OPERATION_PHASES = {
    "prepare": frozenset({"prepared", "rejected"}),
    "commit": frozenset({"committed", "rejected"}),
    "abort": frozenset({"aborted", "prepared", "rejected"}),
    "rollback": frozenset({"rolled_back", "committed"}),
}
LIFECYCLE_STAGES = frozenset({
    "TASK_RECEIVED",
    "PLAN_FINISHED",
    "COMMON_INFRASTRUCTURE_INSTALLED",
    "FAULT_INJECTED",
    "VERIFIED_CORRECT",
    "TRIAL_FAILED",
    "BACKGROUND_CLEANUP_FAILED",
    "TOPOLOGY_TEARDOWN_FAILED",
    TERMINAL_STAGE,
})
ALLOWED_EVENT_STAGES = LIFECYCLE_STAGES | frozenset(TRANSACTION_STAGE_OPERATIONS)
CHECKED_IN_FORMAL_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "configs" / "exp1_transactional_formation_v1.yaml"
)
RAW_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs" / "wcnc_final_v3_raw_schema.json"
)

REQUIRED_RUN_FIELDS = frozenset({
    "protocol_id", "arm_id", "phase", "run_id", "run_sequence",
    "scenario_class", "seed", "num_agents", "num_gateways",
    "num_business_edges", "method_id", "fault_schedule_fingerprint",
    "logical_fault_target", "observation_version_fingerprint",
    "verifier_fingerprint", "common_infrastructure_fingerprint",
    "common_infrastructure_provenance", "configuration_sha256",
    "scenario_fingerprint", "execution_mode_detail",
    "attempt_count", "prepare_attempts", "commit_attempts", "rollback_count",
    "rollback_scope_objects", "wasted_rule_commands",
    "partial_state_exposure_ms", "planning_latency_ms",
    "common_infrastructure_latency_ms", "prepare_latency_ms",
    "commit_latency_ms", "rollback_replan_latency_ms",
    "method_owned_formation_latency_ms", "final_verification_latency_ms",
    "time_to_correct_formation_ms", "verified_correct",
    "infrastructure_cleanup_success", "cleanup_failure_reason",
    "leaked_state_fingerprint", "success", "timeout", "failure_stage",
    "failure_reason", "result_mode",
})


def _truth(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _load_formal_config(config: Path | str | Mapping[str, object]) -> tuple[dict[str, Any], str]:
    if isinstance(config, Mapping):
        raise ValueError("transactional normalizer requires a trusted config file")
    supplied_path = Path(config)
    checked_in_bytes = CHECKED_IN_FORMAL_CONFIG.read_bytes()
    if hashlib.sha256(supplied_path.read_bytes()).hexdigest() != hashlib.sha256(checked_in_bytes).hexdigest():
        raise ValueError("transactional normalizer config does not match the checked-in formal config")
    loaded = load_transactional_config(supplied_path)
    checked_in = load_transactional_config(CHECKED_IN_FORMAL_CONFIG)
    if loaded != checked_in:
        raise ValueError("transactional normalizer config differs from frozen formal values")
    experiment = loaded.get("experiment")
    if not isinstance(experiment, Mapping):
        raise ValueError("transactional formal config has no experiment section")
    if (
        experiment.get("protocol_id") != PROTOCOL_ID
        or experiment.get("arm_id") != ARM_ID
        or experiment.get("phase") != "formal"
    ):
        raise ValueError("transactional normalizer requires the frozen formal arm")
    return checked_in, hashlib.sha256(checked_in_bytes).hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"attempts.jsonl line {line_number} is not JSON") from error
            if not isinstance(event, dict):
                raise ValueError(f"attempts.jsonl line {line_number} is not an object")
            events.append(event)
    return events


def _grid_errors(rows: list[dict[str, str]]) -> list[str]:
    expected = {
        (scenario_class, size, seed, method)
        for scenario_class in SCENARIO_CLASSES
        for size in TASK_SIZES
        for seed in FORMAL_SEEDS
        for method in METHODS
    }
    observed: list[tuple[str, int, int, str]] = []
    try:
        observed = [
            (
                row["scenario_class"], int(row["num_agents"]), int(row["seed"]),
                row["method_id"],
            )
            for row in rows
        ]
    except (KeyError, TypeError, ValueError):
        return ["transactional formal grid has an invalid scenario class, size, or seed"]
    errors: list[str] = []
    if len(observed) != len(expected):
        errors.append(
            f"transactional formal grid must contain exactly {len(expected)} rows; "
            f"got {len(observed)}"
        )
    if set(observed) != expected:
        errors.append(
            "transactional formal grid does not match 3 fault classes x 5 sizes "
            "x 50 seeds x 3 methods"
        )
    if len(observed) != len(set(observed)):
        errors.append("transactional formal grid contains duplicate trials")
    return errors


def _require_source_fields(rows: list[dict[str, str]]) -> None:
    for index, row in enumerate(rows, start=2):
        missing = sorted(field for field in REQUIRED_RUN_FIELDS if field not in row)
        if missing:
            raise ValueError(f"runs.csv row {index} is missing required fields: {','.join(missing)}")


def _validate_rows(
    rows: list[dict[str, str]], config: Mapping[str, object],
) -> None:
    if not rows:
        raise ValueError("transactional formal input is empty")
    _require_source_fields(rows)
    run_ids: set[str] = set()
    expected_sequences = {
        (
            scheduled.scenario_class, scheduled.num_agents,
            scheduled.seed, scheduled.method_id,
        ): scheduled.run_sequence
        for scheduled in build_transactional_schedule(config)
    }
    for index, row in enumerate(rows, start=2):
        if row["protocol_id"] != PROTOCOL_ID:
            raise ValueError(f"runs.csv row {index} has an unexpected protocol")
        if row["arm_id"] != ARM_ID or row["phase"] != "formal":
            raise ValueError(f"runs.csv row {index} is not in the formal transactional arm")
        if row["result_mode"] != SOURCE_RESULT_MODE:
            raise ValueError("transactional canonical input must be measured Linux netns data")
        if row["configuration_sha256"] != stable_fingerprint(config):
            raise ValueError("transactional configuration hash does not match the frozen formal config")
        run_id = row["run_id"].strip()
        if not run_id or run_id in run_ids:
            raise ValueError("transactional formal input has missing or duplicate run_id values")
        run_ids.add(run_id)
        grid_key = (
            row["scenario_class"], int(row["num_agents"]),
            int(row["seed"]), row["method_id"],
        )
        if int(row["run_sequence"]) != expected_sequences.get(grid_key):
            raise ValueError("transactional run sequence does not match the frozen schedule")
        construction_failure = row["failure_stage"] == "TOPOLOGY_CONSTRUCTION"
        expected_construction_fingerprint = _construction_fault_schedule_fingerprint(
            row["protocol_id"], row["scenario_class"],
            int(row["num_agents"]), int(row["seed"]),
        )
        if construction_failure:
            if (
                row["fault_schedule_fingerprint"] != expected_construction_fingerprint
                or row["logical_fault_target"] != CONSTRUCTION_FAULT_TARGET
                or int(row["attempt_count"]) != 0
            ):
                raise ValueError("transactional topology construction sentinel is invalid")
        elif (
            not row["fault_schedule_fingerprint"].strip()
            or row["fault_schedule_fingerprint"] == expected_construction_fingerprint
            or row["logical_fault_target"] == CONSTRUCTION_FAULT_TARGET
        ):
            raise ValueError("transactional formal input has an invalid fault schedule fingerprint")
        if not row["scenario_fingerprint"].strip():
            raise ValueError("transactional formal input has a missing scenario fingerprint")
        expected_scenario_fingerprint = _logical_scenario_fingerprint(
            config, row["scenario_class"], int(row["seed"]), int(row["num_agents"]),
        )
        if row["scenario_fingerprint"] != expected_scenario_fingerprint:
            raise ValueError("transactional scenario fingerprint does not match logical inputs")
        if not row["execution_mode_detail"].strip():
            raise ValueError("transactional formal input has a missing execution mode detail")
        try:
            decoded_scope = json.loads(row["rollback_scope_objects"])
        except json.JSONDecodeError as error:
            raise ValueError("rollback_scope_objects must retain a JSON array") from error
        if not isinstance(decoded_scope, list):
            raise ValueError("rollback_scope_objects must retain a JSON array")
        for field in (
            "attempt_count", "prepare_attempts", "commit_attempts", "rollback_count",
            "wasted_rule_commands",
        ):
            try:
                int(row[field])
            except ValueError as error:
                raise ValueError(f"transactional field {field} must be an integer") from error
        for field in (
            "partial_state_exposure_ms", "planning_latency_ms",
            "common_infrastructure_latency_ms", "prepare_latency_ms",
            "commit_latency_ms", "rollback_replan_latency_ms",
            "final_verification_latency_ms",
        ):
            try:
                float(row[field])
            except ValueError as error:
                raise ValueError(f"transactional field {field} must be numeric") from error
        for field in ("method_owned_formation_latency_ms", "time_to_correct_formation_ms"):
            if row[field].strip():
                try:
                    float(row[field])
                except ValueError as error:
                    raise ValueError(f"transactional field {field} must be numeric when present") from error


def _validate_paired_fault_fingerprints(rows: list[dict[str, str]]) -> None:
    paired: dict[tuple[str, int, int], list[dict[str, str]]] = {}
    for row in rows:
        key = (row["scenario_class"], int(row["num_agents"]), int(row["seed"]))
        paired.setdefault(key, []).append(row)
    for key, group in paired.items():
        methods = {row["method_id"] for row in group}
        fingerprints = {row["fault_schedule_fingerprint"] for row in group}
        if methods != set(METHODS) or len(group) != len(METHODS):
            raise ValueError(f"transactional paired trial {key!r} is missing a canonical method")
        if len(fingerprints) != 1:
            raise ValueError(f"transactional paired trial {key!r} has divergent fault fingerprints")


def _require_event_details(event: Mapping[str, object]) -> Mapping[str, object]:
    details = event.get("details")
    if not isinstance(details, Mapping):
        raise ValueError("transactional attempt event details must be an object")
    return details


def _validate_transaction_event(event: Mapping[str, object]) -> None:
    stage = str(event["stage"])
    operation = TRANSACTION_STAGE_OPERATIONS[stage]
    details = _require_event_details(event)
    if details.get("operation") != operation:
        raise ValueError("transaction event operation does not match its stage")
    if details.get("phase") not in TRANSACTION_OPERATION_PHASES[operation]:
        raise ValueError("transaction event has an illegal operation phase")
    if not isinstance(details.get("transaction_id"), str) or not details["transaction_id"]:
        raise ValueError("transaction event has no transaction identity")
    if not isinstance(details.get("accepted"), bool):
        raise ValueError("transaction event accepted flag must be boolean")
    for field in ("attempt_index", "started_ns", "ended_ns", "commands_attempted"):
        value = details.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"transaction event {field} must be a nonnegative integer")
    if int(details["ended_ns"]) < int(details["started_ns"]):
        raise ValueError("transaction event ends before it starts")
    affected = details.get("affected_objects")
    if not isinstance(affected, list) or any(not isinstance(item, str) for item in affected):
        raise ValueError("transaction event affected_objects must be a string array")
    for field in (
        "reason", "readback_before_fingerprint", "readback_after_fingerprint",
    ):
        if not isinstance(details.get(field), str):
            raise ValueError(f"transaction event {field} must be a string")


def _validate_fault_event(event: Mapping[str, object], row: Mapping[str, str]) -> None:
    details = _require_event_details(event)
    if details.get("fault_class") != row["scenario_class"]:
        raise ValueError("fault event class does not match its trial")
    if (
        not isinstance(details.get("fault_attempt"), int)
        or isinstance(details.get("fault_attempt"), bool)
        or int(details["fault_attempt"]) < 1
    ):
        raise ValueError("fault event attempt must be a positive integer")
    logical_edge_id = details.get("logical_edge_id")
    gateway_index = details.get("gateway_index")
    if (
        not isinstance(logical_edge_id, str) or not logical_edge_id
        or not isinstance(gateway_index, int) or isinstance(gateway_index, bool)
        or f"{logical_edge_id}@gateway-{gateway_index}" != row["logical_fault_target"]
    ):
        raise ValueError("fault event target does not match its trial")
    mechanisms = {
        "executor_rejection", "observed_version_mismatch", "threading.Event.wait",
    }
    if details.get("mechanism") not in mechanisms or not isinstance(details.get("reason"), str):
        raise ValueError("fault event has invalid mechanism provenance")


def _validate_lifecycle_event(event: Mapping[str, object], row: Mapping[str, str]) -> None:
    stage = str(event["stage"])
    details = _require_event_details(event)
    if stage == "PLAN_FINISHED" and (
        details.get("method_id") != row["method_id"]
        or details.get("fault_schedule_visible") is not False
    ):
        raise ValueError("plan event has invalid hidden-fault provenance")
    if stage == "FAULT_INJECTED":
        _validate_fault_event(event, row)
    if stage == "VERIFIED_CORRECT" and row["verified_correct"].lower() != "true":
        raise ValueError("verified-correct event disagrees with its trial")
    if stage == "TRIAL_FAILED" and (
        row["success"].lower() == "true"
        or details.get("failure_stage") != row["failure_stage"]
        or details.get("reason") != row["failure_reason"]
    ):
        raise ValueError("trial-failed event disagrees with its trial")
    if stage in {"BACKGROUND_CLEANUP_FAILED", "TOPOLOGY_TEARDOWN_FAILED"} and (
        row["success"].lower() == "true"
        or row["infrastructure_cleanup_success"].lower() == "true"
        or not row["cleanup_failure_reason"]
        or not isinstance(details.get("reason"), str)
        or not details["reason"]
    ):
        raise ValueError("cleanup lifecycle event disagrees with its trial")


def _validate_attempt_events(
    rows: list[dict[str, str]], events: list[dict[str, object]],
) -> dict[str, bool]:
    rows_by_id = {row["run_id"]: row for row in rows}
    grouped: dict[str, list[dict[str, object]]] = {}
    block_order: list[str] = []
    closed_blocks: set[str] = set()
    current_run_id: str | None = None
    for event in events:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or run_id not in rows_by_id:
            raise ValueError("attempt event has an unknown or missing run_id")
        if run_id != current_run_id:
            if run_id in closed_blocks:
                raise ValueError("transactional attempt event stream is split or reordered")
            if current_run_id is not None:
                closed_blocks.add(current_run_id)
            block_order.append(run_id)
            current_run_id = run_id
        row = rows_by_id[run_id]
        for field in (
            "run_sequence", "method_id", "scenario_class", "seed", "num_agents",
            "success", "timeout", "failure_stage", "failure_reason",
        ):
            if str(event.get(field, "")) != row[field]:
                raise ValueError(f"attempt event {field} does not match its terminal row")
        if not isinstance(event.get("stage"), str) or not event["stage"]:
            raise ValueError("attempt event has an invalid stage")
        if event["stage"] not in ALLOWED_EVENT_STAGES:
            raise ValueError("attempt event has an unknown stage")
        try:
            timestamp = float(event["timestamp"])
            sequence = int(event["event_sequence"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("attempt event lacks a numeric timestamp or sequence") from error
        if not math.isfinite(timestamp) or sequence < 1:
            raise ValueError("attempt event has an invalid timestamp or sequence")
        if event["stage"] in TRANSACTION_STAGE_OPERATIONS:
            _validate_transaction_event(event)
        else:
            _validate_lifecycle_event(event, row)
        grouped.setdefault(run_id, []).append(event)
    expected_block_order = [
        row["run_id"] for row in sorted(rows, key=lambda item: int(item["run_sequence"]))
    ]
    if block_order != expected_block_order:
        raise ValueError("transactional attempt run blocks do not follow run sequence")
    first_attempt_commit: dict[str, bool] = {}
    for run_id, row in rows_by_id.items():
        stream = grouped.get(run_id, [])
        if not stream:
            raise ValueError("transactional trial has no attempt provenance")
        sequences = [int(event["event_sequence"]) for event in stream]
        if sequences != list(range(1, len(stream) + 1)):
            raise ValueError("transactional event sequences must be unique and contiguous")
        timestamps = [float(event["timestamp"]) for event in stream]
        if timestamps != sorted(timestamps):
            raise ValueError("transactional attempt events are not in monotonic timestamp order")
        terminals = [event for event in stream if event["stage"] == TERMINAL_STAGE]
        if len(terminals) != 1:
            raise ValueError("each transactional trial requires exactly one terminal attempt event")
        if stream[-1]["stage"] != TERMINAL_STAGE:
            raise ValueError("transactional terminal attempt event must close its stream")
        if len(stream) < 2 or not any(event["stage"] != TERMINAL_STAGE for event in stream):
            raise ValueError("each transactional trial requires a real attempt or transition event")
        stage_counts = {
            stage: sum(event["stage"] == stage for event in stream)
            for stage in ALLOWED_EVENT_STAGES
        }
        transaction_events = [
            event for event in stream if event["stage"] in TRANSACTION_STAGE_OPERATIONS
        ]
        if len(transaction_events) != int(row["attempt_count"]):
            raise ValueError("transaction event count does not match attempt_count")
        if stage_counts["TRANSACTION_PREPARE"] != int(row["prepare_attempts"]):
            raise ValueError("transaction prepare count does not match prepare_attempts")
        if stage_counts["TRANSACTION_COMMIT"] != int(row["commit_attempts"]):
            raise ValueError("transaction commit count does not match commit_attempts")
        accepted_rollbacks = sum(
            event["stage"] == "TRANSACTION_ROLLBACK"
            and bool(_require_event_details(event)["accepted"])
            for event in stream
        )
        if accepted_rollbacks != int(row["rollback_count"]):
            raise ValueError("transaction rollback count does not match rollback_count")
        construction_failure = row["failure_stage"] == "TOPOLOGY_CONSTRUCTION"
        if construction_failure and [event["stage"] for event in stream] != [
            "TRIAL_FAILED", TERMINAL_STAGE,
        ]:
            raise ValueError("topology construction failure has fabricated lifecycle events")
        if int(row["attempt_count"]) > 0 and not transaction_events:
            raise ValueError("transactional trial has no real transaction events")
        for singleton_stage in LIFECYCLE_STAGES - {TERMINAL_STAGE}:
            if stage_counts[singleton_stage] > 1:
                raise ValueError(f"transactional lifecycle stage {singleton_stage} is duplicated")
        verified_events = stage_counts["VERIFIED_CORRECT"]
        if verified_events != int(row["verified_correct"].lower() == "true"):
            raise ValueError("verified-correct lifecycle count disagrees with its trial")
        trial_failed_events = stage_counts["TRIAL_FAILED"]
        if row["success"].lower() == "true" and trial_failed_events:
            raise ValueError("successful trial cannot contain TRIAL_FAILED")
        if (
            row["success"].lower() != "true"
            and row["failure_stage"] not in {"BACKGROUND_CLEANUP", "TOPOLOGY_TEARDOWN"}
            and trial_failed_events != 1
        ):
            raise ValueError("failed trial lacks its TRIAL_FAILED lifecycle event")
        if construction_failure and stage_counts["FAULT_INJECTED"]:
            raise ValueError("topology construction failure cannot inject a fault")
        first_attempt_commit[run_id] = (
            row["success"].lower() == "true"
            and bool(transaction_events)
            and all(
                int(_require_event_details(event)["attempt_index"]) == 0
                for event in transaction_events
            )
        )
        details = terminals[0].get("details")
        if not isinstance(details, Mapping):
            raise ValueError("transactional terminal attempt event has no details")
        for field in ("success", "timeout", "failure_stage", "failure_reason", "verified_correct"):
            if field not in details or str(details[field]) != row[field]:
                raise ValueError("transactional terminal attempt event disagrees with its terminal row")
    return first_attempt_commit


def _read_scope(path: Path) -> dict[str, object]:
    try:
        scope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("transactional measurement_scope.json is missing or invalid") from error
    if not isinstance(scope, dict):
        raise ValueError("transactional measurement_scope.json must be an object")
    return scope


def _validate_scope(
    scope: Mapping[str, object], *, rows: list[dict[str, str]], events: list[dict[str, object]],
    config: Mapping[str, object], source_runs_sha256: str, source_attempts_sha256: str,
) -> None:
    expected_grid = _grid_payload(build_transactional_schedule(config))
    expected_configuration_hash = stable_fingerprint(config)
    expected = {
        "protocol_id": PROTOCOL_ID,
        "arm_id": ARM_ID,
        "phase": "formal",
        "configuration_sha256": expected_configuration_hash,
        "grid_source": "frozen_config",
        "runs_csv_sha256": source_runs_sha256,
        "attempts_jsonl_sha256": source_attempts_sha256,
        "row_count": len(rows),
        "completed_rows": len(rows),
        "event_count": len(events),
        "invocation_grid": expected_grid,
        "frozen_config_grid": expected_grid,
        "invocation_grid_sha256": stable_fingerprint(expected_grid),
    }
    for field, value in expected.items():
        if scope.get(field) != value:
            raise ValueError(f"transactional measurement scope mismatch: {field}")


def _reject_nominal_target(target: Path) -> None:
    parts = target.parts
    if any(parts[index:index + 2] == ("raw", "exp1") for index in range(len(parts) - 1)):
        raise ValueError("transactional normalization must never overwrite raw/exp1")


def _schema_types(rule: Mapping[str, object]) -> tuple[str, ...]:
    declared = rule.get("type")
    if isinstance(declared, str):
        return (declared,)
    if isinstance(declared, list) and all(isinstance(item, str) for item in declared):
        return tuple(declared)
    return ()


def _coerce_schema_value(field: str, value: object, rule: Mapping[str, object]) -> object:
    allowed = _schema_types(rule)
    text = str(value)
    if text == "" and "null" in allowed and "string" not in allowed:
        return None
    try:
        if "integer" in allowed:
            return int(text)
        if "number" in allowed:
            number = float(text)
            if not math.isfinite(number):
                raise ValueError
            return number
        if "boolean" in allowed:
            normalized = text.strip().lower()
            if normalized in {"true", "1"}:
                return True
            if normalized in {"false", "0"}:
                return False
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"canonical raw schema field {field} has invalid type"
        ) from error
    return text


def _matches_schema_type(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _validate_schema_rule(field: str, value: object, rule: Mapping[str, object]) -> None:
    allowed = _schema_types(rule)
    if allowed and not any(_matches_schema_type(value, expected) for expected in allowed):
        raise ValueError(f"canonical raw schema field {field} has invalid type")
    if "const" in rule and value != rule["const"]:
        raise ValueError(f"canonical raw schema field {field} violates const")
    enum = rule.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ValueError(f"canonical raw schema field {field} violates enum")
    minimum_length = rule.get("minLength")
    if isinstance(minimum_length, int) and (
        not isinstance(value, str) or len(value) < minimum_length
    ):
        raise ValueError(f"canonical raw schema field {field} is too short")


def _condition_matches(
    row: Mapping[str, object], condition: Mapping[str, object],
) -> bool:
    required = condition.get("required", [])
    if isinstance(required, list) and any(field not in row for field in required):
        return False
    properties = condition.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    for field, rule in properties.items():
        if field not in row or not isinstance(rule, Mapping):
            continue
        if "const" in rule and row[field] != rule["const"]:
            return False
    return True


def _validate_canonical_rows(rows: list[dict[str, str]]) -> None:
    """Evaluate CSV rows against the checked-in canonical raw JSON schema."""
    try:
        schema = json.loads(RAW_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("canonical raw schema is missing or invalid") from error
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise ValueError("canonical raw schema has an invalid root")
    properties: dict[str, object] = schema["properties"]
    root_required = schema.get("required", [])
    all_of = schema.get("allOf", [])
    for index, source_row in enumerate(rows, start=2):
        typed: dict[str, object] = {}
        for field, value in source_row.items():
            rule = properties.get(field, {})
            if not isinstance(rule, Mapping):
                raise ValueError(f"canonical raw schema field {field} has an invalid rule")
            typed[field] = _coerce_schema_value(field, value, rule)
        required = list(root_required) if isinstance(root_required, list) else []
        conditional_rules: dict[str, object] = {}
        if isinstance(all_of, list):
            for branch in all_of:
                if not isinstance(branch, Mapping):
                    continue
                condition = branch.get("if")
                consequence = branch.get("then")
                if not isinstance(condition, Mapping) or not isinstance(consequence, Mapping):
                    continue
                if not _condition_matches(typed, condition):
                    continue
                extra_required = consequence.get("required", [])
                if isinstance(extra_required, list):
                    required.extend(extra_required)
                extra_properties = consequence.get("properties", {})
                if isinstance(extra_properties, Mapping):
                    conditional_rules.update(extra_properties)
        missing = sorted(field for field in required if field not in typed)
        if missing:
            raise ValueError(
                f"canonical raw schema row {index} is missing: {','.join(missing)}"
            )
        for field, value in typed.items():
            rule = properties.get(field, {})
            if isinstance(rule, Mapping):
                _validate_schema_rule(field, value, rule)
            override = conditional_rules.get(field)
            if isinstance(override, Mapping):
                _validate_schema_rule(field, value, override)


def normalize_transactional(
    source: Path | str,
    target: Path | str = CANONICAL_TARGET,
    formal_config: Path | str | Mapping[str, object] = Path(
        "configs/exp1_transactional_formation_v1.yaml"
    ),
    attempts_path: Path | str | None = None,
    scope_path: Path | str | None = None,
) -> list[dict[str, str]]:
    """Normalize one complete measured formal arm without discarding failures."""
    source_path = Path(source)
    target_path = Path(target)
    attempts = Path(attempts_path) if attempts_path is not None else source_path.with_name("attempts.jsonl")
    scope_artifact = Path(scope_path) if scope_path is not None else source_path.with_name("measurement_scope.json")
    _reject_nominal_target(target_path)
    rows = _read_csv(source_path)
    grid_errors = _grid_errors(rows)
    if grid_errors:
        raise ValueError("; ".join(grid_errors))
    config, config_file_sha256 = _load_formal_config(formal_config)
    _validate_rows(rows, config)
    _validate_paired_fault_fingerprints(rows)
    events = _read_events(attempts)
    source_runs_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_attempts_sha256 = hashlib.sha256(attempts.read_bytes()).hexdigest()
    _validate_scope(
        _read_scope(scope_artifact), rows=rows, events=events, config=config,
        source_runs_sha256=source_runs_sha256,
        source_attempts_sha256=source_attempts_sha256,
    )
    first_attempt_commit = _validate_attempt_events(rows, events)
    canonical: list[dict[str, str]] = []
    for source_row in rows:
        row = dict(source_row)
        row["trial_id"] = row["run_id"]
        row["result_mode"] = "measured_netns"
        row["first_attempt_commit"] = str(
            first_attempt_commit[row["run_id"]]
        ).lower()
        row["source_runs_sha256"] = source_runs_sha256
        row["source_attempts_sha256"] = source_attempts_sha256
        canonical.append(row)
    _validate_canonical_rows(canonical)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(canonical[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(canonical)
    trials_sha256 = hashlib.sha256(target_path.read_bytes()).hexdigest()
    manifest = {
        "protocol_id": PROTOCOL_ID,
        "arm_id": ARM_ID,
        "phase": "formal",
        "result_mode": "measured_netns",
        "configuration_sha256": stable_fingerprint(config),
        "canonical_config": config,
        "formal_config_path": str(CHECKED_IN_FORMAL_CONFIG.relative_to(CHECKED_IN_FORMAL_CONFIG.parents[1])),
        "formal_config_file_sha256": config_file_sha256,
        "raw_schema_file_sha256": hashlib.sha256(RAW_SCHEMA_PATH.read_bytes()).hexdigest(),
        "source_runs_sha256": source_runs_sha256,
        "source_attempts_sha256": source_attempts_sha256,
        "source_measurement_scope_sha256": hashlib.sha256(scope_artifact.read_bytes()).hexdigest(),
        "trials_sha256": trials_sha256,
        "row_count": len(canonical),
        "event_count": len(events),
    }
    target_path.with_name("normalization_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return canonical


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--attempts")
    parser.add_argument("--scope")
    parser.add_argument("--target", default=str(CANONICAL_TARGET))
    parser.add_argument("--config", default="configs/exp1_transactional_formation_v1.yaml")
    args = parser.parse_args()
    normalize_transactional(args.source, args.target, args.config, args.attempts, args.scope)


if __name__ == "__main__":
    main()
