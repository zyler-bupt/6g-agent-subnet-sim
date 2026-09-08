from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from src.agents.phy_agent import PhyAgent
from src.controller.impact import ImpactScope, ImpactScopeAnalyzer
from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
from src.core.events import RuntimeEvent, RuntimeEventType
from src.core.models import TaskState, TaskSubnet
from src.core.rules import RuleDelta, compute_rule_delta


class ProposedIncrementalStrategy:
    method = "proposed"

    def __init__(self, controller, scope_analyzer: ImpactScopeAnalyzer | None = None) -> None:
        self.controller = controller
        self.scope_analyzer = scope_analyzer or ImpactScopeAnalyzer()

    async def plan(
        self,
        stable_state: TaskSubnet,
        event: RuntimeEvent,
    ) -> ReconfigurationPlan:
        _validate_removal_event(stable_state, event)
        scope = self.scope_analyzer.analyze(stable_state, event)
        target = _incremental_target(
            self.controller,
            stable_state,
            event,
            scope,
        )
        delta = compute_rule_delta(stable_state.rules.values(), target.rules.values())
        verification_gateways = frozenset(
            stable_state.involved_gateways | target.involved_gateways
        )
        changed_gateways = frozenset(delta.gateway_ids | set(scope.affected_gateways))
        # Every task gateway joins the zero-delta version barrier, while the
        # reported affected scope contains only gateways whose content changes.
        return ReconfigurationPlan(
            method=self.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, verification_gateways),
            affected_gateways=changed_gateways,
            affected_layers=frozenset({"application", "transport", "network", "physical"}),
            transaction_gateways=verification_gateways,
            verification_gateways=verification_gateways,
            planning_details={"version_barrier_gateways": sorted(verification_gateways)},
        )


class FullRebuildStrategy:
    method = "full_rebuild"

    def __init__(self, controller, scope_analyzer: ImpactScopeAnalyzer | None = None) -> None:
        self.controller = controller
        self.scope_analyzer = scope_analyzer or ImpactScopeAnalyzer()

    async def plan(
        self,
        stable_state: TaskSubnet,
        event: RuntimeEvent,
    ) -> ReconfigurationPlan:
        _validate_removal_event(stable_state, event)
        scope = self.scope_analyzer.analyze(stable_state, event)
        removed = set(event.agent_ids)
        updated_task = replace(
            stable_state.task,
            app_agents=tuple(
                agent_id
                for agent_id in stable_state.task.app_agents
                if agent_id not in removed
            ),
            biz_edges=tuple(
                edge
                for edge in stable_state.task.biz_edges
                if edge.source not in removed and edge.target not in removed
            ),
        )
        compilation = await self.controller.compile_task_subnet(
            updated_task,
            version=stable_state.version + 1,
        )
        target = compilation.subnet
        removal_scope = scope
        old_rules = stable_state.rules
        new_rules = target.rules
        additions = tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) - set(old_rules))
        )
        # Full rebuild deliberately reinstalls every surviving target rule.
        updates = tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) & set(old_rules))
        )
        deletions = tuple(
            old_rules[rule_id] for rule_id in sorted(set(old_rules) - set(new_rules))
        )
        delta = RuleDelta(additions=additions, updates=updates, deletions=deletions)
        gateways = frozenset(stable_state.involved_gateways | target.involved_gateways)
        scope = ImpactScope(
            affected_agents=frozenset(
                stable_state.app_agents
                | stable_state.trans_agents
                | stable_state.net_agents
                | stable_state.phy_agents
                | target.app_agents
                | target.trans_agents
                | target.net_agents
                | target.phy_agents
            ),
            affected_business_edges=frozenset(
                set(stable_state.business_edges) | set(target.business_edges)
            ),
            affected_sessions=frozenset(
                {session.session_id for session in stable_state.sessions}
                | {session.session_id for session in target.sessions}
            ),
            affected_routes=frozenset(
                set(stable_state.routes) | set(target.routes)
            ),
            affected_physical_resources=frozenset(
                set(stable_state.physical_bindings) | set(target.physical_bindings)
            ),
            affected_gateways=gateways,
            affected_rules=frozenset(set(old_rules) | set(new_rules)),
            unaffected_business_edges=removal_scope.unaffected_business_edges,
        )
        return ReconfigurationPlan(
            method=self.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, gateways),
            affected_gateways=gateways,
            affected_layers=frozenset({"application", "transport", "network", "physical"}),
            transaction_gateways=gateways,
            verification_gateways=gateways,
            full_rule_install=True,
            planning_details={
                "mapping_finished_at": compilation.mapping_finished_at,
                "layer_binding_finished_at": compilation.layer_binding_finished_at,
                "feasibility_finished_at": compilation.feasibility_finished_at,
                "compile_finished_at": compilation.compile_finished_at,
            },
        )


