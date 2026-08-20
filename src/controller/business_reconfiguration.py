from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from src.controller.impact import ImpactScope
from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
from src.core.events import RuntimeEvent
from src.core.models import SessionSpec, TaskSubnet
from src.core.rules import RuleDelta, compute_rule_delta
from src.simulation.business_change_generator import (
    BusinessChangeSnapshot,
    BusinessChangeType,
    exact_dependency_closure,
)


@dataclass(frozen=True)
class BusinessReconfigurationPlan(ReconfigurationPlan):
    scope_policy: str = ""
    escalation_tier: int = 1
    selected_edge_ids: frozenset[str] = field(default_factory=frozenset)
    ground_truth_affected_edge_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def changed_rule_ids(self) -> frozenset[str]:
        return frozenset(self.rule_delta.changed_rule_ids)


async def plan_business_change(
    controller,
    stable: TaskSubnet,
    snapshot: BusinessChangeSnapshot,
    method_id: str,
) -> BusinessReconfigurationPlan:
    """Compile one correct target, then apply the method's permitted scope policy."""

    if stable.task.task_id != snapshot.before_task.task_id:
        raise ValueError("stable task does not match the business-change snapshot")
    if stable.version < 1:
        raise ValueError("business reconfiguration requires a stable initial version")
    method_id = method_id.strip().lower()
    if method_id not in {"proposed", "netren", "local_only", "full_rebuild"}:
        raise ValueError(f"unsupported Exp.3 method: {method_id}")

    compilation = await controller.compile_task_subnet(
        snapshot.after_task,
        version=stable.version + 1,
    )
    target = compilation.subnet
    _stabilize_unchanged_session_ids(controller, stable, target)
    base_delta = compute_rule_delta(stable.rules.values(), target.rules.values())
    direct_edge_ids = _directly_changed_edge_ids(snapshot)
    verification_gateways = frozenset(
        stable.involved_gateways | target.involved_gateways
    )

    if method_id == "proposed":
        closure = exact_dependency_closure(snapshot.before_task, snapshot.change)
        selected_edge_ids = frozenset(closure.edge_ids | direct_edge_ids)
        delta = _delta_with_edge_updates(
            stable,
            target,
            base_delta,
            selected_edge_ids,
        )
        affected_gateways = frozenset(delta.gateway_ids)
        transaction_gateways = verification_gateways
        scope_policy = "exact_task_dependency_closure"
        escalation_tier = 1
        full_rule_install = False
    elif method_id == "netren":
        direct_gateways = _gateways_for_edges(stable, target, direct_edge_ids)
        delta = _delta_with_gateway_updates(
            stable,
            target,
            base_delta,
            direct_gateways,
        )
        selected_edge_ids = frozenset(
            _edges_on_gateways(stable, target, direct_gateways) | direct_edge_ids
        )
        affected_gateways = frozenset(delta.gateway_ids)
        transaction_gateways = verification_gateways
        scope_policy = "network_flow_resynthesis"
        escalation_tier = 2
        full_rule_install = False
    elif method_id == "local_only":
        selected_edge_ids = _one_hop_edge_ids(snapshot, stable, target)
        local_gateways = _directly_attached_gateways(snapshot, stable, target)
        candidate = _delta_with_edge_updates(
            stable,
            target,
            base_delta,
            selected_edge_ids,
        )
        delta = _filter_delta_gateways(candidate, local_gateways)
        affected_gateways = frozenset(local_gateways)
        # Non-local Gateways join only the zero-delta version barrier.  The
        # method's configuration write scope remains the directly attached
        # Gateway set above, and the common verifier still rejects missing
        # remote rule changes.
        transaction_gateways = verification_gateways
        scope_policy = "changed_object_plus_one_hop"
        escalation_tier = 1
        full_rule_install = False
    else:
        delta = _full_rebuild_delta(stable, target)
        selected_edge_ids = frozenset(
            set(stable.business_edges) | set(target.business_edges)
        )
        affected_gateways = verification_gateways
        transaction_gateways = verification_gateways
        scope_policy = "complete_task_subnet_rebuild"
        escalation_tier = 3
        full_rule_install = True

    scope = _impact_scope(
        stable,
        target,
        selected_edge_ids,
        snapshot.affected_edge_ids,
        delta,
        affected_gateways,
    )
    return BusinessReconfigurationPlan(
        method=method_id,
        target_state=target,
        impact_scope=scope,
        rule_delta_by_gateway=delta_by_gateway(delta, transaction_gateways),
        affected_gateways=affected_gateways,
        affected_layers=frozenset({"application", "transport", "network", "physical"}),
        transaction_gateways=transaction_gateways,
        verification_gateways=verification_gateways,
        full_rule_install=full_rule_install,
        planning_details={
            "scope_policy": scope_policy,
            "escalation_tier": escalation_tier,
            "direct_changed_edges": sorted(direct_edge_ids),
            "selected_edges": sorted(selected_edge_ids),
            "ground_truth_closure_fingerprint": snapshot.closure.fingerprint,
            "mapping_finished_at": compilation.mapping_finished_at,
            "layer_binding_finished_at": compilation.layer_binding_finished_at,
            "feasibility_finished_at": compilation.feasibility_finished_at,
            "compile_finished_at": compilation.compile_finished_at,
        },
        scope_policy=scope_policy,
        escalation_tier=escalation_tier,
        selected_edge_ids=frozenset(selected_edge_ids),
        ground_truth_affected_edge_ids=snapshot.affected_edge_ids,
    )


