from __future__ import annotations

import random
from dataclasses import dataclass, replace

from experiments.paper_protocol import stable_fingerprint
from src.core.events import RuntimeEvent
from src.core.failures import FailureMonitorSample, FaultContext
from src.core.models import AgentLayer, AgentRole, TaskSpec, to_jsonable
from src.sim.topology import AgentSpec, TopologyCatalog
from src.simulation.failure_scenario_generator import (
    FailureScenarioConfig,
    FailureScenarioGenerator,
    FaultScenarioSnapshot,
)


PAPER_FAILURE_TYPES = (
    "agent_failure",
    "link_failure",
    "capacity_degradation",
)


@dataclass(frozen=True)
class PaperFailureSnapshot:
    seed: int
    event_id: int
    failure_type: str
    failure_severity: float
    base: FaultScenarioSnapshot
    compatible_replacements: tuple[str, ...]
    pre_capacity_mbps: float
    post_capacity_mbps: float

    @property
    def task(self) -> TaskSpec:
        return self.base.task

    @property
    def catalog(self) -> TopologyCatalog:
        return self.base.catalog

    @property
    def fault_target(self) -> str:
        if self.failure_type == "agent_failure":
            return self.base.target_agent_id
        return "->".join(self.base.failed_link)

    @property
    def failed_link(self) -> tuple[str, str]:
        return self.base.failed_link

    @property
    def backup_path(self) -> tuple[str, ...]:
        return self.base.backup_path

    @property
    def failed_link_in_active_task_path(self) -> bool:
        return bool(self.affected_business_edge_ids)

    @property
    def alternate_path_available(self) -> bool:
        return bool(self.base.backup_path) and self.base.failed_link not in set(
            zip(self.base.backup_path, self.base.backup_path[1:])
        )

    @property
    def affected_business_edge_ids(self) -> frozenset[str]:
        gateway_by_agent = {
            item.agent_id: item.gateway_id
            for item in self.catalog.agents
            if item.layer == AgentLayer.APPLICATION
        }
        affected = set()
        for edge in self.task.biz_edges:
            path = self.base.gateway_paths[
                (gateway_by_agent[edge.source], gateway_by_agent[edge.target])
            ]
            if self.failed_link in set(zip(path, path[1:])):
                affected.add(edge.edge_id)
        return frozenset(affected)

    @property
    def affected_flow_ratio(self) -> float:
        return len(self.affected_business_edge_ids) / max(len(self.task.biz_edges), 1)

    @property
    def dependency_closure_ratio(self) -> float:
        target = self.base.target_agent_id
        adjacent: dict[str, set[str]] = {agent: set() for agent in self.task.app_agents}
        for edge in self.task.biz_edges:
            adjacent.setdefault(edge.source, set()).add(edge.target)
        closure = {target}; frontier = [target]
        while frontier:
            node = frontier.pop()
            for neighbor in adjacent.get(node, ()):
                if neighbor not in closure:
                    closure.add(neighbor); frontier.append(neighbor)
        return len(closure) / max(len(self.task.app_agents), 1)

    @property
    def post_fault_capacity_ratio(self) -> float:
        return self.post_capacity_mbps / max(self.pre_capacity_mbps, 1e-9)

    @property
    def topology_fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "gateways": self.catalog.gateways,
                "gateway_paths": self.base.gateway_paths,
            }
        )

    @property
    def qos_fingerprint(self) -> str:
        return stable_fingerprint(self.task.qos)

    @property
    def scenario_fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "seed": self.seed,
                "event_id": self.event_id,
                "failure_type": self.failure_type,
                "failure_severity": self.failure_severity,
                "task": to_jsonable(self.task),
                "catalog": to_jsonable(self.catalog),
                "gateway_paths": self.base.gateway_paths,
                "failed_link": self.failed_link,
                "replacements": self.compatible_replacements,
                "pre_capacity_mbps": self.pre_capacity_mbps,
                "post_capacity_mbps": self.post_capacity_mbps,
            }
        )

    @property
    def event_fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "scenario": self.scenario_fingerprint,
                "failure_type": self.failure_type,
                "fault_target": self.fault_target,
                "severity": self.failure_severity,
            }
        )

    def instantiate(self):
        return self.base.instantiate()

    def apply_fault(self, controller, stable) -> tuple[RuntimeEvent, FaultContext]:
        event, context = self.base.apply_fault(controller, stable)
        metadata = dict(context.metadata)
        metadata.update(
            paper_failure_type=self.failure_type,
            capacity_reduction=self.failure_severity,
            pre_failure_capacity_mbps=self.pre_capacity_mbps,
            post_failure_capacity_mbps=self.post_capacity_mbps,
            compatible_replacements=list(self.compatible_replacements),
        )
        if self.failure_type in {"link_failure", "capacity_degradation"}:
            demand = max(
                0.01,
                sum(
                    session.data_rate_mbps
                    for session in stable.sessions
                    if session.business_edge_id in context.affected_edge_ids
                ),
            )
            metadata["affected_demand_mbps"] = demand
            metadata["service_requirement_mbps"] = demand
            metadata["cspf_te_links"] = self._post_fault_te_links(demand)
            metadata["network_reserve_mbps"] = 0.12 * demand
        return event, replace(
            context,
            severity=(
                1.0 - self.failure_severity
                if self.failure_type == "capacity_degradation"
                else context.severity
            ),
            degraded_link_capacity_mbps=(
                self.post_capacity_mbps
                if self.failure_type == "capacity_degradation"
                else context.degraded_link_capacity_mbps
            ),
            metadata=metadata,
        )

    def _post_fault_te_links(
        self,
        demand_mbps: float,
    ) -> list[dict[str, object]]:
        rows = []
        for source in self._te_link_profiles():
            row = dict(source)
            pair = (str(row["source"]), str(row["target"]))
            if self.failure_type == "link_failure":
                row["up"] = pair != self.failed_link
                row["available_bandwidth_mbps"] = (
                    0.0 if pair == self.failed_link else 1.35 * demand_mbps
                )
            else:
                jitter_digest = stable_fingerprint(
                    {
                        "seed": self.seed,
                        "event_id": self.event_id,
                        "link_id": row["link_id"],
                    }
                )
                jitter = (int(jitter_digest[:8], 16) / 0xFFFFFFFF - 0.5) * 0.10
                headroom = max(
                    0.05,
                    1.13 - 0.70 * self.failure_severity + jitter,
                )
                row["up"] = True
                row["available_bandwidth_mbps"] = demand_mbps * headroom
                if pair == self.failed_link:
                    row["available_bandwidth_mbps"] = self.post_capacity_mbps
            rows.append(row)
        return rows

    def _te_link_profiles(self) -> list[dict[str, object]]:
        """Build the paired network-only TE view without legacy-runner state."""

        rng = random.Random(
            f"paper-exp4-te:{self.seed}:{self.event_id}:{self.failure_type}"
        )
        gateway_ids = tuple(item.gateway_id for item in self.catalog.gateways)
        primary_links = set(zip(self.base.primary_path, self.base.primary_path[1:]))
        backup_links = set(zip(self.base.backup_path, self.base.backup_path[1:]))
        rows = []
        for source in gateway_ids:
            for target in gateway_ids:
                if source == target:
                    continue
                pair = (source, target)
                if pair in primary_links:
                    delay_ms = rng.uniform(3.0, 5.0)
                elif pair in backup_links:
                    delay_ms = rng.uniform(5.0, 8.0)
                else:
                    delay_ms = rng.uniform(7.0, 12.0)
                rows.append(
                    {
                        "link_id": f"{source}->{target}",
                        "source": source,
                        "target": target,
                        "available_bandwidth_mbps": 0.0,
                        "delay_ms": delay_ms,
                        "te_cost": delay_ms + rng.uniform(0.0, 1.0),
                        "up": True,
                    }
                )
        return rows