class LocalOnlyStrategy:
    method = "local_only"

    def __init__(self, controller, scope_analyzer: ImpactScopeAnalyzer | None = None) -> None:
        self.controller = controller
        self.scope_analyzer = scope_analyzer or ImpactScopeAnalyzer()

    async def plan(
        self,
        stable_state: TaskSubnet,
        event: RuntimeEvent,
    ) -> ReconfigurationPlan:
        _validate_removal_event(stable_state, event)
        scope = self.scope_analyzer.analyze(stable_state, event)
        target = _incremental_target(
            self.controller,
            stable_state,
            event,
            scope,
        )
        full_delta = compute_rule_delta(stable_state.rules.values(), target.rules.values())
        local_gateways = _local_and_adjacent_gateways(stable_state, event)
        verification_gateways = frozenset(
            stable_state.involved_gateways | target.involved_gateways
        )
        local_delta = RuleDelta(
            additions=tuple(
                rule for rule in full_delta.additions if rule.gateway_id in local_gateways
            ),
            updates=tuple(
                rule for rule in full_delta.updates if rule.gateway_id in local_gateways
            ),
            deletions=tuple(
                rule for rule in full_delta.deletions if rule.gateway_id in local_gateways
            ),
        )
        return ReconfigurationPlan(
            method=self.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(local_delta, local_gateways),
            affected_gateways=frozenset(local_gateways),
            affected_layers=frozenset({"application", "transport", "network", "physical"}),
            transaction_gateways=frozenset(local_gateways),
            verification_gateways=verification_gateways,
            planning_details={
                "deliberately_untraced_gateways": sorted(
                    set(full_delta.gateway_ids) - local_gateways
                )
            },
        )


def _incremental_target(
    controller,
    stable: TaskSubnet,
    event: RuntimeEvent,
    scope: ImpactScope,
) -> TaskSubnet:
    removed = set(event.agent_ids)
    target = deepcopy(stable)
    target.version = stable.version + 1
    target.state = TaskState.PLANNING
    target.task = replace(
        stable.task,
        app_agents=tuple(
            agent_id for agent_id in stable.task.app_agents if agent_id not in removed
        ),
        biz_edges=tuple(
            edge
            for edge in stable.task.biz_edges
            if edge.edge_id not in scope.affected_business_edges
        ),
    )
    for agent_id in removed:
        target.application_agents.pop(agent_id, None)
    target.sessions = [
        session
        for session in stable.sessions
        if session.session_id not in scope.affected_sessions
    ]
    target.gateway_routes = {
        gateway_id: [
            rule for rule in rules if rule.rule_id not in scope.affected_rules
        ]
        for gateway_id, rules in stable.gateway_routes.items()
    }
    target.gateway_routes = {
        gateway_id: rules for gateway_id, rules in target.gateway_routes.items() if rules
    }
    target.physical_bindings = {
        binding_id: binding
        for binding_id, binding in stable.physical_bindings.items()
        if binding_id not in scope.affected_physical_resources
    }
    target.session_supports = {
        session_id: support
        for session_id, support in stable.session_supports.items()
        if session_id not in scope.affected_sessions
    }
    used_path_supports = {
        support.path_support_id for support in target.session_supports.values()
    }
    target.path_supports = {
        support_id: support
        for support_id, support in stable.path_supports.items()
        if support_id in used_path_supports
    }
    target.monitored_edge_ids -= set(scope.affected_business_edges)
    target.involved_gateways = {
        gateway_id
        for session in target.sessions
        for gateway_id in (
            session.gateway_path
            or (session.source_gateway, session.target_gateway)
        )
    }
    app_cards = controller._current_app_cards(target.task)
    target.application_agents = controller._build_application_states(
        target.task,
        app_cards,
    )
    target.transport_agents, target.network_agents = controller._build_support_states(
        target.task,
        target.sessions,
    )
    removed_binding_ids = set(scope.affected_physical_resources)
    target.physical_agents = {}
    for p_agent_id in sorted(
        {
            p_agent_id
            for session in target.sessions
            for p_agent_id in session.p_agent_ids
        }
    ):
        agent = controller._agent_by_id(p_agent_id)
        if isinstance(agent, PhyAgent):
            target.physical_agents[p_agent_id] = agent.projected_state_without(
                removed_binding_ids
            )
    target.edges = controller._build_edges(target.task, target.sessions)
    target.gateways = {}
    target.gateway_acks = []
    return target


def _local_and_adjacent_gateways(
    stable: TaskSubnet,
    event: RuntimeEvent,
) -> set[str]:
    removed = set(event.agent_ids)
    local = {
        stable.application_agents[agent_id].gateway_id for agent_id in removed
    }
    selected = set(local)
    for session in stable.sessions:
        if session.source not in removed and session.target not in removed:
            continue
        path = session.gateway_path or (session.source_gateway, session.target_gateway)
        for index, gateway_id in enumerate(path):
            if gateway_id not in local:
                continue
            if index > 0:
                selected.add(path[index - 1])
            if index + 1 < len(path):
                selected.add(path[index + 1])
    return selected


def _validate_removal_event(stable: TaskSubnet, event: RuntimeEvent) -> None:
    if event.event_type != RuntimeEventType.AGENT_REMOVE.value:
        raise ValueError(f"unsupported strategy event: {event.event_type}")
    if event.task_id != stable.task.task_id:
        raise ValueError("event task does not match stable task")
    missing = set(event.agent_ids) - set(stable.application_agents)
    if missing:
        raise ValueError("unknown business Agent(s): " + ",".join(sorted(missing)))


STRATEGIES = {
    "proposed": ProposedIncrementalStrategy,
    "proposed_incremental": ProposedIncrementalStrategy,
    "full_rebuild": FullRebuildStrategy,
    "local_only": LocalOnlyStrategy,
}


def make_strategy(
    name: str,
    controller,
    scope_analyzer: ImpactScopeAnalyzer | None = None,
):
    normalized = name.strip().lower().replace("-", "_")
    try:
        strategy_type = STRATEGIES[normalized]
    except KeyError as error:
        raise ValueError(f"unsupported elasticity method: {name}") from error
    return strategy_type(controller, scope_analyzer)
