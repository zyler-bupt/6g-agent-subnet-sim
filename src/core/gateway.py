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
        for session in sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                self.route_table[entry_key(entry)] = entry
        return GatewayAck(gateway_id=self.gateway_id, task_id=task.task_id, accepted=True)

    async def apply_update(
        self,
        task_id: str,
        changed_sessions: list[SessionSpec],
        route_entries: list[GatewayRouteEntry],
    ) -> GatewayAck:
        await asyncio.sleep(0)
        self.update_count += 1
        for session in changed_sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                self.route_table[entry_key(entry)] = entry
        return GatewayAck(gateway_id=self.gateway_id, task_id=task_id, accepted=True)

    def installed_route_table(self) -> list[GatewayRouteEntry]:
        return sorted(
            self.route_table.values(),
            key=lambda item: (item.task_id, item.session_id, item.match.dst_agent, item.action.mode),
        )


def entry_key(entry: GatewayRouteEntry) -> str:
    return f"{entry.task_id}:{entry.session_id}:{entry.gateway_id}:{entry.hop_index}:{entry.action.mode}"


def _session_gateways(session: SessionSpec) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)
