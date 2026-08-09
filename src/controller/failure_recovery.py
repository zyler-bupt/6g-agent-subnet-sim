from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Callable, Protocol

from src.controller.impact import ImpactScope, ImpactScopeAnalyzer
from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
from src.core.cross_layer import AuthorizedAction, LayerProposal
from src.core.events import RuntimeEvent, RuntimeEventType
from src.core.failures import FaultContext
from src.core.models import AgentLayer, BusinessEdge, TaskState, TaskSubnet
from src.core.rules import RuleDelta, compute_rule_delta
from src.e2e.failure_verifier import FaultAwareTaskSubnetVerifier


@dataclass(frozen=True)
class RecoveryPlanningResult:
    method: str
    plan: ReconfigurationPlan | None
    proposals: tuple[LayerProposal, ...]
    selected_proposals: tuple[LayerProposal, ...]
    authorized_actions: tuple[AuthorizedAction, ...]
    rejected_proposals: dict[str, str]
    pre_execution_feasibility_checked: bool
    pre_execution_feasible: bool
    safe_rejection: bool
    failure_reason: str
    localization_latency_ms: float
    proposal_latency_ms: float
    coordination_latency_ms: float
    feasibility_latency_ms: float
    delta_compile_latency_ms: float


class FailureRecoveryStrategy(Protocol):
    method: str

    async def plan(
        self,
        controller,
        stable_state: TaskSubnet,
        event: RuntimeEvent,
        context: FaultContext,
    ) -> RecoveryPlanningResult:
        ...


class ProposedCrossLayerElasticStrategy:
    method = "proposed"

    def __init__(
        self,
        *,
        scope_identification: bool = True,
        pre_execution_verification: bool = True,
        method: str | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.scope_identification = scope_identification
        self.pre_execution_verification = pre_execution_verification
        self.method = method or self.method
        self.clock = clock

    async def plan(self, controller, stable_state, event, context) -> RecoveryPlanningResult:
        localization_started = self.clock()
        localized = ImpactScopeAnalyzer().analyze_failure(stable_state, event)
        scope = localized if self.scope_identification else _full_scope(stable_state, localized)
        localization_ms = _elapsed_ms(localization_started, self.clock())

        proposal_started = self.clock()
        proposals = generate_failure_proposals(stable_state, context)
        proposal_ms = _elapsed_ms(proposal_started, self.clock())
        coordination_started = self.clock()
        selected = _selected_cross_layer_proposals(proposals, context)
        rejected = {
            proposal.proposal_id: "not_selected_by_fault_recovery_policy"
            for proposal in proposals
            if proposal not in selected
        }
        if not context.resolvable:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                "UNRESOLVABLE:no authorized substitute or feasible bounded action",
                localization_ms,
                proposal_ms,
                _elapsed_ms(coordination_started, self.clock()),
                prechecked=self.pre_execution_verification,
            )
        try:
            target, compilation_details = await _compile_recovery_target(
                controller,
                stable_state,
                context,
                network_only=False,
            )
        except (ValueError, KeyError) as error:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                f"UNRESOLVABLE:{error}",
                localization_ms,
                proposal_ms,
                _elapsed_ms(coordination_started, self.clock()),
                prechecked=self.pre_execution_verification,
            )
        coordination_ms = _elapsed_ms(coordination_started, self.clock())
        actions = _authorize(selected)
        target.authorized_cross_layer_actions = actions
        feasibility_started = self.clock()
        feasible = True
        failure_reason = ""
        if self.pre_execution_verification:
            result = await FaultAwareTaskSubnetVerifier(context).verify(target)
            feasible = result.ok
            if not feasible:
                failure_reason = "pre_execution_feasibility_failed:" + ";".join(
                    item.error for item in result.edge_results if not item.ok
                )
        feasibility_ms = _elapsed_ms(feasibility_started, self.clock())
        if not feasible:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                failure_reason,
                localization_ms,
                proposal_ms,
                coordination_ms,
                feasibility_ms=feasibility_ms,
                prechecked=True,
            )
        delta_started = self.clock()
        plan = _incremental_plan(
            self.method,
            stable_state,
            target,
            scope,
            selected,
            compilation_details,
        )
        delta_ms = _elapsed_ms(delta_started, self.clock())
        return RecoveryPlanningResult(
            method=self.method,
            plan=plan,
            proposals=proposals,
            selected_proposals=selected,
            authorized_actions=actions,
            rejected_proposals=rejected,
            pre_execution_feasibility_checked=self.pre_execution_verification,
            pre_execution_feasible=True,
            safe_rejection=False,
            failure_reason="",
            localization_latency_ms=localization_ms,
            proposal_latency_ms=proposal_ms,
            coordination_latency_ms=coordination_ms,
            feasibility_latency_ms=feasibility_ms,
            delta_compile_latency_ms=delta_ms,
        )


