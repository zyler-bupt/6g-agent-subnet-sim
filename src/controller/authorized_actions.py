from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Callable

from src.controller.cross_layer_coordinator import CoordinationResult
from src.controller.conflicts import detect_write_set_conflicts
from src.controller.feasibility import (
    CrossLayerFeasibilityResult,
    evaluate_cross_layer_combination,
    project_cross_layer_state,
)
from src.controller.impact import ImpactScope
from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
from src.controller.transaction_executor import (
    TransactionExecutionResult,
    TransactionExecutor,
)
from src.core.cross_layer import (
    AuthorizedAction,
    CrossLayerTaskState,
    LayerProposal,
)
from src.core.events import RuntimeEvent
from src.core.models import TaskState, TaskSubnet
from src.core.rules import compute_rule_delta
from src.e2e.models import EdgeVerifyResult, VerifyResult
from src.e2e.transactional_installers import TransactionalInstaller


@dataclass(frozen=True)
class CrossLayerExecutionResult:
    authorized_actions: tuple[AuthorizedAction, ...]
    plan: ReconfigurationPlan
    transaction: TransactionExecutionResult
    post_execution_feasibility: CrossLayerFeasibilityResult
    transaction_latency_ms: float


class PostActivationCrossLayerVerifier:
    """Common staged/stable verifier used by every comparison method.

    The first invocation validates staged business connectivity through the
    existing verifier.  Cross-layer feasibility is deliberately evaluated on
    the second invocation, after activation, so the no-verification ablation
    truly skips the pre-execution global check and is still judged by the same
    stable-state verifier as every other method.
    """

    def __init__(self, business_verifier) -> None:
        self.business_verifier = business_verifier
        self.invocations = 0
        self.last_cross_layer_result: CrossLayerFeasibilityResult | None = None

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        self.invocations += 1
        base = await self.business_verifier.verify(subnet)
        runtime = subnet.cross_layer_runtime
        if self.invocations % 2 == 1 or not isinstance(runtime, CrossLayerTaskState):
            return base
        result = evaluate_cross_layer_combination(runtime)
        self.last_cross_layer_result = result
        if result.feasible:
            return VerifyResult(
                ok=base.ok,
                verify_ms=base.verify_ms,
                checked_edges=base.checked_edges,
                passed_edges=base.passed_edges,
                edge_results=base.edge_results,
                mode=f"{base.mode}+cross_layer_stable",
            )
        failed_edges = {
            violation.rsplit(":", 1)[-1]
            for violation in result.violations
            if ":" in violation
            and violation.rsplit(":", 1)[-1] in runtime.constraints
        }
        edge_by_session = {
            session.session_id: session.business_edge_id
            for session in subnet.sessions
        }
        edge_results = []
        for item in base.edge_results:
            edge_id = edge_by_session.get(item.session_id, "")
            if edge_id not in runtime.constraints:
                edge_results.append(item)
                continue
            metric = result.edge_metrics.get(edge_id)
            if edge_id not in failed_edges and metric is not None and metric.qos_satisfied:
                edge_results.append(item)
                continue
            violations = tuple(
                value
                for value in result.violations
                if not edge_id or value.endswith(f":{edge_id}")
                or value.startswith("shared_resource")
                or value.startswith("write_set")
            )
            edge_results.append(
                replace(
                    item,
                    ok=False,
                    latency_ms=(metric.latency_ms if metric else item.latency_ms),
                    loss_rate=(metric.packet_loss_rate if metric else item.loss_rate),
                    throughput_mbps=(metric.throughput_mbps if metric else item.throughput_mbps),
                    error=";".join(violations) or "cross_layer_infeasible",
                )
            )
        passed = sum(item.ok for item in edge_results)
        return VerifyResult(
            ok=False,
            verify_ms=base.verify_ms,
            checked_edges=len(edge_results),
            passed_edges=passed,
            edge_results=tuple(edge_results),
            mode=f"{base.mode}+cross_layer_stable",
        )


