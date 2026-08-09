from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, replace
from time import perf_counter
from typing import Any, Callable

from src.agents.phy_agent import PhyAgent
from src.controller.networking import AgentController
from src.core.events import EventInjector, RuntimeEvent, RuntimeEventType
from src.core.failures import FailureMonitorSample, FaultContext
from src.core.models import AgentLayer, AgentRole, TaskSpec, TaskSubnet, to_jsonable
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.topology import AgentSpec, TopologyCatalog, build_topology_from_catalog
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
)


AGENT_FAILURE_LEVELS = (
    "application_same_gateway",
    "application_different_gateway",
    "transport",
    "network",
    "physical",
)
LINK_FAILURE_LEVELS = (1.0, 0.8, 0.6, 0.4, 0.0)
PHYSICAL_DROP_LEVELS = (1.0, 0.8, 0.6, 0.4, 0.2)


@dataclass(frozen=True)
class FailureScenarioConfig:
    fault_type: str
    fault_level: str
    severity: float
    num_agents: int = 20
    edge_ratio: float = 1.5
    num_gateways: int = 4
    cross_gateway_edge_ratio: float = 0.5
    multi_hop: bool = True
    probe_interval_ms: float = 50.0
    detection_threshold: int = 3
    stable_health_windows: int = 3
    backup_available: bool = True
    post_plan_state_drift: bool = False

    def validate(self) -> None:
        if self.fault_type not in {
            RuntimeEventType.AGENT_FAILURE.value,
            RuntimeEventType.LINK_DEGRADATION.value,
            RuntimeEventType.LINK_FAILURE.value,
            RuntimeEventType.PHYSICAL_CAPACITY_DROP.value,
        }:
            raise ValueError(f"unsupported fault type: {self.fault_type}")
        if self.num_gateways < 4:
            raise ValueError("failure experiments require at least four gateways")
        if self.probe_interval_ms <= 0.0 or self.detection_threshold < 1:
            raise ValueError("monitor timing must be positive")