class FullRebuildFailureStrategy(ProposedCrossLayerElasticStrategy):
    method = "full_rebuild"

    async def plan(self, controller, stable_state, event, context) -> RecoveryPlanningResult:
        base = await super().plan(controller, stable_state, event, context)
        if base.plan is None:
            return replace(base, method=self.method)
        target = base.plan.target_state
        old_rules = stable_state.rules
        new_rules = target.rules
        delta = RuleDelta(
            additions=tuple(
                new_rules[rule_id]
                for rule_id in sorted(set(new_rules) - set(old_rules))
            ),
            updates=tuple(
                new_rules[rule_id]
                for rule_id in sorted(set(new_rules) & set(old_rules))
            ),
            deletions=tuple(
                old_rules[rule_id]
                for rule_id in sorted(set(old_rules) - set(new_rules))
            ),
        )
        gateways = frozenset(stable_state.involved_gateways | target.involved_gateways)
        localized = base.plan.impact_scope
        scope = _full_scope_union(stable_state, target, localized)
        plan = replace(
            base.plan,
            method=self.method,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, gateways),
            affected_gateways=gateways,
            affected_layers=frozenset({"application", "transport", "network", "physical"}),
            transaction_gateways=gateways,
            verification_gateways=gateways,
            full_rule_install=True,
            planning_details={
                **base.plan.planning_details,
                "rebuild": "full four-layer mapping/session/path/rule compilation",
            },
        )
        return replace(base, method=self.method, plan=plan)


class ReactiveNetworkOnlyStrategy:
    method = "network_only"

    def __init__(self, clock: Callable[[], float] = perf_counter) -> None:
        self.clock = clock

    async def plan(self, controller, stable_state, event, context) -> RecoveryPlanningResult:
        localization_started = self.clock()
        scope = ImpactScopeAnalyzer().analyze_failure(stable_state, event)
        localization_ms = _elapsed_ms(localization_started, self.clock())
        proposal_started = self.clock()
        pool = generate_failure_proposals(stable_state, context)
        proposals = tuple(item for item in pool if item.layer == "network")
        proposal_ms = _elapsed_ms(proposal_started, self.clock())
        selected = _select_network_only(proposals, context)
        rejected = {
            proposal.proposal_id: "lower_network_local_utility"
            for proposal in proposals
            if proposal not in selected
        }
        coordinate_started = self.clock()
        recoverable_by_network = (
            context.fault_type in {
                RuntimeEventType.LINK_FAILURE.value,
                RuntimeEventType.LINK_DEGRADATION.value,
            }
            or (
                context.fault_type == RuntimeEventType.AGENT_FAILURE.value
                and context.failed_agent_layer == "network"
                and context.resolvable
            )
            or (
                context.fault_type == RuntimeEventType.PHYSICAL_CAPACITY_DROP.value
                and float(context.metadata.get("affected_demand_mbps", 0.0))
                <= context.physical_capacity_mbps + 1e-9
            )
        )
        if not recoverable_by_network:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                "UNRESOLVABLE_BY_NETWORK_ONLY:cross-layer action required",
                localization_ms,
                proposal_ms,
                _elapsed_ms(coordinate_started, self.clock()),
                prechecked=True,
            )
        try:
            target, details = await _compile_recovery_target(
                controller,
                stable_state,
                context,
                network_only=True,
            )
        except (ValueError, KeyError) as error:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                f"UNRESOLVABLE_BY_NETWORK_ONLY:{error}",
                localization_ms,
                proposal_ms,
                _elapsed_ms(coordinate_started, self.clock()),
                prechecked=True,
            )
        coordination_ms = _elapsed_ms(coordinate_started, self.clock())
        feasibility_started = self.clock()
        verified = await FaultAwareTaskSubnetVerifier(context).verify(target)
        feasibility_ms = _elapsed_ms(feasibility_started, self.clock())
        if not verified.ok:
            return _rejected_result(
                self.method,
                proposals,
                selected,
                rejected,
                "network_only_candidate_infeasible:" + ";".join(
                    item.error for item in verified.edge_results if not item.ok
                ),
                localization_ms,
                proposal_ms,
                coordination_ms,
                feasibility_ms=feasibility_ms,
                prechecked=True,
            )
        actions = _authorize(selected)
        target.authorized_cross_layer_actions = actions
        delta_started = self.clock()
        plan = _incremental_plan(
            self.method,
            stable_state,
            target,
            scope,
            selected,
            details,
        )
        return RecoveryPlanningResult(
            method=self.method,
            plan=plan,
            proposals=proposals,
            selected_proposals=selected,
            authorized_actions=actions,
            rejected_proposals=rejected,
            pre_execution_feasibility_checked=True,
            pre_execution_feasible=True,
            safe_rejection=False,
            failure_reason="",
            localization_latency_ms=localization_ms,
            proposal_latency_ms=proposal_ms,
            coordination_latency_ms=coordination_ms,
            feasibility_latency_ms=feasibility_ms,
            delta_compile_latency_ms=_elapsed_ms(delta_started, self.clock()),
        )


