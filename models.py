from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AgentRole(str, Enum):
    BUSINESS = "business"
    SUPPORT = "support"


class LayerType(str, Enum):
    APPLICATION = "application"
    NETWORK = "network"
    PHYSICAL = "physical"


class MessageType(str, Enum):
    BUSINESS = "business_msg"
    STATE = "state_msg"
    CONTROL = "control_msg"


class SubnetState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    UPDATING = "UPDATING"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class QoSRequirements:
    max_latency_ms: int
    min_bandwidth_mbps: int
    reliability: float
    priority: int


@dataclass(frozen=True)
class SupportRequirements:
    physical: tuple[str, ...] = ()
    network: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessFlow:
    source: str
    target: str
    flow_type: str
    priority: int = 1
    bandwidth_mbps: int = 1
    latency_budget_ms: int = 100


@dataclass(frozen=True)
class Intent:
    task_id: str
    semantic_goal: str
    business_agents: tuple[str, ...]
    business_graph: tuple[BusinessFlow, ...]
    qos_requirements: QoSRequirements
    support_requirements: SupportRequirements


@dataclass(frozen=True)
class AgentCard:
    agent_id: str
    name: str
    capabilities: tuple[str, ...]
    endpoint: str
    node: str
    gateway_id: str
    protocol: str
    status: str
    agent_role: AgentRole
    layer_type: LayerType
    state_items: tuple[str, ...] = ()
    link_id: str | None = None
    path_id: str | None = None


@dataclass(frozen=True)
class GatewayRule:
    task_id: str
    source: str
    target: str
    source_gateway: str
    target_gateway: str
    relation: str
    message_type: MessageType
    priority: int
    action: str = "allow"
    policy: str = "normal"

    @property
    def key(self) -> tuple[str, str, str, MessageType]:
        return (self.task_id, self.source, self.target, self.message_type)


@dataclass(frozen=True)
class RuleDelta:
    task_id: str
    reason: str
    add_rules: tuple[GatewayRule, ...] = ()
    remove_rule_keys: tuple[tuple[str, str, str, MessageType], ...] = ()
    update_rules: tuple[GatewayRule, ...] = ()

    @property
    def changed_rule_count(self) -> int:
        return len(self.add_rules) + len(self.remove_rule_keys) + len(self.update_rules)


@dataclass(frozen=True)
class Message:
    task_id: str
    message_type: MessageType
    source: str
    target: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Metrics:
    build_time_ms: float = 0.0
    total_rules: int = 0
    involved_gateways: int = 0
    blocked_flows: int = 0
    delta_rules: int = 0
    delta_update_time_ms: float = 0.0


@dataclass
class TaskCommunicationGraph:
    business_agents: set[str] = field(default_factory=set)
    support_agents: set[str] = field(default_factory=set)
    business_edges: set[tuple[str, str]] = field(default_factory=set)
    support_edges: set[tuple[str, str]] = field(default_factory=set)