@dataclass(frozen=True)
class FaultScenarioSnapshot:
    seed: int
    config: FailureScenarioConfig
    task: TaskSpec
    catalog: TopologyCatalog
    gateway_paths: dict[tuple[str, str], tuple[str, ...]]
    target_edge_id: str
    target_agent_id: str
    local_backup_agent_id: str
    remote_backup_agent_id: str
    primary_path: tuple[str, ...]
    backup_path: tuple[str, ...]
    failed_link: tuple[str, str]
    fault_effective_at: float
    failure_detected_at: float
    monitor_samples: tuple[FailureMonitorSample, ...]

    @property
    def scenario(self) -> str:
        return f"{self.config.fault_type}:{self.config.fault_level}"

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @property
    def fault_fingerprint(self) -> str:
        payload = {
            "scenario_fingerprint": self.fingerprint,
            "fault_type": self.config.fault_type,
            "fault_level": self.config.fault_level,
            "severity": self.config.severity,
            "target_edge_id": self.target_edge_id,
            "target_agent_id": self.target_agent_id,
            "failed_link": self.failed_link,
            "monitor_samples": [asdict(item) for item in self.monitor_samples],
        }
        value = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(value).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "config": asdict(self.config),
            "task": to_jsonable(self.task),
            "catalog": to_jsonable(self.catalog),
            "gateway_paths": {
                f"{source}->{target}": list(path)
                for (source, target), path in sorted(self.gateway_paths.items())
            },
            "target_edge_id": self.target_edge_id,
            "target_agent_id": self.target_agent_id,
            "local_backup_agent_id": self.local_backup_agent_id,
            "remote_backup_agent_id": self.remote_backup_agent_id,
            "primary_path": list(self.primary_path),
            "backup_path": list(self.backup_path),
            "failed_link": list(self.failed_link),
            "fault_effective_at": self.fault_effective_at,
            "failure_detected_at": self.failure_detected_at,
            "monitor_samples": [asdict(item) for item in self.monitor_samples],
        }

    def instantiate(
        self,
    ) -> tuple[AgentController, SyntheticTaskSubnetVerifier, SyntheticMetricProvider]:
        provider = SyntheticMetricProvider(
            seed=self.seed,
            base_bandwidth_mbps=250.0,
            base_latency_ms=8.0,
            base_app_rate_mbps=2.0,
        )
        controller = AgentController(
            build_topology_from_catalog(self.catalog, provider),
            gateway_paths=self.gateway_paths,
        )
        return controller, SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0), provider

    def apply_fault(
        self,
        controller: AgentController,
        stable: TaskSubnet,
        *,
        injector: EventInjector | None = None,
    ) -> tuple[RuntimeEvent, FaultContext]:
        """Apply true fault state and then publish its already-recorded time."""

        session = next(
            item for item in stable.sessions if item.business_edge_id == self.target_edge_id
        )
        injector = injector or EventInjector(prefix="exp4")
        affected_edges: set[str] = {self.target_edge_id}
        failed_agent_id = ""
        failed_layer = ""
        replacement = ""
        physical_id = ""
        baseline_physical = 0.0
        physical_capacity = 0.0
        degraded_link_capacity = 0.0
        affected_demand = 0.0
        resolvable = True
        payload: dict[str, Any] = {
            "scenario_fingerprint": self.fingerprint,
            "fault_fingerprint": self.fault_fingerprint,
            "fault_level": self.config.fault_level,
            "severity": self.config.severity,
            "target_edge_id": self.target_edge_id,
            "failure_detected_at": self.failure_detected_at,
        }

        if self.config.fault_type == RuntimeEventType.AGENT_FAILURE.value:
            level = self.config.fault_level
            if level.startswith("application"):
                failed_agent_id = self.target_agent_id
                replacement = (
                    self.local_backup_agent_id
                    if level == "application_same_gateway"
                    else self.remote_backup_agent_id
                )
                failed_layer = "application"
                affected_edges = {
                    edge.edge_id
                    for edge in stable.task.biz_edges
                    if failed_agent_id in {edge.source, edge.target}
                }
            elif level == "transport":
                failed_agent_id = session.t_agent_id
                failed_layer = "transport"
            elif level == "network":
                failed_agent_id = session.n_agent_id
                failed_layer = "network"
            elif level == "physical":
                failed_agent_id = session.p_agent_ids[0]
                failed_layer = "physical"
                physical_id = failed_agent_id
            else:
                raise ValueError(f"unsupported Agent failure level: {level}")
            agent = controller._agent_by_id(failed_agent_id)
            if agent is None or not controller.gateways[agent.card.gateway_id].fail_agent(failed_agent_id):
                raise ValueError(f"cannot apply Agent failure: {failed_agent_id}")
            if not self.config.backup_available:
                replacement = ""
                resolvable = False
            payload.update(
                agent_id=failed_agent_id,
                agent_layer=failed_layer,
                replacement_agent_id=replacement,
            )
            event_type = RuntimeEventType.AGENT_FAILURE
        elif self.config.fault_type in {
            RuntimeEventType.LINK_DEGRADATION.value,
            RuntimeEventType.LINK_FAILURE.value,
        }:
            affected_edges = {
                item.business_edge_id
                for item in stable.sessions
                if self.failed_link in set(zip(item.gateway_path, item.gateway_path[1:]))
            }
            affected_demand = sum(
                item.data_rate_mbps
                for item in stable.sessions
                if item.business_edge_id in affected_edges
            )
            baseline = max(1.0, affected_demand * 1.20)
            degraded_link_capacity = baseline * self.config.severity
            payload.update(
                source_gateway=self.failed_link[0],
                target_gateway=self.failed_link[1],
                degradation_factor=self.config.severity,
                degraded_capacity_mbps=degraded_link_capacity,
            )
            event_type = (
                RuntimeEventType.LINK_FAILURE
                if self.config.severity <= 0.0
                else RuntimeEventType.LINK_DEGRADATION
            )
        else:
            physical_id = session.p_agent_ids[0]
            agent = controller._agent_by_id(physical_id)
            if not isinstance(agent, PhyAgent):
                raise ValueError(f"physical Agent unavailable: {physical_id}")
            task_reserved = sum(
                binding.reserved_capacity_mbps
                for binding in stable.physical_bindings.values()
                if binding.agent_id == physical_id
            )
            baseline_physical = max(1.0, task_reserved * 1.25)
            agent.configure_capacity(baseline_physical)
            physical_capacity = max(0.01, baseline_physical * self.config.severity)
            agent.configure_capacity(physical_capacity)
            affected_edges = {
                item.business_edge_id
                for item in stable.sessions
                if physical_id in item.p_agent_ids
            }
            payload.update(
                physical_agent_id=physical_id,
                capacity_factor=self.config.severity,
                baseline_capacity_mbps=baseline_physical,
                available_capacity_mbps=physical_capacity,
            )
            affected_demand = task_reserved
            event_type = RuntimeEventType.PHYSICAL_CAPACITY_DROP

        event = injector.fault(
            stable.task.task_id,
            event_type,
            payload=payload,
            occurred_at=self.fault_effective_at,
        )
        context = FaultContext(
            fault_type=event.event_type,
            fault_target=failed_agent_id or physical_id or "->".join(self.failed_link),
            fault_level=self.config.fault_level,
            severity=self.config.severity,
            fault_effective_at=self.fault_effective_at,
            failure_detected_at=self.failure_detected_at,
            affected_edge_ids=frozenset(affected_edges),
            failed_agent_id=failed_agent_id,
            failed_agent_layer=failed_layer,
            replacement_agent_id=replacement,
            failed_link=(
                self.failed_link
                if event.event_type
                in {
                    RuntimeEventType.LINK_FAILURE.value,
                    RuntimeEventType.LINK_DEGRADATION.value,
                }
                else ("", "")
            ),
            primary_path=self.primary_path,
            backup_path=self.backup_path,
            degraded_link_capacity_mbps=degraded_link_capacity,
            physical_agent_id=physical_id,
            physical_capacity_mbps=physical_capacity,
            baseline_physical_capacity_mbps=baseline_physical,
            resolvable=resolvable,
            metadata={
                "target_edge_id": self.target_edge_id,
                "scenario_fingerprint": self.fingerprint,
                "fault_fingerprint": self.fault_fingerprint,
                "stable_health_windows": self.config.stable_health_windows,
                "probe_interval_ms": self.config.probe_interval_ms,
                "affected_demand_mbps": affected_demand,
                "session_edge_map": {
                    item.session_id: item.business_edge_id for item in stable.sessions
                },
            },
        )
        return event, context


