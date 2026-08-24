from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from src.core.failures import RecoveryProbeSample, RecoveryTimelineSample


@dataclass(frozen=True)
class Exp4RunMetrics:
    run_id: str
    seed: int
    scenario: str
    method: str
    result_mode: str
    scenario_fingerprint: str
    fault_fingerprint: str
    fault_type: str
    fault_target: str
    fault_level: str
    fault_severity: float
    fault_effective_at: float
    failure_detected_at: float
    fault_was_disruptive: bool
    pre_recovery_requirement_mbps: float
    post_failure_capacity_mbps: float
    pre_recovery_violation_margin_mbps: float
    requirement_violated_before_recovery: bool
    num_agents: int
    num_edges: int
    num_gateways: int
    old_version: int
    new_version: int
    detection_latency_ms: float
    localization_latency_ms: float
    proposal_latency_ms: float
    coordination_latency_ms: float
    feasibility_latency_ms: float
    delta_compile_latency_ms: float
    stage_latency_ms: float
    activation_latency_ms: float
    verification_latency_ms: float
    reconfiguration_latency_ms: float
    repair_latency_ms: float
    recovery_latency_ms: float
    failure_handling_ms: float
    service_interruption_ms: float
    recovery_success: bool
    qos_recovered: bool
    safe_rejection: bool
    pre_execution_feasibility_checked: bool
    post_execution_verification_success: bool
    affected_agents: int
    affected_edges: int
    affected_sessions: int
    affected_routes: int
    affected_gateways: int
    affected_physical_resources: int
    changed_agents: int
    changed_sessions: int
    changed_routes: int
    changed_physical_bindings: int
    changed_rules: int
    rule_change_ratio: float
    gateway_impact_ratio: float
    unaffected_edges: int
    unaffected_edges_interrupted: int
    collateral_interruption_rate: float
    collateral_interruption_ms: float
    collateral_latency_change_ms: float
    collateral_packet_loss_change: float
    packet_loss_during_recovery: float
    throughput_before: float
    throughput_during: float
    throughput_after: float
    latency_before: float
    latency_during: float
    latency_after: float
    rollback_triggered: bool
    rollback_success: bool
    residual_rules: int
    control_messages: int
    control_bytes: int
    selected_actions: str
    selected_layers: str
    transaction_executor: str
    verifier_mode: str
    stable_versions_consistent: bool
    staged_rules_empty: bool
    service_recovered_at: float
    stage_started_at: float
    activation_finished_at: float
    stable_verification_finished_at: float
    rollback_finished_at: float
    success: bool
    failure_reason: str


def write_exp4_metrics_csv(path: str | Path, rows: Iterable[Exp4RunMetrics]) -> None:
    _write_dataclass_csv(path, rows, Exp4RunMetrics)


def write_probe_samples_csv(path: str | Path, rows: Iterable[RecoveryProbeSample]) -> None:
    _write_dataclass_csv(path, rows, RecoveryProbeSample)


def write_timeline_csv(path: str | Path, rows: Iterable[RecoveryTimelineSample]) -> None:
    _write_dataclass_csv(path, rows, RecoveryTimelineSample)


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
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(row_type.__dataclass_fields__),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(materialized)
