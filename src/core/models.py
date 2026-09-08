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
    PLANNING = "planning"
    STAGED = "staged"
    VERIFIED = "verified"
    ACTIVATING = "activating"
    STABLE = "stable"
    # Compatibility alias for callers written before versioned transactions.
    NETWORKED = "stable"
    ROLLING_BACK = "rolling_back"
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
    edge_id: str = ""
    max_loss_rate: float | None = None
    min_reliability: float | None = None

    def __post_init__(self) -> None:
        if not self.edge_id:
            object.__setattr__(
                self,
                "edge_id",
                f"{self.source}->{self.target}:{self.flow_type}",
            )


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
    ip: str = ""
    port: int | None = None

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
class ApplicationAgentState:
    agent_id: str
    gateway_id: str
    node: str = ""
    data_rate_mbps: float = 0.0
    task_stage: str = "active"
    priority: int = 1
    online: bool = True


@dataclass(frozen=True)
class TransportAgentState:
    agent_id: str
    gateway_id: str
    session_ids: tuple[str, ...] = field(default_factory=tuple)
    connection_active: bool = True
    protocol: str = "tcp"
    send_rate_mbps: float = 0.0
    retransmission_rate: float = 0.0
    reliability: float = 1.0
    online: bool = True


@dataclass(frozen=True)
class NetworkAgentState:
    agent_id: str
    gateway_id: str
    path_ids: tuple[str, ...] = field(default_factory=tuple)
    reachable: bool = True
    available_capacity_mbps: float = 0.0
    latency_ms: float = 0.0
    queue_utilization: float = 0.0
    loss_rate: float = 0.0
    online: bool = True


@dataclass(frozen=True)
class PhysicalAgentState:
    agent_id: str
    gateway_id: str
    signal_quality: float
    available_capacity_mbps: float
    resource_utilization: float
    reliability: float
    online: bool
    total_capacity_mbps: float = 0.0
    binding_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PhysicalResourceBinding:
    binding_id: str
    task_id: str
    edge_id: str
    agent_id: str
    gateway_id: str
    reserved_capacity_mbps: float
    version: int
    active: bool = True


@dataclass(frozen=True)
class RouteState:
    route_id: str
    business_edge_id: str
    gateway_path: tuple[str, ...]
    path_id: str = ""
    active: bool = True


@dataclass(frozen=True)
class GatewayState:
    gateway_id: str
    online: bool
    stable_version: int
    staged_version: int | None = None
    stable_rule_ids: tuple[str, ...] = field(default_factory=tuple)
    staged_rule_ids: tuple[str, ...] = field(default_factory=tuple)


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
    path_id: str = ""
    gateway_path: tuple[str, ...] = field(default_factory=tuple)
    status: str = "established"
    business_edge_id: str = ""
    p_agent_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PathSupportSpec:
    support_id: str
    path_id: str
    gateway_path: tuple[str, ...]
    n_agent_id: str
    monitored_links: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    support_scope: str = "path"
    p_agent_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SessionSupportSpec:
    support_id: str
    session_id: str
    t_agent_id: str
    path_support_id: str
    path_id: str
    gateway_path: tuple[str, ...]
    p_agent_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AgentConfirmAck:
    task_id: str
    gateway_id: str
    agent_id: str
    accepted: bool
    purpose: str
    layer: AgentLayer | None = None
    role: AgentRole | None = None
    endpoint: str = ""
    ip: str = ""
    port: int | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    reason: str = "ok"


@dataclass(frozen=True)
class FlowMatch:
    src_agent: str
    dst_agent: str
    flow_type: str
    protocol: str = "tcp"
    dst_port: int | None = None


@dataclass(frozen=True)
class GatewayRouteAction:
    mode: str  # local_delivery | forward_to_gateway | deny
    allow: bool
    next_hop_gateway: str | None = None
    next_hop_gateway_ip: str | None = None
    local_agent: str | None = None
    local_agent_ip: str | None = None
    local_agent_port: int | None = None
    dscp: int = 0
    priority: int = 1


@dataclass(frozen=True)
class GatewayRouteEntry:
    task_id: str
    session_id: str
    gateway_id: str
    flow_id: str
    match: FlowMatch
    action: GatewayRouteAction
    t_agent_id: str
    n_agent_id: str
    p_agent_id: str | None = None
    session_support_id: str = ""
    path_support_id: str = ""
    path_id: str = ""
    gateway_path: tuple[str, ...] = field(default_factory=tuple)
    hop_index: int = 0
    latency_budget_ms: float = 0.0
    min_bandwidth_mbps: float = 0.0
    status: str = "installed"
    version: int = 1
    rule_id: str = ""
    route_id: str = ""
    p_agent_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.rule_id:
            object.__setattr__(
                self,
                "rule_id",
                f"{self.task_id}:{self.session_id}:{self.gateway_id}:{self.hop_index}",
            )
        if not self.route_id:
            object.__setattr__(self, "route_id", f"{self.session_id}:route")

    @property
    def src_agent(self) -> str:
        return self.match.src_agent

    @property
    def dst_agent(self) -> str:
        return self.match.dst_agent

    @property
    def next_hop(self) -> str:
        return (
            self.action.next_hop_gateway
            or self.action.local_agent
            or ""
        )

    @property
    def priority(self) -> int:
        return self.action.priority

    @property
    def allowed(self) -> bool:
        return self.action.allow