class AuthorizedActionExecutor:
    """Controller-only bridge from selected proposals to a versioned commit."""

    def __init__(
        self,
        controller,
        business_verifier,
        *,
        installer: TransactionalInstaller | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.controller = controller
        self.business_verifier = business_verifier
        self.installer = installer
        self.clock = clock

    def authorize(
        self,
        proposals: tuple[LayerProposal, ...],
    ) -> tuple[AuthorizedAction, ...]:
        return tuple(
            AuthorizedAction(
                proposal_id=proposal.proposal_id,
                task_id=proposal.task_id,
                layer=proposal.layer,
                action=proposal.action,
                authorized_by="AgentController",
                target_objects=proposal.target_objects,
                parameters=dict(proposal.parameters),
            )
            for proposal in proposals
        )

    def plan(
        self,
        stable_state: TaskSubnet,
        observation: CrossLayerTaskState,
        coordination: CoordinationResult,
    ) -> tuple[ReconfigurationPlan, tuple[AuthorizedAction, ...]]:
        actions = self.authorize(coordination.selected_proposals)
        if any(action.authorized_by != "AgentController" for action in actions):
            raise PermissionError("all cross-layer actions require Controller authorization")
        projected = project_cross_layer_state(
            observation,
            coordination.selected_proposals,
        )
        write_conflicts = detect_write_set_conflicts(
            coordination.selected_proposals
        )
        stable_version = observation.metadata.get("stable_version")
        stale_proposals = tuple(
            proposal.proposal_id
            for proposal in coordination.selected_proposals
            if stable_version is not None
            and proposal.parameters.get("observed_version", stable_version)
            != stable_version
        )
        projected = replace(
            projected,
            metadata={
                **projected.metadata,
                "executed_write_conflicts": tuple(
                    record.conflict_id for record in write_conflicts
                ),
                "executed_stale_proposals": stale_proposals,
            },
        )
        target = _compile_authorized_target(stable_state, projected, actions)
        delta = compute_rule_delta(stable_state.rules.values(), target.rules.values())
        changed_edges = frozenset(
            edge_id
            for proposal in coordination.selected_proposals
            if not proposal.is_keep
            for edge_id in proposal.affected_edges
        )
        changed_sessions = frozenset(
            session_id
            for proposal in coordination.selected_proposals
            if not proposal.is_keep
            for session_id in proposal.affected_sessions
        )
        changed_routes = frozenset(
            route_id
            for proposal in coordination.selected_proposals
            if not proposal.is_keep
            for route_id in proposal.affected_routes
        )
        changed_gateways = frozenset(
            set(delta.gateway_ids)
            | {
                gateway_id
                for proposal in coordination.selected_proposals
                if not proposal.is_keep
                for gateway_id in proposal.affected_gateways
            }
        )
        physical_resources = frozenset(
            binding.binding_id
            for binding in target.physical_bindings.values()
            if binding.edge_id in changed_edges
        )
        affected_agents = set()
        for edge_id in changed_edges:
            edge = target.business_edges.get(edge_id)
            if edge is not None:
                affected_agents.update((edge.source, edge.target))
        for session in target.sessions:
            if session.business_edge_id in changed_edges:
                affected_agents.update(
                    (session.t_agent_id, session.n_agent_id, *session.p_agent_ids)
                )
        scope = ImpactScope(
            affected_agents=frozenset(affected_agents),
            affected_business_edges=changed_edges,
            affected_sessions=changed_sessions,
            affected_routes=changed_routes,
            affected_physical_resources=physical_resources,
            affected_gateways=changed_gateways,
            affected_rules=frozenset(delta.changed_rule_ids),
            unaffected_business_edges=frozenset(
                set(target.business_edges) - set(changed_edges)
            ),
        )
        # All task gateways join the version barrier.  The rule-changing scope
        # remains separate in affected_gateways for overhead measurements.
        transaction_gateways = frozenset(target.involved_gateways)
        plan = ReconfigurationPlan(
            method=coordination.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, transaction_gateways),
            affected_gateways=changed_gateways,
            affected_layers=frozenset(
                proposal.layer
                for proposal in coordination.selected_proposals
                if not proposal.is_keep
            ),
            transaction_gateways=transaction_gateways,
            verification_gateways=transaction_gateways,
            planning_details={
                "selected_proposal_ids": [
                    proposal.proposal_id
                    for proposal in coordination.selected_proposals
                ],
                "controller_authorized": True,
                "global_check_performed": coordination.global_check_performed,
            },
        )
        return plan, actions

    async def execute(
        self,
        stable_state: TaskSubnet,
        observation: CrossLayerTaskState,
        coordination: CoordinationResult,
        event: RuntimeEvent,
        *,
        run_id: int,
        seed: int,
        received_at: float | None = None,
    ) -> CrossLayerExecutionResult:
        plan, actions = self.plan(stable_state, observation, coordination)
        verifier = PostActivationCrossLayerVerifier(self.business_verifier)
        started = self.clock()
        transaction = await TransactionExecutor(
            self.controller,
            installer=self.installer,
            verifier=verifier,
            clock=self.clock,
        ).execute(
            stable_state,
            plan,
            event,
            run_id=run_id,
            seed=seed,
            received_at=received_at,
        )
        elapsed = (self.clock() - started) * 1000.0
        post = verifier.last_cross_layer_result
        if post is None:
            post = evaluate_cross_layer_combination(plan.target_state.cross_layer_runtime)
        return CrossLayerExecutionResult(
            authorized_actions=actions,
            plan=plan,
            transaction=transaction,
            post_execution_feasibility=post,
            transaction_latency_ms=max(0.0, elapsed),
        )