def make_business_change_event(
    snapshot: BusinessChangeSnapshot,
    *,
    occurred_at: float,
) -> RuntimeEvent:
    removed_agents = (
        [
            agent_id
            for agent_id in snapshot.change.changed_object_ids
            if agent_id in snapshot.before_task.app_agents
            and agent_id not in snapshot.after_task.app_agents
        ]
        if snapshot.change.change_type == BusinessChangeType.AGENT_REMOVE
        else []
    )
    return RuntimeEvent(
        event_id=snapshot.change.change_id,
        task_id=snapshot.before_task.task_id,
        event_type=snapshot.change.change_type.value.upper(),
        occurred_at=float(occurred_at),
        payload={
            "business_change_type": snapshot.change.change_type.value,
            "changed_object_ids": list(snapshot.change.changed_object_ids),
            "agent_ids": removed_agents,
            "affected_scope_ratio": snapshot.affected_scope_ratio,
            "event_fingerprint": snapshot.event_fingerprint,
        },
    )


def _stabilize_unchanged_session_ids(controller, stable: TaskSubnet, target: TaskSubnet) -> None:
    stable_by_edge = {
        session.business_edge_id: session.session_id for session in stable.sessions
    }
    sessions: list[SessionSpec] = []
    used_ids: set[str] = set()
    for session in target.sessions:
        session_id = stable_by_edge.get(session.business_edge_id)
        if session_id is None:
            session_id = f"{target.task.task_id}-sess-{session.business_edge_id}"
        if session_id in used_ids:
            raise ValueError(f"duplicate stabilized session id: {session_id}")
        used_ids.add(session_id)
        sessions.append(replace(session, session_id=session_id))
    target.sessions = sessions
    target.path_supports, target.session_supports = controller._build_support_bindings(
        sessions
    )
    target.transport_agents, target.network_agents = controller._build_support_states(
        target.task,
        sessions,
    )
    app_cards = controller._current_app_cards(target.task)
    target.application_agents = controller._build_application_states(
        target.task,
        app_cards,
    )
    target.gateway_routes = controller._build_gateway_route_tables(
        target.task,
        sessions,
        app_cards,
        version=target.version,
    )
    target.edges = controller._build_edges(target.task, sessions)
    target.involved_gateways = {
        gateway_id
        for session in sessions
        for gateway_id in (
            session.gateway_path
            or (session.source_gateway, session.target_gateway)
        )
    }
    target.monitored_edge_ids = {edge.edge_id for edge in target.task.biz_edges}


def _directly_changed_edge_ids(snapshot: BusinessChangeSnapshot) -> frozenset[str]:
    before = {edge.edge_id: edge for edge in snapshot.before_task.biz_edges}
    after = {edge.edge_id: edge for edge in snapshot.after_task.biz_edges}
    changed = set(before) ^ set(after)
    changed.update(
        edge_id
        for edge_id in set(before) & set(after)
        if before[edge_id] != after[edge_id]
    )
    return frozenset(changed)


def _delta_with_edge_updates(
    stable: TaskSubnet,
    target: TaskSubnet,
    base_delta: RuleDelta,
    edge_ids: Iterable[str],
) -> RuleDelta:
    selected = set(edge_ids)
    old_rules = stable.rules
    additions = {rule.rule_id: rule for rule in base_delta.additions}
    updates = {rule.rule_id: rule for rule in base_delta.updates}
    deletions = {rule.rule_id: rule for rule in base_delta.deletions}
    target_session_edge = {
        session.session_id: session.business_edge_id for session in target.sessions
    }
    for rule in target.rules.values():
        if (
            rule.rule_id in old_rules
            and target_session_edge.get(rule.session_id) in selected
        ):
            updates[rule.rule_id] = rule
    return RuleDelta(
        additions=tuple(additions[key] for key in sorted(additions)),
        updates=tuple(updates[key] for key in sorted(updates)),
        deletions=tuple(deletions[key] for key in sorted(deletions)),
    )


