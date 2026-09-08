from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApplicationLayerState:
    task_id: str
    stage: str
    required_rate_mbps: float
    max_latency_ms: float
    max_loss_rate: float
    min_reliability: float
    priority: int
    quality_level: int


@dataclass(frozen=True)
class TransportLayerState:
    session_id: str
    send_rate_mbps: float
    congestion_window: float
    retransmission_rate: float
    rtt_ms: float
    multipath_enabled: bool
    reliability_mode: str
    reliability: float = 0.999
    # None preserves the legacy model in which send_rate_mbps doubled as the
    # transport admission ceiling. New scenarios should set this explicitly.
    admissible_capacity_mbps: float | None = None


@dataclass(frozen=True)
class NetworkLayerState:
    route_id: str
    available_bandwidth_mbps: float
    utilization: float
    queue_occupancy: float
    latency_ms: float
    packet_loss_rate: float
    reachable: bool
    candidate_routes: tuple[str, ...]
    selected_route: str = "primary"
    reliability: float = 0.999


@dataclass(frozen=True)
class PhysicalLayerState:
    access_id: str
    signal_quality: float
    available_capacity_mbps: float
    resource_utilization: float
    reliability: float
    online: bool
    access_latency_ms: float = 2.0


@dataclass(frozen=True)
class EdgeQoSConstraint:
    edge_id: str
    max_latency_ms: float
    max_loss_rate: float
    min_reliability: float
    shared_resource_id: str
    desired_rate_mbps: float


@dataclass(frozen=True)
class CrossLayerTaskState:
    task_id: str
    scenario: str
    pressure: float
    application: dict[str, ApplicationLayerState]
    transport: dict[str, TransportLayerState]
    network: dict[str, NetworkLayerState]
    physical: dict[str, PhysicalLayerState]
    constraints: dict[str, EdgeQoSConstraint]
    shared_resource_capacity_mbps: dict[str, float]
    applied_proposal_ids: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayerObservation:
    task_id: str
    layer: str
    edge_id: str
    observed_state: Any
    scenario: str
    pressure: float
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayerProposal:
    proposal_id: str
    task_id: str
    layer: str
    action: str
    target_objects: frozenset[str]
    read_set: frozenset[str]
    write_set: frozenset[str]
    expected_qos_gain: float
    expected_cost: float
    confidence: float
    required_bandwidth_mbps: float
    required_physical_capacity_mbps: float
    expected_latency_ms: float
    expected_loss_rate: float
    affected_edges: frozenset[str]
    affected_sessions: frozenset[str]
    affected_routes: frozenset[str]
    affected_gateways: frozenset[str]
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def utility(self) -> float:
        return self.expected_qos_gain * self.confidence - self.expected_cost

    @property
    def is_keep(self) -> bool:
        return self.action.startswith("KEEP_")


@dataclass(frozen=True)
class AuthorizedAction:
    proposal_id: str
    task_id: str
    layer: str
    action: str
    authorized_by: str
    target_objects: frozenset[str]
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorizedExecutionResult:
    authorized_actions: tuple[AuthorizedAction, ...]
    target_state: CrossLayerTaskState
    transaction_success: bool
    rollback_triggered: bool
    rollback_success: bool
    failure_reason: str = ""