def generate_paper_failure_snapshot(
    failure_type: str,
    severity: float,
    seed: int,
    event_id: int,
    *,
    capacity_ratio: float | None = None,
) -> PaperFailureSnapshot:
    normalized = failure_type.strip().lower()
    if normalized not in PAPER_FAILURE_TYPES:
        raise ValueError(f"unsupported paper failure type: {failure_type}")
    if seed < 0 or event_id < 0:
        raise ValueError("seed and event_id must be non-negative")
    if capacity_ratio is not None:
        if normalized != "capacity_degradation" or capacity_ratio <= 0.0:
            raise ValueError("capacity_ratio is valid only for positive capacity scenarios")
        severity = 1.0 - float(capacity_ratio)
    elif normalized == "capacity_degradation" and not 0.0 < severity < 1.0:
        raise ValueError("capacity reduction must be within (0, 1)")

    if normalized == "agent_failure":
        old_type = "AGENT_FAILURE"
        old_level = "application_different_gateway"
        old_severity = 1.0
    elif normalized == "link_failure":
        old_type = "LINK_FAILURE"
        old_level = "link_down"
        old_severity = 0.0
    else:
        old_type = "LINK_DEGRADATION"
        old_level = f"capacity_reduction_{100.0 * severity:.0f}pct"
        old_severity = min(0.999, max(0.001, 1.0 - severity))

    generated = FailureScenarioGenerator().generate(
        FailureScenarioConfig(
            fault_type=old_type,
            fault_level=old_level,
            severity=old_severity,
            num_agents=24,
            edge_ratio=1.5,
            num_gateways=12,
            cross_gateway_edge_ratio=0.65,
            multi_hop=True,
        ),
        seed,
    )
    if normalized in {"link_failure", "agent_failure"}:
        candidates = [
            _select_independent_event(
                generated, seed, event_id,
                selection_salt=f"{normalized}:candidate:{index}",
            )
            for index in range(64)
        ]
        measure = _base_affected_flow_ratio if normalized == "link_failure" else _base_dependency_closure_ratio
        generated = min(
            candidates,
            key=lambda candidate: (
                abs(measure(candidate) - severity),
                stable_fingerprint({"target": candidate.target_edge_id, "agent": candidate.target_agent_id}),
            ),
        )
    else:
        ratio_for_salt = capacity_ratio if capacity_ratio is not None else 1.0 - severity
        generated = _select_independent_event(
            generated, seed, event_id, selection_salt=f"capacity:{ratio_for_salt:g}"
        )
    affected_demand = sum(
        edge.data_rate_mbps
        for edge in generated.task.biz_edges
        if edge.edge_id == generated.target_edge_id
    )
    pre_capacity = affected_demand if normalized == "capacity_degradation" else 0.0
    post_capacity = (
        pre_capacity * (1.0 - severity)
        if normalized == "capacity_degradation"
        else 0.0
    )
    replacements = (
        (generated.local_backup_agent_id, generated.remote_backup_agent_id)
        if normalized == "agent_failure"
        else ()
    )
    return PaperFailureSnapshot(
        seed=seed,
        event_id=event_id,
        failure_type=normalized,
        failure_severity=float(severity),
        base=generated,
        compatible_replacements=replacements,
        pre_capacity_mbps=pre_capacity,
        post_capacity_mbps=post_capacity,
    )


