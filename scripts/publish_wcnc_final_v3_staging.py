#!/usr/bin/env python3
"""Validate staged Exp2--Exp4 grids and publish them without overwrites."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import fields
from pathlib import Path
from typing import Iterable

from experiments.paper_protocol import PROTOCOL_ID, load_and_validate_wcnc_v3_config
from src.metrics.exp2_robustness import RobustnessRunMetrics
from src.metrics.paper import PaperTrial


_EXP2_FIELDS = {field.name for field in fields(RobustnessRunMetrics)} | {
    "protocol_id", "execution_mode_detail", "method_id", "gamma",
    "environment_fingerprint", "observation_fingerprint", "oracle_fingerprint",
    "pre_verification_correct_decision", "pre_verification_feasible",
    "unsafe_proposal_before_verification", "verification_rescued", "search_timeout",
}
_PAPER_FIELDS = {field.name for field in fields(PaperTrial)} | {"execution_mode_detail"}
_BOOL_FIELDS = {
    "adapted", "ground_truth_conflict", "ground_truth_resolvable",
    "ground_truth_uses_true_state", "pre_execution_feasibility_checked",
    "conflict_detected", "conflict_resolved", "qos_satisfied",
    "infeasible_configuration", "decision_rejected", "safe_rejection",
    "unsafe_execution", "transaction_attempted", "rollback_triggered",
    "rollback_success", "no_partial_commit", "stale_state_detected",
    "stale_proposal_rejected", "proposal_regenerated", "success", "timeout",
    "pre_verification_correct_decision", "pre_verification_decision_correct",
    "pre_verification_feasible", "unsafe_proposal_before_verification",
    "verification_rescued", "search_timeout", "applicable",
}
_INT_FIELDS = {
    "seed", "event_id", "task_size", "num_dag_edges", "num_gateways",
    "total_rules", "total_rule_objects", "changed_rules", "total_paths",
    "changed_paths", "total_agents", "changed_agents", "affected_agent_count",
    "total_gateways", "changed_gateways", "total_flows", "unaffected_flows",
    "disturbed_unaffected_flows", "control_messages", "control_bytes",
    "rollback_count", "evaluated_combinations", "ground_truth_feasible_combinations",
    "stale_ms", "proposals_per_layer", "num_proposals", "num_layers_observed",
    "num_raw_combinations", "num_pruned_combinations", "num_evaluated_combinations",
    "num_feasible_combinations", "stable_version_before", "stable_version_after",
    "proposal_generated_version", "execution_version", "read_set_version",
}
_NONNEGATIVE_NUMERIC_FIELDS = {
    "gamma", "affected_scope_bucket_percent",
    "affected_scope_ratio", "affected_flow_ratio", "dependency_closure_ratio",
    "post_fault_capacity_ratio", "conflict_pressure", "noise_ratio",
    "coordination_latency_ms", "feasibility_latency_ms", "transaction_latency_ms",
    "total_latency_ms", "peak_memory_mb", "reconfiguration_latency_ms",
    "recovery_latency_ms", "modification_scope_ratio", "rule_change_ratio",
    "gateway_change_ratio", "unaffected_disturbance_ratio",
}
_UNIT_INTERVAL_FIELDS = {
    "affected_scope_ratio", "affected_flow_ratio", "dependency_closure_ratio",
    "modification_scope_ratio", "rule_change_ratio", "gateway_change_ratio",
    "unaffected_disturbance_ratio",
}


def _number(value: object) -> float:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric grid value: {value!r}") from error
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite numeric grid value: {value!r}")
    return parsed


def _strict_bool(value: object, field: str, *, nullable: bool = False) -> bool | None:
    normalized = str(value).strip().lower()
    if nullable and normalized == "":
        return None
    if normalized not in {"true", "false"}:
        raise ValueError(f"staged {field} boolean domain is invalid")
    return normalized == "true"


def _validate_metric_domains(row: dict[str, str], experiment: str) -> None:
    def require_finite(field: str) -> float:
        value = row.get(field, "")
        if value == "":
            raise ValueError(f"staged {experiment} requires nonempty {field}")
        return _number(value)

    for field in _BOOL_FIELDS & set(row):
        _strict_bool(row[field], field, nullable=field not in {"success", "timeout", "adapted"})
    for field in _INT_FIELDS & set(row):
        value = row[field]
        if value == "":
            continue
        if re.fullmatch(r"-?[0-9]+", value) is None or int(value) < 0:
            raise ValueError(f"staged {field} integer domain is invalid")
    for field in _NONNEGATIVE_NUMERIC_FIELDS & set(row):
        value = row[field]
        if value == "":
            continue
        parsed = _number(value)
        if parsed < 0:
            raise ValueError(f"staged {field} numeric domain is negative")
        if field in _UNIT_INTERVAL_FIELDS and parsed > 1:
            raise ValueError(f"staged {field} ratio is outside [0,1]")
    failure_severity = row.get("failure_severity", "")
    if failure_severity != "":
        parsed_severity = _number(failure_severity)
        signed_capacity_headroom = (
            experiment == "exp4"
            and row.get("failure_type") == "capacity_degradation"
            and _number(row.get("post_fault_capacity_ratio")) > 1.0
        )
        if parsed_severity < 0 and not signed_capacity_headroom:
            raise ValueError("staged failure_severity numeric domain is negative")
    if len(row.get("scenario_fingerprint", "")) < 32:
        raise ValueError("staged scenario fingerprint is invalid")
    if experiment == "exp2":
        producer_method = row.get("method", "")
        failed_before_metric = (
            producer_method == ""
            and _strict_bool(row.get("success", ""), "success") is False
            and bool(row.get("failure_reason", ""))
        )
        if producer_method != row.get("method_id") and not failed_before_metric:
            raise ValueError("staged Exp2 producer method identity drift")
        if not failed_before_metric:
            latency = require_finite("coordination_latency_ms")
            if latency < 0:
                raise ValueError("staged coordination_latency_ms is negative")
    if experiment in {"exp3", "exp4"}:
        if row.get("experiment") != experiment or row.get("mode") != "paper":
            raise ValueError(f"staged {experiment} producer identity drift")
        applicable = _strict_bool(row.get("applicable", ""), "applicable", nullable=True)
        if applicable is False:
            raise ValueError("inapplicable baseline must be N/A, not a staged zero row")
        for field in ("modification_scope_ratio", "unaffected_disturbance_ratio"):
            value = require_finite(field)
            if not 0 <= value <= 1:
                raise ValueError(f"staged {field} ratio is outside [0,1]")
        if _strict_bool(row.get("success", ""), "success"):
            latency_field = (
                "reconfiguration_latency_ms" if experiment == "exp3"
                else "recovery_latency_ms"
            )
            latency = require_finite(latency_field)
            if latency < 0:
                raise ValueError(f"staged {latency_field} is negative")


def _point(row: dict[str, str], experiment: str) -> float:
    if experiment == "exp2":
        return _number(row.get("gamma"))
    if experiment == "exp3":
        return _number(row.get("affected_scope_bucket_percent"))
    failure_type = row.get("failure_type", "")
    if failure_type == "capacity_degradation":
        return _number(row.get("post_fault_capacity_ratio"))
    # Link/agent sweeps target a ratio, while the topology-discrete realized
    # ratio is recorded separately and can legitimately differ by seed.
    return _number(row.get("failure_severity"))


def _expected_grid(config: dict[str, object], experiment: str, seeds: tuple[int, ...]) -> dict[tuple[str, float, int, int], set[str]]:
    expected: dict[tuple[str, float, int, int], set[str]] = {}
    if experiment == "exp2":
        for point in config["exp2"]["gamma"]:
            for seed in seeds:
                expected[("", float(point), seed, 0)] = set(config["exp2"]["methods"])
    elif experiment == "exp3":
        for point in config["exp3"]["affected_dependency_scope_percent"]:
            for seed in seeds:
                for event_id in range(5):
                    expected[("", float(point), seed, event_id)] = set(config["exp3"]["methods"])
    elif experiment == "exp4":
        specs = (
            ("link_failure", "link_affected_flow_ratio"),
            ("agent_failure", "agent_dependency_closure_ratio"),
            ("capacity_degradation", "capacity_ratio"),
        )
        for failure_type, field in specs:
            for point in config["exp4"][field]:
                for seed in seeds:
                    expected[(failure_type, float(point), seed, 0)] = set(
                        config["exp4"]["methods_by_failure"][failure_type]
                    )
    else:
        raise ValueError(f"unsupported staged experiment: {experiment}")
    return expected


def validate_staged_experiment(
    raw_dir: Path,
    experiment: str,
    *,
    seeds: Iterable[int],
    config_path: Path = Path("configs/wcnc_final_v3.yaml"),
    expected_commit: str,
    repo: Path = Path("."),
) -> None:
    """Fail unless ``raw_dir`` is the exact frozen, paired invocation grid."""
    trials = raw_dir / "trials.csv"
    commit_path = raw_dir / "execution_commit.txt"
    if not trials.is_file() or not commit_path.is_file():
        raise ValueError("staged raw lacks trials.csv or execution_commit.txt")
    commit = commit_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or commit != expected_commit:
        raise ValueError("staged execution commit differs from expected current commit")
    if subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo,
        capture_output=True,
    ).returncode != 0:
        raise ValueError("staged execution commit is not git-resolvable")
    with trials.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_fields = _EXP2_FIELDS if experiment == "exp2" else _PAPER_FIELDS
        if reader.fieldnames != sorted(expected_fields):
            raise ValueError(f"staged {experiment} trials.csv schema differs from exact producer schema")
        rows = list(reader)
    expected = _expected_grid(
        load_and_validate_wcnc_v3_config(config_path), experiment,
        tuple(int(seed) for seed in seeds),
    )
    observed: dict[tuple[str, float, int, int], list[str]] = defaultdict(list)
    fingerprints: dict[tuple[str, float, int, int], set[str]] = defaultdict(set)
    for row in rows:
        required = ("protocol_id", "seed", "method_id", "scenario_fingerprint", "result_mode", "success", "timeout", "failure_reason")
        if any(name not in row for name in required):
            raise ValueError("staged row schema is incomplete")
        _validate_metric_domains(row, experiment)
        if row["protocol_id"] != PROTOCOL_ID or row["result_mode"] != "transactional_simulation":
            raise ValueError("staged row protocol/result mode drift")
        key = (
            row.get("failure_type", "") if experiment == "exp4" else "",
            _point(row, experiment), int(row["seed"]), int(row.get("event_id") or 0),
        )
        observed[key].append(row["method_id"])
        fingerprints[key].add(row["scenario_fingerprint"])
    if set(observed) != set(expected):
        raise ValueError("staged invocation grid is incomplete or has unexpected rows")
    for key, methods in observed.items():
        if len(methods) != len(set(methods)):
            raise ValueError(f"duplicate method row in staged grid: {key}")
        if set(methods) != expected[key]:
            raise ValueError(f"staged method grid drift: {key}")
        if len(fingerprints[key]) != 1:
            raise ValueError(f"methods do not share one scenario fingerprint: {key}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_immutable_tree(source: Path, target: Path, *, experiment: str) -> None:
    """Recoverably publish a complete immutable tree; write completion last."""
    marker = target / "publication_complete.json"
    if (source / "publication_complete.json").exists():
        raise ValueError("publication source must not contain a completion marker")
    paths = sorted(source.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("publication source may not contain symbolic links")
    files = [path for path in paths if path.is_file()]
    if not files:
        raise ValueError(f"empty publication source: {source}")
    artifact_hashes = {
        str(path.relative_to(source)): _sha256(path) for path in files
    }
    completion = {
        "publication_version": "wcnc-final-v3-immutable-tree-v1",
        "protocol_id": PROTOCOL_ID,
        "experiment": experiment,
        "artifact_hashes": artifact_hashes,
    }
    encoded_completion = json.dumps(completion, indent=2, sort_keys=True) + "\n"
    if marker.exists() and marker.read_text(encoding="utf-8") != encoded_completion:
        raise ValueError("existing publication completion marker differs from source tree")
    for path in files:
        destination = target / path.relative_to(source)
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError(f"refusing to overwrite immutable canonical artifact: {destination}")
    for path in files:
        destination = target / path.relative_to(source)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    target.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(marker.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(encoded_completion)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, marker)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--experiment", choices=("exp2", "exp3", "exp4"), required=True)
    parser.add_argument("--seeds", default="0:99")
    parser.add_argument("--config", type=Path, default=Path("configs/wcnc_final_v3.yaml"))
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args()
    start, end = (int(value) for value in args.seeds.split(":", 1))
    validate_staged_experiment(
        args.source, args.experiment, seeds=range(start, end + 1), config_path=args.config,
        expected_commit=args.expected_commit,
    )
    publish_immutable_tree(args.source, args.target, experiment=args.experiment)


if __name__ == "__main__":
    main()