class FailureScenarioGenerator:
    def generate(self, config: FailureScenarioConfig, seed: int) -> FaultScenarioSnapshot:
        config.validate()
        base = ElasticScenarioGenerator().generate(
            ScenarioConfig(
                num_agents=config.num_agents,
                edge_ratio=config.edge_ratio,
                num_gateways=config.num_gateways,
                cross_gateway_edge_ratio=config.cross_gateway_edge_ratio,
                agent_removal_ratio=0.05,
                removed_agent_type="leaf",
                multi_hop=config.multi_hop,
            ),
            seed,
        )
        app_specs = {
            item.agent_id: item
            for item in base.catalog.agents
            if item.layer == AgentLayer.APPLICATION
        }
        cross_edges = [
            edge
            for edge in base.task.biz_edges
            if app_specs[edge.source].gateway_id != app_specs[edge.target].gateway_id
        ]
        if not cross_edges:
            raise ValueError("failure scenario requires a cross-gateway business edge")
        rng = random.Random(seed * 7919 + 17)
        target_edge = cross_edges[rng.randrange(len(cross_edges))]
        target_agent = target_edge.source
        source_gateway = app_specs[target_edge.source].gateway_id
        target_gateway = app_specs[target_edge.target].gateway_id
        gateways = tuple(item.gateway_id for item in base.catalog.gateways)
        intermediates = [item for item in gateways if item not in {source_gateway, target_gateway}]
        if len(intermediates) < 2:
            raise ValueError("two disjoint candidate paths require four gateways")
        rng.shuffle(intermediates)
        primary = (source_gateway, intermediates[0], target_gateway)
        backup = (source_gateway, intermediates[1], target_gateway)
        paths = dict(base.gateway_paths)
        paths[(source_gateway, target_gateway)] = primary
        failed_link = (primary[0], primary[1])

        remote_gateway = next(item for item in gateways if item != source_gateway)
        local_backup = f"{target_agent}-backup-local"
        remote_backup = f"{target_agent}-backup-remote"
        extras: list[AgentSpec] = [
            AgentSpec(
                agent_id=local_backup,
                name=f"Standby for {target_agent}",
                layer=AgentLayer.APPLICATION,
                role=AgentRole.BUSINESS,
                gateway_id=source_gateway,
                capabilities=("business_stage", "standby"),
                port=19001,
            ),
            AgentSpec(
                agent_id=remote_backup,
                name=f"Remote standby for {target_agent}",
                layer=AgentLayer.APPLICATION,
                role=AgentRole.BUSINESS,
                gateway_id=remote_gateway,
                capabilities=("business_stage", "standby"),
                port=19002,
            ),
        ]
        for gateway_id in gateways:
            extras.extend(_standby_support_specs(gateway_id))
        catalog = TopologyCatalog(
            gateways=base.catalog.gateways,
            agents=base.catalog.agents + tuple(extras),
        )
        task = replace(
            base.task,
            task_id=f"exp4-{config.fault_type.lower()}-{config.fault_level}-seed-{seed:04d}",
            goal="conflict- and failure-driven elastic reconfiguration",
        )
        fault_effective = 1000.0 + seed * 10.0 + _fault_time_offset(config.fault_type)
        detection = fault_effective + (
            config.probe_interval_ms * config.detection_threshold / 1000.0
        )
        monitor = tuple(
            FailureMonitorSample(
                timestamp=fault_effective + index * config.probe_interval_ms / 1000.0,
                sample_index=index,
                healthy=False,
                reachable=config.severity > 0.0,
                available_capacity_mbps=max(0.0, config.severity * 10.0),
                latency_ms=8.0 + (1.0 - config.severity) * 80.0,
                packet_loss_rate=min(1.0, (1.0 - config.severity) * 0.2),
            )
            for index in range(1, config.detection_threshold + 1)
        )
        return FaultScenarioSnapshot(
            seed=seed,
            config=config,
            task=task,
            catalog=catalog,
            gateway_paths=paths,
            target_edge_id=target_edge.edge_id,
            target_agent_id=target_agent,
            local_backup_agent_id=local_backup,
            remote_backup_agent_id=remote_backup,
            primary_path=primary,
            backup_path=backup,
            failed_link=failed_link,
            fault_effective_at=fault_effective,
            failure_detected_at=detection,
            monitor_samples=monitor,
        )


