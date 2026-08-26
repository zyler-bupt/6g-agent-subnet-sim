from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


@dataclass(frozen=True)
class FormationEdge:
    edge_id: str
    source_index: int
    target_index: int
    required_throughput_mbps: float


@dataclass(frozen=True)
class DeploymentCommand:
    namespace: str
    pid: int | None
    argv: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentBatch:
    label: str
    commands: tuple[DeploymentCommand, ...]


@dataclass(frozen=True)
class FormationDeploymentPlan:
    method_id: str
    planning_policy: str
    tie_break: str
    ordered_edge_ids: tuple[str, ...]
    service_chain_count: int
    planning_work_units: int
    batches: tuple[DeploymentBatch, ...]
    path_records: tuple[dict[str, object], ...] = ()

    @property
    def control_messages(self) -> int:
        return len(self.batches)

    @property
    def rules_installed(self) -> int:
        return sum(len(batch.commands) for batch in self.batches)

    def audit(self) -> dict[str, object]:
        return {
            "planning_policy": self.planning_policy,
            "tie_break": self.tie_break,
            "planned_edge_count": len(self.ordered_edge_ids),
            "ordered_edge_ids": list(self.ordered_edge_ids),
            "service_chain_count": self.service_chain_count,
            "planning_work_units": self.planning_work_units,
            "deployment_batch_count": self.control_messages,
            "planned_rule_count": self.rules_installed,
            "path_records": list(self.path_records),
        }


@dataclass(frozen=True)
class NetnsFormationRun:
    run_id: str
    run_sequence: int
    block_index: int
    order_position: int
    method_order_position: int
    seed: int
    method_id: str
    method_label: str
    num_agents: int
    num_gateways: int
    num_business_edges: int
    control_messages: int
    rules_installed: int
    scenario_fingerprint: str
    result_mode: str
    configuration_sha256: str
    task_received_at: float
    mapping_finished_at: float
    traffic_control_started_at: float
    traffic_control_finished_at: float
    route_install_started_at: float
    route_install_finished_at: float
    activation_finished_at: float
    verification_started_at: float
    ping_verify_started_at: float
    ping_verify_finished_at: float
    iperf_verify_started_at: float
    data_plane_verified_at: float
    mapping_latency_s: float
    traffic_control_latency_s: float
    route_install_latency_s: float
    ping_verification_latency_s: float
    iperf3_verification_latency_s: float
    verification_latency_s: float
    verified_formation_latency_s: float | None
    ping_edges_total: int
    ping_edges_passed: int
    ping_attempts: int
    ping_retried_edges: int
    ping_mean_rtt_ms: float
    ping_max_packet_loss_percent: float
    ping_timeouts: int
    iperf_flows_total: int
    iperf_flows_passed: int
    iperf_attempts: int
    iperf_retried_flows: int
    iperf_retransmissions: int
    iperf_reported_duration_s: float
    aggregate_receiver_throughput_mbps: float
    retry_backoff_ms: int
    retry_count: int
    background_traffic_enabled: bool
    background_utilization: float
    background_target_mbps: float
    tc_profile_count: int
    failure_stage: str
    formation_timing_valid: bool
    formation_failed: bool
    success: bool
    failure_reason: str


@dataclass(frozen=True)
class TrafficControlProfile:
    endpoint: str
    namespace_pid: int | None
    interface: str
    base_delay_ms: float
    jitter_ms: float
    packet_loss_percent: float
    bandwidth_mbps: float
    queue_limit_packets: int


@dataclass
class BackgroundTraffic:
    enabled: bool
    utilization: float = 0.0
    target_mbps: float = 0.0
    source_gateway_index: int = -1
    target_gateway_index: int = -1
    port: int = 0
    server: subprocess.Popen[str] | None = None
    client: subprocess.Popen[str] | None = None
    server_command: tuple[str, ...] = ()
    client_command: tuple[str, ...] = ()
    preparation_commands: tuple[dict[str, object], ...] = ()
    readiness_ready: bool = False
    readiness_timeout_s: float = 0.0
    minimum_payload_bytes: int = 0
    source_up0_tx_bytes_before: int = 0
    source_up0_tx_bytes_after: int = 0
    target_up0_rx_bytes_before: int = 0
    target_up0_rx_bytes_after: int = 0
    source_up0_tx_delta_bytes: int = 0
    target_up0_rx_delta_bytes: int = 0
    forward_path_endpoints: tuple[str, ...] = ()
    forward_path_bottleneck_mbps: float = 0.0


@dataclass
class _NamespaceProcess:
    label: str
    process: subprocess.Popen[str]

    @property
    def pid(self) -> int:
        return self.process.pid


def background_gateway_pair(
    num_agents: int, num_gateways: int, seed: int
) -> tuple[int, int]:
    if num_gateways < 2:
        raise ValueError("background traffic requires at least two gateways")
    pairs = [
        (source_gateway, target_gateway)
        for source_gateway in range(num_gateways)
        for target_gateway in range(num_gateways)
        if source_gateway != target_gateway
    ]
    return random.Random(f"wcnc-exp1-background:{num_agents}:{seed}").choice(pairs)


def background_gateway_route_commands(
    source_gateway: int, target_gateway: int
) -> tuple[tuple[str, ...], ...]:
    """Return pre-task routes that address only gateway uplink IPs."""
    source_ip = f"10.200.{source_gateway}.2"
    target_ip = f"10.200.{target_gateway}.2"
    return (
        (
            "ip", "route", "replace", f"{target_ip}/32", "via",
            f"10.200.{source_gateway}.1", "dev", "up0",
        ),
        (
            "ip", "route", "replace", f"{source_ip}/32", "via",
            f"10.200.{target_gateway}.1", "dev", "up0",
        ),
        ("ip", "route", "replace", f"{target_ip}/32", "dev", f"og{target_gateway}"),
        ("ip", "route", "replace", f"{source_ip}/32", "dev", f"og{source_gateway}"),
    )


