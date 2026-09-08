from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from experiments.exp1_transactional_formation import (
    ARM_ID as TRANSACTIONAL_ARM_ID,
    load_transactional_config,
)
from experiments.paper_protocol import PROTOCOL_ID, stable_fingerprint
from scripts.normalize_wcnc_final_v3_exp1_transactional import (
    RAW_SCHEMA_PATH as TRANSACTIONAL_RAW_SCHEMA_PATH,
    _validate_canonical_rows,
)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def truth(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def wilson(numerator: int, denominator: int) -> tuple[float, float, float]:
    if denominator == 0:
        return math.nan, math.nan, math.nan
    z = 1.959963984540054
    p = numerator / denominator
    scale = 1 + z * z / denominator
    center = (p + z * z / (2 * denominator)) / scale
    half = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator**2)) / scale
    return p, max(0.0, center - half), min(1.0, center + half)


def cluster_bootstrap(values: list[tuple[int, float]], iterations: int = 5000) -> tuple[float, float, float]:
    if not values:
        return math.nan, math.nan, math.nan
    clusters: dict[int, list[float]] = defaultdict(list)
    for seed, value in values:
        clusters[seed].append(value)
    keys = sorted(clusters)
    mean = sum(value for _, value in values) / len(values)
    if all(value == values[0][1] for _, value in values):
        return mean, mean, mean
    rng = random.Random("wcnc_final_v3_cluster_bootstrap")
    samples = []
    for _ in range(iterations):
        selected = [rng.choice(keys) for _ in keys]
        materialized = [value for key in selected for value in clusters[key]]
        samples.append(sum(materialized) / len(materialized))
    samples.sort()
    return mean, samples[int(0.025 * (iterations - 1))], samples[int(0.975 * (iterations - 1))]


TRANSACTIONAL_CONTINUOUS = (
    ("method_owned_formation_latency_ms", False),
    ("time_to_correct_formation_ms", True),
    ("rollback_scope_objects", False),
    ("wasted_rule_commands", False),
    ("partial_state_exposure_ms", False),
)
TRANSACTIONAL_FORMAL_METHODS = ("proposed", "cspf", "global_sfc_embedding")
TRANSACTIONAL_FORMAL_SCENARIOS = (
    "stale_version", "prepare_ack_timeout", "command_rejection",
)
TRANSACTIONAL_FORMAL_SIZES = (4, 8, 12, 16, 20)
TRANSACTIONAL_FORMAL_SEEDS = tuple(range(50))
TRANSACTIONAL_CANONICAL_REQUIRED = frozenset({
    "protocol_id", "arm_id", "phase", "result_mode", "trial_id", "run_id",
    "scenario_class", "scenario_fingerprint", "seed", "num_agents", "method_id",
    "fault_schedule_fingerprint", "configuration_sha256", "execution_mode_detail",
    "success", "timeout", "failure_stage", "failure_reason", "verified_correct",
    "infrastructure_cleanup_success", "cleanup_failure_reason",
    "leaked_state_fingerprint", "source_runs_sha256", "source_attempts_sha256",
})
TRANSACTIONAL_FORMAL_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "configs" / "exp1_transactional_formation_v1.yaml"
)


def _number(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value))


