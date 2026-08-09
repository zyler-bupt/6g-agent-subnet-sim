from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

try:
    from scripts.aggregate_exp3 import (
        METRICS,
        ROLLBACK_ONLY,
        SUCCESS_ONLY,
        summarize,
    )
    from scripts.plot_exp3 import (
        _breakdown_rows,
        _metric_rows,
        _success_residual_rows,
    )
except ModuleNotFoundError:  # direct ``python3 scripts/audit_exp3_results.py``
    from aggregate_exp3 import METRICS, ROLLBACK_ONLY, SUCCESS_ONLY, summarize
    from plot_exp3 import _breakdown_rows, _metric_rows, _success_residual_rows
from src.simulation.scenario_generator import ElasticScenarioGenerator, ScenarioConfig


class Audit:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def check(
        self,
        name: str,
        condition: bool,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.results.append(
            {
                "check": name,
                "passed": bool(condition),
                "details": list(details or []),
            }
        )

    @property
    def passed(self) -> bool:
        return all(item["passed"] for item in self.results)


def audit_exp3(results_dir: Path) -> dict[str, Any]:
    raw = results_dir / "raw"
    processed = results_dir / "processed"
    figures = results_dir / "figures"
    required = (
        raw / "runs.csv",
        raw / "events.jsonl",
        raw / "probe_samples.csv",
        processed / "summary.csv",
        processed / "failures.csv",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("missing Exp3 artifact(s): " + ", ".join(missing))

    runs = _read_csv(raw / "runs.csv")
    summary = _read_csv(processed / "summary.csv")
    failures = _read_csv(processed / "failures.csv")
    events = _read_jsonl(raw / "events.jsonl")
    probes = _read_csv(raw / "probe_samples.csv")
    audit = Audit()

    point_seeds: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in runs:
        point_seeds[(row["scenario"], row["method"])].add(int(row["seed"]))
    bad_points = [
        {"scenario": scenario, "method": method, "seeds": sorted(seeds)}
        for (scenario, method), seeds in point_seeds.items()
        if seeds != set(range(30))
    ]
    audit.check("30 seeds per experiment point", not bad_points, bad_points)

    methods_by_pair: dict[tuple[str, int], set[str]] = defaultdict(set)
    fingerprints_by_pair: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in runs:
        key = (row["scenario"], int(row["seed"]))
        methods_by_pair[key].add(row["method"])
        fingerprints_by_pair[key].add(row["scenario_fingerprint"])
    expected_methods = {"proposed", "full_rebuild", "local_only"}
    bad_method_pairs = [
        {"scenario": key[0], "seed": key[1], "methods": sorted(methods)}
        for key, methods in methods_by_pair.items()
        if methods != expected_methods
    ]
    audit.check("all methods present for each scenario/seed", not bad_method_pairs, bad_method_pairs)
    bad_fingerprints = [
        {"scenario": key[0], "seed": key[1], "fingerprints": sorted(values)}
        for key, values in fingerprints_by_pair.items()
        if len(values) != 1
    ]
    audit.check("scenario fingerprints agree", not bad_fingerprints, bad_fingerprints)

    counts = defaultdict(int)
    for row in runs:
        counts[row["method"]] += 1
    count_ok = counts["proposed"] == 630 and counts["full_rebuild"] == 630
    audit.check(
        "Proposed and Full-Rebuild each have 630 runs",
        count_ok,
        [] if count_ok else [dict(counts)],
    )

    residual_failures = [
        {
            "run_id": row["run_id"],
            "method": row["method"],
            "residual_rules": row["residual_rules"],
        }
        for row in runs
        if _bool(row["success"]) and int(float(row["residual_rules"])) != 0
    ]
    audit.check("successful runs have zero residual rules", not residual_failures, residual_failures)

    raw_failed = {row["run_id"] for row in runs if not _bool(row["success"])}
    saved_failed = {row["run_id"] for row in failures}
    failure_details = []
    if raw_failed != saved_failed:
        failure_details.append(
            {
                "missing_from_failures": sorted(raw_failed - saved_failed),
                "extra_in_failures": sorted(saved_failed - raw_failed),
            }
        )
    audit.check("all failed runs are preserved", raw_failed == saved_failed, failure_details)

    local_failure_errors = [
        {"run_id": row["run_id"], "failure_reason": row["failure_reason"]}
        for row in runs
        if row["method"] == "local_only"
        and not _bool(row["success"])
        and "verification failed" not in row["failure_reason"]
    ]
    audit.check(
        "Local-Only failures originate from shared verifier",
        not local_failure_errors,
        local_failure_errors,
    )

    separation_errors = _check_success_rollback_separation(runs, summary)
    audit.check(
        "success and rollback latency statistics are separated",
        not separation_errors,
        separation_errors,
    )

    statistics_errors = _check_summary_statistics(runs, summary)
    audit.check(
        "confidence intervals are recomputed from raw samples",
        not statistics_errors,
        statistics_errors,
    )

    chart_errors = _check_chart_data(summary, figures)
    audit.check("figure CSV data matches processed summary", not chart_errors, chart_errors)

    timestamp_errors = _check_timestamps_and_stages(runs, events)
    audit.check(
        "latencies/stages/timestamps are valid and monotonic",
        not timestamp_errors,
        timestamp_errors,
    )

    isolation_errors = _check_state_isolation()
    audit.check(
        "methods instantiate independent mutable state",
        not isolation_errors,
        isolation_errors,
    )

    pairwise = build_pairwise_comparison(runs)
    _write_csv(processed / "pairwise_comparison.csv", pairwise)
    report = {
        "passed": audit.passed,
        "checks": audit.results,
        "counts": {
            "runs": len(runs),
            "events": len(events),
            "probe_samples": len(probes),
            "failed_runs": len(raw_failed),
            "pairwise_rows": len(pairwise),
            "methods": dict(sorted(counts.items())),
        },
        "artifacts": {
            "pairwise_comparison": str(processed / "pairwise_comparison.csv"),
            "integrity_report": str(processed / "experiment_integrity_report.json"),
        },
    }
    report_path = processed / "experiment_integrity_report.json"
    with report_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return report


def build_pairwise_comparison(runs: list[dict[str, str]]) -> list[dict[str, Any]]:
    indexed = {
        (row["scenario"], int(row["seed"]), row["method"]): row for row in runs
    }
    rows: list[dict[str, Any]] = []
    for scenario, seed in sorted({(row["scenario"], int(row["seed"])) for row in runs}):
        proposed = indexed.get((scenario, seed, "proposed"))
        rebuilt = indexed.get((scenario, seed, "full_rebuild"))
        if proposed is None or rebuilt is None:
            continue
        proposed_changed = sum(
            float(proposed[field]) for field in ("added_rules", "updated_rules", "deleted_rules")
        )
        rebuilt_changed = sum(
            float(rebuilt[field]) for field in ("added_rules", "updated_rules", "deleted_rules")
        )
        rows.append(
            {
                "scenario": scenario,
                "seed": seed,
                "scenario_fingerprint": proposed["scenario_fingerprint"],
                "proposed_success": proposed["success"],
                "full_rebuild_success": rebuilt["success"],
                "proposed_minus_full_rebuild_latency": (
                    float(proposed["elastic_latency_ms"])
                    - float(rebuilt["elastic_latency_ms"])
                    if _bool(proposed["success"]) and _bool(rebuilt["success"])
                    else ""
                ),
                "proposed_rule_reduction": rebuilt_changed - proposed_changed,
                "proposed_gateway_reduction": (
                    float(rebuilt["affected_gateways"])
                    - float(proposed["affected_gateways"])
                ),
                "proposed_unaffected_interruption_reduction": (
                    float(rebuilt["unaffected_interruption_ms"])
                    - float(proposed["unaffected_interruption_ms"])
                ),
            }
        )
    return rows


def _check_success_rollback_separation(
    runs: list[dict[str, str]],
    summary: list[dict[str, str]],
) -> list[dict[str, Any]]:
    errors = []
    indexed = {(row["scenario"], row["method"]): row for row in summary}
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in runs:
        groups[(row["scenario"], row["method"])].append(row)
        if _bool(row["success"]) and float(row["failure_handling_ms"]) != 0.0:
            errors.append({"run_id": row["run_id"], "reason": "success_has_failure_latency"})
        if not _bool(row["success"]) and float(row["elastic_latency_ms"]) != 0.0:
            errors.append({"run_id": row["run_id"], "reason": "failure_has_elastic_latency"})
    for key, group in groups.items():
        item = indexed[key]
        successful = sum(_bool(row["success"]) for row in group)
        rollbacks = sum(_bool(row["rollback_triggered"]) for row in group)
        if int(item["elastic_latency_ms_count"]) != successful:
            errors.append({"scenario": key[0], "method": key[1], "reason": "elastic_count"})
        if int(item["failure_handling_ms_count"]) != rollbacks:
            errors.append({"scenario": key[0], "method": key[1], "reason": "rollback_count"})
    return errors


def _check_summary_statistics(
    runs: list[dict[str, str]],
    summary: list[dict[str, str]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in runs:
        groups[(row["scenario"], row["method"])].append(row)
    errors = []
    for item in summary:
        key = (item["scenario"], item["method"])
        group = groups[key]
        successful = [row for row in group if _bool(row["success"])]
        for metric in METRICS:
            if metric in SUCCESS_ONLY:
                source = successful
            elif metric in ROLLBACK_ONLY:
                source = [row for row in group if _bool(row["rollback_triggered"])]
            else:
                source = group
            expected = summarize(float(row[metric]) for row in source)
            for statistic in ("count", "mean", "ci95"):
                observed = item[f"{metric}_{statistic}"]
                value = expected[statistic]
                if value == "":
                    equal = observed == ""
                elif statistic == "count":
                    equal = int(observed) == int(value)
                else:
                    equal = math.isclose(float(observed), float(value), rel_tol=1e-9, abs_tol=1e-9)
                if not equal:
                    errors.append(
                        {
                            "scenario": key[0],
                            "method": key[1],
                            "field": f"{metric}_{statistic}",
                            "observed": observed,
                            "expected": value,
                        }
                    )
                    if len(errors) >= 50:
                        return errors
    return errors


def _check_chart_data(
    summary: list[dict[str, str]],
    figures: Path,
) -> list[dict[str, Any]]:
    definitions: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "fig_elastic_latency_vs_agent_count.csv": lambda: _metric_rows(
            summary, "agent_count", "elastic_latency_ms"
        ),
        "fig_elastic_latency_vs_removal_ratio.csv": lambda: _metric_rows(
            summary, "removal_ratio", "elastic_latency_ms"
        ),
        "fig_elastic_latency_vs_gateway_count.csv": lambda: _metric_rows(
            summary, "gateway_count", "elastic_latency_ms"
        ),
        "fig_changed_rules_ratio.csv": lambda: _metric_rows(
            summary, "removal_ratio", "changed_rules_ratio"
        ),
        "fig_affected_gateways.csv": lambda: _metric_rows(
            summary, "removal_ratio", "affected_gateways"
        ),
        "fig_unaffected_service_interruption.csv": lambda: _metric_rows(
            summary, "removal_ratio", "unaffected_interruption_ms"
        ),
        "fig_agent_removal_position.csv": lambda: _metric_rows(
            summary, "removal_position", "elastic_latency_ms"
        ),
        "fig_elastic_latency_breakdown.csv": lambda: _breakdown_rows(summary),
        "fig_success_and_residual_rule_rate.csv": lambda: _success_residual_rows(summary),
    }
    errors = []
    for filename, build_expected in definitions.items():
        path = figures / filename
        if not path.exists():
            errors.append({"file": filename, "reason": "missing"})
            continue
        actual = _read_csv(path)
        expected = build_expected()
        if not _rows_equivalent(actual, expected):
            errors.append(
                {
                    "file": filename,
                    "reason": "content_mismatch",
                    "actual_rows": len(actual),
                    "expected_rows": len(expected),
                }
            )
    return errors


def _check_timestamps_and_stages(
    runs: list[dict[str, str]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    errors = []
    latency_fields = [
        field
        for field in runs[0]
        if field.endswith("_latency_ms") or field.endswith("_handling_ms")
    ]
    for row in runs:
        negative = [field for field in latency_fields if float(row[field]) < 0.0]
        if negative:
            errors.append({"run_id": row["run_id"], "negative_fields": negative})
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[(int(event["run_id"]), event["event_id"])].append(event)
    run_by_index = {index: row for index, row in enumerate(runs, start=1)}
    success_required = {
        "EVENT_OCCURRED",
        "EVENT_RECEIVED",
        "SCOPE_STARTED",
        "SCOPE_FINISHED",
        "DELTA_COMPILED",
        "STAGE_STARTED",
        "STAGE_FINISHED",
        "VERIFY_STARTED",
        "VERIFY_FINISHED",
        "ACTIVATE_STARTED",
        "ACTIVATE_FINISHED",
        "POST_ACTIVATE_VERIFY_STARTED",
        "POST_ACTIVATE_VERIFY_FINISHED",
        "TRANSACTION_COMMITTED",
    }
    rollback_required = {
        "EVENT_OCCURRED",
        "EVENT_RECEIVED",
        "STAGE_STARTED",
        "ROLLBACK_STARTED",
        "ROLLBACK_FINISHED",
    }
    for (run_index, event_id), records in grouped.items():
        timestamps = [float(record["timestamp"]) for record in records]
        if timestamps != sorted(timestamps):
            errors.append({"run_index": run_index, "event_id": event_id, "reason": "timestamp_order"})
        stages = {record["event_stage"] for record in records}
        run = run_by_index.get(run_index)
        if run is None:
            errors.append({"run_index": run_index, "reason": "event_without_run"})
            continue
        required = success_required if _bool(run["success"]) else rollback_required
        missing = sorted(required - stages)
        if missing:
            errors.append({"run_id": run["run_id"], "missing_stages": missing})
    if len(grouped) != len(runs):
        errors.append(
            {"reason": "event_run_count", "event_groups": len(grouped), "runs": len(runs)}
        )
    return errors[:100]


def _check_state_isolation() -> list[dict[str, Any]]:
    snapshot = ElasticScenarioGenerator().generate(ScenarioConfig(num_agents=10), 17)
    first, _first_verifier, first_provider = snapshot.instantiate()
    second, _second_verifier, second_provider = snapshot.instantiate()
    errors = []
    if first is second or first_provider is second_provider:
        errors.append({"reason": "controller_or_provider_reused"})
    for gateway_id in first.gateways:
        if first.gateways[gateway_id] is second.gateways[gateway_id]:
            errors.append({"gateway_id": gateway_id, "reason": "gateway_reused"})
        for agent_id in first.gateways[gateway_id].agents:
            if (
                first.gateways[gateway_id].agents[agent_id]
                is second.gateways[gateway_id].agents[agent_id]
            ):
                errors.append({"gateway_id": gateway_id, "agent_id": agent_id, "reason": "agent_reused"})
    first.gateways[next(iter(first.gateways))].online = False
    if not second.gateways[next(iter(second.gateways))].online:
        errors.append({"reason": "mutation_leaked_between_instances"})
    return errors


def _rows_equivalent(
    actual: list[dict[str, str]],
    expected: list[dict[str, Any]],
) -> bool:
    if len(actual) != len(expected):
        return False
    def normalized(row: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        values = []
        for key, value in sorted(row.items()):
            try:
                numeric = float(value)
                values.append((key, round(numeric, 10)))
            except (TypeError, ValueError):
                values.append((key, str(value)))
        return tuple(values)
    return sorted(normalized(row) for row in actual) == sorted(
        normalized(row) for row in expected
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("pairwise comparison is empty")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Stage-3 experiment artifacts")
    parser.add_argument("--results-dir", default="results/exp3")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = audit_exp3(Path(args.results_dir))
    print("PASS" if report["passed"] else "FAIL")
    for item in report["checks"]:
        label = "PASS" if item["passed"] else "FAIL"
        print(f"[{label}] {item['check']}")
        for detail in item["details"][:10]:
            print("  " + json.dumps(detail, ensure_ascii=False, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
