from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable

from src.controller.impact import ImpactScope, ImpactScopeAnalyzer
from src.controller.strategies import ProposedIncrementalStrategy
from src.controller.transaction_executor import TransactionExecutor
from src.core.events import EventLogRecord, EventStage, RuntimeEvent, RuntimeEventType
from src.core.models import GatewayRouteEntry, TaskState, TaskSubnet
from src.core.rules import RuleDelta
from src.metrics.collector import AgentRemovalMetrics, MetricsCollector
from src.simulation.continuous_probe import ContinuousBusinessProbe

if TYPE_CHECKING:
    from src.controller.networking import AgentController
    from src.e2e.verifiers import TaskSubnetVerifier


@dataclass(frozen=True)
class AgentRemovalResult:
    success: bool
    subnet: TaskSubnet
    scope: ImpactScope
    delta: RuleDelta
    metrics: AgentRemovalMetrics
    event_log: tuple[EventLogRecord, ...]
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    failure_reason: str = ""


class AgentRemovalTransaction:
    """Compatibility facade over the shared strategy and transaction engine."""

    def __init__(
        self,
        controller: "AgentController",
        *,
        verifier: "TaskSubnetVerifier | None" = None,
        scope_analyzer: ImpactScopeAnalyzer | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.controller = controller
        self.verifier = verifier
        self.scope_analyzer = scope_analyzer or ImpactScopeAnalyzer()
        self.clock = clock

    async def handle(
        self,
        event: RuntimeEvent,
        *,
        run_id: int = 1,
        seed: int = 0,
    ) -> AgentRemovalResult:
        if event.event_type != RuntimeEventType.AGENT_REMOVE.value:
            raise ValueError(f"unsupported transaction event: {event.event_type}")
        stable = self.controller.tasks.get(event.task_id)
        if stable is None:
            raise ValueError(f"task subnet is not active: {event.task_id}")
        if stable.state not in {TaskState.STABLE, TaskState.NETWORKED}:
            raise ValueError(f"task subnet is not stable: {stable.state.value}")

        received_at = self.clock()
        scope_started_at = self.clock()
        scope = self.scope_analyzer.analyze(stable, event)
        scope_finished_at = self.clock()
        plan = await ProposedIncrementalStrategy(
            self.controller,
            self.scope_analyzer,
        ).plan(stable, event)
        delta_compiled_at = self.clock()
        pre_stage_records = (
            _record(
                run_id,
                event,
                stable.version,
                plan.target_state.version,
                EventStage.SCOPE_STARTED,
                scope_started_at,
                "ImpactScopeAnalyzer",
                {"agent_ids": list(event.agent_ids)},
            ),
            _record(
                run_id,
                event,
                stable.version,
                plan.target_state.version,
                EventStage.SCOPE_FINISHED,
                scope_finished_at,
                "ImpactScopeAnalyzer",
                _scope_details(scope),
            ),
            _record(
                run_id,
                event,
                stable.version,
                plan.target_state.version,
                EventStage.DELTA_COMPILED,
                delta_compiled_at,
                "SubnetCompiler",
                {
                    "additions": sorted(rule.rule_id for rule in plan.rule_delta.additions),
                    "updates": sorted(rule.rule_id for rule in plan.rule_delta.updates),
                    "deletions": sorted(rule.rule_id for rule in plan.rule_delta.deletions),
                },
            ),
        )
        before_snapshot = gateway_rule_snapshot(
            self.controller,
            event.task_id,
            set(plan.verification_gateways),
        )
        probe = ContinuousBusinessProbe(
            self.controller,
            stable,
            scope.unaffected_business_edges,
            interval_ms=10.0,
            clock=self.clock,
        )
        execution = await TransactionExecutor(
            self.controller,
            verifier=self.verifier,
            clock=self.clock,
        ).execute(
            stable,
            plan,
            event,
            run_id=run_id,
            seed=seed,
            probe=probe,
            received_at=received_at,
            pre_stage_records=pre_stage_records,
        )
        actual = execution.state
        residual_rules = _residual_rule_count(
            self.controller,
            event.task_id,
            set(event.agent_ids),
        )

        collector = MetricsCollector(
            run_id=run_id,
            seed=seed,
            event=event,
            old_version=stable.version,
            new_version=plan.target_state.version,
            clock=self.clock,
        )
        collector.records = list(execution.event_log)
        metrics = collector.build_agent_removal_metrics(
            num_agents_before=len(stable.application_agents),
            num_agents_after=len(actual.application_agents),
            num_edges_before=len(stable.task.biz_edges),
            num_edges_after=len(actual.task.biz_edges),
            num_gateways=len(plan.verification_gateways),
            scope=scope,
            delta=plan.rule_delta,
            delta_compile_started_at=scope_finished_at,
            residual_rules=residual_rules if execution.success else 0,
            unaffected_edges_interrupted=(
                execution.probe_summary.unaffected_edges_interrupted
            ),
            unaffected_service_interruption_ms=(
                execution.probe_summary.unaffected_interruption_ms
            ),
            success=execution.success,
            rollback_triggered=execution.rollback_triggered,
            rollback_success=execution.rollback_success,
            failure_reason=execution.failure_reason,
        )
        return AgentRemovalResult(
            success=execution.success,
            subnet=actual,
            scope=scope,
            delta=plan.rule_delta,
            metrics=metrics,
            event_log=execution.event_log,
            before_snapshot=before_snapshot,
            after_snapshot=gateway_rule_snapshot(
                self.controller,
                event.task_id,
                set(plan.verification_gateways),
            ),
            failure_reason=execution.failure_reason,
        )


def gateway_rule_snapshot(
    controller: "AgentController",
    task_id: str,
    gateway_ids: set[str],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for gateway_id in sorted(gateway_ids):
        gateway = controller.gateways[gateway_id]
        snapshot[gateway_id] = {
            "stable_version": gateway.get_stable_version(task_id),
            "staged_version": gateway.get_staged_version(task_id),
            "stable_rules": [
                _rule_snapshot(rule)
                for rule in sorted(
                    gateway.get_stable_rules(task_id).values(),
                    key=lambda item: item.rule_id,
                )
            ],
            "staged_rules": [
                _rule_snapshot(rule)
                for rule in sorted(
                    gateway.get_staged_rules(task_id).values(),
                    key=lambda item: item.rule_id,
                )
            ],
        }
    return snapshot


def _record(
    run_id: int,
    event: RuntimeEvent,
    old_version: int,
    new_version: int,
    stage: EventStage,
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
        event_stage=stage.value,
        details=details,
    )


def _residual_rule_count(
    controller: "AgentController",
    task_id: str,
    removed_agents: set[str],
) -> int:
    return sum(
        1
        for gateway in controller.gateways.values()
        for rule in gateway.get_stable_rules(task_id).values()
        if rule.src_agent in removed_agents or rule.dst_agent in removed_agents
    )


def _rule_snapshot(rule: GatewayRouteEntry) -> dict[str, Any]:
    return {
        "task_id": rule.task_id,
        "version": rule.version,
        "rule_id": rule.rule_id,
        "gateway_id": rule.gateway_id,
        "src_agent": rule.src_agent,
        "dst_agent": rule.dst_agent,
        "next_hop": rule.next_hop,
        "route_id": rule.route_id,
        "priority": rule.priority,
        "allowed": rule.allowed,
        "action": rule.action.mode,
    }


def _scope_details(scope: ImpactScope) -> dict[str, Any]:
    return {
        "affected_agents": sorted(scope.affected_agents),
        "affected_business_edges": sorted(scope.affected_business_edges),
        "affected_sessions": sorted(scope.affected_sessions),
        "affected_routes": sorted(scope.affected_routes),
        "affected_physical_resources": sorted(scope.affected_physical_resources),
        "affected_gateways": sorted(scope.affected_gateways),
        "affected_rules": sorted(scope.affected_rules),
        "unaffected_business_edges": sorted(scope.unaffected_business_edges),
    }
