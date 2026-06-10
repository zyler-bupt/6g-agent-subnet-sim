from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models import AgentCard, AgentRole, LayerType, Message, MessageType


@dataclass
class SimulatedAgent:
    card: AgentCard

    @property
    def agent_id(self) -> str:
        return self.card.agent_id


class BusinessAgent(SimulatedAgent):
    def create_business_message(
        self,
        task_id: str,
        target: str,
        content: str,
        **extra: Any,
    ) -> Message:
        payload = {"content": content}
        payload.update(extra)
        return Message(
            task_id=task_id,
            message_type=MessageType.BUSINESS,
            source=self.agent_id,
            target=target,
            payload=payload,
        )


class NetworkSupportAgent(SimulatedAgent):
    def report_path_state(
        self,
        task_id: str,
        target: str,
        path_state: str,
        bandwidth_mbps: int,
        congestion_level: float,
    ) -> Message:
        return Message(
            task_id=task_id,
            message_type=MessageType.STATE,
            source=self.agent_id,
            target=target,
            payload={
                "layer": LayerType.NETWORK.value,
                "path_id": self.card.path_id,
                "path_state": path_state,
                "bandwidth_mbps": bandwidth_mbps,
                "congestion_level": congestion_level,
            },
        )


class PhysicalSupportAgent(SimulatedAgent):
    def report_link_state(
        self,
        task_id: str,
        target: str,
        link_quality: str,
        channel_available: bool,
        signal_score: int,
    ) -> Message:
        return Message(
            task_id=task_id,
            message_type=MessageType.STATE,
            source=self.agent_id,
            target=target,
            payload={
                "layer": LayerType.PHYSICAL.value,
                "link_id": self.card.link_id,
                "link_quality": link_quality,
                "channel_available": channel_available,
                "signal_score": signal_score,
            },
        )


def business_agent(
    agent_id: str,
    name: str,
    capabilities: tuple[str, ...],
    node: str,
    gateway_id: str,
) -> BusinessAgent:
    return BusinessAgent(
        AgentCard(
            agent_id=agent_id,
            name=name,
            capabilities=capabilities,
            endpoint=f"{gateway_id}://{agent_id}",
            node=node,
            gateway_id=gateway_id,
            protocol="sim-msg",
            status="available",
            agent_role=AgentRole.BUSINESS,
            layer_type=LayerType.APPLICATION,
        )
    )


def n_agent(
    agent_id: str,
    name: str,
    node: str,
    gateway_id: str,
    path_id: str,
) -> NetworkSupportAgent:
    return NetworkSupportAgent(
        AgentCard(
            agent_id=agent_id,
            name=name,
            capabilities=("path_state_monitor", "bandwidth_monitor", "congestion_monitor"),
            endpoint=f"{gateway_id}://{agent_id}",
            node=node,
            gateway_id=gateway_id,
            protocol="sim-state",
            status="available",
            agent_role=AgentRole.SUPPORT,
            layer_type=LayerType.NETWORK,
            state_items=("path_state", "bandwidth_mbps", "congestion_level"),
            path_id=path_id,
        )
    )


def p_agent(
    agent_id: str,
    name: str,
    node: str,
    gateway_id: str,
    link_id: str,
) -> PhysicalSupportAgent:
    return PhysicalSupportAgent(
        AgentCard(
            agent_id=agent_id,
            name=name,
            capabilities=("wireless_link_monitor", "channel_availability_monitor"),
            endpoint=f"{gateway_id}://{agent_id}",
            node=node,
            gateway_id=gateway_id,
            protocol="sim-state",
            status="available",
            agent_role=AgentRole.SUPPORT,
            layer_type=LayerType.PHYSICAL,
            state_items=("link_quality", "channel_available", "signal_score"),
            link_id=link_id,
        )
    )

