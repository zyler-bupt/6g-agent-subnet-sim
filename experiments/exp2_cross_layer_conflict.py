from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import yaml

from src.controller.authorized_actions import (
    AuthorizedActionExecutor,
    CrossLayerExecutionResult,
)
from src.controller.conflicts import conflicts_from_violations
from src.controller.cross_layer_coordinator import (
    CoordinationResult,
    CrossLayerCoordinator,
)
from src.controller.ground_truth import proposals_for_ids
from src.core.events import EventInjector, EventStage
from src.core.models import TaskSubnet, to_jsonable
from src.metrics.exp2 import (
    CompoundTimelineSample,
    Exp2RunMetrics,
    write_compound_timeline_csv,
    write_exp2_metrics_csv,
    write_jsonl,
)
from src.simulation.conflict_scenario_generator import (
    ConflictScenarioConfig,
    ConflictScenarioGenerator,
    ConflictScenarioSnapshot,
    MAIN_SCENARIOS,
)


async def run_method(
    snapshot: ConflictScenarioSnapshot,
    method: str,
    *,
    run_index: int,
) -> tuple[
    Exp2RunMetrics,
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    controller, verifier, _provider = snapshot.instantiate()
    stable, formation = await controller.build_task_subnet(
        snapshot.base.task,
        verifier=verifier,
        run_id=run_index,
        seed=snapshot.seed,
    )
    if not formation.networking_success:
        raise RuntimeError(
            "initial transactional build failed: " + formation.failure_reason
        )
    injector = EventInjector(prefix=f"exp2-{method}-{snapshot.seed}")
    artifacts = await _execute_snapshot(
        controller,
        verifier,
        stable,
        snapshot,
        method,
        injector,
        run_index=run_index,
    )
    return (
        artifacts.metrics,
        artifacts.events,
        artifacts.proposals,
        artifacts.conflicts,
    )


class _StepArtifacts:
    def __init__(
        self,
        metrics: Exp2RunMetrics,
        execution: CrossLayerExecutionResult,
        coordination: CoordinationResult,
        events: list[dict[str, object]],
        proposals: list[dict[str, object]],
        conflicts: list[dict[str, object]],
    ) -> None:
        self.metrics = metrics
        self.execution = execution
        self.coordination = coordination
        self.events = events
        self.proposals = proposals
        self.conflicts = conflicts


async def _execute_snapshot(
    controller,
    verifier,
    stable: TaskSubnet,
    snapshot: ConflictScenarioSnapshot,
    method: str,
    injector: EventInjector,
    *,
    run_index: int,
    time_step: int | None = None,
    run_id_override: str | None = None,
) -> _StepArtifacts:
    event = injector.cross_layer_conflict(
        stable.task.task_id,
        scenario=snapshot.scenario,
        pressure=snapshot.pressure,
        scenario_fingerprint=snapshot.fingerprint,
    )
    received_at = perf_counter()
    coordination_started = perf_counter()
    coordination = CrossLayerCoordinator().coordinate(
        method,
        snapshot.cross_layer_state,
        snapshot.proposals,
    )
    coordination_finished = perf_counter()
    execution = await AuthorizedActionExecutor(controller, verifier).execute(
        stable,
        snapshot.cross_layer_state,
        coordination,
        event,
        run_id=run_index,
        seed=snapshot.seed,
        received_at=received_at,
    )
    run_id = run_id_override or (
        f"{snapshot.scenario}:pressure={snapshot.pressure:g}:"
        f"seed={snapshot.seed}:method={method}"
    )
    metrics = _build_metrics(
        run_id,
        snapshot,
        method,
        coordination,
        execution,
        event.occurred_at,
    )
    events = _event_rows(
        run_id,
        snapshot,
        method,
        execution,
        coordination,
        received_at,
        coordination_started,
        coordination_finished,
        time_step,
    )
    proposals = _proposal_rows(
        run_id,
        snapshot,
        method,
        coordination,
        time_step,
    )
    conflicts = _conflict_rows(
        run_id,
        snapshot,
        method,
        coordination,
        time_step,
    )
    return _StepArtifacts(
        metrics,
        execution,
        coordination,
        events,
        proposals,
        conflicts,
    )


async def run_compound_method(
    snapshots: tuple[ConflictScenarioSnapshot, ...],
    method: str,
    *,
    run_index: int,
) -> tuple[
    Exp2RunMetrics,
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[CompoundTimelineSample],
]:
    first = snapshots[0]
    controller, verifier, _provider = first.instantiate()
    stable, formation = await controller.build_task_subnet(
        first.base.task,
        verifier=verifier,
        run_id=run_index,
        seed=first.seed,
    )
    if not formation.networking_success:
        raise RuntimeError(
            "initial compound build failed: " + formation.failure_reason
        )
    run_id = f"compound:seed={first.seed}:method={method}"
    injector = EventInjector(prefix=f"exp2-compound-{method}-{first.seed}")
    steps: list[_StepArtifacts] = []
    timeline: list[CompoundTimelineSample] = []
    for time_step, snapshot in enumerate(snapshots):
        artifact = await _execute_snapshot(
            controller,
            verifier,
            stable,
            snapshot,
            method,
            injector,
            run_index=run_index,
            time_step=time_step,
            run_id_override=run_id,
        )
        steps.append(artifact)
        if artifact.execution.transaction.success:
            stable = artifact.execution.transaction.state
        metric = artifact.metrics
        transaction = artifact.execution.transaction
        detected_at = metric.event_occurred_at + (
            artifact.coordination.coordination_latency_ms / 1000.0
        )
        action_at = transaction.timestamp(EventStage.ACTIVATE_FINISHED)
        if action_at <= 0.0:
            action_at = transaction.timestamp(EventStage.ROLLBACK_FINISHED, last=True)
        timeline.append(
            CompoundTimelineSample(
                run_id=run_id,
                seed=first.seed,
                method=method,
                time_step=time_step,
                logical_time=float(time_step),
                pressure=snapshot.pressure,
                application_demand_mbps=metric.application_rate_mbps,
                network_available_bandwidth_mbps=(
                    metric.network_available_bandwidth_mbps
                ),
                physical_available_capacity_mbps=(
                    metric.physical_available_capacity_mbps
                ),
                actual_send_rate_mbps=metric.transport_rate_mbps,
                latency_ms=metric.latency_ms,
                packet_loss_rate=metric.packet_loss_rate,
                reliability=metric.reliability,
                qos_satisfied=metric.qos_satisfied,
                conflict_detected=metric.conflict_detected,
                conflict_detected_at=detected_at,
                action_executed_at=action_at,
                transaction_success=metric.success,
                rollback_triggered=metric.rollback_triggered,
                selected_proposals=metric.selected_proposals,
            )
        )
    aggregate = _aggregate_compound_metrics(run_id, snapshots, method, steps)
    return (
        aggregate,
        [row for step in steps for row in step.events],
        [row for step in steps for row in step.proposals],
        [row for step in steps for row in step.conflicts],
        timeline,
    )


def _build_metrics(
    run_id: str,
    snapshot: ConflictScenarioSnapshot,
    method: str,
    coordination: CoordinationResult,
    execution: CrossLayerExecutionResult,
    occurred_at: float,
) -> Exp2RunMetrics:
    transaction = execution.transaction
    post = execution.post_execution_feasibility
    edge_id = next(iter(snapshot.cross_layer_state.constraints))
    edge_metric = post.edge_metrics[edge_id]
    decision_finished = max(
        (record.timestamp for record in transaction.event_log),
        default=occurred_at,
    )
    conflict_types = sorted(
        {
            record.conflict_type
            for record in conflicts_from_violations(
                proposals_for_ids(
                    snapshot.proposals,
                    snapshot.ground_truth.independent_proposal_ids,
                ),
                snapshot.ground_truth.independent_violations,
            )
        }
    )
    selected = coordination.selected_proposals
    delta = execution.plan.rule_delta
    qos_satisfied = transaction.success and post.feasible
    return Exp2RunMetrics(
        run_id=run_id,
        seed=snapshot.seed,
        scenario=f"{snapshot.scenario}:pressure={snapshot.pressure:g}",
        method=method,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_control_plane_simulation",
        num_agents=len(snapshot.base.task.app_agents),
        num_edges=len(snapshot.base.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        num_proposals=len(snapshot.proposals),
        num_layers_involved=len({item.layer for item in snapshot.proposals}),
        conflict_pressure=snapshot.pressure,
        ground_truth_conflict=snapshot.ground_truth.ground_truth_conflict,
        ground_truth_resolvable=snapshot.ground_truth.ground_truth_resolvable,
        conflict_detected=coordination.conflict_detected,
        conflict_type=";".join(conflict_types),
        conflict_resolved=(
            snapshot.ground_truth.ground_truth_resolvable and qos_satisfied
        ),
        candidate_combinations=coordination.candidate_combinations,
        rejected_combinations=coordination.rejected_combinations,
        selected_proposals=";".join(item.proposal_id for item in selected),
        rejected_proposals=len(coordination.rejected_proposals),
        coordination_latency_ms=coordination.coordination_latency_ms,
        feasibility_latency_ms=coordination.feasibility_latency_ms,
        transaction_latency_ms=execution.transaction_latency_ms,
        total_decision_latency_ms=max(
            0.0,
            (decision_finished - occurred_at) * 1000.0,
        ),
        qos_satisfied=qos_satisfied,
        qos_satisfaction_rate=1.0 if qos_satisfied else 0.0,
        infeasible_configuration=not post.feasible,
        post_execution_verification_success=transaction.success,
        application_rate_mbps=edge_metric.application_rate_mbps,
        transport_rate_mbps=edge_metric.transport_rate_mbps,
        network_available_bandwidth_mbps=edge_metric.network_bandwidth_mbps,
        physical_available_capacity_mbps=edge_metric.physical_capacity_mbps,
        latency_ms=edge_metric.latency_ms,
        packet_loss_rate=edge_metric.packet_loss_rate,
        reliability=edge_metric.reliability,
        changed_actions=sum(not item.is_keep for item in selected),
        changed_rules=(
            len(delta.additions) + len(delta.updates) + len(delta.deletions)
        ),
        affected_gateways=len(execution.plan.affected_gateways),
        control_messages=transaction.control_messages,
        control_bytes=transaction.control_bytes,
        rollback_triggered=transaction.rollback_triggered,
        rollback_success=transaction.rollback_success,
        success=transaction.success,
        failure_reason=transaction.failure_reason,
        event_occurred_at=occurred_at,
        decision_finished_at=decision_finished,
    )


def _aggregate_compound_metrics(
    run_id: str,
    snapshots: tuple[ConflictScenarioSnapshot, ...],
    method: str,
    steps: list[_StepArtifacts],
) -> Exp2RunMetrics:
    last = steps[-1].metrics
    triggered = [step.metrics for step in steps if step.metrics.rollback_triggered]
    conflict_types = sorted(
        {
            value
            for step in steps
            for value in step.metrics.conflict_type.split(";")
            if value
        }
    )
    resolvable_steps = [
        step.metrics for step in steps if step.metrics.ground_truth_resolvable
    ]
    fingerprint = hashlib.sha256(
        "|".join(snapshot.fingerprint for snapshot in snapshots).encode("utf-8")
    ).hexdigest()
    return replace(
        last,
        run_id=run_id,
        scenario="compound",
        scenario_fingerprint=fingerprint,
        num_proposals=sum(step.metrics.num_proposals for step in steps),
        conflict_pressure=snapshots[-1].pressure,
        ground_truth_conflict=any(
            step.metrics.ground_truth_conflict for step in steps
        ),
        ground_truth_resolvable=any(
            step.metrics.ground_truth_resolvable for step in steps
        ),
        conflict_detected=any(step.metrics.conflict_detected for step in steps),
        conflict_type=";".join(conflict_types),
        conflict_resolved=(
            bool(resolvable_steps)
            and all(step.qos_satisfied for step in resolvable_steps)
        ),
        candidate_combinations=sum(
            step.metrics.candidate_combinations for step in steps
        ),
        rejected_combinations=sum(
            step.metrics.rejected_combinations for step in steps
        ),
        selected_proposals="|".join(
            step.metrics.selected_proposals for step in steps
        ),
        rejected_proposals=sum(
            step.metrics.rejected_proposals for step in steps
        ),
        coordination_latency_ms=sum(
            step.metrics.coordination_latency_ms for step in steps
        ),
        feasibility_latency_ms=sum(
            step.metrics.feasibility_latency_ms for step in steps
        ),
        transaction_latency_ms=sum(
            step.metrics.transaction_latency_ms for step in steps
        ),
        total_decision_latency_ms=sum(
            step.metrics.total_decision_latency_ms for step in steps
        ),
        qos_satisfied=all(step.metrics.qos_satisfied for step in steps),
        qos_satisfaction_rate=(
            sum(step.metrics.qos_satisfied for step in steps) / len(steps)
        ),
        infeasible_configuration=any(
            step.metrics.infeasible_configuration for step in steps
        ),
        post_execution_verification_success=all(
            step.metrics.post_execution_verification_success for step in steps
        ),
        changed_actions=sum(step.metrics.changed_actions for step in steps),
        changed_rules=sum(step.metrics.changed_rules for step in steps),
        affected_gateways=sum(step.metrics.affected_gateways for step in steps),
        control_messages=sum(step.metrics.control_messages for step in steps),
        control_bytes=sum(step.metrics.control_bytes for step in steps),
        rollback_triggered=bool(triggered),
        rollback_success=all(step.rollback_success for step in triggered),
        success=all(step.metrics.success for step in steps),
        failure_reason=" | ".join(
            step.metrics.failure_reason
            for step in steps
            if step.metrics.failure_reason
        ),
        event_occurred_at=steps[0].metrics.event_occurred_at,
        decision_finished_at=steps[-1].metrics.decision_finished_at,
    )


def _event_rows(
    run_id: str,
    snapshot: ConflictScenarioSnapshot,
    method: str,
    execution: CrossLayerExecutionResult,
    coordination: CoordinationResult,
    received_at: float,
    coordination_started: float,
    coordination_finished: float,
    time_step: int | None,
) -> list[dict[str, object]]:
    common = {
        "run_id": run_id,
        "seed": snapshot.seed,
        "scenario": snapshot.scenario,
        "method": method,
        "scenario_fingerprint": snapshot.fingerprint,
        "time_step": time_step,
    }
    rows = [
        {
            **common,
            "timestamp": received_at,
            "component": "FourLayerAgents",
            "event_stage": "PROPOSALS_COLLECTED",
            "details": {"proposal_count": len(snapshot.proposals)},
        },
        {
            **common,
            "timestamp": coordination_started,
            "component": "CrossLayerCoordinator",
            "event_stage": "COORDINATION_STARTED",
            "details": {},
        },
        {
            **common,
            "timestamp": coordination_finished,
            "component": "CrossLayerCoordinator",
            "event_stage": "COORDINATION_FINISHED",
            "details": {
                "conflict_detected": coordination.conflict_detected,
                "selected": execution.plan.planning_details.get(
                    "selected_proposal_ids",
                    [],
                ),
            },
        },
    ]
    for record in execution.transaction.event_log:
        # Keep the experiment-level run identifier.  EventLogRecord.run_id is
        # the executor's integer sequence and is retained separately so JSONL
        # rows remain joinable with runs.csv.
        transaction_row = to_jsonable(record)
        transaction_row["transaction_run_index"] = transaction_row.pop("run_id")
        rows.append({**transaction_row, **common})
    return sorted(rows, key=lambda row: (float(row["timestamp"]), str(row["event_stage"])))


def _proposal_rows(
    run_id: str,
    snapshot: ConflictScenarioSnapshot,
    method: str,
    coordination: CoordinationResult,
    time_step: int | None,
) -> list[dict[str, object]]:
    selected = {item.proposal_id for item in coordination.selected_proposals}
    return [
        {
            "run_id": run_id,
            "seed": snapshot.seed,
            "scenario": snapshot.scenario,
            "method": method,
            "scenario_fingerprint": snapshot.fingerprint,
            "time_step": time_step,
            "selected": proposal.proposal_id in selected,
            "rejection_reason": coordination.rejected_proposals.get(
                proposal.proposal_id,
                "",
            ),
            "locally_hard_feasible": snapshot.ground_truth.local_proposal_feasibility[
                proposal.proposal_id
            ],
            "proposal": to_jsonable(proposal),
        }
        for proposal in snapshot.proposals
    ]


def _conflict_rows(
    run_id: str,
    snapshot: ConflictScenarioSnapshot,
    method: str,
    coordination: CoordinationResult,
    time_step: int | None,
) -> list[dict[str, object]]:
    common = {
        "run_id": run_id,
        "seed": snapshot.seed,
        "scenario": snapshot.scenario,
        "method": method,
        "scenario_fingerprint": snapshot.fingerprint,
        "time_step": time_step,
    }
    rows = [
        {
            **common,
            "source": "ground_truth",
            "ground_truth_conflict": snapshot.ground_truth.ground_truth_conflict,
            "ground_truth_resolvable": snapshot.ground_truth.ground_truth_resolvable,
            "independent_violations": list(
                snapshot.ground_truth.independent_violations
            ),
            "best_feasible_combination": list(
                snapshot.ground_truth.best_feasible_combination
            ),
        }
    ]
    rows.extend(
        {
            **common,
            "source": "strategy",
            "conflict": to_jsonable(record),
        }
        for record in coordination.conflicts
    )
    return rows


async def run_experiment(
    config: dict[str, Any],
    methods: tuple[str, ...],
    seeds: tuple[int, ...],
    output_dir: Path,
) -> list[Exp2RunMetrics]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    generator = ConflictScenarioGenerator()
    scenario_config = _scenario_config(config)
    timeout = float(config.get("simulation", {}).get("timeout_ms", 10000)) / 1000.0
    metrics: list[Exp2RunMetrics] = []
    events: list[dict[str, object]] = []
    proposals: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    scenarios: list[dict[str, object]] = []
    timeline: list[CompoundTimelineSample] = []
    run_index = 0
    scenario_config_raw = config.get("scenarios", {})
    for scenario in MAIN_SCENARIOS:
        pressures = scenario_config_raw.get(scenario, {}).get("pressures", ())
        for pressure in pressures:
            for seed in seeds:
                snapshot = generator.generate(
                    scenario,
                    float(pressure),
                    seed,
                    scenario_config,
                )
                scenarios.append(snapshot.to_dict())
                for method in methods:
                    run_index += 1
                    try:
                        result = await asyncio.wait_for(
                            run_method(snapshot, method, run_index=run_index),
                            timeout=timeout,
                        )
                        metric, run_events, run_proposals, run_conflicts = result
                    except Exception as error:
                        metric = _failed_metric(snapshot, method, error)
                        run_events, run_proposals, run_conflicts = [], [], []
                    metrics.append(metric)
                    events.extend(run_events)
                    proposals.extend(run_proposals)
                    conflicts.extend(run_conflicts)

    compound = config.get("compound", {})
    if compound.get("enabled", True):
        compound_pressures = tuple(
            float(value) for value in compound.get("time_pressures", (0, 0.25, 0.5, 0.75, 1))
        )
        for seed in seeds:
            snapshots = tuple(
                generator.generate(
                    "compound",
                    pressure,
                    seed,
                    scenario_config,
                )
                for pressure in compound_pressures
            )
            scenarios.extend(snapshot.to_dict() for snapshot in snapshots)
            for method in methods:
                run_index += 1
                try:
                    result = await asyncio.wait_for(
                        run_compound_method(
                            snapshots,
                            method,
                            run_index=run_index,
                        ),
                        timeout=timeout,
                    )
                    metric, run_events, run_proposals, run_conflicts, run_timeline = result
                except Exception as error:
                    metric = _failed_metric(snapshots[-1], method, error, scenario="compound")
                    run_events, run_proposals, run_conflicts, run_timeline = [], [], [], []
                metrics.append(metric)
                events.extend(run_events)
                proposals.extend(run_proposals)
                conflicts.extend(run_conflicts)
                timeline.extend(run_timeline)

    ablation = config.get("ablation", {})
    if ablation.get("enabled", True):
        scenario = str(ablation.get("scenario", "application_capacity"))
        pressure = float(ablation.get("pressure", 1.2))
        ablation_methods = tuple(str(value) for value in ablation.get("methods", ()))
        for seed in seeds:
            snapshot = generator.generate(
                scenario,
                pressure,
                seed,
                scenario_config,
            )
            ablation_snapshot = replace(snapshot, scenario="ablation")
            scenarios.append(ablation_snapshot.to_dict())
            for method in ablation_methods:
                run_index += 1
                try:
                    result = await asyncio.wait_for(
                        run_method(ablation_snapshot, method, run_index=run_index),
                        timeout=timeout,
                    )
                    metric, run_events, run_proposals, run_conflicts = result
                    metric = replace(metric, scenario="ablation")
                except Exception as error:
                    metric = _failed_metric(
                        ablation_snapshot,
                        method,
                        error,
                        scenario="ablation",
                    )
                    run_events, run_proposals, run_conflicts = [], [], []
                metrics.append(metric)
                events.extend(run_events)
                proposals.extend(run_proposals)
                conflicts.extend(run_conflicts)

    write_exp2_metrics_csv(raw_dir / "runs.csv", metrics)
    write_jsonl(raw_dir / "events.jsonl", events)
    write_jsonl(raw_dir / "proposals.jsonl", proposals)
    write_jsonl(raw_dir / "conflicts.jsonl", conflicts)
    # Scenario snapshots are method-independent, so de-duplicate by fingerprint.
    unique_scenarios = {
        str(row["scenario_fingerprint"]): row for row in scenarios
    }
    write_jsonl(raw_dir / "scenarios.jsonl", unique_scenarios.values())
    write_compound_timeline_csv(raw_dir / "compound_timeline.csv", timeline)
    return metrics


def _failed_metric(
    snapshot: ConflictScenarioSnapshot,
    method: str,
    error: Exception,
    *,
    scenario: str | None = None,
) -> Exp2RunMetrics:
    now = perf_counter()
    return Exp2RunMetrics(
        run_id=f"{scenario or snapshot.scenario}:seed={snapshot.seed}:method={method}",
        seed=snapshot.seed,
        scenario=scenario or f"{snapshot.scenario}:pressure={snapshot.pressure:g}",
        method=method,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_control_plane_simulation",
        num_agents=len(snapshot.base.task.app_agents),
        num_edges=len(snapshot.base.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        num_proposals=len(snapshot.proposals),
        num_layers_involved=4,
        conflict_pressure=snapshot.pressure,
        ground_truth_conflict=snapshot.ground_truth.ground_truth_conflict,
        ground_truth_resolvable=snapshot.ground_truth.ground_truth_resolvable,
        conflict_detected=False,
        conflict_type="",
        conflict_resolved=False,
        candidate_combinations=0,
        rejected_combinations=0,
        selected_proposals="",
        rejected_proposals=0,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        transaction_latency_ms=0.0,
        total_decision_latency_ms=0.0,
        qos_satisfied=False,
        qos_satisfaction_rate=0.0,
        infeasible_configuration=True,
        post_execution_verification_success=False,
        application_rate_mbps=0.0,
        transport_rate_mbps=0.0,
        network_available_bandwidth_mbps=0.0,
        physical_available_capacity_mbps=0.0,
        latency_ms=0.0,
        packet_loss_rate=0.0,
        reliability=0.0,
        changed_actions=0,
        changed_rules=0,
        affected_gateways=0,
        control_messages=0,
        control_bytes=0,
        rollback_triggered=False,
        rollback_success=False,
        success=False,
        failure_reason=f"runner_error:{type(error).__name__}:{error}",
        event_occurred_at=now,
        decision_finished_at=now,
    )


def _scenario_config(config: dict[str, Any]) -> ConflictScenarioConfig:
    task = config.get("task", {})
    topology = config.get("topology", {})
    return ConflictScenarioConfig(
        num_agents=int(task.get("num_agents", 10)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", 4)),
        cross_gateway_edge_ratio=float(
            task.get("cross_gateway_edge_ratio", 0.5)
        ),
        multi_hop=bool(topology.get("multi_hop", True)),
    )


def parse_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, end = (int(part) for part in value.split(":", 1))
        if end < start:
            raise ValueError("seed range end must be >= start")
        return tuple(range(start, end + 1))
    return tuple(int(part) for part in value.split(",") if part.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run four-layer cross-layer conflict experiment",
    )
    parser.add_argument(
        "--config",
        default="configs/exp2_cross_layer_conflict.yaml",
    )
    parser.add_argument(
        "--methods",
        default="proposed,independent,adjacent,no_verification",
    )
    parser.add_argument("--seeds", default="0:29")
    parser.add_argument("--output-dir", default="results/exp2")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    methods = tuple(value.strip() for value in args.methods.split(",") if value.strip())
    metrics = asyncio.run(
        run_experiment(
            config,
            methods,
            parse_seeds(args.seeds),
            Path(args.output_dir),
        )
    )
    print(
        f"Wrote {len(metrics)} experiment-2 runs to "
        f"{(Path(args.output_dir) / 'raw').resolve()}"
    )


if __name__ == "__main__":
    main()
