from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

import yaml

from src.controller.failure_recovery import (
    FullRebuildFailureStrategy,
    ProposedCrossLayerElasticStrategy,
    ReactiveNetworkOnlyStrategy,
    RecoveryPlanningResult,
    WithoutScopeIdentificationStrategy,
    WithoutVerificationRollbackStrategy,
)
from src.controller.impact import ImpactScopeAnalyzer
from src.controller.transaction_executor import TransactionExecutionResult, TransactionExecutor
from src.core.events import EventLogRecord, EventStage, RuntimeEvent
from src.core.failures import FaultContext, RecoveryProbeSample, RecoveryTimelineSample
from src.core.models import TaskSubnet, to_jsonable
from src.core.rules import RuleDelta
from src.e2e.failure_verifier import (
    FaultAwareTaskSubnetVerifier,
    StructuralTaskSubnetVerifier,
)
from src.metrics.exp4 import (
    Exp4RunMetrics,
    write_exp4_metrics_csv,
    write_jsonl,
    write_probe_samples_csv,
    write_timeline_csv,
)
from src.simulation.continuous_probe import ContinuousBusinessProbe
from src.simulation.failure_scenario_generator import (
    AGENT_FAILURE_LEVELS,
    LINK_FAILURE_LEVELS,
    PHYSICAL_DROP_LEVELS,
    FailureScenarioConfig,
    FailureScenarioGenerator,
    FaultScenarioSnapshot,
    SimulationMonotonicClock,
)


MAIN_METHODS = ("proposed", "full_rebuild", "network_only")
ABLATION_METHODS = ("no_scope", "no_verification_rollback")


async def run_experiment(
    config: dict[str, Any],
    *,
    methods: tuple[str, ...] = MAIN_METHODS,
    seeds: tuple[int, ...] = tuple(range(30)),
    output_dir: Path = Path("results/exp4"),
    include_ablations: bool = False,
    include_timeline: bool = True,
) -> list[Exp4RunMetrics]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    generator = FailureScenarioGenerator()
    metrics: list[Exp4RunMetrics] = []
    event_rows: list[dict[str, object]] = []
    proposal_rows: list[dict[str, object]] = []
    scenario_rows: list[dict[str, object]] = []
    probe_rows: list[RecoveryProbeSample] = []
    sequence = 0

    points = tuple(_main_points(config))
    for scenario_config in points:
        for seed in seeds:
            snapshot = generator.generate(scenario_config, seed)
            scenario_rows.append(
                {
                    **snapshot.to_dict(),
                    "scenario": snapshot.scenario,
                    "scenario_fingerprint": snapshot.fingerprint,
                    "fault_fingerprint": snapshot.fault_fingerprint,
                }
            )
            for method in methods:
                sequence += 1
                try:
                    row, events, proposals, probes = await run_one(
                        snapshot,
                        method,
                        run_sequence=sequence,
                    )
                except Exception as error:  # all failed samples remain in raw data
                    row = _failed_run(snapshot, method, error)
                    events = []
                    proposals = []
                    probes = []
                metrics.append(row)
                event_rows.extend(events)
                proposal_rows.extend(proposals)
                probe_rows.extend(probes)

    if include_ablations:
        representatives = tuple(_ablation_points(config))
        for scenario_config in representatives:
            for seed in seeds:
                snapshot = generator.generate(scenario_config, seed)
                scenario_rows.append(
                    {
                        **snapshot.to_dict(),
                        "scenario": snapshot.scenario,
                        "scenario_fingerprint": snapshot.fingerprint,
                        "fault_fingerprint": snapshot.fault_fingerprint,
                        "ablation": True,
                    }
                )
                for method in ABLATION_METHODS:
                    sequence += 1
                    try:
                        row, events, proposals, probes = await run_one(
                            snapshot,
                            method,
                            run_sequence=sequence,
                        )
                    except Exception as error:
                        row = _failed_run(snapshot, method, error)
                        events, proposals, probes = [], [], []
                    metrics.append(row)
                    event_rows.extend(events)
                    proposal_rows.extend(proposals)
                    probe_rows.extend(probes)

    timeline_rows: list[RecoveryTimelineSample] = []
    if include_timeline:
        for seed in seeds:
            for method in MAIN_METHODS:
                timeline_rows.extend(await _compound_timeline(seed, method))

    metrics.sort(key=lambda item: (item.scenario, item.seed, item.method))
    event_rows.sort(
        key=lambda item: (
            str(item.get("experiment_run_id", "")),
            float(item.get("timestamp", 0.0)),
            str(item.get("event_stage", "")),
        )
    )
    proposal_rows.sort(
        key=lambda item: (
            str(item.get("run_id", "")),
            str(item.get("proposal", {}).get("proposal_id", "")),
        )
    )
    probe_rows.sort(key=lambda item: (item.run_id, item.timestamp, item.edge_id, item.phase))
    write_exp4_metrics_csv(raw_dir / "runs.csv", metrics)
    write_jsonl(raw_dir / "events.jsonl", event_rows)
    write_jsonl(raw_dir / "proposals.jsonl", proposal_rows)
    write_jsonl(raw_dir / "scenarios.jsonl", _deduplicate_scenarios(scenario_rows))
    write_probe_samples_csv(raw_dir / "probe_samples.csv", probe_rows)
    write_timeline_csv(raw_dir / "timeline.csv", timeline_rows)
    return metrics