def _rollback_scope_size(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        raise ValueError("rollback_scope_objects must be a JSON array")
    return float(len(decoded))


def _transactional_value(row: dict[str, object], metric: str) -> float | None:
    if metric == "rollback_scope_objects":
        return _rollback_scope_size(row.get(metric))
    return _number(row.get(metric))


def _transactional_first_attempt_commit(row: dict[str, object]) -> bool:
    if str(row.get("first_attempt_commit", "")).strip():
        return truth(row["first_attempt_commit"])
    try:
        first_attempt = int(str(row.get("attempt_count", ""))) == 1
    except ValueError:
        return False
    return first_attempt and truth(row.get("success"))


def _transactional_group_key(row: dict[str, object]) -> tuple[str, str, int]:
    return (
        str(row.get("method_id", "")),
        str(row.get("scenario_class", "")),
        int(str(row["num_agents"])),
    )


def _transactional_formal_grid_errors(rows: list[dict[str, object]]) -> list[str]:
    expected = {
        (scenario_class, num_agents, seed, method_id)
        for scenario_class in TRANSACTIONAL_FORMAL_SCENARIOS
        for num_agents in TRANSACTIONAL_FORMAL_SIZES
        for seed in TRANSACTIONAL_FORMAL_SEEDS
        for method_id in TRANSACTIONAL_FORMAL_METHODS
    }
    try:
        observed = [
            (
                str(row["scenario_class"]), int(str(row["num_agents"])),
                int(str(row["seed"])), str(row["method_id"]),
            )
            for row in rows
        ]
    except (KeyError, ValueError) as error:
        raise ValueError("transactional formal raw has an invalid grid key") from error
    errors: list[str] = []
    if len(observed) != len(expected):
        errors.append(
            f"transactional formal raw must contain exactly {len(expected)} rows; "
            f"got {len(observed)}"
        )
    if set(observed) != expected:
        errors.append("transactional formal raw does not match the frozen grid")
    if len(observed) != len(set(observed)):
        errors.append("transactional formal raw contains duplicate trial identities")
    return errors


def _validate_transactional_pairing(rows: list[dict[str, object]]) -> None:
    paired: dict[tuple[str, int, int], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        try:
            key = (
                str(row["scenario_class"]), int(str(row["num_agents"])),
                int(str(row["seed"])),
            )
        except (KeyError, ValueError) as error:
            raise ValueError("transactional aggregation requires scenario, size, and seed") from error
        paired[key].append(row)
    for key, group in paired.items():
        methods = [str(row.get("method_id", "")) for row in group]
        fingerprints = {str(row.get("fault_schedule_fingerprint", "")) for row in group}
        if len(methods) != len(set(methods)):
            raise ValueError(f"transactional paired trial {key!r} has duplicate methods")
        if set(methods) != {"proposed", "cspf", "global_sfc_embedding"}:
            raise ValueError(f"transactional paired trial {key!r} lacks the canonical methods")
        if len(fingerprints) != 1 or "" in fingerprints:
            raise ValueError(f"transactional paired trial {key!r} has divergent fault fingerprints")


def _validate_transactional_canonical_artifact(
    raw_path: Path, rows: list[dict[str, str]], raw_sha: str,
) -> None:
    manifest_path = raw_path.with_name("normalization_manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("transactional aggregation requires a normalization manifest") from error
    if not isinstance(manifest, dict):
        raise ValueError("transactional normalization manifest must be an object")
    config = load_transactional_config(TRANSACTIONAL_FORMAL_CONFIG)
    expected_config_hash = stable_fingerprint(config)
    expected_manifest = {
        "protocol_id": PROTOCOL_ID,
        "arm_id": TRANSACTIONAL_ARM_ID,
        "phase": "formal",
        "result_mode": "measured_netns",
        "configuration_sha256": expected_config_hash,
        "trials_sha256": raw_sha,
        "row_count": len(rows),
        "raw_schema_file_sha256": hashlib.sha256(
            TRANSACTIONAL_RAW_SCHEMA_PATH.read_bytes()
        ).hexdigest(),
    }
    for field, value in expected_manifest.items():
        if manifest.get(field) != value:
            raise ValueError(f"transactional normalization manifest mismatch: {field}")
    if manifest.get("canonical_config") != config:
        raise ValueError("transactional normalization manifest canonical config is untrusted")
    if manifest.get("formal_config_file_sha256") != hashlib.sha256(
        TRANSACTIONAL_FORMAL_CONFIG.read_bytes()
    ).hexdigest():
        raise ValueError("transactional normalization manifest config file hash is untrusted")
    if manifest.get("formal_config_path") != "configs/exp1_transactional_formation_v1.yaml":
        raise ValueError("transactional normalization manifest config path is untrusted")
    event_count = manifest.get("event_count")
    if not isinstance(event_count, int) or event_count < 2 * len(rows):
        raise ValueError("transactional normalization manifest event count is invalid")
    source_hashes = {
        "source_runs_sha256": manifest.get("source_runs_sha256"),
        "source_attempts_sha256": manifest.get("source_attempts_sha256"),
        "source_measurement_scope_sha256": manifest.get("source_measurement_scope_sha256"),
    }
    if not all(
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in source_hashes.values()
    ):
        raise ValueError("transactional normalization manifest lacks source provenance hashes")
    _validate_canonical_rows(rows)
    for index, row in enumerate(rows, start=2):
        missing = sorted(TRANSACTIONAL_CANONICAL_REQUIRED - set(row))
        if missing:
            raise ValueError(f"transactional canonical row {index} is missing: {','.join(missing)}")
        if (
            row["protocol_id"] != PROTOCOL_ID
            or row["arm_id"] != TRANSACTIONAL_ARM_ID
            or row["phase"] != "formal"
            or row["result_mode"] != "measured_netns"
            or row["configuration_sha256"] != expected_config_hash
        ):
            raise ValueError("transactional canonical row has untrusted arm provenance")
        if (
            row["source_runs_sha256"] != source_hashes["source_runs_sha256"]
            or row["source_attempts_sha256"] != source_hashes["source_attempts_sha256"]
        ):
            raise ValueError("transactional canonical row source provenance disagrees with manifest")


def aggregate_transactional(
    rows: list[dict[str, object]],
    *,
    raw_path: Path | str = Path("<in-memory>"),
    raw_sha: str | None = None,
) -> list[dict[str, object]]:
    """Aggregate complete transactional trials while retaining failed attempts."""
    raw_path = Path(raw_path)
    raw_sha = raw_sha if raw_sha is not None else hashlib.sha256(
        b"wcnc_final_v3_transactional_in_memory"
    ).hexdigest()
    _validate_transactional_pairing(rows)
    groups: dict[tuple[str, str, int], list[dict[str, object]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row)
        groups[_transactional_group_key(row)].append(row)
    out: list[dict[str, object]] = []
    for (method, scenario_class, num_agents), items in sorted(groups.items()):
        rate_metrics = (
            ("success_rate", sum(truth(row.get("success")) for row in items)),
            ("timeout_rate", sum(truth(row.get("timeout")) for row in items)),
            (
                "first_attempt_commit_rate",
                sum(_transactional_first_attempt_commit(row) for row in items),
            ),
        )
        for metric, numerator in rate_metrics:
            estimate, low, high = wilson(numerator, len(items))
            out.append(_aggregate_row(
                "exp1_transactional", method, scenario_class, "num_agents",
                num_agents, metric, estimate, low, high, numerator, len(items),
                raw_path, raw_sha, "wilson_95",
            ))
        for metric, successful_verified_only in TRANSACTIONAL_CONTINUOUS:
            samples: list[tuple[int, float]] = []
            for row in items:
                if successful_verified_only and not (
                    truth(row.get("success")) and truth(row.get("verified_correct"))
                ):
                    continue
                value = _transactional_value(row, metric)
                if value is not None:
                    samples.append((int(str(row["seed"])), value))
            estimate, low, high = cluster_bootstrap(samples)
            out.append(_aggregate_row(
                "exp1_transactional", method, scenario_class, "num_agents",
                num_agents, metric, estimate, low, high, len(samples), len(items),
                raw_path, raw_sha, "topology_cluster_bootstrap_95",
            ))
    return out


def _write_transactional_paired_differences(
    rows: list[dict[str, object]], path: Path, raw_path: Path, raw_sha: str,
) -> None:
    indexed: dict[tuple[str, int, int, str, str], dict[str, object]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        key = (
            str(row["scenario_class"]), int(str(row["num_agents"])),
            int(str(row["seed"])), str(row["fault_schedule_fingerprint"]),
            str(row["method_id"]),
        )
        if key in indexed:
            raise ValueError("transactional paired analysis has duplicate trial identities")
        indexed[key] = row
    differences: dict[tuple[str, int, str, str], list[tuple[int, float]]] = defaultdict(list)
    eligible: dict[tuple[str, int, str, str], int] = defaultdict(int)
    exclusion_reasons: dict[tuple[str, int, str, str], set[str]] = defaultdict(set)
    rate_metrics = (
        ("success_rate", lambda row: float(truth(row.get("success")))),
        ("timeout_rate", lambda row: float(truth(row.get("timeout")))),
        ("first_attempt_commit_rate", lambda row: float(_transactional_first_attempt_commit(row))),
    )
    for (scenario_class, num_agents, seed, fingerprint, method), row in indexed.items():
        if method == "proposed":
            continue
        proposed = indexed.get((scenario_class, num_agents, seed, fingerprint, "proposed"))
        if proposed is None:
            continue
        for metric, value in rate_metrics:
            key = (scenario_class, num_agents, method, metric)
            eligible[key] += 1
            differences[key].append(
                (seed, value(row) - value(proposed))
            )
        for metric, successful_verified_only in TRANSACTIONAL_CONTINUOUS:
            key = (scenario_class, num_agents, method, metric)
            eligible[key] += 1
            if successful_verified_only and not (
                truth(row.get("success")) and truth(row.get("verified_correct"))
                and truth(proposed.get("success")) and truth(proposed.get("verified_correct"))
            ):
                exclusion_reasons[key].add("not_successful_verified_both")
                continue
            baseline_value = _transactional_value(row, metric)
            proposed_value = _transactional_value(proposed, metric)
            if baseline_value is None or proposed_value is None:
                exclusion_reasons[key].add("missing_source_value")
                continue
            differences[key].append(
                (seed, baseline_value - proposed_value)
            )
    output: list[dict[str, object]] = []
    for (scenario_class, num_agents, method, metric), count in sorted(eligible.items()):
        values = differences[(scenario_class, num_agents, method, metric)]
        estimate, low, high = cluster_bootstrap(values)
        paired_trials = len(values)
        excluded_pairs = count - paired_trials
        output.append({
            "protocol_id": "wcnc_final_v3", "experiment": "exp1_transactional",
            "series": scenario_class, "method_id": method,
            "reference_method_id": "proposed", "x_name": "num_agents",
            "x_value": num_agents, "metric": metric,
            "paired_difference": estimate if paired_trials else "",
            "estimate": estimate if paired_trials else "",
            "ci_low": low if paired_trials else "", "ci_high": high if paired_trials else "",
            "eligible_pairs": count, "paired_trials": paired_trials,
            "excluded_pairs": excluded_pairs,
            "exclusion_reason": ";".join(sorted(exclusion_reasons[(scenario_class, num_agents, method, metric)])),
            "interval": "paired_topology_cluster_bootstrap_95",
            "raw_path": str(raw_path), "raw_sha256": raw_sha,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fields = list(output[0]) if output else [
            "protocol_id", "experiment", "series", "method_id",
            "reference_method_id", "x_name", "x_value", "metric",
            "paired_difference", "estimate", "ci_low", "ci_high", "eligible_pairs",
            "paired_trials", "excluded_pairs", "exclusion_reason",
            "interval", "raw_path", "raw_sha256",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)


def _x(exp: str, row: dict[str, str]) -> tuple[str, float]:
    if exp == "exp1":
        return "num_agents", float(row["num_agents"])
    if exp == "exp2":
        return "gamma", float(row["gamma"])
    if exp == "exp3":
        return "affected_dependency_scope_percent", float(row["affected_scope_bucket_percent"])
    if row["failure_type"] == "capacity_degradation":
        return "post_fault_capacity_ratio", float(row["post_fault_capacity_ratio"])
    if row["failure_type"] == "agent_failure":
        return "target_dependency_closure_ratio", float(row["failure_severity"])
    return "target_affected_flow_ratio", float(row["failure_severity"])


def aggregate_experiment(exp: str, raw_path: Path, output_path: Path) -> list[dict[str, object]]:
    rows = read_rows(raw_path)
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if exp == "exp1_transactional":
        grid_errors = _transactional_formal_grid_errors(rows)
        if grid_errors:
            raise ValueError("; ".join(grid_errors))
        _validate_transactional_canonical_artifact(raw_path, rows, raw_sha)
        out = aggregate_transactional(rows, raw_path=raw_path, raw_sha=raw_sha)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fields = list(out[0]) if out else []
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(out)
        _write_transactional_paired_differences(
            rows, output_path.with_name("paired_differences.csv"), raw_path, raw_sha,
        )
        return out
    groups: dict[tuple[str, str, float, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        method = row.get("method_id") or row.get("method") or ""
        x_name, x_value = _x(exp, row)
        series = row.get("failure_type", "") if exp == "exp4" else row.get("series", "")
        groups[(method, x_name, x_value, series)].append(row)
    out: list[dict[str, object]] = []
    for (method, x_name, x_value, series), items in sorted(groups.items()):
        raw_items = items
        if exp == "exp1":
            items = [
                row
                for row in raw_items
                if truth(row.get("paired_analysis_eligible", "true"))
            ]
        rate_metrics: dict[str, tuple[int, int]] = {
            "success_rate": (sum(truth(row.get("success")) for row in items), len(items)),
            "timeout_rate": (sum(truth(row.get("timeout")) for row in items), len(items)),
        }
        if exp == "exp1":
            rate_metrics.update({
                "paired_scenario_retention_rate": (
                    sum(
                        truth(row.get("paired_analysis_eligible", "true"))
                        for row in raw_items
                    ),
                    len(raw_items),
                ),
                "preparation_failure_rate": (
                    sum(
                        not truth(row.get("paired_analysis_eligible", "true"))
                        for row in raw_items
                    ),
                    len(raw_items),
                ),
            })
        if exp == "exp2":
            feasible = [row for row in items if row.get("conflict_class") != "UNRESOLVABLE_CONFLICT"]
            unresolvable = [row for row in items if row.get("conflict_class") == "UNRESOLVABLE_CONFLICT"]
            rate_metrics.update({
                "feasible_qos_satisfaction_rate": (sum(truth(row.get("qos_satisfied")) for row in feasible), len(feasible)),
                "pre_verification_correct_decision_rate": (sum(truth(row.get("pre_verification_correct_decision")) for row in items), len(items)),
                "system_safe_rejection_rate": (sum(truth(row.get("safe_rejection")) for row in unresolvable), len(unresolvable)),
                "rollback_rate": (sum(truth(row.get("rollback_triggered")) for row in items), len(items)),
                "verification_rescue_rate": (sum(truth(row.get("verification_rescued")) for row in items), len(items)),
                "unsafe_proposal_rate": (sum(truth(row.get("unsafe_proposal_before_verification")) for row in items), len(items)),
            })
        for metric, (num, den) in rate_metrics.items():
            estimate, low, high = wilson(num, den)
            out.append(_aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, num, den, raw_path, raw_sha, "wilson_95"))
        continuous = {
            "exp1": (("conditional_verified_latency_ms", "verified_formation_latency_s", 1000.0), ("route_install_latency_ms", "route_install_latency_s", 1000.0), ("control_messages", "control_messages", 1.0), ("rules_installed", "rules_installed", 1.0)),
            "exp2": (("coordination_latency_ms", "coordination_latency_ms", 1.0),),
            "exp3": (("conditional_verified_latency_ms", "reconfiguration_latency_ms", 1.0), ("modification_scope_ratio", "modification_scope_ratio", 1.0), ("unaffected_flow_disturbance", "unaffected_disturbance_ratio", 1.0)),
            "exp4": (("conditional_verified_latency_ms", "recovery_latency_ms", 1.0), ("modification_scope_ratio", "modification_scope_ratio", 1.0), ("unaffected_flow_interruption", "unaffected_disturbance_ratio", 1.0)),
        }[exp]
        for metric, field, factor in continuous:
            samples = [
                (int(row["seed"]), float(row[field]) * factor)
                for row in items if row.get(field, "") not in {"", None}
                and (not metric.startswith("conditional_") or truth(row.get("success")))
            ]
            estimate, low, high = cluster_bootstrap(samples)
            out.append(_aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, len(samples), len(items), raw_path, raw_sha, "topology_cluster_bootstrap_95"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(out[0]) if out else []
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(out)
    _write_paired_differences(exp, rows, output_path.with_name("paired_differences.csv"), raw_path, raw_sha)
    return out


def _write_paired_differences(exp: str, rows: list[dict[str, str]], path: Path, raw_path: Path, raw_sha: str) -> None:
    if exp == "exp1":
        rows = [
            row
            for row in rows
            if truth(row.get("paired_analysis_eligible", "true"))
        ]
    indexed: dict[tuple[str, float, str, str, str], dict[str, str]] = {}
    for row in rows:
        method = row.get("method_id") or row.get("method") or ""; x_name, x_value = _x(exp, row)
        series = row.get("failure_type", "") if exp == "exp4" else row.get("series", "")
        trial_identity = row.get("trial_id") or row.get("scenario_fingerprint") or ""
        # A scenario fingerprint identifies scenario content, not a unique
        # repeated trial.  Keep the topology seed in the pairing key so that
        # multiple seeds sharing identical content cannot overwrite each other.
        trial = f"{trial_identity}|seed={row['seed']}"
        indexed[(x_name, x_value, series, trial, method)] = row
    comparisons: dict[tuple[str, float, str, str, str], list[tuple[int, float]]] = defaultdict(list)
    metrics = [("success_rate", "success")]
    if exp == "exp2": metrics.append(("pre_verification_correct_decision_rate", "pre_verification_correct_decision"))
    continuous_metrics = (
        (
            ("conditional_verified_latency_ms", "verified_formation_latency_s", 1000.0, True),
            ("route_install_latency_ms", "route_install_latency_s", 1000.0, False),
        )
        if exp == "exp1"
        else ()
    )
    for (x_name, x_value, series, trial, method), row in indexed.items():
        if method == "proposed": continue
        reference = indexed.get((x_name, x_value, series, trial, "proposed"))
        if reference is None: continue
        for metric, field in metrics:
            comparisons[(x_name, x_value, series, method, metric)].append((int(row["seed"]), float(truth(row.get(field))) - float(truth(reference.get(field)))))
        for metric, field, factor, successful_runs_only in continuous_metrics:
            if successful_runs_only and not (
                truth(row.get("success")) and truth(reference.get("success"))
            ):
                continue
            if row.get(field, "") in {"", None} or reference.get(field, "") in {"", None}:
                continue
            difference = (
                float(row[field]) - float(reference[field])
            ) * factor
            comparisons[(x_name, x_value, series, method, metric)].append(
                (int(row["seed"]), difference)
            )
    output = []
    for (x_name, x_value, series, method, metric), values in sorted(comparisons.items()):
        estimate, low, high = cluster_bootstrap(values)
        output.append({"protocol_id": "wcnc_final_v3", "experiment": exp, "series": series,
            "method_id": method, "reference_method_id": "proposed", "x_name": x_name, "x_value": x_value,
            "metric": metric, "paired_difference": estimate, "ci_low": low, "ci_high": high,
            "paired_trials": len(values), "interval": "paired_topology_cluster_bootstrap_95",
            "raw_path": str(raw_path), "raw_sha256": raw_sha})
    if output:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(output[0]), lineterminator="\n"); writer.writeheader(); writer.writerows(output)


def _aggregate_row(exp, method, series, x_name, x_value, metric, estimate, low, high, numerator, denominator, raw_path, raw_sha, interval):
    return {"protocol_id": "wcnc_final_v3", "experiment": exp, "series": series,
        "method_id": method, "x_name": x_name, "x_value": x_value, "metric": metric,
        "estimate": estimate, "ci_low": low, "ci_high": high, "numerator": numerator,
        "denominator": denominator, "interval": interval, "raw_path": str(raw_path), "raw_sha256": raw_sha}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="results/paper/wcnc_final_v3"); parser.add_argument("--experiment", choices=("all", "exp1", "exp1_transactional", "exp2", "exp3", "exp4"), default="all")
    args = parser.parse_args(); root = Path(args.root)
    experiments = ("exp1", "exp1_transactional", "exp2", "exp3", "exp4") if args.experiment == "all" else (args.experiment,)
    for exp in experiments:
        aggregate_experiment(exp, root / "raw" / exp / "trials.csv", root / "aggregated" / exp / "metrics.csv")


if __name__ == "__main__": main()
