from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Exp2RunMetrics:
    run_id: str
    seed: int
    scenario: str
    method: str
    scenario_fingerprint: str
    result_mode: str
    num_agents: int
    num_edges: int
    num_gateways: int
    num_proposals: int
    num_layers_involved: int
    conflict_pressure: float
    ground_truth_conflict: bool
    ground_truth_resolvable: bool
    conflict_detected: bool
    conflict_type: str
    conflict_resolved: bool
    candidate_combinations: int
    rejected_combinations: int
    selected_proposals: str
    rejected_proposals: int
    coordination_latency_ms: float
    feasibility_latency_ms: float
    transaction_latency_ms: float
    total_decision_latency_ms: float
    qos_satisfied: bool
    qos_satisfaction_rate: float
    infeasible_configuration: bool
    post_execution_verification_success: bool
    application_rate_mbps: float
    transport_rate_mbps: float
    network_available_bandwidth_mbps: float
    physical_available_capacity_mbps: float
    latency_ms: float
    packet_loss_rate: float
    reliability: float
    changed_actions: int
    changed_rules: int
    affected_gateways: int
    control_messages: int
    control_bytes: int
    rollback_triggered: bool
    rollback_success: bool
    success: bool
    failure_reason: str
    event_occurred_at: float
    decision_finished_at: float


@dataclass(frozen=True)
class CompoundTimelineSample:
    run_id: str
    seed: int
    method: str
    time_step: int
    logical_time: float
    pressure: float
    application_demand_mbps: float
    network_available_bandwidth_mbps: float
    physical_available_capacity_mbps: float
    actual_send_rate_mbps: float
    latency_ms: float
    packet_loss_rate: float
    reliability: float
    qos_satisfied: bool
    conflict_detected: bool
    conflict_detected_at: float
    action_executed_at: float
    transaction_success: bool
    rollback_triggered: bool
    selected_proposals: str


def write_exp2_metrics_csv(
    path: str | Path,
    rows: Iterable[Exp2RunMetrics],
) -> None:
    _write_dataclass_csv(path, rows, Exp2RunMetrics)


def write_compound_timeline_csv(
    path: str | Path,
    rows: Iterable[CompoundTimelineSample],
) -> None:
    _write_dataclass_csv(path, rows, CompoundTimelineSample)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_dataclass_csv(path, rows, row_type) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    materialized = [asdict(item) for item in rows]
    fieldnames = list(row_type.__dataclass_fields__)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)
