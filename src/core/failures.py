from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FaultType(str, Enum):
    AGENT_FAILURE = "AGENT_FAILURE"
    LINK_DEGRADATION = "LINK_DEGRADATION"
    LINK_FAILURE = "LINK_FAILURE"
    PHYSICAL_CAPACITY_DROP = "PHYSICAL_CAPACITY_DROP"


@dataclass(frozen=True)
class FailureMonitorSample:
    timestamp: float
    sample_index: int
    healthy: bool
    reachable: bool
    available_capacity_mbps: float
    latency_ms: float
    packet_loss_rate: float


@dataclass(frozen=True)
class FaultContext:
    fault_type: str
    fault_target: str
    fault_level: str
    severity: float
    fault_effective_at: float
    failure_detected_at: float
    affected_edge_ids: frozenset[str]
    failed_agent_id: str = ""
    failed_agent_layer: str = ""
    replacement_agent_id: str = ""
    failed_link: tuple[str, str] = ("", "")
    primary_path: tuple[str, ...] = field(default_factory=tuple)
    backup_path: tuple[str, ...] = field(default_factory=tuple)
    degraded_link_capacity_mbps: float = 0.0
    physical_agent_id: str = ""
    physical_capacity_mbps: float = 0.0
    baseline_physical_capacity_mbps: float = 0.0
    resolvable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryProbeSample:
    run_id: str
    seed: int
    scenario: str
    method: str
    edge_id: str
    timestamp: float
    phase: str
    directly_affected: bool
    reachable: bool
    qos_satisfied: bool
    throughput_mbps: float
    latency_ms: float
    packet_loss_rate: float


@dataclass(frozen=True)
class RecoveryTimelineSample:
    run_id: str
    seed: int
    method: str
    logical_time_s: float
    phase: str
    application_demand_mbps: float
    actual_throughput_mbps: float
    network_capacity_mbps: float
    physical_capacity_mbps: float
    latency_ms: float
    qos_satisfied: bool
    failure_detected: bool
    action_selected: str
    activated: bool
    recovered: bool
