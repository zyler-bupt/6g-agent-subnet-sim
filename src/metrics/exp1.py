from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Exp1RunMetrics:
    run_id: str
    seed: int
    task_id: str
    result_mode: str
    verification_backend: str
    real_ping_verification: bool
    real_iperf3_verification: bool
    num_agents: int
    num_edges: int
    num_gateways: int
    cross_gateway_edge_ratio: float
    version: int
    success: bool
    failure_reason: str
    control_plane_latency_ms: float
    legacy_controller_compute_ms: float
    formation_latency_ms: float
    formation_latency_s: float
    mapping_latency_ms: float
    binding_latency_ms: float
    feasibility_latency_ms: float
    cross_layer_coordination_latency_ms: float
    compile_latency_ms: float
    transaction_planning_latency_ms: float
    compilation_and_planning_latency_ms: float
    installation_latency_ms: float
    activation_latency_ms: float
    # New explicit five-phase breakdown from src/simulation/latency_model.py.
    # T_form = t_ctrl + t_dispatch + t_install + t_verify + t_activate.
    t_ctrl_ms: float = 0.0
    t_dispatch_ms: float = 0.0
    t_install_ms: float = 0.0
    t_verify_ms: float = 0.0
    t_activate_ms: float = 0.0
    deploy_mode: str = ""
    verification_latency_ms: float = 0.0
    verifier_runtime_ms: float = 0.0
    session_count: int = 0
    rule_count: int = 0
    control_messages: int = 0
    control_bytes: int = 0
    rollback_triggered: bool = False
    rollback_success: bool = False
    t_task_received: float = 0.0
    t_mapping_finished: float = 0.0
    t_layer_binding_finished: float = 0.0
    t_feasibility_finished: float = 0.0
    t_compile_finished: float = 0.0
    t_stage_started: float = 0.0
    t_stage_finished: float = 0.0
    t_staged_verify_finished: float = 0.0
    t_activate_finished: float = 0.0
    t_stable_verify_finished: float = 0.0


def write_exp1_metrics_csv(path: Path, rows: list[Exp1RunMetrics]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(Exp1RunMetrics.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