class SimulationMonotonicClock:
    """Maps measured processing time onto a shared logical detection instant."""

    def __init__(self, start_at: float, wall_clock: Callable[[], float] = perf_counter) -> None:
        self.start_at = float(start_at)
        self.wall_clock = wall_clock
        self._wall_start = wall_clock()
        self._last = self.start_at

    def __call__(self) -> float:
        value = self.start_at + max(0.0, self.wall_clock() - self._wall_start)
        self._last = max(self._last, value)
        return self._last


def _standby_support_specs(gateway_id: str) -> tuple[AgentSpec, ...]:
    return (
        AgentSpec(
            agent_id=f"tagent-{gateway_id}-standby",
            name=f"{gateway_id} standby transport Agent",
            layer=AgentLayer.TRANSPORT,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("transport_session", "rtt_monitor", "retransmission_control"),
        ),
        AgentSpec(
            agent_id=f"nagent-{gateway_id}-standby",
            name=f"{gateway_id} standby network Agent",
            layer=AgentLayer.NETWORK,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("network_bearer", "bandwidth_monitor", "congestion_monitor"),
        ),
        AgentSpec(
            agent_id=f"pagent-{gateway_id}-standby",
            name=f"{gateway_id} standby physical Agent",
            layer=AgentLayer.PHYSICAL,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("physical_access", "radio_resource_monitor", "physical_reservation"),
            state_values={
                "total_capacity_mbps": 10000.0,
                "signal_quality": 0.99,
                "reliability": 0.999,
            },
        ),
    )


def _fault_time_offset(fault_type: str) -> float:
    return {
        RuntimeEventType.AGENT_FAILURE.value: 1.0,
        RuntimeEventType.LINK_DEGRADATION.value: 2.0,
        RuntimeEventType.LINK_FAILURE.value: 2.0,
        RuntimeEventType.PHYSICAL_CAPACITY_DROP.value: 3.0,
    }[fault_type]
