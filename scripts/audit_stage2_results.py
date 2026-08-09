from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AuditFailure:
    check: str
    detail: str
    task_id: str = ""
    gateway_id: str = ""
    rule_id: str = ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit stage-2 transaction artifacts.")
    parser.add_argument("--results-dir", default="results/stage2")
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"{path}:{line_number}: {error}") from error
    return records


def _load_metrics(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def _failure(
    failures: list[AuditFailure],
    check: str,
    detail: str,
    *,
    task_id: str = "",
    gateway_id: str = "",
    rule_id: str = "",
) -> None:
    failures.append(
        AuditFailure(
            check=check,
            detail=detail,
            task_id=task_id,
            gateway_id=gateway_id,
            rule_id=rule_id,
        )
    )


def audit(results_dir: Path) -> list[AuditFailure]:
    required = {
        "success_events": results_dir / "agent_remove_success_events.jsonl",
        "failure_events": results_dir / "agent_remove_stage_failure_events.jsonl",
        "success_snapshot": results_dir / "agent_remove_success_rule_snapshots.json",
        "failure_snapshot": results_dir / "agent_remove_stage_failure_rule_snapshots.json",
        "metrics": results_dir / "agent_removal_metrics.csv",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        return [AuditFailure("input", f"missing required file: {path}") for path in missing]

    success_events = _load_jsonl(required["success_events"])
    failure_events = _load_jsonl(required["failure_events"])
    success_snapshot = _load_json(required["success_snapshot"])
    failure_snapshot = _load_json(required["failure_snapshot"])
    metrics = _load_metrics(required["metrics"])
    failures: list[AuditFailure] = []

    # 1. Event timestamps are monotonic within each event log.
    for label, records in (
        ("success", success_events),
        ("stage_failure", failure_events),
    ):
        timestamps = [float(record["timestamp"]) for record in records]
        for index, (left, right) in enumerate(zip(timestamps, timestamps[1:]), start=1):
            if right < left:
                record = records[index]
                _failure(
                    failures,
                    "1.timestamps_monotonic",
                    f"{label} timestamp decreased at {record.get('event_stage')}: {right} < {left}",
                    task_id=str(record.get("task_id", "")),
                )

    success_rows = [row for row in metrics if _as_bool(row.get("success", ""))]
    failure_rows = [
        row
        for row in metrics
        if not _as_bool(row.get("success", ""))
        and _as_bool(row.get("rollback_triggered", ""))
    ]
    if len(success_rows) != 1:
        _failure(failures, "2.success_versions", f"expected one success row, found {len(success_rows)}")
        success_row: dict[str, str] = {}
    else:
        success_row = success_rows[0]
        if success_row.get("old_version") != "1" or success_row.get("new_version") != "2":
            _failure(
                failures,
                "2.success_versions",
                f"expected 1->2, got {success_row.get('old_version')}->{success_row.get('new_version')}",
                task_id=success_row.get("task_id", ""),
            )

    task_id = str(success_snapshot.get("event", {}).get("task_id", ""))
    removed_agent = str(
        success_snapshot.get("event", {}).get("payload", {}).get("agent_id", "")
    )

    # 3-4. Success store versions and staged cleanup.
    after = success_snapshot.get("after", {})
    for gateway_id, gateway in after.items():
        if gateway.get("stable_version") != 2:
            _failure(
                failures,
                "3.success_stable_version",
                f"stable_version={gateway.get('stable_version')}, expected 2",
                task_id=task_id,
                gateway_id=gateway_id,
            )
        if gateway.get("staged_version") is not None or gateway.get("staged_rules"):
            _failure(
                failures,
                "4.success_staged_empty",
                "staged version or rules remain after activation",
                task_id=task_id,
                gateway_id=gateway_id,
            )

    # 5. Residual-rule metric.
    if success_row and int(success_row.get("residual_rules", "-1")) != 0:
        _failure(
            failures,
            "5.success_residual_rules",
            f"residual_rules={success_row.get('residual_rules')}",
            task_id=success_row.get("task_id", ""),
        )

    # 6. Removed objects must be absent from the committed task-state snapshot.
    state_after = success_snapshot.get("task_state_after")
    if not isinstance(state_after, dict):
        _failure(
            failures,
            "6.removed_objects_absent",
            "task_state_after evidence is missing",
            task_id=task_id,
        )
    else:
        _audit_removed_objects(
            failures,
            task_id=task_id,
            removed_agent=removed_agent,
            scope=success_snapshot.get("impact_scope", {}),
            state_after=state_after,
            gateway_after=after,
        )

    # 7-9. Failure rollback store state and exact stable snapshot restoration.
    failure_task = str(failure_snapshot.get("event", {}).get("task_id", ""))
    rollback_after = failure_snapshot.get("after_rollback", {})
    for gateway_id, gateway in rollback_after.items():
        if gateway.get("stable_version") != 1:
            _failure(
                failures,
                "7.failure_stable_version",
                f"stable_version={gateway.get('stable_version')}, expected 1",
                task_id=failure_task,
                gateway_id=gateway_id,
            )
        if gateway.get("staged_version") is not None or gateway.get("staged_rules"):
            _failure(
                failures,
                "8.failure_staged_empty",
                "staged version or rules remain after rollback",
                task_id=failure_task,
                gateway_id=gateway_id,
            )
    if failure_snapshot.get("before") != rollback_after:
        _failure(
            failures,
            "9.rollback_snapshot_equal",
            "stable rule snapshots differ before and after rollback",
            task_id=failure_task,
        )
    if len(failure_rows) != 1 or not _as_bool(failure_rows[0].get("rollback_success", "")):
        _failure(
            failures,
            "9.rollback_snapshot_equal",
            "metrics do not contain exactly one successful rollback",
            task_id=failure_task,
        )

    # 10. Every unaffected edge remains in state and has an observed reachable result.
    unaffected = set(success_snapshot.get("impact_scope", {}).get("unaffected_business_edges", []))
    if not isinstance(state_after, dict):
        _failure(
            failures,
            "10.unaffected_edges_reachable",
            "task_state_after reachability evidence is missing",
            task_id=task_id,
        )
    else:
        edge_ids = set(state_after.get("business_edges", {}))
        reachability = state_after.get("edge_reachability", {})
        for edge_id in sorted(unaffected):
            if edge_id not in edge_ids:
                _failure(
                    failures,
                    "10.unaffected_edges_reachable",
                    "unaffected business edge is absent",
                    task_id=task_id,
                    rule_id=edge_id,
                )
            if reachability.get(edge_id) is not True:
                _failure(
                    failures,
                    "10.unaffected_edges_reachable",
                    f"reachability={reachability.get(edge_id)!r}",
                    task_id=task_id,
                    rule_id=edge_id,
                )
    return failures


def _audit_removed_objects(
    failures: list[AuditFailure],
    *,
    task_id: str,
    removed_agent: str,
    scope: dict[str, Any],
    state_after: dict[str, Any],
    gateway_after: dict[str, Any],
) -> None:
    if removed_agent in state_after.get("business_agents", []):
        _failure(
            failures,
            "6.removed_objects_absent",
            "removed Agent remains in business_agents",
            task_id=task_id,
            rule_id=removed_agent,
        )

    affected_edges = set(scope.get("affected_business_edges", []))
    for edge_id, edge in state_after.get("business_edges", {}).items():
        if edge_id in affected_edges or removed_agent in {edge.get("source"), edge.get("target")}:
            _failure(
                failures,
                "6.removed_objects_absent",
                "removed business edge remains",
                task_id=task_id,
                rule_id=edge_id,
            )

    affected_sessions = set(scope.get("affected_sessions", []))
    for session_id, session in state_after.get("sessions", {}).items():
        if session_id in affected_sessions or removed_agent in {
            session.get("source"),
            session.get("target"),
        }:
            _failure(
                failures,
                "6.removed_objects_absent",
                "removed Session remains",
                task_id=task_id,
                rule_id=session_id,
            )

    affected_routes = set(scope.get("affected_routes", []))
    for route_id in affected_routes & set(state_after.get("routes", {})):
        _failure(
            failures,
            "6.removed_objects_absent",
            "removed Route remains",
            task_id=task_id,
            rule_id=route_id,
        )

    affected_resources = set(scope.get("affected_physical_resources", []))
    for binding_id in affected_resources & set(state_after.get("physical_bindings", {})):
        _failure(
            failures,
            "6.removed_objects_absent",
            "removed physical binding remains",
            task_id=task_id,
            rule_id=binding_id,
        )
    actual_resources = {
        binding_id
        for bindings in state_after.get("physical_agent_resources", {}).values()
        for binding_id in bindings
    }
    for binding_id in affected_resources & actual_resources:
        _failure(
            failures,
            "6.removed_objects_absent",
            "removed physical resource remains in pAgent state",
            task_id=task_id,
            rule_id=binding_id,
        )

    affected_rules = set(scope.get("affected_rules", []))
    for gateway_id, gateway in gateway_after.items():
        for rule in gateway.get("stable_rules", []):
            rule_id = str(rule.get("rule_id", ""))
            if (
                rule_id in affected_rules
                or rule.get("src_agent") == removed_agent
                or rule.get("dst_agent") == removed_agent
            ):
                _failure(
                    failures,
                    "6.removed_objects_absent",
                    "removed Agent rule remains",
                    task_id=task_id,
                    gateway_id=gateway_id,
                    rule_id=rule_id,
                )


def _format_failure(failure: AuditFailure) -> str:
    context = []
    if failure.task_id:
        context.append(f"task_id={failure.task_id}")
    if failure.gateway_id:
        context.append(f"gateway_id={failure.gateway_id}")
    if failure.rule_id:
        context.append(f"rule_id={failure.rule_id}")
    suffix = f" ({', '.join(context)})" if context else ""
    return f"- [{failure.check}] {failure.detail}{suffix}"


def main() -> None:
    args = _parser().parse_args()
    try:
        failures = audit(Path(args.results_dir))
    except (OSError, ValueError, KeyError) as error:
        failures = [AuditFailure("input", str(error))]
    if failures:
        print("FAIL")
        print("失败检查项：")
        for failure in failures:
            print(_format_failure(failure))
        raise SystemExit(1)
    print("PASS")
    print("10/10 stage-2 artifact checks passed")


if __name__ == "__main__":
    main()