async def run_one(
    snapshot: FaultScenarioSnapshot,
    method: str,
    *,
    run_sequence: int,
) -> tuple[
    Exp4RunMetrics,
    list[dict[str, object]],
    list[dict[str, object]],
    list[RecoveryProbeSample],
]:
    controller, initial_verifier, metric_provider = snapshot.instantiate()
    stable, formation = await controller.build_task_subnet(
        snapshot.task,
        verifier=initial_verifier,
        run_id=run_sequence,
        seed=snapshot.seed,
    )
    if not formation.networking_success:
        raise RuntimeError("initial transactional build failed:" + formation.failure_reason)
    old_version = stable.version
    event, context = snapshot.apply_fault(controller, stable)
    run_id = f"{snapshot.scenario}:seed={snapshot.seed}:method={method}"
    true_verifier = FaultAwareTaskSubnetVerifier(context)
    before_fault = _baseline_metrics(stable, context)
    during_result = await true_verifier.verify(stable)
    during = _verify_metrics(during_result, context)

    clock = SimulationMonotonicClock(snapshot.failure_detected_at)
    strategy = _strategy(method, clock)
    planning = await strategy.plan(controller, stable, event, context)
    if snapshot.config.post_plan_state_drift and planning.plan is not None:
        # Both ablations receive the same post-observation state drift.  The
        # normal verifier detects it and rolls back; the no-verification/no-
        # rollback variant can commit the stale candidate and is judged only by
        # the external stable-state verifier below.
        drift_session = next(
            (
                item
                for item in planning.plan.target_state.sessions
                if item.business_edge_id in context.affected_edge_ids
            ),
            planning.plan.target_state.sessions[0],
        )
        drift_agent = drift_session.p_agent_ids[0]
        context = replace(
            context,
            physical_agent_id=drift_agent,
            physical_capacity_mbps=0.01,
            metadata={
                **context.metadata,
                "post_plan_state_drift": True,
                "post_plan_drift_physical_agent": drift_agent,
            },
        )
        true_verifier = FaultAwareTaskSubnetVerifier(context)
    scope = (
        planning.plan.impact_scope
        if planning.plan is not None
        else ImpactScopeAnalyzer().analyze_failure(stable, event)
    )
    event_records = _planning_event_records(
        run_sequence,
        event,
        stable.version,
        stable.version + 1,
        planning,
    )
    probe: ContinuousBusinessProbe | None = None
    execution: TransactionExecutionResult | None = None
    post_result = during_result
    if planning.plan is not None:
        probe = ContinuousBusinessProbe(
            controller,
            stable,
            scope.unaffected_business_edges,
            metric_provider=None,
            interval_ms=snapshot.config.probe_interval_ms,
            healthy_samples_required=snapshot.config.stable_health_windows,
            clock=clock,
        )
        transaction_verifier = (
            StructuralTaskSubnetVerifier()
            if method == "no_verification_rollback"
            else true_verifier
        )
        execution = await TransactionExecutor(
            controller,
            verifier=transaction_verifier,
            clock=clock,
        ).execute(
            stable,
            planning.plan,
            event,
            run_id=run_sequence,
            seed=snapshot.seed,
            probe=probe,
            received_at=snapshot.failure_detected_at,
            pre_stage_records=tuple(event_records),
        )
        post_result = await FaultAwareTaskSubnetVerifier(context).verify(execution.state)

    committed = execution is not None and execution.success
    final_state = execution.state if execution is not None else stable
    stable_versions_consistent, staged_empty = _gateway_transaction_invariants(
        controller,
        final_state,
    )
    residual_rules = (
        len(execution.detected_residual_rule_ids)
        if execution is not None
        else 0
    )
    qos_recovered = post_result.ok
    recovery_success = bool(
        committed
        and qos_recovered
        and stable_versions_consistent
        and staged_empty
        and residual_rules == 0
        and final_state.version == old_version + 1
    )
    stable_verify_at = (
        execution.timestamp(EventStage.POST_ACTIVATE_VERIFY_FINISHED, last=True)
        if execution is not None and execution.success
        else 0.0
    )
    rollback_at = (
        execution.timestamp(EventStage.ROLLBACK_FINISHED, last=True)
        if execution is not None and execution.rollback_triggered
        else 0.0
    )
    service_recovered_at = (
        stable_verify_at
        + (snapshot.config.stable_health_windows - 1)
        * snapshot.config.probe_interval_ms
        / 1000.0
        if recovery_success
        else 0.0
    )
    fault_was_disruptive = not during_result.ok
    recovery_latency = (
        _duration_ms(snapshot.fault_effective_at, service_recovered_at)
        if recovery_success
        else 0.0
    )
    repair_latency = (
        _duration_ms(snapshot.failure_detected_at, service_recovered_at)
        if recovery_success
        else 0.0
    )
    service_interruption = recovery_latency if fault_was_disruptive and recovery_success else 0.0
    failure_handling = (
        _duration_ms(snapshot.fault_effective_at, rollback_at)
        if rollback_at
        else 0.0
    )

    probe_summary = execution.probe_summary if execution is not None else None
    probes = _recovery_probe_rows(
        run_id,
        snapshot,
        method,
        stable,
        context,
        during_result,
        post_result,
        execution,
        service_recovered_at,
    )
    after = _verify_metrics(post_result, context)
    plan = planning.plan
    changed = _changed_objects(stable, plan.target_state if plan else stable)
    delta = plan.rule_delta if plan is not None else RuleDelta()
    total_rules = max(1, len(stable.rules))
    affected_gateways = len(plan.affected_gateways) if plan else len(scope.affected_gateways)
    task_gateways = max(1, len(stable.involved_gateways | final_state.involved_gateways))
    stage_started = execution.timestamp(EventStage.STAGE_STARTED) if execution else 0.0
    stage_finished = execution.timestamp(EventStage.STAGE_FINISHED, last=True) if execution else 0.0
    activate_started = execution.timestamp(EventStage.ACTIVATE_STARTED) if execution else 0.0
    activate_finished = execution.timestamp(EventStage.ACTIVATE_FINISHED, last=True) if execution else 0.0
    staged_verify_started = execution.timestamp(EventStage.VERIFY_STARTED) if execution else 0.0
    staged_verify_finished = execution.timestamp(EventStage.VERIFY_FINISHED, last=True) if execution else 0.0
    post_verify_started = execution.timestamp(EventStage.POST_ACTIVATE_VERIFY_STARTED) if execution else 0.0
    failure_reason = planning.failure_reason
    if execution is not None and execution.failure_reason:
        failure_reason = execution.failure_reason
    elif committed and not qos_recovered:
        failure_reason = "post_execution_qos_verification_failed"

    row = Exp4RunMetrics(
        run_id=run_id,
        seed=snapshot.seed,
        scenario=snapshot.scenario,
        method=method,
        result_mode="in_memory_transactional_control_plane_simulation",
        scenario_fingerprint=snapshot.fingerprint,
        fault_fingerprint=snapshot.fault_fingerprint,
        fault_type=context.fault_type,
        fault_target=context.fault_target,
        fault_level=context.fault_level,
        fault_severity=context.severity,
        fault_effective_at=context.fault_effective_at,
        failure_detected_at=context.failure_detected_at,
        num_agents=len(stable.application_agents),
        num_edges=len(stable.business_edges),
        num_gateways=len(stable.involved_gateways),
        old_version=old_version,
        new_version=final_state.version,
        detection_latency_ms=_duration_ms(context.fault_effective_at, context.failure_detected_at),
        localization_latency_ms=planning.localization_latency_ms,
        proposal_latency_ms=planning.proposal_latency_ms,
        coordination_latency_ms=planning.coordination_latency_ms,
        feasibility_latency_ms=planning.feasibility_latency_ms,
        delta_compile_latency_ms=planning.delta_compile_latency_ms,
        stage_latency_ms=_duration_ms(stage_started, stage_finished),
        activation_latency_ms=_duration_ms(activate_started, activate_finished),
        verification_latency_ms=(
            _duration_ms(staged_verify_started, staged_verify_finished)
            + _duration_ms(post_verify_started, stable_verify_at)
        ),
        reconfiguration_latency_ms=_duration_ms(stage_started, activate_finished),
        repair_latency_ms=repair_latency,
        recovery_latency_ms=recovery_latency,
        failure_handling_ms=failure_handling,
        service_interruption_ms=service_interruption,
        recovery_success=recovery_success,
        qos_recovered=qos_recovered and recovery_success,
        safe_rejection=planning.safe_rejection,
        pre_execution_feasibility_checked=planning.pre_execution_feasibility_checked,
        post_execution_verification_success=post_result.ok,
        affected_agents=len(scope.affected_agents),
        affected_edges=len(scope.affected_business_edges),
        affected_sessions=len(scope.affected_sessions),
        affected_routes=len(scope.affected_routes),
        affected_gateways=affected_gateways,
        affected_physical_resources=len(scope.affected_physical_resources),
        changed_agents=changed["agents"],
        changed_sessions=changed["sessions"],
        changed_routes=changed["routes"],
        changed_physical_bindings=changed["physical"],
        changed_rules=len(delta.changed_rule_ids),
        rule_change_ratio=len(delta.changed_rule_ids) / total_rules,
        gateway_impact_ratio=affected_gateways / task_gateways,
        unaffected_edges=(probe_summary.unaffected_edges_total if probe_summary else len(scope.unaffected_business_edges)),
        unaffected_edges_interrupted=(probe_summary.unaffected_edges_interrupted if probe_summary else 0),
        collateral_interruption_rate=(
            probe_summary.unaffected_edges_interrupted / probe_summary.unaffected_edges_total
            if probe_summary and probe_summary.unaffected_edges_total
            else 0.0
        ),
        collateral_interruption_ms=(probe_summary.unaffected_interruption_ms if probe_summary else 0.0),
        collateral_latency_change_ms=(probe_summary.unaffected_latency_change_ms if probe_summary else 0.0),
        collateral_packet_loss_change=(probe_summary.unaffected_packet_loss_change if probe_summary else 0.0),
        packet_loss_during_recovery=during["loss"],
        throughput_before=before_fault["throughput"],
        throughput_during=during["throughput"],
        throughput_after=after["throughput"],
        latency_before=before_fault["latency"],
        latency_during=during["latency"],
        latency_after=after["latency"],
        rollback_triggered=(execution.rollback_triggered if execution else False),
        rollback_success=(execution.rollback_success if execution else False),
        residual_rules=residual_rules,
        control_messages=(execution.control_messages if execution else 0),
        control_bytes=(execution.control_bytes if execution else 0),
        selected_actions=";".join(item.action for item in planning.selected_proposals),
        selected_layers=";".join(sorted({item.layer for item in planning.selected_proposals if not item.is_keep})),
        transaction_executor=TransactionExecutor.__qualname__,
        verifier_mode=("structural_only_then_external_fault_verifier" if method == "no_verification_rollback" else true_verifier.mode),
        stable_versions_consistent=stable_versions_consistent,
        staged_rules_empty=staged_empty,
        service_recovered_at=service_recovered_at,
        stage_started_at=stage_started,
        activation_finished_at=activate_finished,
        stable_verification_finished_at=stable_verify_at,
        rollback_finished_at=rollback_at,
        success=recovery_success,
        failure_reason=failure_reason,
    )
    if execution is not None:
        all_records = list(execution.event_log)
    else:
        all_records = [
            EventLogRecord(
                run_id=run_sequence,
                event_id=event.event_id,
                task_id=event.task_id,
                old_version=old_version,
                new_version=old_version + 1,
                timestamp=event.occurred_at,
                component="FaultInjector",
                event_type=event.event_type,
                event_stage=EventStage.EVENT_OCCURRED.value,
                details=dict(event.payload),
            ),
            EventLogRecord(
                run_id=run_sequence,
                event_id=event.event_id,
                task_id=event.task_id,
                old_version=old_version,
                new_version=old_version + 1,
                timestamp=context.failure_detected_at,
                component="FailureMonitor",
                event_type=event.event_type,
                event_stage=EventStage.EVENT_RECEIVED.value,
                details={"method": method},
            ),
            *event_records,
        ]
    events = [
        {
            **to_jsonable(record),
            "experiment_run_id": run_id,
            "scenario": snapshot.scenario,
            "method": method,
        }
        for record in all_records
    ]
    # Full gateway snapshots are intentionally retained for rollback evidence.
    # Successful transactions are already represented by their stage/activate
    # records and run-level metrics; serializing the same complete rule stores
    # for every successful run only duplicates tens of megabytes of data.
    if execution is not None and execution.rollback_triggered:
        snapshot_timestamp = rollback_at or stable_verify_at or snapshot.failure_detected_at
        events.append(
            {
                "experiment_run_id": run_id,
                "event_id": event.event_id,
                "task_id": event.task_id,
                "timestamp": snapshot_timestamp,
                "component": "TransactionSnapshot",
                "event_type": context.fault_type,
                "event_stage": "TRANSACTION_SNAPSHOT",
                "details": {
                    "before": execution.before_snapshot,
                    "after": execution.after_snapshot,
                    "rollback_triggered": execution.rollback_triggered,
                    "rollback_success": execution.rollback_success,
                    "physical_before": to_jsonable(execution.physical_bindings_before),
                    "physical_after": to_jsonable(execution.physical_bindings_after),
                },
                "scenario": snapshot.scenario,
                "method": method,
            }
        )
    events.extend(
        {
            "experiment_run_id": run_id,
            "event_id": event.event_id,
            "task_id": event.task_id,
            "timestamp": sample.timestamp,
            "component": "FailureMonitor",
            "event_type": context.fault_type,
            "event_stage": "MONITOR_SAMPLE",
            "details": asdict(sample),
            "scenario": snapshot.scenario,
            "method": method,
        }
        for sample in snapshot.monitor_samples
    )
    proposals = [
        {
            "run_id": run_id,
            "scenario": snapshot.scenario,
            "method": method,
            "selected": item in planning.selected_proposals,
            "authorized": any(
                action.proposal_id == item.proposal_id
                for action in planning.authorized_actions
            ),
            "rejection_reason": planning.rejected_proposals.get(item.proposal_id, ""),
            "proposal": to_jsonable(item),
        }
        for item in planning.proposals
    ]
    return row, events, proposals, probes


