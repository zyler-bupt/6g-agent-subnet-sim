from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from experiments.paper_protocol import (
    EXPERIMENT_METHODS,
    METHODS,
    RunMode,
    mode_spec,
)
from src.controller.formation_strategies import FormationOutcome, run_formation_method
from src.metrics.exp1 import Exp1RunMetrics, write_exp1_metrics_csv
from src.metrics.paper import PaperTrial, write_paper_trials
from src.simulation.paper_scenarios import (
    FormationScenarioSnapshot,
    generate_formation_snapshot,
)
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
    ScenarioSnapshot,
)


PAPER_RESULT_MODE = "discrete_event_transactionally_verified_control_plane_simulation"


def _duration_ms(start: float, finish: float) -> float:
    if start <= 0.0 or finish <= 0.0:
        return 0.0
    return max(0.0, (finish - start) * 1000.0)


async def run_one(
    snapshot: ScenarioSnapshot,
    *,
    run_index: int,
    verification_config: dict[str, Any],
) -> Exp1RunMetrics:
    controller, verifier, _provider = snapshot.instantiate()
    subnet, metrics = await controller.build_task_subnet(
        snapshot.task,
        verifier=verifier,
        run_id=run_index,
        seed=snapshot.seed,
    )
    task_received = metrics.t_task_received
    stage_started = metrics.t_stage_started
    stable_finished = metrics.t_stable_verify_finished
    control_plane_ms = _duration_ms(task_received, stage_started)
    formation_ms = _duration_ms(task_received, stable_finished) if metrics.networking_success else 0.0
    mapping_ms = metrics.mapping_latency_ms
    coordination_ms = metrics.binding_latency_ms + metrics.feasibility_latency_ms
    planning_ms = _duration_ms(metrics.t_compile_finished, stage_started)
    compilation_and_planning_ms = _duration_ms(metrics.t_feasibility_finished, stage_started)
    # This residual closes the full task-received -> stable-verified interval.
    # It contains staged verification, post-activation business-edge
    # verification, and the small transaction transitions surrounding them.
    displayed_without_verify = (
        mapping_ms
        + coordination_ms
        + compilation_and_planning_ms
        + metrics.stage_latency_ms
        + metrics.activation_latency_ms
    )
    verification_phase_ms = max(0.0, formation_ms - displayed_without_verify)
    return Exp1RunMetrics(
        run_id=f"agents={snapshot.config.num_agents}:seed={snapshot.seed}",
        seed=snapshot.seed,
        task_id=snapshot.task.task_id,
        result_mode="in_memory_transactionally_verified_control_plane_simulation",
        verification_backend=str(
            verification_config.get(
                "backend",
                "in_memory_synthetic_business_edge_verifier",
            )
        ),
        real_ping_verification=bool(verification_config.get("contains_ping", False)),
        real_iperf3_verification=bool(verification_config.get("contains_iperf3", False)),
        num_agents=snapshot.config.num_agents,
        num_edges=len(snapshot.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        cross_gateway_edge_ratio=snapshot.actual_cross_gateway_edge_ratio,
        version=subnet.version if metrics.networking_success else 0,
        success=metrics.networking_success,
        failure_reason=metrics.failure_reason,
        control_plane_latency_ms=control_plane_ms,
        legacy_controller_compute_ms=metrics.controller_compute_ms,
        formation_latency_ms=formation_ms,
        formation_latency_s=formation_ms / 1000.0,
        mapping_latency_ms=mapping_ms,
        binding_latency_ms=metrics.binding_latency_ms,
        feasibility_latency_ms=metrics.feasibility_latency_ms,
        cross_layer_coordination_latency_ms=coordination_ms,
        compile_latency_ms=metrics.compile_latency_ms,
        transaction_planning_latency_ms=planning_ms,
        compilation_and_planning_latency_ms=compilation_and_planning_ms,
        installation_latency_ms=metrics.stage_latency_ms,
        activation_latency_ms=metrics.activation_latency_ms,
        verification_latency_ms=verification_phase_ms,
        verifier_runtime_ms=metrics.verification_latency_ms,
        session_count=len(subnet.sessions),
        rule_count=len(subnet.rules),
        control_messages=metrics.control_messages,
        control_bytes=metrics.control_bytes,
        rollback_triggered=metrics.rollback_triggered,
        rollback_success=metrics.rollback_success,
        t_task_received=task_received,
        t_mapping_finished=metrics.t_mapping_finished,
        t_layer_binding_finished=metrics.t_layer_binding_finished,
        t_feasibility_finished=metrics.t_feasibility_finished,
        t_compile_finished=metrics.t_compile_finished,
        t_stage_started=stage_started,
        t_stage_finished=metrics.t_stage_finished,
        t_staged_verify_finished=metrics.t_staged_verify_finished,
        t_activate_finished=metrics.t_activate_finished,
        t_stable_verify_finished=stable_finished,
    )


async def run_experiment(
    config: dict[str, Any],
    *,
    seeds: tuple[int, ...],
    output_dir: Path,
) -> list[Exp1RunMetrics]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    task = config.get("task", {})
    topology = config.get("topology", {})
    verification = config.get("verification", {})
    timeout_s = float(config.get("simulation", {}).get("timeout_ms", 10000.0)) / 1000.0
    generator = ElasticScenarioGenerator()
    rows: list[Exp1RunMetrics] = []
    run_index = 0
    for num_agents in task.get("agent_counts", [10, 20, 40, 60, 80]):
        scenario_config = ScenarioConfig(
            num_agents=int(num_agents),
            edge_ratio=float(task.get("edge_ratio", 1.5)),
            num_gateways=int(task.get("num_gateways", 4)),
            cross_gateway_edge_ratio=float(task.get("cross_gateway_edge_ratio", 0.5)),
            agent_removal_ratio=0.1,
            removed_agent_type="leaf",
            dag_type=str(task.get("dag_type", "random")),
            multi_hop=bool(topology.get("multi_hop", True)),
        )
        for seed in seeds:
            run_index += 1
            snapshot = generator.generate(scenario_config, seed)
            try:
                row = await asyncio.wait_for(
                    run_one(
                        snapshot,
                        run_index=run_index,
                        verification_config=verification,
                    ),
                    timeout=timeout_s,
                )
            except Exception as error:
                row = _failed_row(snapshot, verification, error)
            rows.append(row)
    rows.sort(key=lambda row: (row.num_agents, row.seed))
    write_exp1_metrics_csv(raw_dir / "runs.csv", rows)
    scope = {
        "result_mode": "in_memory_transactionally_verified_control_plane_simulation",
        "verification_backend": verification.get(
            "backend",
            "in_memory_synthetic_business_edge_verifier",
        ),
        "contains_ping": bool(verification.get("contains_ping", False)),
        "contains_iperf3": bool(verification.get("contains_iperf3", False)),
        "t_ctrl_definition": "t_stage_started - t_task_received",
        "t_form_definition": "t_stable_verify_finished - t_task_received",
        "paper_scope_note": (
            "In-memory transactionally verified control-plane simulation; "
            "not ping/iPerf3 or 6G data-plane measurement."
        ),
    }
    with (raw_dir / "measurement_scope.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(scope, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return rows


async def run_exp1(
    mode: str | RunMode,
    output_root: Path,
    *,
    seeds: tuple[int, ...] | None = None,
    task_sizes: tuple[int, ...] = (8, 12, 16, 20, 24, 28, 32),
    churn_points: tuple[int | float, ...] = (0, 5, 10, 15, 20, 30),
    churn_task_size: int = 24,
) -> list[PaperTrial]:
    """Run the canonical paired Exp.1 grid and write trial-level raw data."""

    selected_mode = RunMode(mode)
    specification = mode_spec(selected_mode)
    selected_seeds = (
        tuple(range(specification.topology_seeds))
        if seeds is None
        else tuple(sorted(set(seeds)))
    )
    if not selected_seeds:
        raise ValueError("Exp.1 requires at least one topology seed")
    normalized_sizes = tuple(sorted(set(int(value) for value in task_sizes)))
    normalized_churn = tuple(sorted(set(float(value) for value in churn_points)))
    if not normalized_sizes:
        raise ValueError("Exp.1 requires at least one task size")
    if not normalized_churn:
        raise ValueError("Exp.1 requires at least one churn point")
    if any(value < 0.0 or value > 100.0 for value in normalized_churn):
        raise ValueError("Exp.1 churn points must be percentages in [0, 100]")

    rows: list[PaperTrial] = []
    for task_size in normalized_sizes:
        for seed in selected_seeds:
            for event_id in range(specification.events_per_seed):
                snapshot = generate_formation_snapshot(task_size, seed, event_id)
                trial_id = _exp1_trial_id(
                    "task_size",
                    task_size,
                    seed,
                    event_id,
                )
                for method_id in EXPERIMENT_METHODS["exp1"]:
                    outcome = await run_formation_method(snapshot, method_id)
                    rows.append(
                        _paper_trial_from_formation(
                            selected_mode,
                            snapshot,
                            outcome,
                            trial_id=trial_id,
                            series="task_size",
                            churn_probability=None,
                        )
                    )

    for churn_percent in normalized_churn:
        churn_probability = churn_percent / 100.0
        for seed in selected_seeds:
            for event_id in range(specification.rate_events_per_seed):
                snapshot = generate_formation_snapshot(
                    churn_task_size,
                    seed,
                    event_id,
                )
                trial_id = _exp1_trial_id(
                    "state_churn",
                    churn_percent,
                    seed,
                    event_id,
                )
                for method_id in EXPERIMENT_METHODS["exp1"]:
                    outcome = await run_formation_method(
                        snapshot,
                        method_id,
                        churn_probability=churn_probability,
                    )
                    rows.append(
                        _paper_trial_from_formation(
                            selected_mode,
                            snapshot,
                            outcome,
                            trial_id=trial_id,
                            series="state_churn",
                            churn_probability=churn_probability,
                        )
                    )

    rows.sort(
        key=lambda row: (
            row.series or "",
            row.state_churn_probability
            if row.state_churn_probability is not None
            else -1.0,
            row.task_size or 0,
            row.seed,
            row.event_id,
            row.method_id,
        )
    )
    write_paper_trials(
        output_root / "raw" / selected_mode.value / "exp1" / "trials.csv",
        rows,
    )
    return rows


def _paper_trial_from_formation(
    mode: RunMode,
    snapshot: FormationScenarioSnapshot,
    outcome: FormationOutcome,
    *,
    trial_id: str,
    series: str,
    churn_probability: float | None,
) -> PaperTrial:
    method = METHODS[outcome.method_id]
    return PaperTrial(
        experiment="exp1",
        mode=mode.value,
        trial_id=trial_id,
        seed=snapshot.seed,
        event_id=snapshot.event_id,
        method_id=method.method_id,
        method_label=method.label,
        method_source=method.source,
        adapted=method.adapted,
        topology_fingerprint=snapshot.topology.fingerprint,
        scenario_fingerprint=snapshot.fingerprint,
        qos_fingerprint=snapshot.qos_fingerprint,
        event_fingerprint=outcome.trace.churn_fingerprint,
        series=series,
        result_mode=PAPER_RESULT_MODE,
        task_received_at=outcome.task_received_at,
        stable_verify_finished_at=outcome.stable_verify_finished_at,
        task_size=len(snapshot.task.app_agents),
        num_dag_edges=len(snapshot.task.biz_edges),
        num_gateways=len(snapshot.topology.gateway_ids),
        state_churn_probability=churn_probability,
        formation_latency_ms=outcome.formation_latency_ms,
        success=outcome.success,
        qos_satisfied=outcome.qos_satisfied,
        failure_reason=outcome.failure_reason or None,
        total_rules=outcome.total_rules,
        total_gateways=len(snapshot.topology.gateway_ids),
        total_flows=outcome.total_flows,
        control_messages=outcome.control_messages,
        control_bytes=outcome.control_bytes,
        rollback_count=outcome.rollback_count,
        stale_state_detected=outcome.stale_state_detected,
    )


def _exp1_trial_id(
    series: str,
    point: int | float,
    seed: int,
    event_id: int,
) -> str:
    point_label = f"{float(point):g}"
    return f"exp1:{series}:{point_label}:seed:{seed:04d}:event:{event_id:03d}"


def _failed_row(
    snapshot: ScenarioSnapshot,
    verification_config: dict[str, Any],
    error: Exception,
) -> Exp1RunMetrics:
    values: dict[str, Any] = {
        field: 0 for field in Exp1RunMetrics.__dataclass_fields__
    }
    values.update(
        run_id=f"agents={snapshot.config.num_agents}:seed={snapshot.seed}",
        seed=snapshot.seed,
        task_id=snapshot.task.task_id,
        result_mode="in_memory_transactionally_verified_control_plane_simulation",
        verification_backend=str(
            verification_config.get(
                "backend",
                "in_memory_synthetic_business_edge_verifier",
            )
        ),
        real_ping_verification=bool(verification_config.get("contains_ping", False)),
        real_iperf3_verification=bool(verification_config.get("contains_iperf3", False)),
        num_agents=snapshot.config.num_agents,
        num_edges=len(snapshot.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        cross_gateway_edge_ratio=snapshot.actual_cross_gateway_edge_ratio,
        success=False,
        failure_reason=f"{type(error).__name__}: {error}",
    )
    return Exp1RunMetrics(**values)


def parse_seed_range(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        if finish < start:
            raise ValueError("seed range finish must be >= start")
        return tuple(range(start, finish + 1))
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run initial task-subnet formation experiment")
    parser.add_argument("--config", default="configs/exp1_initial_formation.yaml")
    parser.add_argument("--seeds", default="0:29")
    parser.add_argument("--output-dir", default="results/exp1")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    rows = asyncio.run(
        run_experiment(
            config,
            seeds=parse_seed_range(args.seeds),
            output_dir=Path(args.output_dir),
        )
    )
    successes = sum(row.success for row in rows)
    print(
        f"Experiment 1 wrote {len(rows)} raw runs to {Path(args.output_dir).resolve()} "
        f"(successful={successes}, failed={len(rows) - successes})"
    )


if __name__ == "__main__":
    main()