class WithoutScopeIdentificationStrategy(ProposedCrossLayerElasticStrategy):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            scope_identification=False,
            method="no_scope",
            **kwargs,
        )


class WithoutVerificationRollbackStrategy(ProposedCrossLayerElasticStrategy):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            pre_execution_verification=False,
            method="no_verification_rollback",
            **kwargs,
        )


def generate_failure_proposals(
    stable: TaskSubnet,
    context: FaultContext,
) -> tuple[LayerProposal, ...]:
    edge_ids = context.affected_edge_ids or frozenset(stable.business_edges)
    session_ids = frozenset(
        session.session_id
        for session in stable.sessions
        if session.business_edge_id in edge_ids
    )
    route_ids = frozenset(f"{session_id}:route" for session_id in session_ids)
    gateways = frozenset(
        gateway_id
        for session in stable.sessions
        if session.session_id in session_ids
        for gateway_id in session.gateway_path
    )
    profiles = {
        "application": ("KEEP_QUALITY", "ACTIVATE_SUBSTITUTE", "DECREASE_APPLICATION_RATE"),
        "transport": ("KEEP_TRANSPORT", "REBIND_SESSION", "DECREASE_SEND_RATE"),
        "network": ("KEEP_ROUTE", "SWITCH_ROUTE", "RESERVE_BANDWIDTH"),
        "physical": ("KEEP_RESOURCE", "SWITCH_ACCESS", "REALLOCATE_RESOURCE"),
    }
    proposals: list[LayerProposal] = []
    for layer, actions in profiles.items():
        for index, action in enumerate(actions):
            keep = action.startswith("KEEP_")
            relevant = _proposal_relevant(layer, action, context)
            gain = 0.0 if keep else (1.0 if relevant else 0.05)
            cost = 0.0 if keep else 0.08 + 0.03 * index
            proposals.append(
                LayerProposal(
                    proposal_id=(
                        f"{stable.task.task_id}:recovery:{layer}:{index:02d}:{action}"
                    ),
                    task_id=stable.task.task_id,
                    layer=layer,
                    action=action,
                    target_objects=frozenset(f"edge:{item}" for item in edge_ids),
                    read_set=frozenset(
                        {
                            f"task:{stable.task.task_id}:version:{stable.version}",
                            f"{layer}:fault_observation",
                        }
                    ),
                    write_set=(
                        frozenset()
                        if keep
                        else frozenset(f"{layer}:edge:{item}" for item in edge_ids)
                    ),
                    expected_qos_gain=gain,
                    expected_cost=cost,
                    confidence=0.98,
                    required_bandwidth_mbps=sum(
                        edge.data_rate_mbps
                        for edge in stable.task.biz_edges
                        if edge.edge_id in edge_ids
                    ),
                    required_physical_capacity_mbps=sum(
                        edge.data_rate_mbps
                        for edge in stable.task.biz_edges
                        if edge.edge_id in edge_ids
                    ),
                    expected_latency_ms=20.0,
                    expected_loss_rate=0.001,
                    affected_edges=edge_ids,
                    affected_sessions=session_ids,
                    affected_routes=route_ids,
                    affected_gateways=gateways,
                    parameters={
                        "observed_version": stable.version,
                        "fault_type": context.fault_type,
                        "fault_target": context.fault_target,
                        "relevant": relevant,
                    },
                )
            )
    return tuple(proposals)


