from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class PaperTrial:
    experiment: str
    mode: str
    trial_id: str
    seed: int
    event_id: int
    method_id: str
    method_label: str
    method_source: str
    adapted: bool
    topology_fingerprint: str
    scenario_fingerprint: str
    qos_fingerprint: str
    event_fingerprint: str
    series: str | None = None
    result_mode: str | None = None
    task_received_at: float | None = None
    event_occurred_at: float | None = None
    stable_verify_finished_at: float | None = None
    task_size: int | None = None
    num_dag_edges: int | None = None
    num_gateways: int | None = None
    state_churn_probability: float | None = None
    conflict_density: float | None = None
    conflict_type: str | None = None
    ground_truth_feasible: bool | None = None
    business_change_type: str | None = None
    affected_scope_ratio: float | None = None
    affected_scope_bucket_percent: float | None = None
    failure_type: str | None = None
    failure_severity: float | None = None
    controller_processing_latency_ms: float | None = None
    formation_latency_ms: float | None = None
    resolution_latency_ms: float | None = None
    reconfiguration_latency_ms: float | None = None
    recovery_latency_ms: float | None = None
    success: bool | None = None
    qos_satisfied: bool | None = None
    safe_rejection: bool | None = None
    failure_reason: str | None = None
    total_rules: int | None = None
    total_rule_objects: int | None = None
    changed_rules: int | None = None
    rule_change_ratio: float | None = None
    total_paths: int | None = None
    changed_paths: int | None = None
    total_agents: int | None = None
    changed_agents: int | None = None
    modification_scope_ratio: float | None = None
    affected_agent_count: int | None = None
    total_gateways: int | None = None
    changed_gateways: int | None = None
    gateway_change_ratio: float | None = None
    total_flows: int | None = None
    unaffected_flows: int | None = None
    disturbed_unaffected_flows: int | None = None
    unaffected_disturbance_ratio: float | None = None
    control_messages: int | None = None
    control_bytes: int | None = None
    rollback_count: int | None = None
    stale_state_detected: bool | None = None


PAPER_TRIAL_FIELDS = tuple(item.name for item in fields(PaperTrial))


def write_paper_trials(path: str | Path, rows: Iterable[PaperTrial]) -> None:
    output = Path(path)
    materialized = [asdict(row) for row in rows]
    if not materialized:
        raise ValueError("paper trial output must not be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(PAPER_TRIAL_FIELDS),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(materialized)
