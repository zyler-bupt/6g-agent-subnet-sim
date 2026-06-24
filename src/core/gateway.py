from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.agents.base import BaseAgent
from src.core.models import AgentCard, GatewayAck, SessionSpec, TaskSpec


@dataclass
class Gateway:
    gateway_id: str
    subnet_id: str
    node: str
    agents: dict[str, BaseAgent] = field(default_factory=dict)
    installed_sessions: dict[str, SessionSpec] = field(default_factory=dict)
    update_count: int = 0

    def register(self, agent: BaseAgent) -> None:
        self.agents[agent.agent_id] = agent

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

    async def install_subnet(self, task: TaskSpec, sessions: list[SessionSpec]) -> GatewayAck:
        await asyncio.sleep(0)
        for session in sessions:
            if self.gateway_id in {session.source_gateway, session.target_gateway}:
                self.installed_sessions[session.session_id] = session
        return GatewayAck(gateway_id=self.gateway_id, task_id=task.task_id, accepted=True)

    async def apply_update(self, task_id: str, changed_sessions: list[SessionSpec]) -> GatewayAck:
        await asyncio.sleep(0)
        self.update_count += 1
        for session in changed_sessions:
            if self.gateway_id in {session.source_gateway, session.target_gateway}:
                self.installed_sessions[session.session_id] = session
        return GatewayAck(gateway_id=self.gateway_id, task_id=task_id, accepted=True)