async def _compile_recovery_target(
    controller,
    stable: TaskSubnet,
    context: FaultContext,
    *,
    network_only: bool,
) -> tuple[TaskSubnet, dict[str, object]]:
    task = stable.task
    exclusions: set[str] = set()
    path_overrides: dict[tuple[str, str], tuple[str, ...]] = {}
    if context.fault_type == RuntimeEventType.AGENT_FAILURE.value:
        if context.failed_agent_layer == "application":
            if network_only or not context.replacement_agent_id:
                raise ValueError("business Agent replacement requires upper/application action")
            task = _replace_business_agent(
                task,
                context.failed_agent_id,
                context.replacement_agent_id,
            )
        elif context.failed_agent_layer == "network":
            exclusions.add(context.failed_agent_id)
        elif not network_only:
            exclusions.add(context.failed_agent_id)
        else:
            raise ValueError(f"{context.failed_agent_layer} failure is not network-repairable")
    elif context.fault_type in {
        RuntimeEventType.LINK_FAILURE.value,
        RuntimeEventType.LINK_DEGRADATION.value,
    }:
        demand = sum(
            session.data_rate_mbps
            for session in stable.sessions
            if session.business_edge_id in context.affected_edge_ids
        )
        if demand > context.degraded_link_capacity_mbps + 1e-9:
            for session in stable.sessions:
                if session.business_edge_id not in context.affected_edge_ids:
                    continue
                key = (session.source_gateway, session.target_gateway)
                if key == (context.primary_path[0], context.primary_path[-1]):
                    path_overrides[key] = context.backup_path
                else:
                    path_overrides[key] = _alternate_path(
                        tuple(controller.gateways),
                        session.source_gateway,
                        session.target_gateway,
                        context.failed_link,
                    )
    elif context.fault_type == RuntimeEventType.PHYSICAL_CAPACITY_DROP.value:
        demand = float(context.metadata.get("affected_demand_mbps", 0.0))
        if network_only and demand > context.physical_capacity_mbps + 1e-9:
            raise ValueError("all routes share the degraded access resource")
        if not network_only and demand > context.physical_capacity_mbps + 1e-9:
            exclusions.add(context.physical_agent_id)

    compilation = await controller.compile_task_subnet(
        task,
        version=stable.version + 1,
        gateway_path_overrides=path_overrides or None,
        excluded_support_agents=frozenset(exclusions),
    )
    target = compilation.subnet
    return target, {
        "mapping_finished_at": compilation.mapping_finished_at,
        "layer_binding_finished_at": compilation.layer_binding_finished_at,
        "feasibility_finished_at": compilation.feasibility_finished_at,
        "compile_finished_at": compilation.compile_finished_at,
        "gateway_path_overrides": {
            f"{source}->{target_gateway}": list(path)
            for (source, target_gateway), path in path_overrides.items()
        },
        "excluded_support_agents": sorted(exclusions),
    }