def _main_points(config: dict[str, Any]) -> Iterable[FailureScenarioConfig]:
    task = config.get("task", {})
    monitor = config.get("monitor", {})
    base = {
        "num_agents": int(task.get("num_agents", 20)),
        "edge_ratio": float(task.get("edge_ratio", 1.5)),
        "num_gateways": int(task.get("num_gateways", 4)),
        "cross_gateway_edge_ratio": float(task.get("cross_gateway_edge_ratio", 0.5)),
        "multi_hop": bool(config.get("topology", {}).get("multi_hop", True)),
        "probe_interval_ms": float(monitor.get("probe_interval_ms", 50.0)),
        "detection_threshold": int(monitor.get("failure_detection_threshold", 3)),
        "stable_health_windows": int(monitor.get("stable_health_windows", 3)),
    }
    sweeps = config.get("fault_sweeps", {})
    for level in sweeps.get("agent_failure_levels", AGENT_FAILURE_LEVELS):
        yield FailureScenarioConfig(
            fault_type="AGENT_FAILURE",
            fault_level=str(level),
            severity=1.0,
            **base,
        )
    for factor in sweeps.get("link_capacity_factors", LINK_FAILURE_LEVELS):
        yield FailureScenarioConfig(
            fault_type="LINK_FAILURE",
            fault_level=f"capacity_factor_{float(factor):.1f}",
            severity=float(factor),
            **base,
        )
    for factor in sweeps.get("physical_capacity_factors", PHYSICAL_DROP_LEVELS):
        yield FailureScenarioConfig(
            fault_type="PHYSICAL_CAPACITY_DROP",
            fault_level=f"capacity_factor_{float(factor):.1f}",
            severity=float(factor),
            **base,
        )