def _delta_with_gateway_updates(
    stable: TaskSubnet,
    target: TaskSubnet,
    base_delta: RuleDelta,
    gateway_ids: Iterable[str],
) -> RuleDelta:
    selected = set(gateway_ids)
    additions = {rule.rule_id: rule for rule in base_delta.additions}
    updates = {rule.rule_id: rule for rule in base_delta.updates}
    deletions = {rule.rule_id: rule for rule in base_delta.deletions}
    for rule in target.rules.values():
        if rule.rule_id in stable.rules and rule.gateway_id in selected:
            updates[rule.rule_id] = rule
    return RuleDelta(
        additions=tuple(additions[key] for key in sorted(additions)),
        updates=tuple(updates[key] for key in sorted(updates)),
        deletions=tuple(deletions[key] for key in sorted(deletions)),
    )


def _full_rebuild_delta(stable: TaskSubnet, target: TaskSubnet) -> RuleDelta:
    old_rules = stable.rules
    new_rules = target.rules
    return RuleDelta(
        additions=tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) - set(old_rules))
        ),
        updates=tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) & set(old_rules))
        ),
        deletions=tuple(
            old_rules[rule_id] for rule_id in sorted(set(old_rules) - set(new_rules))
        ),
    )


def _filter_delta_gateways(delta: RuleDelta, gateway_ids: Iterable[str]) -> RuleDelta:
    selected = set(gateway_ids)
    return RuleDelta(
        additions=tuple(rule for rule in delta.additions if rule.gateway_id in selected),
        updates=tuple(rule for rule in delta.updates if rule.gateway_id in selected),
        deletions=tuple(rule for rule in delta.deletions if rule.gateway_id in selected),
    )


def _gateways_for_edges(
    stable: TaskSubnet,
    target: TaskSubnet,
    edge_ids: Iterable[str],
) -> frozenset[str]:
    selected = set(edge_ids)
    return frozenset(
        gateway_id
        for subnet in (stable, target)
        for session in subnet.sessions
        if session.business_edge_id in selected
        for gateway_id in (
            session.gateway_path
            or (session.source_gateway, session.target_gateway)
        )
    )


def _edges_on_gateways(
    stable: TaskSubnet,
    target: TaskSubnet,
    gateway_ids: Iterable[str],
) -> set[str]:
    selected = set(gateway_ids)
    return {
        session.business_edge_id
        for subnet in (stable, target)
        for session in subnet.sessions
        if selected
        & set(
            session.gateway_path
            or (session.source_gateway, session.target_gateway)
        )
    }


def _one_hop_edge_ids(
    snapshot: BusinessChangeSnapshot,
    stable: TaskSubnet,
    target: TaskSubnet,
) -> frozenset[str]:
    anchors = set(snapshot.change.anchor_agent_ids)
    direct = set(_directly_changed_edge_ids(snapshot))
    selected = set(direct)
    for task in (stable.task, target.task):
        for edge in task.biz_edges:
            if edge.source in anchors or edge.target in anchors:
                selected.add(edge.edge_id)
    return frozenset(selected)


def _directly_attached_gateways(
    snapshot: BusinessChangeSnapshot,
    stable: TaskSubnet,
    target: TaskSubnet,
) -> frozenset[str]:
    gateways = set()
    for agent_id in snapshot.change.anchor_agent_ids:
        for subnet in (stable, target):
            state = subnet.application_agents.get(agent_id)
            if state is not None:
                gateways.add(state.gateway_id)
    if not gateways:
        gateways.update(
            _gateways_for_edges(
                stable,
                target,
                _directly_changed_edge_ids(snapshot),
            )
        )
    return frozenset(gateways)


def _impact_scope(
    stable: TaskSubnet,
    target: TaskSubnet,
    selected_edge_ids: Iterable[str],
    ground_truth_edge_ids: Iterable[str],
    delta: RuleDelta,
    affected_gateways: Iterable[str],
) -> ImpactScope:
    selected = set(selected_edge_ids)
    sessions = [
        session
        for subnet in (stable, target)
        for session in subnet.sessions
        if session.business_edge_id in selected
    ]
    session_ids = {session.session_id for session in sessions}
    agents = {
        agent_id
        for session in sessions
        for agent_id in (
            session.source,
            session.target,
            session.t_agent_id,
            session.n_agent_id,
            *session.p_agent_ids,
        )
    }
    route_ids = {f"{session.session_id}:route" for session in sessions}
    resources = {
        binding.binding_id
        for subnet in (stable, target)
        for binding in subnet.physical_bindings.values()
        if binding.edge_id in selected
    }
    ground_truth = set(ground_truth_edge_ids)
    return ImpactScope(
        affected_agents=frozenset(agents),
        affected_business_edges=frozenset(selected),
        affected_sessions=frozenset(session_ids),
        affected_routes=frozenset(route_ids),
        affected_physical_resources=frozenset(resources),
        affected_gateways=frozenset(affected_gateways),
        affected_rules=frozenset(delta.changed_rule_ids),
        unaffected_business_edges=frozenset(
            set(stable.business_edges) - ground_truth
        ),
    )
