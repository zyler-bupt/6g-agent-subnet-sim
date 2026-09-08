from __future__ import annotations

from dataclasses import dataclass, field

from src.core.events import RuntimeEvent, RuntimeEventType
from src.core.models import TaskSubnet


@dataclass(frozen=True)
class ImpactScope:
    affected_agents: frozenset[str] = field(default_factory=frozenset)
    affected_business_edges: frozenset[str] = field(default_factory=frozenset)
    affected_sessions: frozenset[str] = field(default_factory=frozenset)
    affected_routes: frozenset[str] = field(default_factory=frozenset)
    affected_physical_resources: frozenset[str] = field(default_factory=frozenset)
    affected_gateways: frozenset[str] = field(default_factory=frozenset)
    affected_rules: frozenset[str] = field(default_factory=frozenset)
    unaffected_business_edges: frozenset[str] = field(default_factory=frozenset)


class ImpactScopeAnalyzer:
    def analyze(self, subnet: TaskSubnet, event: RuntimeEvent) -> ImpactScope:
        if event.task_id != subnet.task.task_id:
            raise ValueError(
                f"event task {event.task_id} does not match subnet {subnet.task.task_id}"
            )
        if event.event_type != RuntimeEventType.AGENT_REMOVE.value:
            raise ValueError(f"unsupported runtime event: {event.event_type}")
        agent_ids = set(event.agent_ids)
        missing = agent_ids - set(subnet.application_agents)
        if missing:
            raise ValueError(
                "business Agent is not a task member: " + ",".join(sorted(missing))
            )

        affected_edges = {
            edge.edge_id
            for edge in subnet.task.biz_edges
            if edge.source in agent_ids or edge.target in agent_ids
        }
        affected_sessions = {
            session.session_id
            for session in subnet.sessions
            if session.business_edge_id in affected_edges
            or session.source in agent_ids
            or session.target in agent_ids
        }
        affected_support_agents = {
            support_agent_id
            for session in subnet.sessions
            if session.session_id in affected_sessions
            for support_agent_id in (
                session.t_agent_id,
                session.n_agent_id,
                *session.p_agent_ids,
            )
        }
        affected_routes = {
            f"{session.session_id}:route"
            for session in subnet.sessions
            if session.session_id in affected_sessions
        }
        affected_resources = {
            binding.binding_id
            for binding in subnet.physical_bindings.values()
            if binding.edge_id in affected_edges
        }
        affected_rules = {
            rule.rule_id
            for rule in subnet.rules.values()
            if rule.session_id in affected_sessions
            or rule.src_agent in agent_ids
            or rule.dst_agent in agent_ids
        }
        affected_gateways = {
            rule.gateway_id
            for rule in subnet.rules.values()
            if rule.rule_id in affected_rules
        }
        affected_gateways.update(
            binding.gateway_id
            for binding in subnet.physical_bindings.values()
            if binding.binding_id in affected_resources
        )
        affected_gateways.update(
            subnet.application_agents[agent_id].gateway_id for agent_id in agent_ids
        )
        unaffected = set(subnet.business_edges) - affected_edges
        return ImpactScope(
            affected_agents=frozenset(agent_ids | affected_support_agents),
            affected_business_edges=frozenset(affected_edges),
            affected_sessions=frozenset(affected_sessions),
            affected_routes=frozenset(affected_routes),
            affected_physical_resources=frozenset(affected_resources),
            affected_gateways=frozenset(affected_gateways),
            affected_rules=frozenset(affected_rules),
            unaffected_business_edges=frozenset(unaffected),
        )

    def analyze_failure(self, subnet: TaskSubnet, event: RuntimeEvent) -> ImpactScope:
        """Trace a runtime fault through edge/session/path/resource dependencies."""

        if event.task_id != subnet.task.task_id:
            raise ValueError(
                f"event task {event.task_id} does not match subnet {subnet.task.task_id}"
            )
        supported = {
            RuntimeEventType.AGENT_FAILURE.value,
            RuntimeEventType.LINK_DEGRADATION.value,
            RuntimeEventType.LINK_FAILURE.value,
            RuntimeEventType.PHYSICAL_CAPACITY_DROP.value,
            RuntimeEventType.GATEWAY_FAILURE.value,
        }
        if event.event_type not in supported:
            raise ValueError(f"unsupported failure event: {event.event_type}")

        failed_agent = str(event.payload.get("agent_id", ""))
        physical_agent = str(event.payload.get("physical_agent_id", ""))
        source_gateway = str(event.payload.get("source_gateway", ""))
        target_gateway = str(event.payload.get("target_gateway", ""))
        failed_gateway = str(event.payload.get("gateway_id", ""))
        failed_link = (source_gateway, target_gateway)

        affected_sessions: set[str] = set()
        affected_agents: set[str] = {value for value in (failed_agent, physical_agent) if value}
        for session in subnet.sessions:
            links = set(zip(session.gateway_path, session.gateway_path[1:]))
            incident_app = failed_agent in {session.source, session.target}
            incident_support = failed_agent in {
                session.t_agent_id,
                session.n_agent_id,
                *session.p_agent_ids,
            }
            incident_physical = physical_agent in session.p_agent_ids
            incident_link = bool(source_gateway and target_gateway and failed_link in links)
            incident_gateway = bool(failed_gateway and failed_gateway in session.gateway_path)
            if incident_app or incident_support or incident_physical or incident_link or incident_gateway:
                affected_sessions.add(session.session_id)

        affected_edges = {
            session.business_edge_id
            for session in subnet.sessions
            if session.session_id in affected_sessions
        }
        affected_agents.update(
            support_id
            for session in subnet.sessions
            if session.session_id in affected_sessions
            for support_id in (
                session.source,
                session.target,
                session.t_agent_id,
                session.n_agent_id,
                *session.p_agent_ids,
            )
        )
        affected_routes = {
            route.route_id
            for route in subnet.routes.values()
            if route.business_edge_id in affected_edges
        }
        affected_resources = {
            binding.binding_id
            for binding in subnet.physical_bindings.values()
            if binding.edge_id in affected_edges
            or (physical_agent and binding.agent_id == physical_agent)
        }
        affected_rules = {
            rule.rule_id
            for rule in subnet.rules.values()
            if rule.session_id in affected_sessions
            or (failed_gateway and rule.gateway_id == failed_gateway)
        }
        affected_gateways = {
            rule.gateway_id
            for rule in subnet.rules.values()
            if rule.rule_id in affected_rules
        }
        affected_gateways.update(
            gateway_id
            for session in subnet.sessions
            if session.session_id in affected_sessions
            for gateway_id in session.gateway_path
        )
        if failed_gateway:
            affected_gateways.add(failed_gateway)
        return ImpactScope(
            affected_agents=frozenset(affected_agents),
            affected_business_edges=frozenset(affected_edges),
            affected_sessions=frozenset(affected_sessions),
            affected_routes=frozenset(affected_routes),
            affected_physical_resources=frozenset(affected_resources),
            affected_gateways=frozenset(affected_gateways),
            affected_rules=frozenset(affected_rules),
            unaffected_business_edges=frozenset(
                set(subnet.business_edges) - affected_edges
            ),
        )