def _ablation_points(config: dict[str, Any]) -> Iterable[FailureScenarioConfig]:
    points = list(_main_points(config))
    for fault_type in ("AGENT_FAILURE", "LINK_FAILURE", "PHYSICAL_CAPACITY_DROP"):
        candidates = [item for item in points if item.fault_type == fault_type]
        yield replace(candidates[len(candidates) // 2], post_plan_state_drift=True)


def _strategy(method: str, clock):
    if method == "proposed":
        return ProposedCrossLayerElasticStrategy(clock=clock)
    if method == "full_rebuild":
        return FullRebuildFailureStrategy(clock=clock)
    if method == "network_only":
        return ReactiveNetworkOnlyStrategy(clock=clock)
    if method == "no_scope":
        return WithoutScopeIdentificationStrategy(clock=clock)
    if method == "no_verification_rollback":
        return WithoutVerificationRollbackStrategy(clock=clock)
    raise ValueError(f"unknown failure-recovery method: {method}")


def _planning_event_records(
    run_sequence: int,
    event: RuntimeEvent,
    old_version: int,
    new_version: int,
    planning: RecoveryPlanningResult,
) -> list[EventLogRecord]:
    cursor = max(event.occurred_at, float(event.payload.get("failure_detected_at", 0.0)))
    # Detection is shared in the scenario; the caller's event payload may omit
    # it, so use occurrence + the measured fixed detection offset from planning.
    cursor = max(cursor, event.occurred_at)
    stages = (
        ("FAILURE_DETECTED", 0.0, "FailureMonitor"),
        (EventStage.SCOPE_STARTED.value, 0.0, "ImpactScopeAnalyzer"),
        (EventStage.SCOPE_FINISHED.value, planning.localization_latency_ms, "ImpactScopeAnalyzer"),
        ("PROPOSALS_GENERATED", planning.proposal_latency_ms, "LayerAgents"),
        ("COORDINATION_FINISHED", planning.coordination_latency_ms, "CrossLayerCoordinator"),
        ("FEASIBILITY_FINISHED", planning.feasibility_latency_ms, "RecoveryFeasibility"),
        (EventStage.DELTA_COMPILED.value, planning.delta_compile_latency_ms, "SubnetCompiler"),
    )
    # TransactionExecutor inserts these after EVENT_RECEIVED; start at the
    # method-independent detection instant carried in the caller.
    detection = float(event.payload.get("failure_detected_at", 0.0))
    if detection <= 0.0:
        detection = event.occurred_at + 0.150
    cursor = detection
    records = []
    for stage, duration_ms, component in stages:
        cursor += duration_ms / 1000.0
        records.append(
            EventLogRecord(
                run_id=run_sequence,
                event_id=event.event_id,
                task_id=event.task_id,
                old_version=old_version,
                new_version=new_version,
                timestamp=cursor,
                component=component,
                event_type=event.event_type,
                event_stage=stage,
                details={"method": planning.method},
            )
        )
    return records


def _gateway_transaction_invariants(controller, state: TaskSubnet) -> tuple[bool, bool]:
    gateways = state.involved_gateways
    versions = {
        controller.gateways[item].get_stable_version(state.task.task_id)
        for item in gateways
    }
    staged_empty = all(
        controller.gateways[item].get_staged_version(state.task.task_id) is None
        and not controller.gateways[item].get_staged_rules(state.task.task_id)
        for item in gateways
    )
    return len(versions) <= 1 and (not versions or next(iter(versions)) == state.version), staged_empty


def _changed_objects(before: TaskSubnet, after: TaskSubnet) -> dict[str, int]:
    before_agents = {
        **before.application_agents,
        **before.transport_agents,
        **before.network_agents,
        **before.physical_agents,
    }
    after_agents = {
        **after.application_agents,
        **after.transport_agents,
        **after.network_agents,
        **after.physical_agents,
    }
    return {
        "agents": _mapping_difference(before_agents, after_agents),
        "sessions": _mapping_difference(before.session_map, after.session_map),
        "routes": _mapping_difference(before.routes, after.routes),
        "physical": _mapping_difference(before.physical_bindings, after.physical_bindings),
    }


def _mapping_difference(left: dict, right: dict) -> int:
    return len(
        {
            key for key in set(left) | set(right) if left.get(key) != right.get(key)
        }
    )


def _verify_metrics(result, context: FaultContext) -> dict[str, float]:
    affected = [
        item
        for item in result.edge_results
        if _edge_id_from_session(item.session_id, context) in context.affected_edge_ids
    ]
    source = affected or list(result.edge_results)
    if not source:
        return {"throughput": 0.0, "latency": 0.0, "loss": 0.0}
    return {
        "throughput": sum(float(item.throughput_mbps or 0.0) for item in source),
        "latency": sum(float(item.latency_ms or 0.0) for item in source) / len(source),
        "loss": sum(float(item.loss_rate or 0.0) for item in source) / len(source),
    }


def _edge_id_from_session(session_id: str, context: FaultContext) -> str:
    return str(context.metadata.get("session_edge_map", {}).get(session_id, session_id))


def _baseline_metrics(stable: TaskSubnet, context: FaultContext) -> dict[str, float]:
    sessions = [
        item for item in stable.sessions if item.business_edge_id in context.affected_edge_ids
    ]
    if not sessions:
        sessions = list(stable.sessions)
    return {
        "throughput": sum(item.data_rate_mbps for item in sessions),
        "latency": sum(5.0 + max(0, len(item.gateway_path) - 1) * 4.0 for item in sessions)
        / max(1, len(sessions)),
        "loss": 0.001,
    }


def _recovery_probe_rows(
    run_id: str,
    snapshot: FaultScenarioSnapshot,
    method: str,
    stable: TaskSubnet,
    context: FaultContext,
    during_result,
    post_result,
    execution: TransactionExecutionResult | None,
    service_recovered_at: float,
) -> list[RecoveryProbeSample]:
    rows: list[RecoveryProbeSample] = []
    during_by_session = {item.session_id: item for item in during_result.edge_results}
    post_by_session = {item.session_id: item for item in post_result.edge_results}
    for session in stable.sessions:
        directly = session.business_edge_id in context.affected_edge_ids
        before = during_by_session.get(session.session_id)
        timestamps = (
            (context.fault_effective_at, "FAULT_EFFECTIVE", before),
            (context.failure_detected_at, "FAILURE_DETECTED", before),
        )
        for timestamp, phase, result in timestamps:
            rows.append(_probe_row(run_id, snapshot.seed, snapshot.scenario, method, session.business_edge_id, timestamp, phase, directly, result, session.data_rate_mbps))
        if execution is not None:
            for stage in (
                EventStage.STAGE_FINISHED,
                EventStage.ACTIVATE_FINISHED,
                EventStage.POST_ACTIVATE_VERIFY_FINISHED,
            ):
                timestamp = execution.timestamp(stage, last=True)
                if timestamp:
                    result = post_by_session.get(session.session_id) if stage != EventStage.STAGE_FINISHED else before
                    rows.append(_probe_row(run_id, snapshot.seed, snapshot.scenario, method, session.business_edge_id, timestamp, stage.value, directly, result, session.data_rate_mbps))
        if service_recovered_at and directly:
            result = next(
                (item for item in post_result.edge_results if item.source == session.source or item.target == session.target),
                None,
            )
            for window in range(snapshot.config.stable_health_windows):
                timestamp = service_recovered_at - (
                    snapshot.config.stable_health_windows - 1 - window
                ) * snapshot.config.probe_interval_ms / 1000.0
                rows.append(_probe_row(run_id, snapshot.seed, snapshot.scenario, method, session.business_edge_id, timestamp, f"HEALTH_WINDOW_{window + 1}", directly, result, session.data_rate_mbps))
    return rows


def _probe_row(run_id, seed, scenario, method, edge_id, timestamp, phase, directly, result, required):
    return RecoveryProbeSample(
        run_id=run_id,
        seed=seed,
        scenario=scenario,
        method=method,
        edge_id=edge_id,
        timestamp=timestamp,
        phase=phase,
        directly_affected=directly,
        reachable=bool(result.reachable) if result is not None else False,
        qos_satisfied=bool(result.ok) if result is not None else False,
        throughput_mbps=float(result.throughput_mbps or 0.0) if result is not None else 0.0,
        latency_ms=float(result.latency_ms or 0.0) if result is not None else 0.0,
        packet_loss_rate=float(result.loss_rate or 0.0) if result is not None else 1.0,
    )


async def _compound_timeline(seed: int, method: str) -> list[RecoveryTimelineSample]:
    """Build the display timeline from a real strategy/transaction execution.

    Exogenous demand/capacity events are shared formulas.  The selected action,
    commit result and final throughput come from ``run_one``; no method-specific
    curve or multiplier is used.
    """

    snapshot = FailureScenarioGenerator().generate(
        FailureScenarioConfig(
            fault_type="PHYSICAL_CAPACITY_DROP",
            fault_level="compound_capacity_factor_0.4",
            severity=0.4,
            num_agents=20,
            num_gateways=4,
        ),
        seed,
    )
    row, _events, _proposals, _probes = await run_one(
        snapshot,
        method,
        run_sequence=900000 + seed * 10 + MAIN_METHODS.index(method),
    )
    baseline_demand = max(row.throughput_before, 1e-9)
    demand = baseline_demand * 1.25
    baseline_network = demand * 1.8
    utilized_network = demand * 1.1
    dropped_physical = max(row.throughput_during, baseline_demand * 0.4)
    degraded_network = demand * 0.65
    final_throughput = row.throughput_after
    final_network = max(utilized_network, final_throughput)
    final_physical = max(dropped_physical, final_throughput)
    activated = row.new_version == row.old_version + 1
    action = row.selected_actions
    definitions = (
        (0.0, "STABLE", baseline_demand, baseline_network, baseline_network, row.throughput_before, False, False),
        (2.0, "DEMAND_INCREASE", demand, baseline_network, baseline_network, min(demand, baseline_network), False, False),
        (4.0, "NETWORK_UTILIZATION_RISE", demand, utilized_network, baseline_network, min(demand, utilized_network), False, False),
        (6.0, "PHYSICAL_CAPACITY_DROP", demand, utilized_network, dropped_physical, min(demand, utilized_network, dropped_physical), False, False),
        (8.0, "ROUTE_DEGRADED", demand, degraded_network, dropped_physical, min(demand, degraded_network, dropped_physical), True, False),
        (8.2, "RECOVERY_DECISION", demand, degraded_network, dropped_physical, min(demand, degraded_network, dropped_physical), True, False),
        (8.4, "ACTIVATION_RESULT", baseline_demand, final_network, final_physical, final_throughput, True, activated),
        (8.6, "STABLE_HEALTH_WINDOW", baseline_demand, final_network, final_physical, final_throughput, True, activated),
    )
    output = []
    for logical_time, phase, app_rate, network, physical, throughput, detected, is_activated in definitions:
        qos = throughput + 1e-9 >= app_rate
        output.append(
            RecoveryTimelineSample(
                run_id=f"compound:seed={seed}:method={method}",
                seed=seed,
                method=method,
                logical_time_s=logical_time,
                phase=phase,
                application_demand_mbps=app_rate,
                actual_throughput_mbps=throughput,
                network_capacity_mbps=network,
                physical_capacity_mbps=physical,
                latency_ms=10.0 + max(0.0, app_rate - throughput) * 3.0,
                qos_satisfied=qos,
                failure_detected=detected,
                action_selected=action if detected else "",
                activated=is_activated,
                recovered=phase == "STABLE_HEALTH_WINDOW" and row.recovery_success and qos,
            )
        )
    return output


def _failed_run(snapshot: FaultScenarioSnapshot, method: str, error: Exception) -> Exp4RunMetrics:
    values: dict[str, Any] = {field: 0 for field in Exp4RunMetrics.__dataclass_fields__}
    values.update(
        run_id=f"{snapshot.scenario}:seed={snapshot.seed}:method={method}",
        seed=snapshot.seed,
        scenario=snapshot.scenario,
        method=method,
        result_mode="in_memory_transactional_control_plane_simulation",
        scenario_fingerprint=snapshot.fingerprint,
        fault_fingerprint=snapshot.fault_fingerprint,
        fault_type=snapshot.config.fault_type,
        fault_target=snapshot.target_agent_id,
        fault_level=snapshot.config.fault_level,
        fault_severity=snapshot.config.severity,
        fault_effective_at=snapshot.fault_effective_at,
        failure_detected_at=snapshot.failure_detected_at,
        num_agents=snapshot.config.num_agents,
        num_edges=len(snapshot.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        success=False,
        recovery_success=False,
        qos_recovered=False,
        failure_reason=f"runner_error:{type(error).__name__}:{error}",
        transaction_executor=TransactionExecutor.__qualname__,
        verifier_mode="runner_error",
        selected_actions="",
        selected_layers="",
    )
    return Exp4RunMetrics(**values)


def _duration_ms(start: float, finish: float) -> float:
    if start <= 0.0 or finish <= 0.0:
        return 0.0
    return max(0.0, (finish - start) * 1000.0)


def _deduplicate_scenarios(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    output = {}
    for row in rows:
        output[
            (
                str(row["scenario"]),
                int(row["seed"]),
                str(row["scenario_fingerprint"]),
            )
        ] = row
    return [output[key] for key in sorted(output)]


def parse_seed_range(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        if finish < start:
            raise ValueError("seed range finish must be >= start")
        return tuple(range(start, finish + 1))
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run fault-driven elastic reconfiguration experiment")
    parser.add_argument("--config", default="configs/exp4_failure_reconfiguration.yaml")
    parser.add_argument("--methods", default=",".join(MAIN_METHODS))
    parser.add_argument("--seeds", default="0:29")
    parser.add_argument("--output-dir", default="results/exp4")
    parser.add_argument("--include-ablations", action="store_true")
    parser.add_argument("--skip-timeline", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    methods = tuple(item.strip() for item in args.methods.split(",") if item.strip())
    rows = asyncio.run(
        run_experiment(
            config,
            methods=methods,
            seeds=parse_seed_range(args.seeds),
            output_dir=Path(args.output_dir),
            include_ablations=args.include_ablations,
            include_timeline=not args.skip_timeline,
        )
    )
    successful = sum(item.success for item in rows)
    print(
        f"Experiment 4 wrote {len(rows)} raw runs to {Path(args.output_dir).resolve()} "
        f"(successful={successful}, failed={len(rows) - successful})"
    )


if __name__ == "__main__":
    main()