def _replace_business_agent(task, failed_agent_id: str, replacement_agent_id: str):
    if failed_agent_id not in task.app_agents:
        raise ValueError(f"failed business Agent is not a member: {failed_agent_id}")
    app_agents = tuple(
        replacement_agent_id if item == failed_agent_id else item
        for item in task.app_agents
    )
    edges: list[BusinessEdge] = []
    for edge in task.biz_edges:
        edges.append(
            replace(
                edge,
                source=(replacement_agent_id if edge.source == failed_agent_id else edge.source),
                target=(replacement_agent_id if edge.target == failed_agent_id else edge.target),
            )
        )
    return replace(task, app_agents=app_agents, biz_edges=tuple(edges))


def _alternate_path(
    gateways: tuple[str, ...],
    source: str,
    target: str,
    failed_link: tuple[str, str],
) -> tuple[str, ...]:
    if source == target:
        return (source,)
    if (source, target) != failed_link:
        return (source, target)
    for intermediate in gateways:
        if intermediate in {source, target}:
            continue
        path = (source, intermediate, target)
        if failed_link not in set(zip(path, path[1:])):
            return path
    raise ValueError(f"no alternate path for {source}->{target}")


def _incremental_plan(
    method: str,
    stable: TaskSubnet,
    target: TaskSubnet,
    scope: ImpactScope,
    selected: tuple[LayerProposal, ...],
    details: dict[str, object],
) -> ReconfigurationPlan:
    delta = compute_rule_delta(stable.rules.values(), target.rules.values())
    barrier = frozenset(stable.involved_gateways | target.involved_gateways)
    changed_gateways = frozenset(
        set(delta.gateway_ids)
        | (set(scope.affected_gateways) & set(barrier))
    )
    changed_layers = frozenset(
        proposal.layer for proposal in selected if not proposal.is_keep
    )
    return ReconfigurationPlan(
        method=method,
        target_state=target,
        impact_scope=scope,
        rule_delta_by_gateway=delta_by_gateway(delta, barrier),
        affected_gateways=changed_gateways,
        affected_layers=changed_layers,
        transaction_gateways=barrier,
        verification_gateways=barrier,
        planning_details={
            **details,
            "version_barrier_gateways": sorted(barrier),
            "selected_actions": [proposal.action for proposal in selected],
        },
    )


def _selected_cross_layer_proposals(
    proposals: tuple[LayerProposal, ...],
    context: FaultContext,
) -> tuple[LayerProposal, ...]:
    desired: dict[str, str] = {
        "application": "KEEP_QUALITY",
        "transport": "KEEP_TRANSPORT",
        "network": "KEEP_ROUTE",
        "physical": "KEEP_RESOURCE",
    }
    if context.fault_type == RuntimeEventType.AGENT_FAILURE.value:
        desired[context.failed_agent_layer] = {
            "application": "ACTIVATE_SUBSTITUTE",
            "transport": "REBIND_SESSION",
            "network": "SWITCH_ROUTE",
            "physical": "SWITCH_ACCESS",
        }[context.failed_agent_layer]
        if context.failed_agent_layer == "application":
            desired["transport"] = "REBIND_SESSION"
            desired["network"] = "SWITCH_ROUTE"
            desired["physical"] = "SWITCH_ACCESS"
    elif context.fault_type in {
        RuntimeEventType.LINK_FAILURE.value,
        RuntimeEventType.LINK_DEGRADATION.value,
    }:
        demand = float(context.metadata.get("affected_demand_mbps", 0.0))
        if demand > context.degraded_link_capacity_mbps + 1e-9:
            desired["network"] = "SWITCH_ROUTE"
            desired["transport"] = "REBIND_SESSION"
    elif context.fault_type == RuntimeEventType.PHYSICAL_CAPACITY_DROP.value:
        demand = float(context.metadata.get("affected_demand_mbps", 0.0))
        if demand > context.physical_capacity_mbps + 1e-9:
            desired["physical"] = "SWITCH_ACCESS"
            desired["transport"] = "REBIND_SESSION"
    return tuple(
        next(
            proposal
            for proposal in proposals
            if proposal.layer == layer and proposal.action == action
        )
        for layer, action in desired.items()
    )