def _select_independent_event(
    snapshot: FaultScenarioSnapshot,
    seed: int,
    event_id: int,
    *,
    selection_salt: str = "",
) -> FaultScenarioSnapshot:
    app_specs = {
        item.agent_id: item
        for item in snapshot.catalog.agents
        if item.layer == AgentLayer.APPLICATION
    }
    cross_edges = sorted(
        (
            edge
            for edge in snapshot.task.biz_edges
            if app_specs[edge.source].gateway_id != app_specs[edge.target].gateway_id
        ),
        key=lambda edge: edge.edge_id,
    )
    event_digest = stable_fingerprint(
        {"seed": seed, "event_id": event_id, "selection_salt": selection_salt, "namespace": "paper-exp4-event"}
    )
    selected = cross_edges[int(event_digest[:16], 16) % len(cross_edges)]
    source_gateway = app_specs[selected.source].gateway_id
    target_gateway = app_specs[selected.target].gateway_id
    gateway_ids = tuple(item.gateway_id for item in snapshot.catalog.gateways)
    intermediates = [
        gateway_id
        for gateway_id in gateway_ids
        if gateway_id not in {source_gateway, target_gateway}
    ]
    rotation = int(event_digest[16:24], 16) % len(intermediates)
    intermediates = intermediates[rotation:] + intermediates[:rotation]
    primary = (source_gateway, intermediates[0], target_gateway)
    backup = (source_gateway, intermediates[1], target_gateway)
    failed_link = (primary[0], primary[1])
    paths = dict(snapshot.gateway_paths)
    paths[(source_gateway, target_gateway)] = primary

    local_backup = f"{selected.source}-paper-backup-local-{event_id:03d}"
    remote_backup = f"{selected.source}-paper-backup-remote-{event_id:03d}"
    remote_gateway = next(
        gateway_id
        for gateway_id in gateway_ids
        if gateway_id not in {source_gateway, target_gateway}
    )
    extras = (
        AgentSpec(
            agent_id=local_backup,
            name=f"Local paper standby for {selected.source}",
            layer=AgentLayer.APPLICATION,
            role=AgentRole.BUSINESS,
            gateway_id=source_gateway,
            capabilities=("business_stage", "standby"),
            port=19500 + event_id * 2,
        ),
        AgentSpec(
            agent_id=remote_backup,
            name=f"Remote paper standby for {selected.source}",
            layer=AgentLayer.APPLICATION,
            role=AgentRole.BUSINESS,
            gateway_id=remote_gateway,
            capabilities=("business_stage", "standby"),
            port=19501 + event_id * 2,
        ),
    )
    existing_ids = {item.agent_id for item in snapshot.catalog.agents}
    catalog = replace(
        snapshot.catalog,
        agents=snapshot.catalog.agents
        + tuple(item for item in extras if item.agent_id not in existing_ids),
    )
    task = replace(
        snapshot.task,
        task_id=f"{snapshot.task.task_id}-event-{event_id:03d}",
    )
    time_shift = event_id * 0.1
    monitor = tuple(
        replace(item, timestamp=item.timestamp + time_shift)
        for item in snapshot.monitor_samples
    )
    return replace(
        snapshot,
        task=task,
        catalog=catalog,
        gateway_paths=paths,
        target_edge_id=selected.edge_id,
        target_agent_id=selected.source,
        local_backup_agent_id=local_backup,
        remote_backup_agent_id=remote_backup,
        primary_path=primary,
        backup_path=backup,
        failed_link=failed_link,
        fault_effective_at=snapshot.fault_effective_at + time_shift,
        failure_detected_at=snapshot.failure_detected_at + time_shift,
        monitor_samples=monitor,
    )


def _base_affected_flow_ratio(snapshot: FaultScenarioSnapshot) -> float:
    gateway_by_agent = {
        item.agent_id: item.gateway_id for item in snapshot.catalog.agents
        if item.layer == AgentLayer.APPLICATION
    }
    affected = 0
    for edge in snapshot.task.biz_edges:
        path = snapshot.gateway_paths[(gateway_by_agent[edge.source], gateway_by_agent[edge.target])]
        affected += snapshot.failed_link in set(zip(path, path[1:]))
    return affected / max(len(snapshot.task.biz_edges), 1)


def _base_dependency_closure_ratio(snapshot: FaultScenarioSnapshot) -> float:
    outgoing: dict[str, set[str]] = {agent: set() for agent in snapshot.task.app_agents}
    for edge in snapshot.task.biz_edges:
        outgoing.setdefault(edge.source, set()).add(edge.target)
    closure = {snapshot.target_agent_id}; frontier = [snapshot.target_agent_id]
    while frontier:
        node = frontier.pop()
        for neighbor in outgoing.get(node, ()):
            if neighbor not in closure:
                closure.add(neighbor); frontier.append(neighbor)
    return len(closure) / max(len(snapshot.task.app_agents), 1)
