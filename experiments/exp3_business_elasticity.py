from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import resource
from dataclasses import asdict
from pathlib import Path
from time import perf_counter, process_time
from typing import Any, Iterable

import yaml

from src.controller.impact import ImpactScopeAnalyzer
from src.controller.strategies import make_strategy
from src.controller.transaction_executor import TransactionExecutor
from src.core.events import EventInjector, EventLogRecord, EventStage, RuntimeEvent
from src.metrics.exp3 import (
    Exp3RunMetrics,
    write_exp3_events_jsonl,
    write_exp3_metrics_csv,
    write_probe_samples_csv,
)
from src.simulation.continuous_probe import BusinessProbeSample, ContinuousBusinessProbe
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
    ScenarioSnapshot,
)


class TimedImpactScopeAnalyzer(ImpactScopeAnalyzer):
    def __init__(self) -> None:
        self.started_at = 0.0
        self.finished_at = 0.0

    def analyze(self, subnet, event):
        self.started_at = perf_counter()
        result = super().analyze(subnet, event)
        self.finished_at = perf_counter()
        return result


async def run_method(
    snapshot: ScenarioSnapshot,
    method: str,
    scenario: str,
    *,
    run_index: int,
    probe_interval_ms: float,
) -> tuple[
    Exp3RunMetrics,
    tuple[EventLogRecord, ...],
    tuple[BusinessProbeSample, ...],
]:
    controller, verifier, _provider = snapshot.instantiate()
    initial, formation = await controller.build_task_subnet(
        snapshot.task,
        verifier=verifier,
        run_id=run_index,
        seed=snapshot.seed,
    )
    if not formation.networking_success:
        raise RuntimeError("initial transactional build failed: " + formation.failure_reason)

    before_rule_count = sum(
        len(gateway.get_stable_rules(snapshot.task.task_id))
        for gateway in controller.gateways.values()
    )
    injector = EventInjector(prefix=f"exp3-{method}-{snapshot.seed}")
    event = injector.agent_remove_many(
        snapshot.task.task_id,
        snapshot.removed_agent_ids,
    )
    event_received_at = perf_counter()
    cpu_started = process_time()
    wall_started = perf_counter()
    analyzer = TimedImpactScopeAnalyzer()
    strategy = make_strategy(method, controller, analyzer)
    plan_started_at = perf_counter()
    plan = await strategy.plan(initial, event)
    plan_finished_at = perf_counter()
    scope = plan.impact_scope
    pre_records = (
        _event_record(
            run_index,
            event,
            initial.version,
            plan.target_state.version,
            EventStage.SCOPE_STARTED.value,
            analyzer.started_at,
            "ImpactScopeAnalyzer",
            {"agent_ids": list(event.agent_ids)},
        ),
        _event_record(
            run_index,
            event,
            initial.version,
            plan.target_state.version,
            EventStage.SCOPE_FINISHED.value,
            analyzer.finished_at,
            "ImpactScopeAnalyzer",
            {
                "affected_edges": len(scope.affected_business_edges),
                "affected_gateways": len(scope.affected_gateways),
            },
        ),
        _event_record(
            run_index,
            event,
            initial.version,
            plan.target_state.version,
            EventStage.DELTA_COMPILED.value,
            plan_finished_at,
            "ElasticUpdateStrategy",
            {
                "method": plan.method,
                "added_rules": len(plan.rule_delta.additions),
                "updated_rules": len(plan.rule_delta.updates),
                "deleted_rules": len(plan.rule_delta.deletions),
            },
        ),
    )
    probe = ContinuousBusinessProbe(
        controller,
        initial,
        scope.unaffected_business_edges,
        interval_ms=probe_interval_ms,
    )
    execution = await TransactionExecutor(
        controller,
        verifier=verifier,
    ).execute(
        initial,
        plan,
        event,
        run_id=run_index,
        seed=snapshot.seed,
        probe=probe,
        received_at=event_received_at,
        pre_stage_records=pre_records,
    )
    wall_finished = perf_counter()
    cpu_finished = process_time()
    actual = execution.state
    after_rule_count = sum(
        len(gateway.get_stable_rules(snapshot.task.task_id))
        for gateway in controller.gateways.values()
    )
    actual_residuals = {
        rule.rule_id
        for gateway in controller.gateways.values()
        for rule in gateway.get_stable_rules(snapshot.task.task_id).values()
        if rule.src_agent in set(event.agent_ids)
        or rule.dst_agent in set(event.agent_ids)
    }
    residual_count = (
        len(actual_residuals)
        if execution.success
        else len(execution.detected_residual_rule_ids)
    )

    stage_started = execution.timestamp(EventStage.STAGE_STARTED)
    stage_finished = execution.timestamp(EventStage.STAGE_FINISHED)
    verify_started = execution.timestamp(EventStage.VERIFY_STARTED)
    verify_finished = execution.timestamp(EventStage.VERIFY_FINISHED)
    stable_verify_started = execution.timestamp(EventStage.POST_ACTIVATE_VERIFY_STARTED)
    stable_verify_finished = execution.timestamp(
        EventStage.POST_ACTIVATE_VERIFY_FINISHED
    )
    activate_started = execution.timestamp(EventStage.ACTIVATE_STARTED)
    activate_finished = execution.timestamp(EventStage.ACTIVATE_FINISHED)
    rollback_started = execution.timestamp(EventStage.ROLLBACK_STARTED)
    rollback_finished = execution.timestamp(EventStage.ROLLBACK_FINISHED, last=True)
    changed_rules = (
        len(plan.rule_delta.additions)
        + len(plan.rule_delta.updates)
        + len(plan.rule_delta.deletions)
    )
    probe_summary = execution.probe_summary
    wall_elapsed = max(1e-12, wall_finished - wall_started)
    cpu_percent = max(0.0, (cpu_finished - cpu_started) / wall_elapsed * 100.0)
    memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    metrics = Exp3RunMetrics(
        run_id=f"{scenario}:seed={snapshot.seed}:method={method}",
        seed=snapshot.seed,
        method=method,
        scenario=scenario,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_simulation",
        num_agents_before=len(initial.application_agents),
        num_agents_after=len(actual.application_agents),
        num_edges_before=len(initial.business_edges),
        num_edges_after=len(actual.business_edges),
        num_gateways=snapshot.config.num_gateways,
        cross_gateway_edge_ratio=snapshot.actual_cross_gateway_edge_ratio,
        agent_removal_ratio=(
            len(snapshot.removed_agent_ids) / len(snapshot.task.app_agents)
        ),
        removed_agent_type=snapshot.config.removed_agent_type,
        removed_agent_ids=";".join(snapshot.removed_agent_ids),
        old_version=initial.version,
        new_version=plan.target_state.version,
        success=execution.success,
        failure_reason=execution.failure_reason,
        elastic_latency_ms=(
            _duration_ms(event.occurred_at, stable_verify_finished)
            if execution.success
            else 0.0
        ),
        failure_handling_ms=(
            _duration_ms(event.occurred_at, rollback_finished)
            if execution.rollback_triggered
            else 0.0
        ),
        scope_latency_ms=_duration_ms(analyzer.started_at, analyzer.finished_at),
        plan_latency_ms=_duration_ms(plan_started_at, plan_finished_at),
        compile_delta_latency_ms=_duration_ms(
            analyzer.finished_at,
            plan_finished_at,
        ),
        stage_latency_ms=_duration_ms(stage_started, stage_finished),
        verification_latency_ms=(
            _duration_ms(verify_started, verify_finished)
            + _duration_ms(stable_verify_started, stable_verify_finished)
        ),
        activation_latency_ms=_duration_ms(activate_started, activate_finished),
        rollback_latency_ms=_duration_ms(rollback_started, rollback_finished),
        affected_agents=len(scope.affected_agents),
        affected_edges=len(scope.affected_business_edges),
        affected_sessions=len(scope.affected_sessions),
        affected_routes=len(scope.affected_routes),
        affected_gateways=len(plan.affected_gateways),
        affected_physical_resources=len(scope.affected_physical_resources),
        physical_bindings_before=len(execution.physical_bindings_before),
        physical_bindings_after=len(execution.physical_bindings_after),
        released_physical_resources=len(execution.released_physical_resources),
        residual_physical_resources=len(execution.residual_physical_resources),
        total_rules_before=before_rule_count,
        total_rules_after=after_rule_count,
        added_rules=len(plan.rule_delta.additions),
        updated_rules=len(plan.rule_delta.updates),
        deleted_rules=len(plan.rule_delta.deletions),
        changed_rules_ratio=(changed_rules / before_rule_count if before_rule_count else 0.0),
        residual_rules=residual_count,
        control_messages=execution.control_messages,
        control_bytes=execution.control_bytes,
        unaffected_edges_total=probe_summary.unaffected_edges_total,
        unaffected_edges_interrupted=probe_summary.unaffected_edges_interrupted,
        unaffected_interrupted_rate=(
            probe_summary.unaffected_edges_interrupted
            / probe_summary.unaffected_edges_total
            if probe_summary.unaffected_edges_total
            else 0.0
        ),
        unaffected_interruption_ms=probe_summary.unaffected_interruption_ms,
        unaffected_latency_change_ms=probe_summary.unaffected_latency_change_ms,
        unaffected_packet_loss_change=probe_summary.unaffected_packet_loss_change,
        rollback_triggered=execution.rollback_triggered,
        rollback_success=execution.rollback_success,
        controller_cpu_percent=cpu_percent,
        controller_memory_mb=memory_mb,
        event_occurred_at=event.occurred_at,
        event_received_at=event_received_at,
        scope_finished_at=analyzer.finished_at,
        plan_finished_at=plan_finished_at,
        stage_finished_at=stage_finished,
        verification_finished_at=verify_finished,
        activation_finished_at=activate_finished,
        stable_verification_finished_at=stable_verify_finished,
        rollback_finished_at=rollback_finished,
    )
    return metrics, execution.event_log, execution.probe_summary.samples