def _select_network_only(
    proposals: tuple[LayerProposal, ...],
    context: FaultContext,
) -> tuple[LayerProposal, ...]:
    action = (
        "SWITCH_ROUTE"
        if context.fault_type in {
            RuntimeEventType.LINK_FAILURE.value,
            RuntimeEventType.LINK_DEGRADATION.value,
            RuntimeEventType.AGENT_FAILURE.value,
        }
        else "KEEP_ROUTE"
    )
    return tuple(item for item in proposals if item.action == action)[:1]


def _authorize(proposals: tuple[LayerProposal, ...]) -> tuple[AuthorizedAction, ...]:
    return tuple(
        AuthorizedAction(
            proposal_id=item.proposal_id,
            task_id=item.task_id,
            layer=item.layer,
            action=item.action,
            authorized_by="AgentController",
            target_objects=item.target_objects,
            parameters=dict(item.parameters),
        )
        for item in proposals
    )


def _proposal_relevant(layer: str, action: str, context: FaultContext) -> bool:
    if action.startswith("KEEP_"):
        return False
    if context.fault_type == RuntimeEventType.AGENT_FAILURE.value:
        return layer == context.failed_agent_layer or context.failed_agent_layer == "application"
    if context.fault_type in {
        RuntimeEventType.LINK_FAILURE.value,
        RuntimeEventType.LINK_DEGRADATION.value,
    }:
        return layer in {"transport", "network"}
    return layer in {"application", "transport", "physical"}


def _full_scope(stable: TaskSubnet, localized: ImpactScope) -> ImpactScope:
    return ImpactScope(
        affected_agents=frozenset(
            stable.app_agents | stable.trans_agents | stable.net_agents | stable.phy_agents
        ),
        affected_business_edges=frozenset(stable.business_edges),
        affected_sessions=frozenset(item.session_id for item in stable.sessions),
        affected_routes=frozenset(stable.routes),
        affected_physical_resources=frozenset(stable.physical_bindings),
        affected_gateways=frozenset(stable.involved_gateways),
        affected_rules=frozenset(stable.rules),
        unaffected_business_edges=localized.unaffected_business_edges,
    )


def _full_scope_union(stable: TaskSubnet, target: TaskSubnet, localized: ImpactScope) -> ImpactScope:
    return ImpactScope(
        affected_agents=frozenset(
            stable.app_agents | stable.trans_agents | stable.net_agents | stable.phy_agents
            | target.app_agents | target.trans_agents | target.net_agents | target.phy_agents
        ),
        affected_business_edges=frozenset(set(stable.business_edges) | set(target.business_edges)),
        affected_sessions=frozenset(
            {item.session_id for item in stable.sessions}
            | {item.session_id for item in target.sessions}
        ),
        affected_routes=frozenset(set(stable.routes) | set(target.routes)),
        affected_physical_resources=frozenset(
            set(stable.physical_bindings) | set(target.physical_bindings)
        ),
        affected_gateways=frozenset(stable.involved_gateways | target.involved_gateways),
        affected_rules=frozenset(set(stable.rules) | set(target.rules)),
        unaffected_business_edges=localized.unaffected_business_edges,
    )


def _rejected_result(
    method: str,
    proposals: tuple[LayerProposal, ...],
    selected: tuple[LayerProposal, ...],
    rejected: dict[str, str],
    reason: str,
    localization_ms: float,
    proposal_ms: float,
    coordination_ms: float,
    *,
    feasibility_ms: float = 0.0,
    prechecked: bool,
) -> RecoveryPlanningResult:
    return RecoveryPlanningResult(
        method=method,
        plan=None,
        proposals=proposals,
        selected_proposals=selected,
        authorized_actions=(),
        rejected_proposals=rejected,
        pre_execution_feasibility_checked=prechecked,
        pre_execution_feasible=False,
        safe_rejection=True,
        failure_reason=reason,
        localization_latency_ms=localization_ms,
        proposal_latency_ms=proposal_ms,
        coordination_latency_ms=coordination_ms,
        feasibility_latency_ms=feasibility_ms,
        delta_compile_latency_ms=0.0,
    )


def _elapsed_ms(start: float, finish: float) -> float:
    return max(0.0, (finish - start) * 1000.0)
