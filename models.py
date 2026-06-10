from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
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
    flow_type: str = "generic"
    latency_ms: int = 0
    bandwidth_mbps: int = 100
    drop_rate: float = 0.0
    congestion_level: float = 0.0

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
    agent_confirm_time_ms: float = 0.0
    rule_dispatch_time_ms: float = 0.0
    ready_gateway_count: int = 0
    rule_count: int = 0
    business_latency_ms: float = 0.0
    forward_success_count: int = 0
    drop_count: int = 0
    throughput_kbps: float = 0.0
    state_detection_time_ms: float = 0.0
    delta_generation_time_ms: float = 0.0
    delta_dispatch_time_ms: float = 0.0
    affected_gateway_count: int = 0
    updated_rule_count: int = 0
    service_interruption_ms: float = 0.0


@dataclass
class TaskCommunicationGraph:
    business_agents: set[str] = field(default_factory=set)
    support_agents: set[str] = field(default_factory=set)
    business_edges: set[tuple[str, str]] = field(default_factory=set)
    support_edges: set[tuple[str, str]] = field(default_factory=set)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [to_jsonable(item) for item in sorted(value)]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def qos_from_dict(data: dict[str, Any]) -> QoSRequirements:
    return QoSRequirements(**data)


def support_requirements_from_dict(data: dict[str, Any]) -> SupportRequirements:
    return SupportRequirements(
        physical=tuple(data.get("physical", ())),
        network=tuple(data.get("network", ())),
    )


def business_flow_from_dict(data: dict[str, Any]) -> BusinessFlow:
    return BusinessFlow(**data)


def intent_from_dict(data: dict[str, Any]) -> Intent:
    return Intent(
        task_id=data["task_id"],
        semantic_goal=data["semantic_goal"],
        business_agents=tuple(data["business_agents"]),
        business_graph=tuple(business_flow_from_dict(item) for item in data["business_graph"]),
        qos_requirements=qos_from_dict(data["qos_requirements"]),
        support_requirements=support_requirements_from_dict(data["support_requirements"]),
    )


def agent_card_from_dict(data: dict[str, Any]) -> AgentCard:
    return AgentCard(
        agent_id=data["agent_id"],
        name=data["name"],
        capabilities=tuple(data.get("capabilities", ())),
        endpoint=data["endpoint"],
        node=data["node"],
        gateway_id=data["gateway_id"],
        protocol=data["protocol"],
        status=data["status"],
        agent_role=AgentRole(data["agent_role"]),
        layer_type=LayerType(data["layer_type"]),
        state_items=tuple(data.get("state_items", ())),
        link_id=data.get("link_id"),
        path_id=data.get("path_id"),
    )


def gateway_rule_from_dict(data: dict[str, Any]) -> GatewayRule:
    allowed = {field.name for field in fields(GatewayRule)}
    values = {key: value for key, value in data.items() if key in allowed}
    values["message_type"] = MessageType(values["message_type"])
    return GatewayRule(**values)


def rule_delta_from_dict(data: dict[str, Any]) -> RuleDelta:
    return RuleDelta(
        task_id=data["task_id"],
        reason=data["reason"],
        add_rules=tuple(gateway_rule_from_dict(item) for item in data.get("add_rules", ())),
        remove_rule_keys=tuple(
            (item[0], item[1], item[2], MessageType(item[3]))
            for item in data.get("remove_rule_keys", ())
        ),
        update_rules=tuple(gateway_rule_from_dict(item) for item in data.get("update_rules", ())),
    )


def message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        task_id=data["task_id"],
        message_type=MessageType(data["message_type"]),
        source=data["source"],
        target=data["target"],
        payload=data.get("payload", {}),
    )