@dataclass(frozen=True)
class GatewayAck:
    gateway_id: str
    task_id: str
    accepted: bool
    reason: str = "ok"
    operation: str = "install"
    installed_session_ids: tuple[str, ...] = field(default_factory=tuple)
    installed_route_ids: tuple[str, ...] = field(default_factory=tuple)
    session_count: int = 0
    route_count: int = 0
    version: int = 0


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
    version: int = 0
    application_agents: dict[str, ApplicationAgentState] = field(default_factory=dict)
    transport_agents: dict[str, TransportAgentState] = field(default_factory=dict)
    network_agents: dict[str, NetworkAgentState] = field(default_factory=dict)
    physical_agents: dict[str, PhysicalAgentState] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    sessions: list[SessionSpec] = field(default_factory=list)
    involved_gateways: set[str] = field(default_factory=set)
    gateway_routes: dict[str, list[GatewayRouteEntry]] = field(default_factory=dict)
    path_supports: dict[str, PathSupportSpec] = field(default_factory=dict)
    session_supports: dict[str, SessionSupportSpec] = field(default_factory=dict)
    physical_bindings: dict[str, PhysicalResourceBinding] = field(default_factory=dict)
    gateways: dict[str, GatewayState] = field(default_factory=dict)
    monitored_edge_ids: set[str] = field(default_factory=set)
    agent_acks: list[AgentConfirmAck] = field(default_factory=list)
    state: TaskState = TaskState.CREATED
    gateway_acks: list[GatewayAck] = field(default_factory=list)
    predictions: dict[str, AgentPrediction] = field(default_factory=dict)
    actions: list[AgentAction] = field(default_factory=list)
    cross_layer_runtime: Any | None = None
    authorized_cross_layer_actions: tuple[Any, ...] = field(default_factory=tuple)

    @property
    def status(self) -> str:
        return self.state.value

    @property
    def app_agents(self) -> set[str]:
        return set(self.application_agents)

    @property
    def trans_agents(self) -> set[str]:
        return set(self.transport_agents)

    @property
    def net_agents(self) -> set[str]:
        return set(self.network_agents)

    @property
    def phy_agents(self) -> set[str]:
        return set(self.physical_agents)

    @property
    def business_agents(self) -> dict[str, ApplicationAgentState]:
        return self.application_agents

    @property
    def business_edges(self) -> dict[str, BusinessEdge]:
        return {edge.edge_id: edge for edge in self.task.biz_edges}

    @property
    def session_map(self) -> dict[str, SessionSpec]:
        return {session.session_id: session for session in self.sessions}

    @property
    def routes(self) -> dict[str, RouteState]:
        return {
            f"{session.session_id}:route": RouteState(
                route_id=f"{session.session_id}:route",
                business_edge_id=session.business_edge_id,
                gateway_path=session.gateway_path,
                path_id=session.path_id,
                active=session.status not in {"removed", "failed"},
            )
            for session in self.sessions
        }

    @property
    def rules(self) -> dict[str, GatewayRouteEntry]:
        return {
            rule.rule_id: rule
            for rules in self.gateway_routes.values()
            for rule in rules
        }

    @property
    def qos_requirements(self) -> dict[str, float | int]:
        return asdict(self.task.qos)


# Compatibility names for the versioned state model. They deliberately alias
# the existing runtime objects so there is only one source of truth.
TaskSubnetState = TaskSubnet
Rule = GatewayRouteEntry
Session = SessionSpec
BusinessAgent = ApplicationAgentState


@dataclass
class ExperimentMetrics:
    networking_success: bool = False
    controller_compute_ms: float = 0.0
    formation_latency_ms: float = 0.0
    controller_build_ms: float = 0.0
    # Backward-compatible alias. This measures controller-side construction
    # only; it is not end-to-end task-subnet build latency.
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
    estimated_interruption_ms: float = 0.0
    # Backward-compatible alias for the CostModel estimate.
    service_interruption_ms: float = 0.0
    prediction_mae: float = 0.0
    mapping_latency_ms: float = 0.0
    binding_latency_ms: float = 0.0
    feasibility_latency_ms: float = 0.0
    compile_latency_ms: float = 0.0
    stage_latency_ms: float = 0.0
    verification_latency_ms: float = 0.0
    activation_latency_ms: float = 0.0
    t_task_received: float = 0.0
    t_mapping_finished: float = 0.0
    t_layer_binding_finished: float = 0.0
    t_feasibility_finished: float = 0.0
    t_compile_finished: float = 0.0
    t_stage_started: float = 0.0
    t_stage_finished: float = 0.0
    t_staged_verify_finished: float = 0.0
    t_activate_finished: float = 0.0
    t_stable_verify_finished: float = 0.0
    control_messages: int = 0
    control_bytes: int = 0
    rollback_triggered: bool = False
    rollback_success: bool = False
    failure_reason: str = ""
    transaction_event_log: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.controller_compute_ms == 0.0 and self.controller_build_ms != 0.0:
            self.controller_compute_ms = self.controller_build_ms
        elif self.controller_build_ms == 0.0 and self.controller_compute_ms != 0.0:
            self.controller_build_ms = self.controller_compute_ms
        if self.controller_build_ms == 0.0 and self.networking_latency_ms != 0.0:
            self.controller_build_ms = self.networking_latency_ms
        elif self.networking_latency_ms == 0.0 and self.controller_build_ms != 0.0:
            self.networking_latency_ms = self.controller_build_ms

        if self.estimated_interruption_ms == 0.0 and self.service_interruption_ms != 0.0:
            self.estimated_interruption_ms = self.service_interruption_ms
        elif self.service_interruption_ms == 0.0 and self.estimated_interruption_ms != 0.0:
            self.service_interruption_ms = self.estimated_interruption_ms


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_jsonable(item) for item in sorted(value)]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value
