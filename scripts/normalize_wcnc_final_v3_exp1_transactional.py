"""Canonicalize measured formal Exp1 transactional trial artifacts.

This normalizer deliberately accepts only the frozen formal arm.  Pilot and
override artifacts remain source data and must not enter the paper raw tree.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.exp1_transactional_formation import (
    ARM_ID,
    METHODS,
    SCENARIO_CLASSES,
    TASK_SIZES,
    load_transactional_config,
)
from experiments.paper_protocol import PROTOCOL_ID, stable_fingerprint


FORMAL_SEEDS = tuple(range(50))
SOURCE_RESULT_MODE = "real_linux_netns_transactional_formation"
CANONICAL_TARGET = Path(
    "results/paper/wcnc_final_v3/raw/exp1_transactional/trials.csv"
)
TERMINAL_STAGE = "TERMINAL_ROW_READY"

REQUIRED_RUN_FIELDS = frozenset({
    "protocol_id", "arm_id", "phase", "run_id", "run_sequence",
    "scenario_class", "seed", "num_agents", "num_gateways",
    "num_business_edges", "method_id", "fault_schedule_fingerprint",
    "logical_fault_target", "observation_version_fingerprint",
    "verifier_fingerprint", "common_infrastructure_fingerprint",
    "common_infrastructure_provenance", "configuration_sha256",
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


def _load_formal_config(config: Path | str | Mapping[str, object]) -> dict[str, Any]:
    if isinstance(config, Mapping):
        loaded = dict(config)
    else:
        loaded = load_transactional_config(Path(config))
    experiment = loaded.get("experiment")
    if not isinstance(experiment, Mapping):
        raise ValueError("transactional formal config has no experiment section")
    if (
        experiment.get("protocol_id") != PROTOCOL_ID
        or experiment.get("arm_id") != ARM_ID
        or experiment.get("phase") != "formal"
    ):
        raise ValueError("transactional normalizer requires the frozen formal arm")
    return loaded


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


def _validate_rows(rows: list[dict[str, str]], expected_hash: str) -> None:
    if not rows:
        raise ValueError("transactional formal input is empty")
    _require_source_fields(rows)
    run_ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        if row["protocol_id"] != PROTOCOL_ID:
            raise ValueError(f"runs.csv row {index} has an unexpected protocol")
        if row["arm_id"] != ARM_ID or row["phase"] != "formal":
            raise ValueError(f"runs.csv row {index} is not in the formal transactional arm")
        if row["result_mode"] != SOURCE_RESULT_MODE:
            raise ValueError("transactional canonical input must be measured Linux netns data")
        if row["configuration_sha256"] != expected_hash:
            raise ValueError("transactional configuration hash does not match the frozen formal config")
        run_id = row["run_id"].strip()
        if not run_id or run_id in run_ids:
            raise ValueError("transactional formal input has missing or duplicate run_id values")
        run_ids.add(run_id)
        if not row["fault_schedule_fingerprint"].strip():
            raise ValueError("transactional formal input has a missing fault schedule fingerprint")
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


def _validate_terminal_events(rows: list[dict[str, str]], events: list[dict[str, object]]) -> None:
    source_ids = {row["run_id"] for row in rows}
    terminals: dict[str, list[dict[str, object]]] = {}
    for event in events:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or run_id not in source_ids:
            raise ValueError("attempt event has an unknown or missing run_id")
        if event.get("stage") == TERMINAL_STAGE:
            terminals.setdefault(run_id, []).append(event)
    for row in rows:
        matching = terminals.get(row["run_id"], [])
        if len(matching) != 1:
            raise ValueError("each transactional trial requires exactly one terminal attempt event")
        details = matching[0].get("details")
        if not isinstance(details, Mapping):
            raise ValueError("transactional terminal attempt event has no details")
        if _truth(details.get("success")) != _truth(row["success"]) or _truth(
            details.get("timeout")
        ) != _truth(row["timeout"]):
            raise ValueError("transactional terminal attempt event disagrees with its terminal row")


def _reject_nominal_target(target: Path) -> None:
    parts = target.parts
    if any(parts[index:index + 2] == ("raw", "exp1") for index in range(len(parts) - 1)):
        raise ValueError("transactional normalization must never overwrite raw/exp1")


def normalize_transactional(
    source: Path | str,
    target: Path | str = CANONICAL_TARGET,
    formal_config: Path | str | Mapping[str, object] = Path(
        "configs/exp1_transactional_formation_v1.yaml"
    ),
    attempts_path: Path | str | None = None,
) -> list[dict[str, str]]:
    """Normalize one complete measured formal arm without discarding failures."""
    source_path = Path(source)
    target_path = Path(target)
    attempts = Path(attempts_path) if attempts_path is not None else source_path.with_name("attempts.jsonl")
    _reject_nominal_target(target_path)
    rows = _read_csv(source_path)
    grid_errors = _grid_errors(rows)
    if grid_errors:
        raise ValueError("; ".join(grid_errors))
    config = _load_formal_config(formal_config)
    _validate_rows(rows, stable_fingerprint(config))
    _validate_paired_fault_fingerprints(rows)
    events = _read_events(attempts)
    _validate_terminal_events(rows, events)

    source_runs_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source_attempts_sha256 = hashlib.sha256(attempts.read_bytes()).hexdigest()
    canonical: list[dict[str, str]] = []
    for source_row in rows:
        row = dict(source_row)
        row["trial_id"] = row["run_id"]
        row["execution_mode_detail"] = row["result_mode"]
        row["result_mode"] = "measured_netns"
        row["first_attempt_commit"] = str(
            _truth(row["success"]) and int(row["attempt_count"]) == 1
        ).lower()
        row["source_runs_sha256"] = source_runs_sha256
        row["source_attempts_sha256"] = source_attempts_sha256
        canonical.append(row)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(canonical[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(canonical)
    return canonical


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--attempts")
    parser.add_argument("--target", default=str(CANONICAL_TARGET))
    parser.add_argument("--config", default="configs/exp1_transactional_formation_v1.yaml")
    args = parser.parse_args()
    normalize_transactional(args.source, args.target, args.config, args.attempts)


if __name__ == "__main__":
    main()
