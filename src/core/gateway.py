from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from src.agents.base import BaseAgent
from src.core.models import AgentCard, GatewayAck, GatewayRouteEntry, SessionSpec, TaskSpec


@dataclass
class Gateway:
    gateway_id: str
    subnet_id: str
    node: str
    gateway_ip: str = ""
    gateway_port: int = 7000
    agents: dict[str, BaseAgent] = field(default_factory=dict)
    installed_sessions: dict[str, SessionSpec] = field(default_factory=dict)
    route_table: dict[str, GatewayRouteEntry] = field(default_factory=dict)
    update_count: int = 0
    online: bool = True

    def register(self, agent: BaseAgent) -> None:
        self.agents[agent.agent_id] = agent

    def fail_agent(self, agent_id: str) -> bool:
        """Simulate a local Agent going offline (F_m event). Returns success."""
        agent = self.agents.get(agent_id)
        if agent is None:
            return False
        agent.card = replace(agent.card, status="offline")
        return True

    async def confirm_member(self, agent_id: str) -> AgentCard | None:
        await asyncio.sleep(0)
        agent = self.agents.get(agent_id)
        if agent is None or not agent.card.online:
            return None
        return agent.card

    async def confirm_support(self, capability: str) -> list[AgentCard]:
        await asyncio.sleep(0)
        return [
            agent.card
            for agent in self.agents.values()
            if agent.card.online and capability in agent.card.capabilities
        ]

    async def install_subnet(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
        route_entries: list[GatewayRouteEntry],
    ) -> GatewayAck:
        await asyncio.sleep(0)
        rejected = self._validate_route_entries(task.task_id, route_entries, operation="install")
        if rejected is not None:
            return rejected
        installed_session_ids: list[str] = []
        for session in sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
                installed_session_ids.append(session.session_id)
        installed_route_ids: list[str] = []
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                key = entry_key(entry)
                self.route_table[key] = entry
                installed_route_ids.append(key)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task.task_id,
            accepted=True,
            operation="install",
            installed_session_ids=tuple(sorted(installed_session_ids)),
            installed_route_ids=tuple(sorted(installed_route_ids)),
            session_count=len(installed_session_ids),
            route_count=len(installed_route_ids),
        )

    async def apply_update(
        self,
        task_id: str,
        changed_sessions: list[SessionSpec],
        route_entries: list[GatewayRouteEntry],
    ) -> GatewayAck:
        await asyncio.sleep(0)
        self.update_count += 1
        rejected = self._validate_route_entries(task_id, route_entries, operation="update")
        if rejected is not None:
            return rejected
        installed_session_ids: list[str] = []
        for session in changed_sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
                installed_session_ids.append(session.session_id)
        installed_route_ids: list[str] = []
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                key = entry_key(entry)
                self.route_table[key] = entry
                installed_route_ids.append(key)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="update",
            installed_session_ids=tuple(sorted(installed_session_ids)),
            installed_route_ids=tuple(sorted(installed_route_ids)),
            session_count=len(installed_session_ids),
            route_count=len(installed_route_ids),
        )

    def installed_route_table(self) -> list[GatewayRouteEntry]:
        return sorted(
            self.route_table.values(),
            key=lambda item: (item.task_id, item.session_id, item.match.dst_agent, item.action.mode),
        )

    def _validate_route_entries(
        self,
        task_id: str,
        route_entries: list[GatewayRouteEntry],
        *,
        operation: str,
    ) -> GatewayAck | None:
        if not self.online:
            return self._reject(task_id, operation, "gateway_offline")
        for entry in route_entries:
            if entry.gateway_id != self.gateway_id:
                return self._reject(task_id, operation, f"route_for_other_gateway:{entry.gateway_id}")
            action = entry.action
            if action.mode == "local_delivery":
                agent_id = action.local_agent or ""
                agent = self.agents.get(agent_id)
                if agent is None or not agent.card.online:
                    return self._reject(task_id, operation, f"local_agent_unavailable:{agent_id}")
                if not action.local_agent_ip:
                    return self._reject(task_id, operation, f"local_agent_ip_missing:{agent_id}")
            elif action.mode == "forward_to_gateway":
                if not action.next_hop_gateway or not action.next_hop_gateway_ip:
                    return self._reject(task_id, operation, f"next_hop_missing:{entry.session_id}")
            elif action.mode != "deny":
                return self._reject(task_id, operation, f"unsupported_route_action:{action.mode}")
        return None

    def _reject(self, task_id: str, operation: str, reason: str) -> GatewayAck:
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=False,
            reason=reason,
            operation=operation,
        )


def entry_key(entry: GatewayRouteEntry) -> str:
    return f"{entry.task_id}:{entry.session_id}:{entry.gateway_id}:{entry.hop_index}:{entry.action.mode}"


def _session_gateways(session: SessionSpec) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)
