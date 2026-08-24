from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path


METHODS = {"proposed", "full_rebuild", "cspf"}
ALLOWED_CSPF_INPUTS = {
    "topology",
    "link_up",
    "available_bandwidth",
    "link_delay",
    "te_cost",
    "flow_source",
    "flow_destination",
    "required_bandwidth",
    "maximum_delay",
}


def audit(
    input_path: Path,
    output_dir: Path,
    expected_seeds: int = 30,
    *,
    seed_start: int = 0,
) -> dict[str, object]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    empty_input = not rows
    expected_seed_values = set(range(seed_start, seed_start + expected_seeds))
    run_key_counts = Counter(
        (row["scenario"], int(row["seed"]), row["method"])
        for row in rows
    )
    duplicate_run_errors = [
        {
            "scenario": scenario,
            "seed": seed,
            "method": method,
            "rows": count,
        }
        for (scenario, seed, method), count in sorted(run_key_counts.items())
        if count != 1
    ]
    pairs: dict[tuple[str, int], list[dict[str, str]]] = {}
    for row in rows:
        pairs.setdefault((row["scenario"], int(row["seed"])), []).append(row)
    pairing_errors = []
    for key, group in sorted(pairs.items()):
        methods = {row["method"] for row in group}
        scenario_fingerprints = {row["scenario_fingerprint"] for row in group}
        fault_fingerprints = {row["fault_fingerprint"] for row in group}
        effective_times = {row["fault_effective_at"] for row in group}
        detection_times = {row["failure_detected_at"] for row in group}
        if (
            methods != METHODS
            or len(scenario_fingerprints) != 1
            or len(fault_fingerprints) != 1
            or len(effective_times) != 1
            or len(detection_times) != 1
        ):
            pairing_errors.append(
                {
                    "scenario": key[0],
                    "seed": key[1],
                    "methods": sorted(methods),
                    "scenario_fingerprints": sorted(scenario_fingerprints),
                    "fault_fingerprints": sorted(fault_fingerprints),
                    "fault_effective_at": sorted(effective_times),
                    "failure_detected_at": sorted(detection_times),
                }
            )
    cspf_rows = [row for row in rows if row["method"] == "cspf"]
    cspf_layer_errors = [
        row["run_id"]
        for row in cspf_rows
        if row["selected_layers"] not in {"", "network"}
        or int(row["changed_physical_bindings"]) != 0
    ]
    invariant_errors = [
        row["run_id"]
        for row in rows
        if not _bool(row["stable_versions_consistent"])
        or not _bool(row["staged_rules_empty"])
        or int(row["residual_rules"]) != 0
        or (_bool(row["recovery_success"]) and not _bool(row["qos_recovered"]))
    ]
    runner_errors = [
        row["run_id"]
        for row in rows
        if str(row.get("failure_reason", "")).startswith("runner_error:")
    ]
    seed_sets: dict[tuple[str, str], set[int]] = {}
    for row in rows:
        seed_sets.setdefault((row["scenario"], row["method"]), set()).add(
            int(row["seed"])
        )
    seed_set_errors = [
        {
            "scenario": scenario,
            "method": method,
            "observed": sorted(seeds),
            "expected": sorted(expected_seed_values),
        }
        for (scenario, method), seeds in sorted(seed_sets.items())
        if seeds != expected_seed_values
    ]
    cspf_proposal_errors = _audit_cspf_proposals(
        input_path.parent / "proposals.jsonl"
    )
    physical_rows = [
        row for row in rows if row["fault_type"] == "PHYSICAL_CAPACITY_DROP"
    ]
    physical_failure_semantic_errors = [
        row["run_id"]
        for row in physical_rows
        if not _bool(row["fault_was_disruptive"])
        or not _bool(row["requirement_violated_before_recovery"])
        or float(row["pre_recovery_requirement_mbps"])
        <= float(row["post_failure_capacity_mbps"]) + 1e-9
        or not math.isclose(
            float(row["pre_recovery_violation_margin_mbps"]),
            float(row["pre_recovery_requirement_mbps"])
            - float(row["post_failure_capacity_mbps"]),
            abs_tol=1e-9,
        )
    ]
    scenario_counts: dict[tuple[str, str], int] = {}
    for row in rows:
        scenario_counts[(row["scenario"], row["method"])] = scenario_counts.get((row["scenario"], row["method"]), 0) + 1
    count_errors = [
        {"scenario": scenario, "method": method, "samples": scenario_counts.get((scenario, method), 0)}
        for scenario in sorted({row["scenario"] for row in rows})
        for method in sorted(METHODS)
        if scenario_counts.get((scenario, method), 0) != expected_seeds
    ]
    integrity = {
        "pass": not empty_input
        and not pairing_errors
        and not duplicate_run_errors
        and not seed_set_errors
        and not cspf_layer_errors
        and not cspf_proposal_errors
        and not invariant_errors
        and not runner_errors
        and not count_errors
        and not physical_failure_semantic_errors,
        "empty_input": empty_input,
        "raw_runs": len(rows),
        "paired_scenario_seed_groups": len(pairs),
        "expected_seeds_per_scenario": expected_seeds,
        "pairing_errors": pairing_errors,
        "duplicate_run_errors": duplicate_run_errors,
        "seed_set_errors": seed_set_errors,
        "cspf_network_only_errors": cspf_layer_errors,
        "cspf_proposal_errors": cspf_proposal_errors,
        "transaction_invariant_errors": invariant_errors,
        "runner_errors": runner_errors,
        "physical_failure_rows": len(physical_rows),
        "physical_failure_semantic_errors": physical_failure_semantic_errors,
        "physical_failure_definition": (
            "C_p_after = configured_ratio * R_req, with every configured ratio < 1"
        ),
        "count_errors": count_errors,
    }
    summary = _summary(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "summary.csv", summary)
    with (output_dir / "experiment_integrity_report.json").open("w", encoding="utf-8") as handle:
        json.dump(integrity, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return integrity


def _audit_cspf_proposals(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return [{"reason": "missing_proposals_jsonl", "path": str(path)}]
    errors: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("method") != "cspf":
                continue
            proposal = row.get("proposal", {})
            parameters = proposal.get("parameters", {})
            allowed = set(parameters.get("allowed_inputs", ()))
            forbidden_keys = sorted(
                key
                for key in parameters
                if any(
                    token in str(key).lower()
                    for token in (
                        "application",
                        "transport",
                        "physical",
                        "replacement",
                        "adaptation",
                        "reallocation",
                    )
                )
            )
            reasons = []
            if proposal.get("layer") != "network":
                reasons.append("non_network_layer")
            if proposal.get("action") != "SWITCH_ROUTE":
                reasons.append("non_routing_action")
            if allowed != ALLOWED_CSPF_INPUTS:
                reasons.append("allowed_inputs_mismatch")
            if forbidden_keys:
                reasons.append("forbidden_parameter_keys")
            if reasons:
                errors.append(
                    {
                        "line": line_number,
                        "run_id": row.get("run_id", ""),
                        "reasons": reasons,
                        "forbidden_parameter_keys": forbidden_keys,
                        "allowed_inputs": sorted(allowed),
                    }
                )
    return errors


def _summary(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        fault_family = (
            "LINK_FAILURE"
            if row["fault_type"] in {"LINK_DEGRADATION", "LINK_FAILURE"}
            else row["fault_type"]
        )
        groups.setdefault((fault_family, row["method"]), []).append(row)
    output = []
    for (fault_type, method), group in sorted(groups.items()):
        success = [float(_bool(row["recovery_success"])) for row in group]
        change = [float(row["rule_change_ratio"]) for row in group]
        output.append(
            {
                "fault_type": fault_type,
                "method": method,
                "samples": len(group),
                **_stats(success, "recovery_success_rate"),
                **_stats(change, "rule_change_ratio"),
            }
        )
    return output


def _stats(values: list[float], prefix: str) -> dict[str, float]:
    mean = statistics.fmean(values) if values else 0.0
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        f"{prefix}_mean": mean,
        f"{prefix}_ci95": 1.96 * standard_deviation / math.sqrt(len(values)) if values else 0.0,
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit paired Exp4 CSPF results")
    parser.add_argument("--input", default="results/exp4_cspf_final/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp4_cspf_final/processed")
    parser.add_argument("--expected-seeds", type=int, default=30)
    parser.add_argument("--seed-start", type=int, default=0)
    args = parser.parse_args()
    report = audit(
        Path(args.input),
        Path(args.output_dir),
        args.expected_seeds,
        seed_start=args.seed_start,
    )
    print("PASS" if report["pass"] else "FAIL", json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
