from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class RobustnessRunMetrics:
    run_id: str
    seed: int
    experiment: str
    scenario: str
    method: str
    conflict_pressure: float
    scenario_fingerprint: str
    result_mode: str
    conflict_class: str
    ground_truth_conflict: bool
    ground_truth_resolvable: bool
    ground_truth_feasible_combinations: int
    ground_truth_uses_true_state: bool
    noise_ratio: float
    stale_ms: int
    missing_layer: str
    proposals_per_layer: int
    num_proposals: int
    num_layers_observed: int
    num_raw_combinations: int
    num_pruned_combinations: int
    num_evaluated_combinations: int
    num_feasible_combinations: int
    combination_evaluation_mode: str
    selected_proposal_ids: str
    selected_action_set: str
    rejected_proposal_ids: str
    pre_execution_feasibility_checked: bool
    post_execution_verification_result: str
    conflict_detected: bool
    conflict_resolved: bool
    qos_satisfied: bool
    infeasible_configuration: bool
    decision_rejected: bool
    safe_rejection: bool
    unsafe_execution: bool
    transaction_attempted: bool
    rollback_triggered: bool
    rollback_success: bool
    no_partial_commit: bool
    stable_version_before: int
    stable_version_after: int
    proposal_generated_version: int
    execution_version: int
    read_set_version: int
    stale_state_detected: bool
    stale_proposal_rejected: bool
    proposal_regenerated: bool
    coordination_latency_ms: float
    feasibility_latency_ms: float
    transaction_latency_ms: float
    total_latency_ms: float
    peak_memory_mb: float
    timeout: bool
    control_messages: int
    control_bytes: int
    success: bool
    failure_reason: str


def write_robustness_csv(
    path: str | Path,
    rows: Iterable[RobustnessRunMetrics],
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    materialized = [asdict(row) for row in rows]
    fields = list(RobustnessRunMetrics.__dataclass_fields__)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)


def write_jsonl(path: str | Path, rows: Iterable[dict[str, object]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