class ProcessNetnsTopology:
    """Real veth topology built from process-owned Linux network namespaces.

    The experiment launcher first enters an unprivileged user+network namespace.
    Each Agent and gateway is then a child process in its own network namespace.
    This avoids relying on host-global ``/var/run/netns`` mounts while retaining
    real kernel forwarding, route installation, ping and iperf3 execution.
    """

    def __init__(self, num_agents: int, num_gateways: int) -> None:
        self.num_agents = num_agents
        self.num_gateways = min(num_gateways, num_agents)
        self.agents: list[_NamespaceProcess] = []
        self.gateways: list[_NamespaceProcess] = []
        self.agent_ips: list[str] = []
        self.agent_gateways: list[int] = []
        self._servers: list[subprocess.Popen[str]] = []
        self.last_install_commands: list[dict[str, object]] = []

    def __enter__(self) -> ProcessNetnsTopology:
        self._require_commands(
            "ip", "tc", "nsenter", "unshare", "ping", "iperf3", "ss", "sysctl", "cat"
        )
        self._run_host("ip", "link", "set", "lo", "up")
        self._run_host("sysctl", "-qw", "net.ipv4.ip_forward=1")
        try:
            self.gateways = [self._spawn_namespace(f"gateway-{index}") for index in range(self.num_gateways)]
            self.agents = [self._spawn_namespace(f"agent-{index}") for index in range(self.num_agents)]
            for namespace in (*self.gateways, *self.agents):
                self._run_ns(namespace.pid, "ip", "link", "set", "lo", "up")
            self._wire_gateways()
            self._wire_agents()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        for server in self._servers:
            if server.poll() is None:
                server.terminate()
        for server in self._servers:
            try:
                server.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                server.kill()
        self._servers.clear()
        # The host-side gateway veth endpoint lives in the outer experiment
        # network namespace.  Deleting it explicitly is required before the
        # next task-size point reuses the deterministic interface names.
        for index in range(self.num_gateways):
            self._run_host("ip", "link", "del", f"og{index}", check=False)
        for namespace in (*self.agents, *self.gateways):
            if namespace.process.poll() is None:
                namespace.process.terminate()
        for namespace in (*self.agents, *self.gateways):
            try:
                namespace.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                namespace.process.kill()
                namespace.process.wait(timeout=1.0)
        self.agents.clear()
        self.gateways.clear()

    def reset_task_routes(self) -> None:
        commands: list[tuple[int | None, tuple[str, ...]]] = []
        for index, namespace in enumerate(self.agents):
            commands.append((namespace.pid, ("ip", "route", "del", "default")))
            for target_index in range(self.num_agents):
                commands.append(
                    (
                        namespace.pid,
                        (
                            "ip", "rule", "del", "priority",
                            str(10_000 + index * self.num_agents + target_index),
                        ),
                    )
                )
            commands.append(
                (
                    namespace.pid,
                    ("ip", "route", "flush", "table", str(100 + index)),
                )
            )
            commands.append((None, ("ip", "route", "del", f"{self.agent_ips[index]}/32")))
        for gateway in self.gateways:
            commands.append((gateway.pid, ("ip", "route", "del", "default")))
        self._parallel_commands(commands, check=False)

    def plan_task_routes(
        self,
        method_id: str,
        edges: Sequence[FormationEdge],
        *,
        profiles: Sequence[TrafficControlProfile] = (),
        max_path_delay_ms: float | None = None,
    ) -> FormationDeploymentPlan:
        """Build the method-owned route plan used by the shared netns backend."""
        if method_id not in {"proposed", "cspf", "global_sfc_embedding"}:
            raise ValueError(f"unsupported canonical Exp1 method: {method_id}")
        ordered = _ordered_formation_edges(method_id, edges, self.num_gateways)
        common = tuple(self._gateway_default_commands()) + tuple(self._outer_agent_commands())
        if method_id == "proposed":
            peer_commands: list[DeploymentCommand] = []
            seen: set[tuple[int | None, tuple[str, ...]]] = set()
            for edge in ordered:
                for command in self._explicit_peer_commands(edge):
                    identity = (command.pid, command.argv)
                    if identity not in seen:
                        seen.add(identity)
                        peer_commands.append(command)
            commands = common + tuple(peer_commands)
            batches = (DeploymentBatch("task_dag_parallel_batch", commands),)
            policy = "task_dag_batch_parallel_deployment"
            chains: tuple[tuple[FormationEdge, ...], ...] = ()
            work_units = len(self.agents) + len(edges)
        elif method_id == "cspf":
            path_records = self._cspf_path_records(
                ordered, profiles, max_path_delay_ms=max_path_delay_ms
            )
            edge_batches = tuple(
                DeploymentBatch(
                    f"cspf_flow:{edge.edge_id}",
                    tuple(self._explicit_peer_commands(edge)),
                )
                for edge in ordered
            )
            batches = (DeploymentBatch("cspf_shared_infrastructure", common),) + edge_batches
            policy = "failed_or_insufficient_links_pruned_then_deterministic_te_shortest_path"
            chains = ()
            work_units = len(edges) * (len(self.agents) + len(edges))
        else:
            chains = _service_chains(edges)
            seen: set[tuple[int | None, tuple[str, ...]]] = set()
            chain_batches: list[DeploymentBatch] = []
            for chain_index, chain in enumerate(chains):
                for hop_index, edge in enumerate(chain):
                    route_commands: list[DeploymentCommand] = []
                    rule_commands: list[DeploymentCommand] = []
                    for command in self._table_peer_commands(edge):
                        identity = (command.pid, command.argv)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        destination = (
                            route_commands if command.argv[1] == "route" else rule_commands
                        )
                        destination.append(command)
                    if route_commands:
                        chain_batches.append(
                            DeploymentBatch(
                                f"sfc_chain:{chain_index:03d}:hop:{hop_index:03d}:routes",
                                tuple(route_commands),
                            )
                        )
                    if rule_commands:
                        chain_batches.append(
                            DeploymentBatch(
                                f"sfc_chain:{chain_index:03d}:hop:{hop_index:03d}:activate",
                                tuple(rule_commands),
                            )
                        )
            batches = (
                DeploymentBatch(
                    "sfc_gateway_infrastructure", tuple(self._gateway_default_commands())
                ),
                DeploymentBatch(
                    "sfc_outer_reachability", tuple(self._outer_agent_commands())
                ),
                *chain_batches,
            )
            policy = "task_dag_source_to_sink_chain_embedding_heuristic"
            work_units = len(edges) + sum(len(chain) ** 2 for chain in chains)
        if method_id != "cspf":
            path_records = ()
        return FormationDeploymentPlan(
            method_id=method_id,
            planning_policy=policy,
            tie_break="canonical_edge_id",
            ordered_edge_ids=tuple(edge.edge_id for edge in ordered),
            service_chain_count=len(chains),
            planning_work_units=work_units,
            batches=tuple(batches),
            path_records=tuple(path_records),
        )

    def install_deployment_plan(
        self, plan: FormationDeploymentPlan
    ) -> tuple[int, int]:
        """Execute batches sequentially while parallelizing each batch internally."""
        self.last_install_commands = []
        for batch_index, batch in enumerate(plan.batches):
            executable = tuple((command.pid, command.argv) for command in batch.commands)
            self._parallel_commands(executable, check=True)
            self.last_install_commands.extend(
                {
                    "batch_index": batch_index,
                    "batch_label": batch.label,
                    "namespace": command.namespace,
                    "command": list(command.argv),
                }
                for command in batch.commands
            )
        return plan.control_messages, plan.rules_installed

    def _gateway_default_commands(self) -> list[DeploymentCommand]:
        return [
            DeploymentCommand(
                f"gateway-{gateway_index}",
                gateway.pid,
                (
                    "ip", "route", "replace", "default", "via",
                    f"10.200.{gateway_index}.1", "dev", "up0",
                ),
            )
            for gateway_index, gateway in enumerate(self.gateways)
        ]

    def _outer_agent_commands(self) -> list[DeploymentCommand]:
        return [
            DeploymentCommand(
                "outer",
                None,
                (
                    "ip", "route", "replace", f"{self.agent_ips[agent_index]}/32",
                    "via", f"10.200.{gateway_index}.2", "dev", f"og{gateway_index}",
                ),
            )
            for agent_index, gateway_index in enumerate(self.agent_gateways)
        ]

    def _explicit_peer_commands(self, edge: FormationEdge) -> list[DeploymentCommand]:
        commands: list[DeploymentCommand] = []
        for source_index, target_index in (
            (edge.source_index, edge.target_index),
            (edge.target_index, edge.source_index),
        ):
            gateway_index = self.agent_gateways[source_index]
            commands.append(
                DeploymentCommand(
                    f"agent-{source_index}",
                    self.agents[source_index].pid,
                    (
                        "ip", "route", "replace", f"{self.agent_ips[target_index]}/32",
                        "via", f"10.{100 + gateway_index}.{source_index}.1", "dev", "eth0",
                    ),
                )
            )
        return commands

    def _cspf_path_records(
        self,
        edges: Sequence[FormationEdge],
        profiles: Sequence[TrafficControlProfile],
        *,
        max_path_delay_ms: float | None,
    ) -> tuple[dict[str, object], ...]:
        if not profiles:
            return ()
        by_endpoint = {profile.endpoint: profile for profile in profiles}
        records: list[dict[str, object]] = []
        for edge in edges:
            source_gateway = self.agent_gateways[edge.source_index]
            target_gateway = self.agent_gateways[edge.target_index]
            endpoints = [
                f"agent-{edge.source_index}:eth0",
                f"gateway-{source_gateway}:ga{edge.source_index}",
            ]
            path = [
                f"agent-{edge.source_index}",
                f"gateway-{source_gateway}",
            ]
            if source_gateway != target_gateway:
                endpoints.extend(
                    (
                        f"gateway-{source_gateway}:up0",
                        f"outer:og{source_gateway}",
                        f"outer:og{target_gateway}",
                        f"gateway-{target_gateway}:up0",
                    )
                )
                path.extend(("outer", f"gateway-{target_gateway}"))
            endpoints.extend(
                (
                    f"gateway-{target_gateway}:ga{edge.target_index}",
                    f"agent-{edge.target_index}:eth0",
                )
            )
            path.append(f"agent-{edge.target_index}")
            missing = [endpoint for endpoint in endpoints if endpoint not in by_endpoint]
            if missing:
                raise RuntimeError(
                    f"CSPF path observation missing endpoints for {edge.edge_id}: "
                    + ",".join(missing)
                )
            bottleneck = min(by_endpoint[endpoint].bandwidth_mbps for endpoint in endpoints)
            if bottleneck < edge.required_throughput_mbps:
                raise RuntimeError(
                    f"CSPF pruned {edge.edge_id}: path bandwidth {bottleneck:.6f} Mbps "
                    f"is below demand {edge.required_throughput_mbps:.6f} Mbps"
                )
            delay_cost_ms = sum(
                by_endpoint[endpoint].base_delay_ms for endpoint in endpoints
            )
            if max_path_delay_ms is not None and delay_cost_ms > max_path_delay_ms:
                raise RuntimeError(
                    f"CSPF pruned {edge.edge_id}: path delay {delay_cost_ms:.6f} ms "
                    f"exceeds constraint {max_path_delay_ms:.6f} ms"
                )
            records.append(
                {
                    "edge_id": edge.edge_id,
                    "path": path,
                    "path_endpoints": endpoints,
                    "bottleneck_mbps": bottleneck,
                    "delay_cost_ms": delay_cost_ms,
                    "tie_break": edge.edge_id,
                }
            )
        return tuple(records)

    def _table_peer_commands(self, edge: FormationEdge) -> list[DeploymentCommand]:
        commands: list[DeploymentCommand] = []
        for source_index, target_index in (
            (edge.source_index, edge.target_index),
            (edge.target_index, edge.source_index),
        ):
            gateway_index = self.agent_gateways[source_index]
            table = str(100 + source_index)
            commands.extend(
                (
                    DeploymentCommand(
                        f"agent-{source_index}",
                        self.agents[source_index].pid,
                        (
                            "ip", "route", "replace", "table", table,
                            f"10.{100 + gateway_index}.{source_index}.0/30",
                            "dev", "eth0", "scope", "link",
                        ),
                    ),
                    DeploymentCommand(
                        f"agent-{source_index}",
                        self.agents[source_index].pid,
                        (
                            "ip", "rule", "add", "priority",
                            str(
                                10_000
                                + source_index * self.num_agents
                                + target_index
                            ),
                            "to", f"{self.agent_ips[target_index]}/32", "lookup", table,
                        ),
                    ),
                    DeploymentCommand(
                        f"agent-{source_index}",
                        self.agents[source_index].pid,
                        (
                            "ip", "route", "replace", "table", table,
                            f"{self.agent_ips[target_index]}/32", "via",
                            f"10.{100 + gateway_index}.{source_index}.1", "dev", "eth0",
                        ),
                    ),
                )
            )
        return commands

    def configure_traffic_control(
        self,
        config: dict[str, Any],
        *,
        seed: int,
    ) -> list[TrafficControlProfile]:
        """Apply seed-derived HTB and netem settings to every directional veth endpoint."""

        netem = config["netem"]
        htb = config["htb"]
        rng = random.Random(f"wcnc-exp1-tc:{self.num_agents}:{seed}")
        endpoints: list[tuple[str, int | None, str]] = []
        for index, agent in enumerate(self.agents):
            gateway_index = self.agent_gateways[index]
            endpoints.append((f"agent-{index}:eth0", agent.pid, "eth0"))
            endpoints.append(
                (f"gateway-{gateway_index}:ga{index}", self.gateways[gateway_index].pid, f"ga{index}")
            )
        for index, gateway in enumerate(self.gateways):
            endpoints.append((f"gateway-{index}:up0", gateway.pid, "up0"))
            endpoints.append((f"outer:og{index}", None, f"og{index}"))
        profiles: list[TrafficControlProfile] = []
        for endpoint, namespace_pid, interface in endpoints:
            profile = TrafficControlProfile(
                endpoint=endpoint,
                namespace_pid=namespace_pid,
                interface=interface,
                base_delay_ms=rng.uniform(*map(float, netem["base_delay_ms"])),
                jitter_ms=rng.uniform(*map(float, netem["jitter_ms"])),
                packet_loss_percent=rng.uniform(
                    *map(float, netem["packet_loss_percent"])
                ),
                bandwidth_mbps=rng.uniform(
                    *map(float, htb["bandwidth_mbps"])
                ),
                queue_limit_packets=rng.randint(
                    int(netem["queue_limit_packets"][0]),
                    int(netem["queue_limit_packets"][1]),
                ),
            )
            self._apply_tc_profile(profile)
            profiles.append(profile)
        return profiles

    def start_background_traffic(
        self,
        config: dict[str, Any],
        profiles: Sequence[TrafficControlProfile],
        *,
        seed: int,
    ) -> BackgroundTraffic:
        rng = random.Random(f"wcnc-exp1-background:{self.num_agents}:{seed}")
        probability = float(config["enabled_probability"])
        if self.num_gateways < 2 or rng.random() >= probability:
            return BackgroundTraffic(enabled=False)
        source_gateway, target_gateway = background_gateway_pair(
            self.num_agents, self.num_gateways, seed
        )
        preparation_commands = self._install_background_gateway_routes(
            source_gateway, target_gateway
        )
        utilization = rng.uniform(*map(float, config["utilization_range"]))
        forward_path_endpoints = (
            f"gateway-{source_gateway}:up0",
            f"outer:og{target_gateway}",
        )
        profiles_by_endpoint = {profile.endpoint: profile for profile in profiles}
        missing_profiles = [
            endpoint for endpoint in forward_path_endpoints if endpoint not in profiles_by_endpoint
        ]
        if missing_profiles:
            raise RuntimeError(
                "missing traffic-control profiles for background forward path: "
                + ",".join(missing_profiles)
            )
        forward_path_bottleneck_mbps = min(
            profiles_by_endpoint[endpoint].bandwidth_mbps
            for endpoint in forward_path_endpoints
        )
        target_mbps = forward_path_bottleneck_mbps * utilization
        port = int(config["port"])
        duration_s = float(config["duration_s"])
        server_command = (
            "nsenter",
            "-t",
            str(self.gateways[target_gateway].pid),
            "-n",
            "iperf3",
            "-s",
            "-1",
            "-p",
            str(port),
            "-J",
        )
        server = subprocess.Popen(
            server_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._servers.append(server)
        self._wait_for_listener(self.gateways[target_gateway].pid, port, server, udp=False)
        source_up0_tx_bytes_before = self._gateway_up0_tx_bytes(source_gateway)
        target_up0_rx_bytes_before = self._gateway_up0_rx_bytes(target_gateway)
        client_command = (
            "nsenter",
            "-t",
            str(self.gateways[source_gateway].pid),
            "-n",
            "iperf3",
            "-c",
            f"10.200.{target_gateway}.2",
            "-p",
            str(port),
            "-u",
            "-b",
            f"{target_mbps:.6f}M",
            "-t",
            f"{duration_s:g}",
            "-J",
        )
        client = subprocess.Popen(
            client_command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        readiness_timeout_s = float(config.get("readiness_timeout_s", 2.0))
        minimum_payload_bytes = int(config["minimum_payload_bytes"])
        source_up0_tx_bytes_after, target_up0_rx_bytes_after = self._wait_for_background_transfer(
            source_gateway,
            target_gateway,
            client,
            source_up0_tx_bytes_before,
            target_up0_rx_bytes_before,
            minimum_payload_bytes=minimum_payload_bytes,
            timeout_s=readiness_timeout_s,
        )
        return BackgroundTraffic(
            enabled=True,
            utilization=utilization,
            target_mbps=target_mbps,
            source_gateway_index=source_gateway,
            target_gateway_index=target_gateway,
            port=port,
            server=server,
            client=client,
            server_command=server_command,
            client_command=client_command,
            preparation_commands=tuple(preparation_commands),
            readiness_ready=True,
            readiness_timeout_s=readiness_timeout_s,
            minimum_payload_bytes=minimum_payload_bytes,
            source_up0_tx_bytes_before=source_up0_tx_bytes_before,
            source_up0_tx_bytes_after=source_up0_tx_bytes_after,
            target_up0_rx_bytes_before=target_up0_rx_bytes_before,
            target_up0_rx_bytes_after=target_up0_rx_bytes_after,
            source_up0_tx_delta_bytes=source_up0_tx_bytes_after - source_up0_tx_bytes_before,
            target_up0_rx_delta_bytes=target_up0_rx_bytes_after - target_up0_rx_bytes_before,
            forward_path_endpoints=forward_path_endpoints,
            forward_path_bottleneck_mbps=forward_path_bottleneck_mbps,
        )

    def stop_background_traffic(self, traffic: BackgroundTraffic) -> dict[str, object]:
        if not traffic.enabled:
            return {"enabled": False}
        for process in (traffic.client, traffic.server):
            if process is not None and process.poll() is None:
                process.terminate()
        outputs: dict[str, object] = {
            "enabled": True,
            "utilization": traffic.utilization,
            "target_mbps": traffic.target_mbps,
            "source_gateway_index": traffic.source_gateway_index,
            "target_gateway_index": traffic.target_gateway_index,
            "source_endpoint": f"gateway-{traffic.source_gateway_index}:up0",
            "target_endpoint": f"gateway-{traffic.target_gateway_index}:up0",
            "forward_path_endpoints": list(traffic.forward_path_endpoints),
            "forward_path_bottleneck_mbps": traffic.forward_path_bottleneck_mbps,
            "server_command": list(traffic.server_command),
            "client_command": list(traffic.client_command),
            "preparation_commands": list(traffic.preparation_commands),
            "readiness_ready": traffic.readiness_ready,
            "readiness_timeout_s": traffic.readiness_timeout_s,
            "minimum_payload_bytes": traffic.minimum_payload_bytes,
            "source_up0_tx_bytes_before": traffic.source_up0_tx_bytes_before,
            "source_up0_tx_bytes_after": traffic.source_up0_tx_bytes_after,
            "target_up0_rx_bytes_before": traffic.target_up0_rx_bytes_before,
            "target_up0_rx_bytes_after": traffic.target_up0_rx_bytes_after,
            "source_up0_tx_delta_bytes": traffic.source_up0_tx_delta_bytes,
            "target_up0_rx_delta_bytes": traffic.target_up0_rx_delta_bytes,
        }
        for label, process in (("client", traffic.client), ("server", traffic.server)):
            if process is None:
                continue
            try:
                stdout, stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            outputs[f"{label}_returncode"] = process.returncode
            outputs[f"{label}_stdout"] = stdout
            outputs[f"{label}_stderr"] = stderr
        self._servers = [item for item in self._servers if item.poll() is None]
        return outputs

    def _install_background_gateway_routes(
        self, source_gateway: int, target_gateway: int
    ) -> list[dict[str, object]]:
        """Install only gateway-uplink /32 routes before task receipt."""
        source_command, target_command, outer_target_command, outer_source_command = (
            background_gateway_route_commands(source_gateway, target_gateway)
        )
        commands: list[tuple[int | None, tuple[str, ...]]] = [
            (self.gateways[source_gateway].pid, source_command),
            (self.gateways[target_gateway].pid, target_command),
            (None, outer_target_command),
            (None, outer_source_command),
        ]
        self._parallel_commands(commands, check=True)
        return [
            {
                "namespace": "outer" if pid is None else f"pid:{pid}",
                "command": list(command),
            }
            for pid, command in commands
        ]

    def _gateway_up0_bytes(self, gateway_index: int, direction: str) -> int:
        if direction not in {"tx", "rx"}:
            raise ValueError(f"unsupported gateway up0 counter direction: {direction}")
        result = self._run_ns(
            self.gateways[gateway_index].pid,
            "ip",
            "-j",
            "-s",
            "link",
            "show",
            "dev",
            "up0",
        )
        context = f"gateway {gateway_index} up0 {direction} byte counter"
        diagnostics = f"stderr={result.stderr.strip()!r}; stdout={result.stdout.strip()!r}"
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{context}: invalid ip link JSON ({diagnostics})") from error
        if not isinstance(payload, list):
            raise RuntimeError(f"{context}: expected an interface list ({diagnostics})")
        interface = next(
            (
                item
                for item in payload
                if isinstance(item, dict) and item.get("ifname") == "up0"
            ),
            None,
        )
        if interface is None:
            raise RuntimeError(f"{context}: missing up0 interface statistics ({diagnostics})")
        for stats_key in ("stats64", "stats"):
            stats = interface.get(stats_key)
            if not isinstance(stats, dict):
                continue
            directional_stats = stats.get(direction)
            if not isinstance(directional_stats, dict):
                continue
            byte_count = directional_stats.get("bytes")
            if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
                return byte_count
        raise RuntimeError(
            f"{context}: missing or non-integer {direction}.bytes in stats64/stats ({diagnostics})"
        )

    def _gateway_up0_tx_bytes(self, gateway_index: int) -> int:
        return self._gateway_up0_bytes(gateway_index, "tx")

    def _gateway_up0_rx_bytes(self, gateway_index: int) -> int:
        return self._gateway_up0_bytes(gateway_index, "rx")

    def _wait_for_background_transfer(
        self,
        source_gateway: int,
        target_gateway: int,
        client: subprocess.Popen[str],
        source_tx_before: int,
        target_rx_before: int,
        *,
        minimum_payload_bytes: int,
        timeout_s: float,
    ) -> tuple[int, int]:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            source_tx_after = self._gateway_up0_tx_bytes(source_gateway)
            target_rx_after = self._gateway_up0_rx_bytes(target_gateway)
            if (
                source_tx_after - source_tx_before >= minimum_payload_bytes
                and target_rx_after - target_rx_before >= minimum_payload_bytes
            ):
                return source_tx_after, target_rx_after
            if client.poll() is not None:
                raise RuntimeError("background iperf3 client exited before uplink transfer was observed")
            time.sleep(0.005)
        raise TimeoutError("background iperf3 client produced no bidirectional minimum payload evidence")

    def _apply_tc_profile(self, profile: TrafficControlProfile) -> None:
        # A topology is intentionally reused across repetitions. Remove the
        # preceding run's root hierarchy before installing the next frozen,
        # seed-derived HTB/netem profile; replacing an HTB root while its
        # child netem qdisc is attached is not supported by every iproute2
        # version.
        delete_root = (
            "tc",
            "qdisc",
            "del",
            "dev",
            profile.interface,
            "root",
        )
        if profile.namespace_pid is None:
            self._run_host(*delete_root, check=False)
        else:
            self._run_ns(profile.namespace_pid, *delete_root, check=False)
        commands = (
            (
                "tc",
                "qdisc",
                "add",
                "dev",
                profile.interface,
                "root",
                "handle",
                "1:",
                "htb",
                "default",
                "10",
            ),
            (
                "tc",
                "class",
                "add",
                "dev",
                profile.interface,
                "parent",
                "1:",
                "classid",
                "1:10",
                "htb",
                "rate",
                f"{profile.bandwidth_mbps:.6f}mbit",
                "ceil",
                f"{profile.bandwidth_mbps:.6f}mbit",
            ),
            (
                "tc",
                "qdisc",
                "add",
                "dev",
                profile.interface,
                "parent",
                "1:10",
                "handle",
                "10:",
                "netem",
                "delay",
                f"{profile.base_delay_ms:.6f}ms",
                f"{profile.jitter_ms:.6f}ms",
                "distribution",
                "normal",
                "loss",
                f"{profile.packet_loss_percent:.6f}%",
                "limit",
                str(profile.queue_limit_packets),
            ),
        )
        for command in commands:
            if profile.namespace_pid is None:
                self._run_host(*command)
            else:
                self._run_ns(profile.namespace_pid, *command)

    def verify_ping(
        self,
        edges: Sequence[FormationEdge],
        *,
        attempt: int,
        count: int,
        timeout_s: float,
        interval_s: float,
        max_packet_loss_percent: float,
        max_average_rtt_ms: float,
        parallel: bool,
    ) -> tuple[int, list[dict[str, object]]]:
        def probe(edge: FormationEdge) -> dict[str, object]:
            destination = self.agent_ips[edge.target_index]
            command = (
                "ping",
                "-n",
                "-c",
                str(count),
                "-W",
                str(max(1, math.ceil(timeout_s))),
                "-i",
                str(interval_s),
                destination,
            )
            started = time.perf_counter()
            try:
                completed = self._run_ns(
                    self.agents[edge.source_index].pid,
                    *command,
                    check=False,
                    timeout=max(2.0, timeout_s * count + 1.0),
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
                timed_out = False
            except subprocess.TimeoutExpired as error:
                returncode = 124
                stdout = _text(error.stdout)
                stderr = _text(error.stderr)
                timed_out = True
            packet_loss = _ping_packet_loss(stdout)
            average_rtt = _ping_average_rtt(stdout)
            passed = bool(
                not timed_out
                and packet_loss < 100.0
                and average_rtt is not None
                and average_rtt <= max_average_rtt_ms + 1e-9
            )
            return {
                "edge_id": edge.edge_id,
                "source_index": edge.source_index,
                "target_index": edge.target_index,
                "attempt": attempt,
                "command": list(command),
                "returncode": returncode,
                "duration_s": time.perf_counter() - started,
                "packet_loss_percent": packet_loss,
                "average_rtt_ms": average_rtt,
                "timeout": timed_out,
                "passed": passed,
                "stdout": stdout,
                "stderr": stderr,
            }

        rows = self._map_edges(edges, probe, parallel=parallel)
        return sum(bool(row["passed"]) for row in rows), rows

    def verify_iperf3(
        self,
        edges: Sequence[FormationEdge],
        *,
        attempt: int,
        duration_s: float,
        omit_s: float,
        client_timeout_s: float,
        base_port: int,
        parallel: bool,
    ) -> tuple[int, float, list[dict[str, object]]]:
        servers: list[tuple[FormationEdge, int, subprocess.Popen[str]]] = []
        for index, edge in enumerate(edges):
            port = base_port + index
            command = (
                "nsenter",
                "-t",
                str(self.agents[edge.target_index].pid),
                "-n",
                "iperf3",
                "-s",
                "-1",
                "-p",
                str(port),
                "-J",
            )
            server = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._servers.append(server)
            servers.append((edge, port, server))
        for edge, port, server in servers:
            self._wait_for_listener(self.agents[edge.target_index].pid, port, server, udp=False)

        def run_flow(item: tuple[FormationEdge, int, subprocess.Popen[str]]) -> dict[str, object]:
            edge, port, _server = item
            destination = self.agent_ips[edge.target_index]
            command = [
                "iperf3",
                "-c",
                destination,
                "-p",
                str(port),
                "-t",
                str(duration_s),
                "-O",
                str(omit_s),
                "-J",
            ]
            started = time.perf_counter()
            try:
                completed = self._run_ns(
                    self.agents[edge.source_index].pid,
                    *command,
                    check=False,
                    timeout=max(client_timeout_s, duration_s + omit_s + 2.0),
                )
            except subprocess.TimeoutExpired as error:
                return {
                    "edge_id": edge.edge_id,
                    "source_index": edge.source_index,
                    "target_index": edge.target_index,
                    "attempt": attempt,
                    "command": command,
                    "returncode": 124,
                    "duration_s": time.perf_counter() - started,
                    "reported_duration_s": 0.0,
                    "throughput_mbps": 0.0,
                    "retransmissions": 0,
                    "required_throughput_mbps": edge.required_throughput_mbps,
                    "passed": False,
                    "timeout": True,
                    "error": f"timeout:{error.timeout}",
                    "stdout": _text(error.stdout),
                    "stderr": _text(error.stderr),
                }
            throughput = 0.0
            retransmissions = 0
            reported_duration = 0.0
            error = completed.stderr.strip()
            if completed.returncode == 0:
                try:
                    payload = json.loads(completed.stdout)
                    summary = payload.get("end", {}).get("sum_received", {})
                    sent = payload.get("end", {}).get("sum_sent", {})
                    throughput = float(summary.get("bits_per_second", 0.0)) / 1_000_000.0
                    retransmissions = int(sent.get("retransmits", 0) or 0)
                    reported_duration = float(summary.get("seconds", 0.0) or 0.0)
                except (ValueError, TypeError, AttributeError) as parse_error:
                    error = f"iperf3_json:{parse_error}"
            passed = completed.returncode == 0 and throughput + 1e-9 >= edge.required_throughput_mbps
            return {
                "edge_id": edge.edge_id,
                "source_index": edge.source_index,
                "target_index": edge.target_index,
                "attempt": attempt,
                "command": command,
                "returncode": completed.returncode,
                "duration_s": time.perf_counter() - started,
                "reported_duration_s": reported_duration,
                "throughput_mbps": throughput,
                "retransmissions": retransmissions,
                "required_throughput_mbps": edge.required_throughput_mbps,
                "passed": passed,
                "timeout": False,
                "error": error,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }

        rows = self._map_edges(servers, run_flow, parallel=parallel)
        for _edge, _port, server in servers:
            try:
                server.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                server.terminate()
        self._servers = [item for item in self._servers if item.poll() is None]
        return (
            sum(bool(row["passed"]) for row in rows),
            sum(float(row["throughput_mbps"]) for row in rows),
            rows,
        )

    def _spawn_namespace(self, label: str) -> _NamespaceProcess:
        outer_namespace_inode = os.stat("/proc/self/ns/net").st_ino
        process = subprocess.Popen(
            ("unshare", "--net", "sleep", "86400"),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.perf_counter() + 2.0
        namespace_path = Path(f"/proc/{process.pid}/ns/net")
        while time.perf_counter() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"failed to create {label} netns: {stderr.strip()}")
            if namespace_path.exists():
                try:
                    child_namespace_inode = namespace_path.stat().st_ino
                except FileNotFoundError:
                    child_namespace_inode = outer_namespace_inode
                if child_namespace_inode != outer_namespace_inode:
                    return _NamespaceProcess(label, process)
            time.sleep(0.005)
        process.terminate()
        raise TimeoutError(f"timed out creating {label} netns")

    def _wire_gateways(self) -> None:
        for index, gateway in enumerate(self.gateways):
            outer_if = f"og{index}"
            inner_if = f"ig{index}"
            self._run_host("ip", "link", "add", outer_if, "type", "veth", "peer", "name", inner_if)
            self._run_host("ip", "link", "set", inner_if, "netns", str(gateway.pid))
            self._run_host("ip", "addr", "add", f"10.200.{index}.1/30", "dev", outer_if)
            self._run_host("ip", "link", "set", outer_if, "up")
            self._run_ns(gateway.pid, "ip", "link", "set", inner_if, "name", "up0")
            self._run_ns(gateway.pid, "ip", "addr", "add", f"10.200.{index}.2/30", "dev", "up0")
            self._run_ns(gateway.pid, "ip", "link", "set", "up0", "up")
            self._run_ns(gateway.pid, "sysctl", "-qw", "net.ipv4.ip_forward=1")

    def _wire_agents(self) -> None:
        for index, agent in enumerate(self.agents):
            gateway_index = index % self.num_gateways
            gateway = self.gateways[gateway_index]
            agent_if = f"aa{index}"
            gateway_if = f"ga{index}"
            self._run_host("ip", "link", "add", agent_if, "type", "veth", "peer", "name", gateway_if)
            self._run_host("ip", "link", "set", agent_if, "netns", str(agent.pid))
            self._run_host("ip", "link", "set", gateway_if, "netns", str(gateway.pid))
            gateway_ip = f"10.{100 + gateway_index}.{index}.1"
            agent_ip = f"10.{100 + gateway_index}.{index}.2"
            self._run_ns(agent.pid, "ip", "link", "set", agent_if, "name", "eth0")
            self._run_ns(agent.pid, "ip", "addr", "add", f"{agent_ip}/30", "dev", "eth0")
            self._run_ns(agent.pid, "ip", "link", "set", "eth0", "up")
            self._run_ns(gateway.pid, "ip", "addr", "add", f"{gateway_ip}/30", "dev", gateway_if)
            self._run_ns(gateway.pid, "ip", "link", "set", gateway_if, "up")
            self.agent_ips.append(agent_ip)
            self.agent_gateways.append(gateway_index)

    def _wait_for_listener(
        self,
        namespace_pid: int,
        port: int,
        server: subprocess.Popen[str],
        *,
        udp: bool,
    ) -> None:
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                raise RuntimeError(f"iperf3 server {port} exited before listen: {stderr.strip()}")
            result = self._run_ns(
                namespace_pid,
                "ss",
                "-lun" if udp else "-ltn",
                "sport",
                "=",
                f":{port}",
                check=False,
            )
            if f":{port}" in result.stdout:
                return
            time.sleep(0.005)
        raise TimeoutError(f"iperf3 server did not listen on port {port}")

    def _parallel_commands(
        self,
        commands: Sequence[tuple[int | None, tuple[str, ...]]],
        *,
        check: bool,
    ) -> None:
        def execute(item: tuple[int | None, tuple[str, ...]]) -> subprocess.CompletedProcess[str]:
            pid, command = item
            if pid is None:
                return self._run_host(*command, check=check)
            return self._run_ns(pid, *command, check=check)

        with ThreadPoolExecutor(max_workers=min(32, max(1, len(commands)))) as pool:
            futures = [pool.submit(execute, item) for item in commands]
            for future in as_completed(futures):
                future.result()

    @staticmethod
    def _map_edges(items, function, *, parallel: bool):
        if not parallel:
            return [function(item) for item in items]
        with ThreadPoolExecutor(max_workers=min(32, max(1, len(items)))) as pool:
            futures = [pool.submit(function, item) for item in items]
            return [future.result() for future in futures]

    @staticmethod
    def _run_host(
        *command: str,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=check,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    @staticmethod
    def _run_ns(
        pid: int,
        *command: str,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("nsenter", "-t", str(pid), "-n", *command),
            check=check,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    @staticmethod
    def _require_commands(*commands: str) -> None:
        missing = [command for command in commands if shutil.which(command) is None]
        if missing:
            raise RuntimeError("missing commands: " + ",".join(missing))


def generate_dag(num_agents: int, edge_ratio: float, seed: int, throughput: float) -> tuple[FormationEdge, ...]:
    target_count = min(num_agents * (num_agents - 1) // 2, max(num_agents - 1, math.ceil(num_agents * edge_ratio)))
    pairs = [(index, index + 1) for index in range(num_agents - 1)]
    candidates = [
        (source, target)
        for source in range(num_agents)
        for target in range(source + 1, num_agents)
        if (source, target) not in pairs
    ]
    random.Random(seed).shuffle(candidates)
    pairs.extend(candidates[: max(0, target_count - len(pairs))])
    pairs.sort()
    return tuple(
        FormationEdge(
            edge_id=f"edge-{index:03d}-{source:02d}-{target:02d}",
            source_index=source,
            target_index=target,
            required_throughput_mbps=throughput,
        )
        for index, (source, target) in enumerate(pairs)
    )


def scenario_fingerprint(num_agents: int, num_gateways: int, edges: Sequence[FormationEdge]) -> str:
    payload = {
        "num_agents": num_agents,
        "num_gateways": num_gateways,
        "edges": [asdict(edge) for edge in edges],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ScheduledRun:
    run_sequence: int
    block_index: int
    order_position: int
    num_agents: int
    seed: int


_FORMAL_TASK_SIZES = (4, 8, 12, 16, 20)
_FORMAL_SEEDS = tuple(range(50))
_FORMAL_BINDING_VALUES: dict[tuple[str, ...], object] = {
    ("task", "num_gateways"): 4,
    ("task", "edge_ratio"): 1.25,
    ("task", "required_throughput_mbps"): 0.5,
    ("traffic_control", "netem", "base_delay_ms"): (5.0, 20.0),
    ("traffic_control", "netem", "jitter_ms"): (1.0, 5.0),
    ("traffic_control", "netem", "packet_loss_percent"): (0.0, 1.0),
    ("traffic_control", "netem", "queue_limit_packets"): (100, 300),
    ("traffic_control", "htb", "bandwidth_mbps"): (40.0, 100.0),
    ("traffic_control", "background_traffic", "enabled_probability"): 0.30,
    ("traffic_control", "background_traffic", "utilization_range"): (0.10, 0.35),
    ("traffic_control", "background_traffic", "duration_s"): 8,
    ("traffic_control", "background_traffic", "readiness_timeout_s"): 2,
    ("traffic_control", "background_traffic", "protocol"): "udp",
    ("traffic_control", "background_traffic", "port"): 5099,
    ("traffic_control", "background_traffic", "minimum_payload_bytes"): 65_536,
    ("verification", "parallel"): True,
    ("verification", "max_attempts"): 2,
    ("verification", "retry_backoff_ms"): 200,
    ("verification", "ping", "count"): 3,
    ("verification", "ping", "timeout_s"): 1,
    ("verification", "ping", "interval_s"): 0.2,
    ("verification", "ping", "packet_loss_record_only"): True,
    ("verification", "ping", "max_average_rtt_ms"): 300.0,
    ("verification", "iperf3", "omit_s"): 0,
    ("verification", "iperf3", "client_timeout_s"): 6,
    ("verification", "iperf3", "base_port"): 5201,
    ("audit", "path_bottleneck", "relative_tolerance"): 0.10,
    ("audit", "path_bottleneck", "short_tcp_min_tolerance_mbps"): 0.25,
}
_FORMAL_V3_BINDING_VALUES: dict[tuple[str, ...], object] = {
    ("experiment", "protocol_id"): "wcnc_final_v3",
    ("experiment", "provenance"): "wcnc_final_v3_exp1_formal_protocol",
    ("methods",): ("proposed", "cspf", "global_sfc_embedding"),
    ("deployment", "proposed", "planning"): "exact_task_dag_scope",
    ("deployment", "proposed", "routing"): "batched_bidirectional_task_edge_host_routes",
    ("deployment", "proposed", "scheduling"): "parallel_single_batch",
    ("deployment", "cspf", "planning"): "observed_bandwidth_and_delay_constrained_path",
    ("deployment", "cspf", "routing"): "bidirectional_per_flow_host_routes",
    ("deployment", "cspf", "scheduling"): "sequential_flow_batches",
    ("deployment", "global_sfc_embedding", "planning"): "deterministic_source_to_sink_edge_disjoint_chain_cover",
    ("deployment", "global_sfc_embedding", "routing"): "per_chain_policy_table_host_routes",
    ("deployment", "global_sfc_embedding", "scheduling"): "sequential_hop_route_then_activation_batches",
    ("execution_gate", "require_isolated_outer_network_namespace"): True,
    ("execution_gate", "reject_manual_namespace_sentinel"): True,
    ("execution_gate", "require_all_formal_trials_successful"): True,
    ("verification", "iperf3", "duration_s"): 2,
}
_FORMAL_LEGACY_BINDING_VALUES: dict[tuple[str, ...], object] = {
    ("verification", "iperf3", "duration_s"): 1,
}
_FORMAL_LEGACY_PROVENANCE = "wcnc_v2_fig1_formal_protocol"
_PROTECTED_V1_OUTPUT_ROOTS = (
    Path("results/exp1_netns"),
    Path("results/exp1_wcnc_final"),
    Path("results/wcnc_pilot/exp1_frozen_v2"),
)


def validate_formal_protocol(config: dict[str, Any], seeds: Sequence[int]) -> None:
    if config.get("experiment", {}).get("phase") != "formal":
        return
    simulation = config["simulation"]
    if tuple(int(value) for value in simulation["task_sizes"]) != _FORMAL_TASK_SIZES:
        raise ValueError("formal Exp1 task sizes must be (4, 8, 12, 16, 20)")
    if tuple(int(value) for value in seeds) != _FORMAL_SEEDS:
        raise ValueError("formal Exp1 seeds must be exactly 0..49")
    schedule = simulation.get("schedule", {})
    if int(schedule.get("block_size", 0)) != 5:
        raise ValueError("formal Exp1 schedule block_size must be 5")
    if int(schedule.get("blocks", 0)) != 10:
        raise ValueError("formal Exp1 schedule blocks must be 10")
    if schedule.get("task_size_order") != "latin_rotation":
        raise ValueError("formal Exp1 schedule task_size_order must be latin_rotation")
    for path, expected in _FORMAL_BINDING_VALUES.items():
        _validate_formal_binding_value(config, path, expected)
    experiment = config.get("experiment", {})
    protocol_id = experiment.get("protocol_id")
    provenance = experiment.get("provenance")
    if protocol_id == "wcnc_final_v3":
        for path, expected in _FORMAL_V3_BINDING_VALUES.items():
            _validate_formal_binding_value(config, path, expected)
    elif protocol_id is None and provenance == _FORMAL_LEGACY_PROVENANCE:
        for path, expected in _FORMAL_LEGACY_BINDING_VALUES.items():
            _validate_formal_binding_value(config, path, expected)
    else:
        raise ValueError(
            "unsupported formal Exp1 protocol identity: "
            f"protocol_id={protocol_id!r}, provenance={provenance!r}"
        )


def _validate_formal_binding_value(
    config: dict[str, Any], path: tuple[str, ...], expected: object
) -> None:
    value: object = config
    field = ".".join(path)
    try:
        for key in path:
            value = value[key]  # type: ignore[index]
    except (KeyError, TypeError):
        raise ValueError(f"formal Exp1 {field} must be {expected!r}; field is missing") from None
    actual = _normalize_formal_value(value)
    expected_normalized = _normalize_formal_value(expected)
    if actual != expected_normalized:
        raise ValueError(f"formal Exp1 {field} must be {expected!r}; got {value!r}")


def _normalize_formal_value(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)):
        return tuple(_normalize_formal_value(item) for item in value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def validate_output_directory(output_dir: Path) -> None:
    candidate = output_dir.resolve()
    for protected_root in _PROTECTED_V1_OUTPUT_ROOTS:
        root = protected_root.resolve()
        if candidate == root or root in candidate.parents:
            raise ValueError(f"refusing to write v1 Exp1 output location: {output_dir}")


def build_counterbalanced_schedule(
    task_sizes: Sequence[int], seeds: Sequence[int], *, block_size: int = 5
) -> tuple[ScheduledRun, ...]:
    """Return deterministic five-seed blocks with a Latin rotation of sizes."""
    sizes = tuple(int(value) for value in task_sizes)
    if len(sizes) != 5 or len(set(sizes)) != 5:
        raise ValueError("the frozen Exp1 schedule requires five distinct task sizes")
    if block_size != 5 or len(seeds) % block_size:
        raise ValueError("the frozen Exp1 schedule requires complete five-seed blocks")
    schedule: list[ScheduledRun] = []
    for block_index, start in enumerate(range(0, len(seeds), block_size)):
        rotated_sizes = sizes[block_index % len(sizes) :] + sizes[: block_index % len(sizes)]
        for order_position, num_agents in enumerate(rotated_sizes):
            for seed in seeds[start : start + block_size]:
                schedule.append(
                    ScheduledRun(
                        run_sequence=len(schedule) + 1,
                        block_index=block_index,
                        order_position=order_position,
                        num_agents=num_agents,
                        seed=int(seed),
                    )
                )
    return tuple(schedule)


def build_experiment_schedule(
    config: Mapping[str, object], seeds: Sequence[int]
) -> tuple[ScheduledRun, ...]:
    simulation = config["simulation"]  # type: ignore[index]
    task_sizes = tuple(int(value) for value in simulation["task_sizes"])  # type: ignore[index]
    phase = config.get("experiment", {}).get("phase", "formal")  # type: ignore[union-attr]
    if phase == "formal":
        return build_counterbalanced_schedule(task_sizes, seeds)
    return tuple(
        ScheduledRun(
            run_sequence=index + 1,
            block_index=0,
            order_position=size_index,
            num_agents=num_agents,
            seed=int(seed),
        )
        for index, (size_index, num_agents, seed) in enumerate(
            (size_index, num_agents, seed)
            for size_index, num_agents in enumerate(task_sizes)
            for seed in seeds
        )
    )


def counterbalanced_method_order(
    methods: Sequence[str], seed: int
) -> tuple[str, ...]:
    ordered = tuple(methods)
    if not ordered:
        return ()
    offset = int(seed) % len(ordered)
    return ordered[offset:] + ordered[:offset]


def run_one(
    topology: ProcessNetnsTopology,
    config: dict[str, Any],
    *,
    num_agents: int,
    seed: int,
    run_sequence: int = 1,
    block_index: int = 0,
    order_position: int = 0,
    method_order_position: int = 0,
    method_id: str = "proposed",
) -> tuple[NetnsFormationRun, list[dict[str, object]]]:
    task = config["task"]
    verification = config["verification"]
    ping = verification["ping"]
    iperf = verification["iperf3"]
    traffic_control = config["traffic_control"]
    max_attempts = int(verification["max_attempts"])
    if max_attempts != 2:
        raise ValueError("the frozen verification policy requires exactly two attempts")
    retry_backoff_ms = int(verification["retry_backoff_ms"])
    if retry_backoff_ms != 200:
        raise ValueError("the frozen verification policy requires a fixed 200 ms backoff")
    method_labels = {
        "proposed": "Ours",
        "cspf": "CSPF-based Formation",
        "global_sfc_embedding": "Global SFC Embedding",
    }
    if method_id not in method_labels:
        raise ValueError(f"unsupported canonical Exp1 method: {method_id}")
    run_id = f"agents={num_agents}:seed={seed}:method={method_id}"
    topology.reset_task_routes()
    topology.last_install_commands = []
    failure_reason = ""
    ping_rows: list[dict[str, object]] = []
    iperf_rows: list[dict[str, object]] = []
    extra_events: list[dict[str, object]] = []
    profiles: list[TrafficControlProfile] = []
    background = BackgroundTraffic(enabled=False)
    background_result: dict[str, object] = {"enabled": False}
    ping_passed = 0
    iperf_passed = 0
    aggregate_throughput = 0.0
    ping_attempts = 0
    iperf_attempts = 0
    ping_retried_edges = 0
    iperf_retried_flows = 0
    retry_count = 0
    control_messages = 0
    rules_installed = 0
    tc_started = time.perf_counter()
    tc_finished = tc_started
    background_prepared_at = tc_started
    preparation_error: Exception | None = None
    try:
        profiles = topology.configure_traffic_control(traffic_control, seed=seed)
        tc_finished = time.perf_counter()
        background = topology.start_background_traffic(
            traffic_control["background_traffic"], profiles, seed=seed
        )
        background_prepared_at = time.perf_counter()
    except Exception as error:
        tc_finished = time.perf_counter()
        background_prepared_at = tc_finished
        preparation_error = error
    # T_form begins only when the task is received; topology, qdisc setup, and
    # background-load activation are deliberately outside this interval.
    task_received = time.perf_counter()
    edges = generate_dag(
        num_agents,
        float(task["edge_ratio"]),
        seed,
        float(task["required_throughput_mbps"]),
    )
    deployment_plan = topology.plan_task_routes(
        method_id,
        edges,
        profiles=profiles,
        max_path_delay_ms=float(ping["max_average_rtt_ms"]),
    )
    planning_audit = deployment_plan.audit()
    mapping_finished = time.perf_counter()
    install_started = mapping_finished
    install_finished = install_started
    activation_finished = install_finished
    verification_started = activation_finished
    ping_started = verification_started
    ping_finished = ping_started
    iperf_started = ping_finished
    failure_stage = ""
    current_stage = "PREPARATION"
    try:
        if preparation_error is not None:
            raise preparation_error
        current_stage = "ROUTE_INSTALLATION"
        install_started = time.perf_counter()
        control_messages, rules_installed = topology.install_deployment_plan(
            deployment_plan
        )
        install_finished = time.perf_counter()
        activation_finished = time.perf_counter()
        verification_started = activation_finished
        current_stage = "PING_VERIFICATION"
        ping_started = verification_started
        remaining_ping = list(edges)
        final_ping: dict[str, dict[str, object]] = {}
        for attempt in range(1, max_attempts + 1):
            ping_attempts = attempt
            _passed, attempt_rows = topology.verify_ping(
                remaining_ping,
                attempt=attempt,
                count=int(ping["count"]),
                timeout_s=float(ping["timeout_s"]),
                interval_s=float(ping["interval_s"]),
                max_packet_loss_percent=float(ping.get("max_packet_loss_percent", 100.0)),
                max_average_rtt_ms=float(ping["max_average_rtt_ms"]),
                parallel=bool(verification.get("parallel", True)),
            )
            ping_rows.extend(attempt_rows)
            final_ping.update({str(item["edge_id"]): item for item in attempt_rows})
            remaining_ping = [
                edge
                for edge in remaining_ping
                if not bool(final_ping[edge.edge_id]["passed"])
            ]
            if not remaining_ping or attempt == max_attempts:
                break
            if attempt == 1:
                ping_retried_edges = len(remaining_ping)
            retry_count += 1
            extra_events.append(
                _backoff_event(
                    run_id,
                    seed,
                    num_agents,
                    run_sequence,
                    block_index,
                    order_position,
                    "PING",
                    retry_backoff_ms,
                )
            )
        ping_finished = time.perf_counter()
        ping_passed = sum(bool(item["passed"]) for item in final_ping.values())
        iperf_started = ping_finished
        if ping_passed == len(edges):
            current_stage = "IPERF3_VERIFICATION"
            remaining_iperf = list(edges)
            final_iperf: dict[str, dict[str, object]] = {}
            for attempt in range(1, max_attempts + 1):
                iperf_attempts = attempt
                _passed, _throughput, attempt_rows = topology.verify_iperf3(
                    remaining_iperf,
                    attempt=attempt,
                    duration_s=float(iperf["duration_s"]),
                    omit_s=float(iperf.get("omit_s", 0.0)),
                    client_timeout_s=float(iperf["client_timeout_s"]),
                    base_port=int(iperf["base_port"]),
                    parallel=bool(verification.get("parallel", True)),
                )
                iperf_rows.extend(attempt_rows)
                final_iperf.update(
                    {str(item["edge_id"]): item for item in attempt_rows}
                )
                remaining_iperf = [
                    edge
                    for edge in remaining_iperf
                    if not bool(final_iperf[edge.edge_id]["passed"])
                ]
                if not remaining_iperf or attempt == max_attempts:
                    break
                if attempt == 1:
                    iperf_retried_flows = len(remaining_iperf)
                retry_count += 1
                extra_events.append(
                    _backoff_event(
                        run_id,
                        seed,
                        num_agents,
                        run_sequence,
                        block_index,
                        order_position,
                        "IPERF3",
                        retry_backoff_ms,
                    )
                )
            iperf_passed = sum(
                bool(item["passed"]) for item in final_iperf.values()
            )
            aggregate_throughput = sum(
                float(item["throughput_mbps"]) for item in final_iperf.values()
            )
        verified_at = time.perf_counter()
        success = ping_passed == len(edges) and iperf_passed == len(edges)
        if not success:
            failure_stage = "DATA_PLANE_VERIFICATION"
            failure_reason = f"data_plane_verification_failed:ping={ping_passed}/{len(edges)}:iperf3={iperf_passed}/{len(edges)}"
    except Exception as error:
        verified_at = time.perf_counter()
        tc_finished = max(tc_finished, tc_started)
        install_started = max(install_started, tc_finished)
        install_finished = max(install_finished, install_started)
        activation_finished = max(activation_finished, install_finished)
        verification_started = max(verification_started, activation_finished)
        ping_started = max(ping_started, verification_started)
        ping_finished = max(ping_finished, ping_started)
        iperf_started = max(iperf_started, ping_finished)
        success = False
        failure_stage = current_stage
        failure_reason = f"{type(error).__name__}:{error}"
    finally:
        background_result = topology.stop_background_traffic(background)
    final_ping_rows = _final_attempt_rows(ping_rows)
    final_iperf_rows = _final_attempt_rows(iperf_rows)
    average_rtts = [
        float(item["average_rtt_ms"])
        for item in final_ping_rows
        if item.get("average_rtt_ms") is not None
    ]
    reported_durations = [
        float(item.get("reported_duration_s", 0.0)) for item in final_iperf_rows
    ]
    row = NetnsFormationRun(
        run_id=run_id,
        run_sequence=run_sequence,
        block_index=block_index,
        order_position=order_position,
        method_order_position=method_order_position,
        seed=seed,
        method_id=method_id,
        method_label=method_labels[method_id],
        num_agents=num_agents,
        num_gateways=topology.num_gateways,
        num_business_edges=len(edges),
        control_messages=control_messages,
        rules_installed=rules_installed,
        scenario_fingerprint=scenario_fingerprint(num_agents, topology.num_gateways, edges),
        result_mode="real_linux_netns_veth_tc_data_plane",
        configuration_sha256=_configuration_sha256(config),
        task_received_at=task_received,
        mapping_finished_at=mapping_finished,
        traffic_control_started_at=tc_started,
        traffic_control_finished_at=tc_finished,
        route_install_started_at=install_started,
        route_install_finished_at=install_finished,
        activation_finished_at=activation_finished,
        verification_started_at=verification_started,
        ping_verify_started_at=ping_started,
        ping_verify_finished_at=ping_finished,
        iperf_verify_started_at=iperf_started,
        data_plane_verified_at=verified_at,
        mapping_latency_s=mapping_finished - task_received,
        traffic_control_latency_s=tc_finished - tc_started,
        route_install_latency_s=install_finished - install_started,
        ping_verification_latency_s=ping_finished - ping_started,
        iperf3_verification_latency_s=verified_at - iperf_started,
        verification_latency_s=verified_at - verification_started,
        verified_formation_latency_s=measured_formation_latency(
            task_received_at=task_received,
            verified_at=verified_at,
            failure_stage=failure_stage,
        ),
        ping_edges_total=len(edges),
        ping_edges_passed=ping_passed,
        ping_attempts=ping_attempts,
        ping_retried_edges=ping_retried_edges,
        ping_mean_rtt_ms=(sum(average_rtts) / len(average_rtts) if average_rtts else 0.0),
        ping_max_packet_loss_percent=max(
            (float(item["packet_loss_percent"]) for item in final_ping_rows),
            default=100.0,
        ),
        ping_timeouts=sum(bool(item.get("timeout")) for item in ping_rows),
        iperf_flows_total=len(edges),
        iperf_flows_passed=iperf_passed,
        iperf_attempts=iperf_attempts,
        iperf_retried_flows=iperf_retried_flows,
        iperf_retransmissions=sum(
            int(item.get("retransmissions", 0)) for item in final_iperf_rows
        ),
        iperf_reported_duration_s=max(reported_durations, default=0.0),
        aggregate_receiver_throughput_mbps=aggregate_throughput,
        retry_backoff_ms=retry_backoff_ms,
        retry_count=retry_count,
        background_traffic_enabled=background.enabled,
        background_utilization=background.utilization,
        background_target_mbps=background.target_mbps,
        tc_profile_count=len(profiles),
        failure_stage=failure_stage,
        formation_timing_valid=failure_stage != "PREPARATION",
        formation_failed=not success,
        success=success,
        failure_reason=failure_reason,
    )
    events = [
        {
            "run_id": run_id,
            "run_sequence": run_sequence,
            "block_index": block_index,
            "order_position": order_position,
            "method_order_position": method_order_position,
            "seed": seed,
            "method_id": method_id,
            "num_agents": num_agents,
            "timestamp": timestamp,
            "stage": stage,
            "details": details,
        }
        for timestamp, stage, details in (
            (
                background_prepared_at,
                "BACKGROUND_PREPARATION_FAILED"
                if preparation_error is not None
                else "BACKGROUND_PREPARED",
                {
                    "enabled": background.enabled,
                    "source_gateway_index": background.source_gateway_index,
                    "target_gateway_index": background.target_gateway_index,
                    "source_endpoint": f"gateway-{background.source_gateway_index}:up0",
                    "target_endpoint": f"gateway-{background.target_gateway_index}:up0",
                    "forward_path_endpoints": list(background.forward_path_endpoints),
                    "forward_path_bottleneck_mbps": background.forward_path_bottleneck_mbps,
                    "preparation_commands": list(background.preparation_commands),
                    "readiness_ready": background.readiness_ready,
                    "readiness_timeout_s": background.readiness_timeout_s,
                    "minimum_payload_bytes": background.minimum_payload_bytes,
                    "source_up0_tx_bytes_before": background.source_up0_tx_bytes_before,
                    "source_up0_tx_bytes_after": background.source_up0_tx_bytes_after,
                    "target_up0_rx_bytes_before": background.target_up0_rx_bytes_before,
                    "target_up0_rx_bytes_after": background.target_up0_rx_bytes_after,
                    "source_up0_tx_delta_bytes": background.source_up0_tx_delta_bytes,
                    "target_up0_rx_delta_bytes": background.target_up0_rx_delta_bytes,
                },
            ),
            (task_received, "TASK_RECEIVED", {}),
            (mapping_finished, "MAPPING_FINISHED", {"business_edges": len(edges), **planning_audit}),
            (
                tc_started,
                "TRAFFIC_CONTROL_STARTED",
                {"implementation": "Linux tc HTB + netem"},
            ),
            (
                tc_finished,
                "TRAFFIC_CONTROL_FINISHED",
                {"profiles": [asdict(item) for item in profiles]},
            ),
            (
                install_started,
                "ROUTE_INSTALL_STARTED",
                {
                    "control_messages": control_messages,
                    "rules_installed": rules_installed,
                    "deployment_batches": [
                        {
                            "label": batch.label,
                            "command_count": len(batch.commands),
                        }
                        for batch in deployment_plan.batches
                    ],
                    "commands": topology.last_install_commands,
                },
            ),
            (install_finished, "ROUTE_INSTALL_FINISHED", {}),
            (
                activation_finished,
                "ACTIVATION_FINISHED",
                {
                    "background_traffic": {
                        "enabled": background.enabled,
                        "utilization": background.utilization,
                        "target_mbps": background.target_mbps,
                        "minimum_payload_bytes": background.minimum_payload_bytes,
                        "source_gateway_index": background.source_gateway_index,
                        "target_gateway_index": background.target_gateway_index,
                        "source_endpoint": f"gateway-{background.source_gateway_index}:up0",
                        "target_endpoint": f"gateway-{background.target_gateway_index}:up0",
                        "forward_path_endpoints": list(background.forward_path_endpoints),
                        "forward_path_bottleneck_mbps": background.forward_path_bottleneck_mbps,
                        "readiness_ready": background.readiness_ready,
                        "source_up0_tx_bytes_before": background.source_up0_tx_bytes_before,
                        "source_up0_tx_bytes_after": background.source_up0_tx_bytes_after,
                        "target_up0_rx_bytes_before": background.target_up0_rx_bytes_before,
                        "target_up0_rx_bytes_after": background.target_up0_rx_bytes_after,
                        "source_up0_tx_delta_bytes": background.source_up0_tx_delta_bytes,
                        "target_up0_rx_delta_bytes": background.target_up0_rx_delta_bytes,
                        "server_command": list(background.server_command),
                        "client_command": list(background.client_command),
                    }
                },
            ),
            (ping_started, "PING_VERIFY_STARTED", {}),
            (ping_finished, "PING_VERIFY_FINISHED", {"passed": ping_passed, "total": len(edges)}),
            (iperf_started, "IPERF3_VERIFY_STARTED", {}),
            (verified_at, "DATA_PLANE_VERIFIED" if success else "FORMATION_FAILED", {"passed": iperf_passed, "total": len(edges), "failure_reason": failure_reason}),
        )
    ]
    events.extend(extra_events)
    events.extend(
        {
            "run_id": run_id,
            "run_sequence": run_sequence,
            "block_index": block_index,
            "order_position": order_position,
            "seed": seed,
            "num_agents": num_agents,
            "timestamp": ping_finished,
            "stage": "PING_COMMAND_RESULT",
            "details": result,
        }
        for result in ping_rows
    )
    events.extend(
        {
            "run_id": run_id,
            "run_sequence": run_sequence,
            "block_index": block_index,
            "order_position": order_position,
            "seed": seed,
            "num_agents": num_agents,
            "timestamp": verified_at,
            "stage": "IPERF3_COMMAND_RESULT",
            "details": result,
        }
        for result in iperf_rows
    )
    events.append(
        {
            "run_id": run_id,
            "run_sequence": run_sequence,
            "block_index": block_index,
            "order_position": order_position,
            "seed": seed,
            "num_agents": num_agents,
            "timestamp": verified_at,
            "stage": "BACKGROUND_TRAFFIC_RESULT",
            "details": background_result,
        }
    )
    events.sort(key=lambda item: (float(item["timestamp"]), str(item["stage"])))
    return row, events


def run_experiment(config: dict[str, Any], output_dir: Path, seeds: Sequence[int]) -> list[NetnsFormationRun]:
    validate_formal_protocol(config, seeds)
    validate_output_directory(output_dir)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[NetnsFormationRun] = []
    events: list[dict[str, object]] = []
    task_sizes = tuple(int(value) for value in config["simulation"]["task_sizes"])
    schedule = build_experiment_schedule(config, seeds)
    methods = tuple(config.get("methods", ("proposed",)))
    if methods not in {("proposed",), ("proposed", "cspf", "global_sfc_embedding")}:
        raise ValueError("canonical Exp1 requires proposed,cspf,global_sfc_embedding")
    for scheduled_run in schedule:
        # Recreating the process topology makes size-order counterbalancing
        # explicit and prevents qdisc state from leaking across size points.
        method_order = counterbalanced_method_order(methods, scheduled_run.seed)
        for method_offset, method_id in enumerate(method_order):
            with ProcessNetnsTopology(
                scheduled_run.num_agents, int(config["task"]["num_gateways"])
            ) as topology:
                kwargs = {
                    "run_sequence": (scheduled_run.run_sequence - 1) * len(methods) + method_offset + 1,
                    "block_index": scheduled_run.block_index,
                    "order_position": scheduled_run.order_position,
                    "method_order_position": method_offset,
                }
                if len(methods) > 1:
                    kwargs["method_id"] = method_id
                row, run_events = run_one(
                    topology, config, num_agents=scheduled_run.num_agents,
                    seed=scheduled_run.seed, **kwargs,
                )
                rows.append(row)
                events.extend(run_events)
                latency = (
                    f"{row.verified_formation_latency_s:.6f}s"
                    if row.verified_formation_latency_s is not None
                    else "N/A"
                )
                print(
                    f"{row.run_id}: success={row.success} "
                    f"failure_stage={row.failure_stage or 'NONE'} T_form={latency}",
                    flush=True,
                )
    _write_csv(raw_dir / "runs.csv", rows)
    _write_jsonl(raw_dir / "events.jsonl", events)
    metadata = {
        "result_mode": "real_linux_netns_veth_tc_data_plane",
        "namespace_mechanism": "unshare user+network namespace with process-owned child network namespaces",
        "veth": True,
        "route_installation": True,
        "policy_rule_installation": True,
        "traffic_control_implementation": "Linux tc with HTB root class and netem child qdisc",
        "traffic_control": config["traffic_control"],
        "ping_command_template": _ping_template(config),
        "ping_retry_policy": "retry failed business edges once after fixed backoff",
        "iperf3_server_command_template": "iperf3 -s -1 -p <port> -J",
        "iperf3_client_command_template": _iperf_template(config),
        "iperf3_retry_policy": "retry sub-threshold or failed flows once after fixed backoff",
        "retry_backoff_ms": int(config["verification"]["retry_backoff_ms"]),
        "configuration_sha256": _configuration_sha256(config),
        "parallel_verification": bool(config["verification"].get("parallel", True)),
        "seeds": list(seeds),
        "task_sizes": list(task_sizes),
        "methods": list(methods),
        "schedule": [asdict(item) for item in schedule],
        "schedule_policy": "five-seed blocks with Latin task-size rotation",
        "method_schedule_policy": "paired-seed deterministic Latin rotation",
    }
    (raw_dir / "measurement_scope.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return rows


def _plan_canonical_formation(
    method_id: str, edges: Sequence[FormationEdge], num_gateways: int
) -> dict[str, object]:
    """Return deterministic planner evidence independent of kernel execution."""
    ordered = _ordered_formation_edges(method_id, edges, num_gateways)
    if method_id == "proposed":
        policy = "task_dag_batch_parallel_deployment"
        chains = ()
    elif method_id == "cspf":
        policy = "failed_or_insufficient_links_pruned_then_deterministic_te_shortest_path"
        chains = ()
    elif method_id == "global_sfc_embedding":
        policy = "task_dag_source_to_sink_chain_embedding_heuristic"
        chains = _service_chains(edges)
    else:
        raise ValueError(f"unsupported canonical Exp1 method: {method_id}")
    return {
        "planning_policy": policy,
        "tie_break": "canonical_edge_id",
        "planned_edge_count": len(ordered),
        "ordered_edge_ids": [edge.edge_id for edge in ordered],
        "service_chain_count": len(chains),
    }


def _ordered_formation_edges(
    method_id: str, edges: Sequence[FormationEdge], num_gateways: int
) -> tuple[FormationEdge, ...]:
    if method_id == "proposed":
        return tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.source_index % num_gateways,
                    item.target_index % num_gateways,
                    item.edge_id,
                ),
            )
        )
    if method_id == "cspf":
        return tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.source_index % num_gateways != item.target_index % num_gateways,
                    abs(
                        (item.target_index % num_gateways)
                        - (item.source_index % num_gateways)
                    ),
                    item.edge_id,
                ),
            )
        )
    if method_id == "global_sfc_embedding":
        return tuple(sorted(edges, key=lambda item: item.edge_id))
    raise ValueError(f"unsupported canonical Exp1 method: {method_id}")


def _service_chains(
    edges: Sequence[FormationEdge],
) -> tuple[tuple[FormationEdge, ...], ...]:
    """Return a deterministic edge-disjoint chain cover of the Task DAG."""
    ordered = tuple(sorted(edges, key=lambda item: item.edge_id))
    incoming = {edge.target_index for edge in ordered}
    starts = [edge for edge in ordered if edge.source_index not in incoming]
    starts.extend(edge for edge in ordered if edge not in starts)
    outgoing: dict[int, list[FormationEdge]] = {}
    for edge in ordered:
        outgoing.setdefault(edge.source_index, []).append(edge)
    used: set[str] = set()
    chains: list[tuple[FormationEdge, ...]] = []
    for start in starts:
        if start.edge_id in used:
            continue
        chain: list[FormationEdge] = []
        current = start
        while current.edge_id not in used:
            used.add(current.edge_id)
            chain.append(current)
            remaining = [
                edge
                for edge in outgoing.get(current.target_index, ())
                if edge.edge_id not in used
            ]
            if not remaining:
                break
            current = remaining[0]
        chains.append(tuple(chain))
    return tuple(chains)


def _ping_template(config: dict[str, Any]) -> str:
    values = config["verification"]["ping"]
    return (
        f"ping -n -c {int(values['count'])} -W {math.ceil(float(values['timeout_s']))} "
        f"-i {float(values['interval_s'])} <destination_ip>"
    )


def _iperf_template(config: dict[str, Any]) -> str:
    values = config["verification"]["iperf3"]
    return (
        f"iperf3 -c <destination_ip> -p <port> -t {float(values['duration_s'])} "
        f"-O {float(values.get('omit_s', 0.0))} -J"
    )


def _configuration_sha256(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ping_packet_loss(stdout: str) -> float:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s+packet loss", stdout)
    return float(match.group(1)) if match else 100.0


def _ping_average_rtt(stdout: str) -> float | None:
    match = re.search(
        r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
        r"[0-9.]+/([0-9.]+)/",
        stdout,
    )
    return float(match.group(1)) if match else None


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _final_attempt_rows(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    final: dict[str, dict[str, object]] = {}
    for row in rows:
        final[str(row["edge_id"])] = row
    return list(final.values())


def _backoff_event(
    run_id: str,
    seed: int,
    num_agents: int,
    run_sequence: int,
    block_index: int,
    order_position: int,
    verification_type: str,
    backoff_ms: int,
) -> dict[str, object]:
    started = time.perf_counter()
    time.sleep(backoff_ms / 1000.0)
    finished = time.perf_counter()
    return {
        "run_id": run_id,
        "run_sequence": run_sequence,
        "block_index": block_index,
        "order_position": order_position,
        "seed": seed,
        "num_agents": num_agents,
        "timestamp": finished,
        "stage": "VERIFICATION_RETRY_BACKOFF",
        "details": {
            "verification_type": verification_type,
            "configured_backoff_ms": backoff_ms,
            "observed_backoff_ms": (finished - started) * 1000.0,
        },
    }


def _write_csv(path: Path, rows: Iterable[NetnsFormationRun]) -> None:
    materialized = [asdict(row) for row in rows]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(NetnsFormationRun.__dataclass_fields__), lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _parse_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        return tuple(range(start, finish + 1))
    return tuple(int(item) for item in value.split(",") if item.strip())


def measured_formation_latency(
    *, task_received_at: float, verified_at: float, failure_stage: str
) -> float | None:
    if failure_stage == "PREPARATION":
        return None
    return verified_at - task_received_at


def formal_run_exit_code(
    config: Mapping[str, object],
    rows: Sequence[object],
    *,
    seeds: Sequence[int],
    require_all_success: bool = False,
) -> int:
    if (
        config.get("experiment", {}).get("phase") != "formal"  # type: ignore[union-attr]
        and not require_all_success
    ):
        return 0
    simulation = config["simulation"]  # type: ignore[index]
    task_sizes = simulation["task_sizes"]  # type: ignore[index]
    methods = config.get("methods", ("proposed",))
    expected = len(task_sizes) * len(seeds) * len(methods)  # type: ignore[arg-type]
    if len(rows) != expected:
        return 1
    return int(any(not bool(getattr(row, "success", False)) for row in rows))


def _inside_user_namespace() -> bool:
    return os.environ.get("WCNC_EXP1_INSIDE_USERNS") == "1"


def _network_namespace_inode() -> int:
    return os.stat("/proc/self/ns/net").st_ino


def _init_network_namespace_inode() -> int:
    return os.stat("/proc/1/ns/net").st_ino


def _network_interface_names() -> set[str]:
    result = subprocess.run(
        ("ip", "-j", "link", "show"),
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(result.stdout)
    return {
        str(item["ifname"])
        for item in payload
        if isinstance(item, dict) and "ifname" in item
    }


def validate_isolated_outer_namespace(
    environment: Mapping[str, str] | None = None,
    *,
    current_inode: int | None = None,
    init_inode: int | None = None,
    interface_names: set[str] | None = None,
) -> None:
    values = os.environ if environment is None else environment
    if values.get("WCNC_EXP1_INSIDE_USERNS") != "1":
        raise RuntimeError("Exp1 outer namespace sentinel is missing")
    parent_value = values.get("WCNC_EXP1_PARENT_NETNS_INODE")
    if not parent_value:
        raise RuntimeError("Exp1 outer namespace provenance is missing; do not set the sentinel manually")
    try:
        parent_inode = int(parent_value)
    except ValueError as error:
        raise RuntimeError("Exp1 outer namespace provenance is invalid") from error
    actual_inode = _network_namespace_inode() if current_inode is None else current_inode
    if actual_inode == parent_inode:
        raise RuntimeError("Exp1 outer network namespace is not isolated from its launcher")
    pid_one_inode = init_inode
    if init_inode is None:
        try:
            pid_one_inode = _init_network_namespace_inode()
        except (PermissionError, FileNotFoundError):
            # Unprivileged user namespaces may not be allowed to inspect PID 1.
            # Parent provenance and the current netlink view remain mandatory.
            pid_one_inode = None
    if pid_one_inode is not None and actual_inode == pid_one_inode:
        raise RuntimeError("Exp1 outer network namespace still matches PID 1")
    actual_interfaces = (
        _network_interface_names() if interface_names is None else interface_names
    )
    if actual_interfaces != {"lo"}:
        raise RuntimeError(
            "Exp1 outer network namespace is not fresh; expected only lo, got "
            + ",".join(sorted(actual_interfaces))
        )


def namespace_reexec_command(
    argv: Sequence[str], *, euid: int, python: str
) -> tuple[str, ...]:
    namespace_options = (
        ("--net", "--fork")
        if euid == 0
        else ("--user", "--map-root-user", "--net", "--fork")
    )
    return (
        "unshare",
        *namespace_options,
        python,
        "-m",
        "experiments.exp1_netns_verified_formation",
        *argv,
    )


def _reexec_in_user_namespace(argv: Sequence[str]) -> int:
    environment = dict(os.environ)
    environment["WCNC_EXP1_INSIDE_USERNS"] = "1"
    environment["WCNC_EXP1_PARENT_NETNS_INODE"] = str(_network_namespace_inode())
    command = namespace_reexec_command(
        argv,
        euid=os.geteuid(),
        python=sys.executable,
    )
    return subprocess.run(command, env=environment, check=False).returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real Linux netns verified initial formation experiment")
    parser.add_argument("--config", default="configs/exp1_netns_verified_formation_v2.yaml")
    parser.add_argument("--output-dir", default="results/exp1_wcnc_final_v2")
    parser.add_argument("--seeds", default="")
    parser.add_argument("--task-sizes", default="")
    parser.add_argument("--methods", default="")
    parser.add_argument(
        "--require-all-success",
        action="store_true",
        help="return non-zero when a smoke/pilot trial fails or is missing",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if args.task_sizes:
        config["simulation"]["task_sizes"] = [
            int(value) for value in args.task_sizes.split(",") if value.strip()
        ]
    if args.methods:
        config["methods"] = [value for value in args.methods.split(",") if value]
    configured_seeds = args.seeds or str(config["simulation"]["seeds"])
    seeds = _parse_seeds(configured_seeds)
    validate_formal_protocol(config, seeds)
    validate_output_directory(Path(args.output_dir))
    if not _inside_user_namespace():
        return_code = _reexec_in_user_namespace(sys.argv[1:])
        raise SystemExit(return_code)
    validate_isolated_outer_namespace()
    rows = run_experiment(config, Path(args.output_dir), seeds)
    successful = sum(row.success for row in rows)
    print(f"Exp1 netns completed: {successful}/{len(rows)} successful runs")
    exit_code = formal_run_exit_code(
        config,
        rows,
        seeds=seeds,
        require_all_success=args.require_all_success,
    )
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