async def run_experiment(
    config: dict[str, Any],
    *,
    methods: tuple[str, ...],
    seeds: tuple[int, ...],
    output_dir: Path,
) -> list[Exp3RunMetrics]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    generator = ElasticScenarioGenerator()
    metrics: list[Exp3RunMetrics] = []
    events: list[tuple[str, str, EventLogRecord]] = []
    probes: list[tuple[str, str, str, BusinessProbeSample]] = []
    scenarios: dict[str, dict[str, Any]] = {}
    probe_interval_ms = float(config.get("runtime", {}).get("probe_interval_ms", 10.0))
    timeout_ms = float(config.get("simulation", {}).get("timeout_ms", 10000.0))
    run_index = 0
    for scenario, scenario_config in _scenario_points(config):
        for seed in seeds:
            snapshot = generator.generate(scenario_config, seed)
            scenarios[f"{scenario}:seed={seed}"] = snapshot.to_dict()
            for method in methods:
                run_index += 1
                try:
                    result, run_events, run_probes = await asyncio.wait_for(
                        run_method(
                            snapshot,
                            method,
                            scenario,
                            run_index=run_index,
                            probe_interval_ms=probe_interval_ms,
                        ),
                        timeout=timeout_ms / 1000.0,
                    )
                except Exception as error:
                    result = _failed_run_record(snapshot, method, scenario, error)
                    run_events = ()
                    run_probes = ()
                metrics.append(result)
                events.extend((method, scenario, item) for item in run_events)
                probes.extend(
                    (result.run_id, method, scenario, item) for item in run_probes
                )
    write_exp3_metrics_csv(raw_dir / "runs.csv", metrics)
    write_exp3_events_jsonl(raw_dir / "events.jsonl", events)
    write_probe_samples_csv(raw_dir / "probe_samples.csv", probes)
    with (raw_dir / "scenario_snapshots.json").open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        json.dump(scenarios, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return metrics


def _scenario_points(config: dict[str, Any]) -> Iterable[tuple[str, ScenarioConfig]]:
    task = config.get("task", {})
    topology = config.get("topology", {})
    default = ScenarioConfig(
        num_agents=int(task.get("num_agents", 20)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", topology.get("num_gateways", 4))),
        cross_gateway_edge_ratio=float(task.get("cross_gateway_edge_ratio", 0.5)),
        agent_removal_ratio=float(task.get("agent_removal_ratio", 0.1)),
        removed_agent_type=str(task.get("removed_agent_type", "leaf")),
        dag_type=str(task.get("dag_type", "random")),
        multi_hop=bool(topology.get("multi_hop", True)),
    )
    sweeps = config.get("sweeps", {})
    definitions = (
        ("agent_count", "num_agents", sweeps.get("agent_counts", [default.num_agents])),
        ("gateway_count", "num_gateways", sweeps.get("gateway_counts", [default.num_gateways])),
        (
            "cross_gateway_ratio",
            "cross_gateway_edge_ratio",
            sweeps.get("cross_gateway_edge_ratios", [default.cross_gateway_edge_ratio]),
        ),
        (
            "removal_ratio",
            "agent_removal_ratio",
            sweeps.get("agent_removal_ratios", [default.agent_removal_ratio]),
        ),
        (
            "removal_position",
            "removed_agent_type",
            sweeps.get("removed_agent_types", [default.removed_agent_type]),
        ),
    )
    for family, field, values in definitions:
        for value in values:
            row = asdict(default)
            row[field] = value
            yield f"{family}:{value}", ScenarioConfig(**row)


def _failed_run_record(
    snapshot: ScenarioSnapshot,
    method: str,
    scenario: str,
    error: Exception,
) -> Exp3RunMetrics:
    values: dict[str, Any] = {
        field: 0 for field in Exp3RunMetrics.__dataclass_fields__
    }
    values.update(
        run_id=f"{scenario}:seed={snapshot.seed}:method={method}",
        seed=snapshot.seed,
        method=method,
        scenario=scenario,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_simulation",
        num_agents_before=snapshot.config.num_agents,
        num_agents_after=snapshot.config.num_agents,
        num_edges_before=len(snapshot.task.biz_edges),
        num_edges_after=len(snapshot.task.biz_edges),
        num_gateways=snapshot.config.num_gateways,
        cross_gateway_edge_ratio=snapshot.actual_cross_gateway_edge_ratio,
        agent_removal_ratio=(
            len(snapshot.removed_agent_ids) / len(snapshot.task.app_agents)
        ),
        removed_agent_type=snapshot.config.removed_agent_type,
        removed_agent_ids=";".join(snapshot.removed_agent_ids),
        old_version=0,
        new_version=0,
        success=False,
        failure_reason=f"{type(error).__name__}: {error}",
        rollback_triggered=False,
        rollback_success=False,
    )
    return Exp3RunMetrics(**values)


def _event_record(
    run_id: int,
    event: RuntimeEvent,
    old_version: int,
    new_version: int,
    stage: str,
    timestamp: float,
    component: str,
    details: dict[str, Any],
) -> EventLogRecord:
    return EventLogRecord(
        run_id=run_id,
        event_id=event.event_id,
        task_id=event.task_id,
        old_version=old_version,
        new_version=new_version,
        timestamp=timestamp,
        component=component,
        event_type=event.event_type,
        event_stage=stage,
        details=details,
    )


def _duration_ms(start: float, finish: float) -> float:
    if start <= 0.0 or finish <= 0.0:
        return 0.0
    return max(0.0, (finish - start) * 1000.0)


def parse_seed_range(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        if finish < start:
            raise ValueError("seed range finish must be >= start")
        return tuple(range(start, finish + 1))
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run experiment 3 Agent-removal elasticity")
    parser.add_argument("--config", default="configs/exp3_business_elasticity.yaml")
    parser.add_argument(
        "--methods",
        default="proposed,full_rebuild,local_only",
        help="Comma-separated strategy names",
    )
    parser.add_argument("--seeds", default="0:29")
    parser.add_argument("--output-dir", default="results/exp3")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    methods = tuple(item.strip() for item in args.methods.split(",") if item.strip())
    seeds = parse_seed_range(args.seeds)
    results = asyncio.run(
        run_experiment(
            config,
            methods=methods,
            seeds=seeds,
            output_dir=Path(args.output_dir),
        )
    )
    success = sum(item.success for item in results)
    print(
        f"Experiment 3 wrote {len(results)} raw runs to {Path(args.output_dir).resolve()} "
        f"(successful={success}, failed={len(results) - success})"
    )


if __name__ == "__main__":
    main()