def _compile_authorized_target(
    stable: TaskSubnet,
    projected: CrossLayerTaskState,
    actions: tuple[AuthorizedAction, ...],
) -> TaskSubnet:
    if any(action.task_id != stable.task.task_id for action in actions):
        raise ValueError("authorized action does not belong to stable task")
    if any(action.authorized_by != "AgentController" for action in actions):
        raise PermissionError("unauthorized cross-layer action")
    target = deepcopy(stable)
    target.version = stable.version + 1
    target.state = TaskState.PLANNING
    target.cross_layer_runtime = projected
    target.authorized_cross_layer_actions = actions

    changed_edges = set(projected.constraints)
    updated_sessions = []
    for session in target.sessions:
        edge_id = session.business_edge_id
        if edge_id not in changed_edges:
            updated_sessions.append(session)
            continue
        application = projected.application[edge_id]
        network = projected.network[edge_id]
        updated_sessions.append(
            replace(
                session,
                data_rate_mbps=application.required_rate_mbps,
                path_id=network.route_id,
            )
        )
    target.sessions = updated_sessions

    for edge_id in changed_edges:
        edge = target.business_edges.get(edge_id)
        if edge is None:
            continue
        application = projected.application[edge_id]
        source = target.application_agents.get(edge.source)
        if source is not None:
            target.application_agents[edge.source] = replace(
                source,
                data_rate_mbps=application.required_rate_mbps,
                priority=application.priority,
            )
        session = next(
            (
                item
                for item in target.sessions
                if item.business_edge_id == edge_id
            ),
            None,
        )
        if session is not None:
            transport = target.transport_agents.get(session.t_agent_id)
            if transport is not None:
                runtime_transport = projected.transport[edge_id]
                target.transport_agents[session.t_agent_id] = replace(
                    transport,
                    send_rate_mbps=runtime_transport.send_rate_mbps,
                    retransmission_rate=runtime_transport.retransmission_rate,
                    reliability=runtime_transport.reliability,
                )
            # The experiment's edge-specific network observation remains in
            # cross_layer_runtime.  A legacy NetworkAgentState may aggregate
            # several unrelated sessions, so overwriting its shared capacity
            # with one edge's bottleneck would incorrectly fail those flows.

    target.gateway_routes = {
        gateway_id: [
            _updated_rule(rule, target.version, projected)
            for rule in rules
        ]
        for gateway_id, rules in target.gateway_routes.items()
    }
    target.physical_bindings = {
        binding_id: (
            replace(
                binding,
                reserved_capacity_mbps=projected.application[
                    binding.edge_id
                ].required_rate_mbps,
                version=target.version,
            )
            if binding.edge_id in projected.application
            else binding
        )
        for binding_id, binding in target.physical_bindings.items()
    }
    return target


def _updated_rule(rule, version: int, projected: CrossLayerTaskState):
    edge_id = next(
        (
            edge_id
            for edge_id in projected.constraints
            if rule.session_id == projected.transport[edge_id].session_id
        ),
        "",
    )
    if not edge_id:
        return rule
    application = projected.application[edge_id]
    network = projected.network[edge_id]
    return replace(
        rule,
        version=version,
        route_id=network.route_id,
        path_id=network.route_id,
        min_bandwidth_mbps=application.required_rate_mbps,
        action=replace(rule.action, priority=application.priority),
    )
