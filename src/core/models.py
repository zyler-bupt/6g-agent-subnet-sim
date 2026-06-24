from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class AgentLayer(str, Enum):
    APPLICATION = "app"
    TRANSPORT = "trans"
    NETWORK = "net"
    PHYSICAL = "phy"


class AgentRole(str, Enum):
    BUSINESS = "business"
    SUPPORT = "support"


class MessageKind(str, Enum):
    MEMBER_CONFIRM = "member_confirm"
    MEMBER_CONFIRM_ACK = "member_confirm_ack"
    SUBNET_INSTALL = "subnet_install"
    SUBNET_INSTALL_ACK = "subnet_install_ack"
    STATE_REPORT = "state_report"
    CONTROL_UPDATE = "control_update"


class TaskState(str, Enum):
    CREATED = "created"
    NETWORKING = "networking"
    NETWORKED = "networked"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class QoSRequirements:
    max_latency_ms: float
    max_loss_rate: float
    min_reliability: float
    min_bandwidth_mbps: float
    data_volume_mb: float
    priority: int = 1
    risk_threshold: float = 1.0


@dataclass(frozen=True)
class BusinessEdge:
    source: str
    target: str
    flow_type: str
    data_rate_mbps: float
    latency_budget_ms: float
    priority: int = 1


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    goal: str
    app_agents: tuple[str, ...]
    biz_edges: tuple[BusinessEdge, ...]
    qos: QoSRequirements


@dataclass(frozen=True)
class AgentState:
    values: dict[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentCard:
    agent_id: str
    name: str
    layer: AgentLayer
    role: AgentRole
    gateway_id: str
    subnet_id: str
    node: str
    endpoint: str
    capabilities: tuple[str, ...]
    status: str = "online"
    state: AgentState = field(default_factory=AgentState)

    @property
    def online(self) -> bool:
        return self.status == "online"


@dataclass(frozen=True)
class AgentPrediction:
    agent_id: str
    layer: AgentLayer
    metric: str
    horizon: int
    values: tuple[float, ...]


@dataclass(frozen=True)
class AgentAction:
    agent_id: str
    layer: AgentLayer
    action_type: str
    params: dict[str, float | int | str | bool] = field(default_factory=dict)
    expected_effect: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    task_id: str
    source: str
    target: str
    t_agent_id: str
    n_agent_id: str
    source_gateway: str
    target_gateway: str
    latency_budget_ms: float
    data_rate_mbps: float
    status: str = "established"


@dataclass(frozen=True)
class GatewayAck:
    gateway_id: str
    task_id: str
    accepted: bool
    reason: str = "ok"


@dataclass(frozen=True)
class Message:
    kind: MessageKind
    source: str
    target: str
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSubnet:
    task: TaskSpec
    app_agents: set[str] = field(default_factory=set)
    trans_agents: set[str] = field(default_factory=set)
    net_agents: set[str] = field(default_factory=set)
    phy_agents: set[str] = field(default_factory=set)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    sessions: list[SessionSpec] = field(default_factory=list)
    involved_gateways: set[str] = field(default_factory=set)
    state: TaskState = TaskState.CREATED
    gateway_acks: list[GatewayAck] = field(default_factory=list)
    predictions: dict[str, AgentPrediction] = field(default_factory=dict)
    actions: list[AgentAction] = field(default_factory=list)


@dataclass
class ExperimentMetrics:
    networking_success: bool = False
    networking_latency_ms: float = 0.0
    session_count: int = 0
    involved_gateway_count: int = 0
    qos_satisfied: bool = False
    risk_before: float = 0.0
    risk_after: float = 0.0
    changed_agents: int = 0
    changed_edges: int = 0
    changed_gateways: int = 0
    control_updates: int = 0
    service_interruption_ms: float = 0.0
    prediction_mae: float = 0.0


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

