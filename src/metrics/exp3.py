from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from src.core.events import EventLogRecord
from src.simulation.continuous_probe import BusinessProbeSample


@dataclass(frozen=True)
class Exp3RunMetrics:
    run_id: str
    seed: int
    method: str
    scenario: str
    scenario_fingerprint: str
    result_mode: str
    num_agents_before: int
    num_agents_after: int
    num_edges_before: int
    num_edges_after: int
    num_gateways: int
    cross_gateway_edge_ratio: float
    agent_removal_ratio: float
    removed_agent_type: str
    removed_agent_ids: str
    old_version: int
    new_version: int
    success: bool
    failure_reason: str
    elastic_latency_ms: float
    failure_handling_ms: float
    scope_latency_ms: float
    plan_latency_ms: float
    compile_delta_latency_ms: float
    stage_latency_ms: float
    verification_latency_ms: float
    activation_latency_ms: float
    rollback_latency_ms: float
    affected_agents: int
    affected_edges: int
    affected_sessions: int
    affected_routes: int
    affected_gateways: int
    affected_physical_resources: int
    physical_bindings_before: int
    physical_bindings_after: int
    released_physical_resources: int
    residual_physical_resources: int
    total_rules_before: int
    total_rules_after: int
    added_rules: int
    updated_rules: int
    deleted_rules: int
    changed_rules_ratio: float
    residual_rules: int
    control_messages: int
    control_bytes: int
    unaffected_edges_total: int
    unaffected_edges_interrupted: int
    unaffected_interrupted_rate: float
    unaffected_interruption_ms: float
    unaffected_latency_change_ms: float
    unaffected_packet_loss_change: float
    rollback_triggered: bool
    rollback_success: bool
    controller_cpu_percent: float
    controller_memory_mb: float
    event_occurred_at: float
    event_received_at: float
    scope_finished_at: float
    plan_finished_at: float
    stage_finished_at: float
    verification_finished_at: float
    activation_finished_at: float
    stable_verification_finished_at: float
    rollback_finished_at: float


def write_exp3_metrics_csv(
    path: str | Path,
    metrics: Iterable[Exp3RunMetrics],
) -> None:
    rows = [asdict(item) for item in metrics]
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("exp3 metrics must not be empty")
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_exp3_events_jsonl(
    path: str | Path,
    records: Iterable[tuple[str, str, EventLogRecord]],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for method, scenario, record in records:
            row = asdict(record)
            row["method"] = method
            row["scenario"] = scenario
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_probe_samples_csv(
    path: str | Path,
    rows: Iterable[tuple[str, str, str, BusinessProbeSample]],
) -> None:
    materialized = []
    for run_id, method, scenario, sample in rows:
        row = asdict(sample)
        row.update({"run_id": run_id, "method": method, "scenario": scenario})
        materialized.append(row)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "run_id",
        "method",
        "scenario",
        "edge_id",
        "probe_timestamp",
        "stage",
        "reachable",
        "latency_ms",
        "packet_loss",
        "throughput_mbps",
    ]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)
