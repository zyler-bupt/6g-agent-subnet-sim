from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AuditCheck:
    name: str
    passed: bool
    details: Any


def audit_exp4(results_dir: Path, config_path: Path) -> dict[str, object]:
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    runs = _read_csv(raw / "runs.csv")
    events = _read_jsonl(raw / "events.jsonl")
    scenarios = _read_jsonl(raw / "scenarios.jsonl")
    failures = _read_csv(processed / "failures.csv") if (processed / "failures.csv").exists() else []
    pairwise = _read_csv(processed / "pairwise_comparison.csv") if (processed / "pairwise_comparison.csv").exists() else []
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    expected_seeds = int(config.get("simulation", {}).get("seeds", 30))
    main_methods = {"proposed", "full_rebuild", "network_only"}
    main = [row for row in runs if row["method"] in main_methods]
    checks: list[AuditCheck] = []

    groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in main:
        groups[(row["scenario"], int(row["seed"]))].append(row)
    checks.append(
        AuditCheck(
            "same scenario/seed contains all three main methods",
            bool(groups) and all({item["method"] for item in group} == main_methods for group in groups.values()),
            {"groups": len(groups)},
        )
    )
    scenario_count = 15
    checks.append(
        AuditCheck(
            "main design has 15 points x configured seeds x 3 methods",
            len(main) == scenario_count * expected_seeds * 3,
            {"actual": len(main), "expected": scenario_count * expected_seeds * 3},
        )
    )
    checks.append(
        AuditCheck(
            "every scenario point contains configured seed count",
            all(
                len({int(row["seed"]) for row in main if row["scenario"] == scenario}) == expected_seeds
                for scenario in {row["scenario"] for row in main}
            ),
            {"expected_seeds": expected_seeds},
        )
    )

    fairness_errors = []
    for key, group in groups.items():
        fields = (
            "scenario_fingerprint",
            "fault_fingerprint",
            "fault_target",
            "fault_severity",
            "fault_effective_at",
            "failure_detected_at",
        )
        for field in fields:
            if len({item[field] for item in group}) != 1:
                fairness_errors.append({"scenario_seed": key, "field": field})
    checks.append(AuditCheck("paired methods share scenario, target and timing", not fairness_errors, fairness_errors[:20]))

    event_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_by_run[str(event.get("experiment_run_id", ""))].append(event)
    timestamp_errors = []
    for run_id, items in event_by_run.items():
        timestamps = [float(item["timestamp"]) for item in items]
        if timestamps != sorted(timestamps):
            timestamp_errors.append(run_id)
    checks.append(AuditCheck("event logs are monotonic per run", not timestamp_errors, timestamp_errors[:20]))

    monitor_errors = []
    for key, group in groups.items():
        signatures = []
        for row in group:
            samples = [
                (
                    float(item["timestamp"]),
                    json.dumps(item.get("details", {}), sort_keys=True),
                )
                for item in event_by_run[row["run_id"]]
                if item.get("event_stage") == "MONITOR_SAMPLE"
            ]
            signatures.append(tuple(samples))
        if len(set(signatures)) != 1:
            monitor_errors.append(key)
    checks.append(AuditCheck("monitor samples and detection are method-independent", not monitor_errors, monitor_errors[:20]))

    executor_errors = [
        row["run_id"]
        for row in main
        if row["method"] in {"proposed", "full_rebuild"}
        and row["transaction_executor"] != "TransactionExecutor"
    ]
    checks.append(AuditCheck("Proposed and Full-Rebuild share TransactionExecutor", not executor_errors, executor_errors[:20]))
    network_layer_errors = [
        {"run_id": row["run_id"], "selected_layers": row["selected_layers"]}
        for row in main
        if row["method"] == "network_only"
        and any(layer and layer != "network" for layer in row["selected_layers"].split(";"))
    ]
    checks.append(AuditCheck("Network-Only authorizes only network actions", not network_layer_errors, network_layer_errors[:20]))

    successful_errors = [
        {
            "run_id": row["run_id"],
            "qos": row["qos_recovered"],
            "versions": row["stable_versions_consistent"],
            "staged": row["staged_rules_empty"],
            "residual": row["residual_rules"],
        }
        for row in runs
        if _bool(row["recovery_success"])
        and not (
            _bool(row["qos_recovered"])
            and _bool(row["stable_versions_consistent"])
            and _bool(row["staged_rules_empty"])
            and int(float(row["residual_rules"])) == 0
            and int(row["new_version"]) == int(row["old_version"]) + 1
        )
    ]
    checks.append(AuditCheck("successful recovery satisfies commit/QoS invariants", not successful_errors, successful_errors[:20]))

    rollback_errors = []
    for row in runs:
        if not _bool(row["rollback_triggered"]):
            continue
        snapshots = [
            item for item in event_by_run[row["run_id"]]
            if item.get("event_stage") == "TRANSACTION_SNAPSHOT"
        ]
        if not snapshots or snapshots[-1]["details"].get("before") != snapshots[-1]["details"].get("after"):
            rollback_errors.append(row["run_id"])
    checks.append(AuditCheck("rollback restores exact stable gateway snapshot", not rollback_errors, rollback_errors[:20]))

    invalid_success = [
        row["run_id"]
        for row in runs
        if _bool(row["recovery_success"]) and not _bool(row["post_execution_verification_success"])
    ]
    checks.append(AuditCheck("unresolved/QoS-failed state is never successful", not invalid_success, invalid_success[:20]))
    timing_fields = (
        "detection_latency_ms",
        "localization_latency_ms",
        "proposal_latency_ms",
        "coordination_latency_ms",
        "feasibility_latency_ms",
        "stage_latency_ms",
        "activation_latency_ms",
        "verification_latency_ms",
        "repair_latency_ms",
        "recovery_latency_ms",
    )
    negative = [
        {"run_id": row["run_id"], "field": field, "value": row[field]}
        for row in runs
        for field in timing_fields
        if float(row[field]) < 0.0
    ]
    checks.append(AuditCheck("all latency values are non-negative", not negative, negative[:20]))

    raw_failures = {row["run_id"] for row in runs if not _bool(row["success"])}
    processed_failures = {row["run_id"] for row in failures}
    checks.append(
        AuditCheck(
            "all failed samples are retained",
            not failures or raw_failures == processed_failures,
            {
                "missing": sorted(raw_failures - processed_failures)[:20],
                "extra": sorted(processed_failures - raw_failures)[:20],
            },
        )
    )
    checks.append(AuditCheck("paired statistical comparisons exist", bool(pairwise), {"rows": len(pairwise)}))
    checks.append(
        AuditCheck(
            "all results are labeled as control-plane simulation",
            all(row["result_mode"] == "in_memory_transactional_control_plane_simulation" for row in runs),
            {},
        )
    )
    source_text = (
        Path("experiments/exp4_failure_reconfiguration.py").read_text(encoding="utf-8")
        + Path("src/controller/failure_recovery.py").read_text(encoding="utf-8")
    )
    checks.append(
        AuditCheck(
            "CostModel is absent from recovery timing path",
            "CostModel" not in source_text and "cost_model" not in source_text,
            {},
        )
    )
    checks.append(
        AuditCheck(
            "scenario snapshots cover all raw fingerprints",
            {item.get("scenario_fingerprint") for item in scenarios}
            >= {row["scenario_fingerprint"] for row in runs},
            {"snapshots": len(scenarios)},
        )
    )

    passed = all(item.passed for item in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "result_mode": "in_memory_transactional_control_plane_simulation",
        "runs": len(runs),
        "main_runs": len(main),
        "checks_passed": sum(item.passed for item in checks),
        "checks_total": len(checks),
        "checks": [asdict(item) for item in checks],
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit experiment 4 integrity")
    parser.add_argument("--results-dir", default="results/exp4")
    parser.add_argument("--config", default="configs/exp4_failure_reconfiguration.yaml")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = audit_exp4(Path(args.results_dir), Path(args.config))
    output = Path(args.results_dir) / "processed" / "experiment_integrity_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report["status"])
    for item in report["checks"]:
        print(f"{'PASS' if item['passed'] else 'FAIL'} {item['name']}")
    print(f"Wrote {output.resolve()}")
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
