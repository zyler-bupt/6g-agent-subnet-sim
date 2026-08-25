from __future__ import annotations

import asyncio
import csv
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml
import experiments.exp1_netns_verified_formation as exp1_netns
import experiments.exp2_demand_capacity_ratio as exp2_demand
import experiments.exp4_failure_reconfiguration as exp4_failure
import src.controller.cross_layer_coordinator as coordinator_module
from experiments.exp1_netns_verified_formation import generate_dag
from experiments.exp2_cross_layer_robustness import run_case as run_exp2_case
from scripts.audit_exp1_netns import audit as audit_exp1_netns
from scripts.audit_exp1_netns import expected_endpoints as audit_expected_endpoints
import scripts.audit_exp1_netns as audit_exp1_module
from experiments.exp4_failure_reconfiguration import run_one
from scripts.aggregate_exp2_demand_capacity import aggregate as aggregate_exp2_demand
from src.controller.cspf import (
    CspfRequest,
    CspfSolver,
    TrafficEngineeringLink,
)
from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.controller.feasibility import evaluate_cross_layer_combination
from src.controller.failure_recovery import CspfNetworkOnlyStrategy
from src.simulation.conflict_robustness import (
    NO_CONFLICT,
    RESOLVABLE_CONFLICT,
    UNRESOLVABLE_CONFLICT,
)
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig
from src.simulation.conflict_stress import CrossLayerStressGenerator
from src.simulation.demand_capacity_ratio import DemandCapacityRatioGenerator
from src.simulation.failure_scenario_generator import (
    FailureScenarioConfig,
    FailureScenarioGenerator,
)


class RealFormationScenarioTests(unittest.TestCase):
    def test_generated_formation_graph_is_deterministic_dag(self) -> None:
        first = generate_dag(20, 1.25, 7, 0.5)
        second = generate_dag(20, 1.25, 7, 0.5)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertTrue(all(edge.source_index < edge.target_index for edge in first))
        self.assertEqual(len({edge.edge_id for edge in first}), len(first))

    def test_tc_profiles_cover_every_access_and_gateway_veth_direction(self) -> None:
        """A missing agent eth0 or gateway gaN shaper would evade an access bottleneck."""
        topology = exp1_netns.ProcessNetnsTopology(4, 4)
        topology.agents = [SimpleNamespace(pid=100 + index) for index in range(4)]
        topology.gateways = [SimpleNamespace(pid=200 + index) for index in range(4)]
        topology.agent_gateways = [0, 1, 2, 3]
        with patch.object(topology, "_apply_tc_profile"):
            profiles = topology.configure_traffic_control(
                {
                    "netem": {
                        "base_delay_ms": [5.0, 5.0],
                        "jitter_ms": [1.0, 1.0],
                        "packet_loss_percent": [0.0, 0.0],
                        "queue_limit_packets": [100, 100],
                    },
                    "htb": {"bandwidth_mbps": [40.0, 40.0]},
                },
                seed=7,
            )
        endpoints = {profile.endpoint for profile in profiles}
        self.assertEqual(
            endpoints,
            {
                "agent-0:eth0",
                "agent-1:eth0",
                "agent-2:eth0",
                "agent-3:eth0",
                "gateway-0:ga0",
                "gateway-1:ga1",
                "gateway-2:ga2",
                "gateway-3:ga3",
                "gateway-0:up0",
                "gateway-1:up0",
                "gateway-2:up0",
                "gateway-3:up0",
                "outer:og0",
                "outer:og1",
                "outer:og2",
                "outer:og3",
            },
        )

    def test_gateway_uplink_counters_use_namespace_aware_ip_json_stats64(self) -> None:
        """A host-mounted sysfs path cannot measure a gateway network namespace."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology.gateways = [SimpleNamespace(pid=4242)]
        calls: list[tuple[int, tuple[str, ...]]] = []

        def run_ns(pid: int, *command: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((pid, command))
            return subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout=json.dumps(
                    [{"ifname": "up0", "stats64": {"tx": {"bytes": 131072}, "rx": {"bytes": 65536}}}]
                ),
                stderr="",
            )

        topology._run_ns = run_ns

        self.assertEqual(topology._gateway_up0_tx_bytes(0), 131072)
        self.assertEqual(topology._gateway_up0_rx_bytes(0), 65536)
        self.assertEqual(
            calls,
            [
                (4242, ("ip", "-j", "-s", "link", "show", "dev", "up0")),
                (4242, ("ip", "-j", "-s", "link", "show", "dev", "up0")),
            ],
        )

    def test_gateway_uplink_counters_support_stats_fallback_and_explain_invalid_json(self) -> None:
        """Older iproute2 stats payloads work, while malformed output carries useful diagnostics."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology.gateways = [SimpleNamespace(pid=43)]
        topology._run_ns = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout='[{"ifname":"up0","stats":{"tx":{"bytes":7},"rx":{"bytes":9}}}]',
            stderr="",
        )
        self.assertEqual(topology._gateway_up0_tx_bytes(0), 7)
        topology._run_ns = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=(), returncode=0, stdout="not-json", stderr="ip link diagnostic"
        )
        with self.assertRaisesRegex(RuntimeError, "ip link diagnostic"):
            topology._gateway_up0_rx_bytes(0)

    def test_ping_partial_loss_with_a_valid_rtt_is_reachable(self) -> None:
        """Changing ping reachability to require zero loss would reject usable paths."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology.agent_ips = ["10.0.0.2", "10.0.1.2"]
        topology.agents = [SimpleNamespace(pid=10), SimpleNamespace(pid=11)]
        topology._run_ns = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=(
                "3 packets transmitted, 2 received, 33.333% packet loss, time 401ms\n"
                "rtt min/avg/max/mdev = 10.000/20.000/30.000/1.000 ms\n"
            ),
            stderr="",
        )
        edge = exp1_netns.FormationEdge("partial-loss", 0, 1, 0.5)

        passed, rows = topology.verify_ping(
            [edge],
            attempt=1,
            count=3,
            timeout_s=1.0,
            interval_s=0.2,
            max_packet_loss_percent=0.0,
            max_average_rtt_ms=300.0,
            parallel=False,
        )

        self.assertEqual(passed, 1)
        self.assertTrue(rows[0]["passed"])
        self.assertAlmostEqual(float(rows[0]["packet_loss_percent"]), 33.333)

    def test_run_schedule_is_counterbalanced_in_five_seed_blocks(self) -> None:
        """Strictly grouping all seeds by ascending size would confound size with run order."""
        captured: list[tuple[int, int, dict[str, object]]] = []

        class FakeTopology:
            def __init__(self, *_args: object) -> None:
                pass

            def __enter__(self) -> "FakeTopology":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        def fake_run_one(
            _topology: object,
            _config: dict[str, object],
            *,
            num_agents: int,
            seed: int,
            **metadata: object,
        ) -> tuple[SimpleNamespace, list[dict[str, object]]]:
            captured.append((num_agents, seed, metadata))
            return (
                SimpleNamespace(
                    run_id=f"{num_agents}:{seed}",
                    success=True,
                    failure_stage="",
                    verified_formation_latency_s=0.0,
                ),
                [],
            )

        sizes = [4, 8, 12, 16, 20]
        seeds = tuple(range(50))
        config = {
            "simulation": {"task_sizes": sizes},
            "task": {"num_gateways": 4},
            "traffic_control": {},
            "verification": {
                "ping": {"count": 3, "timeout_s": 1, "interval_s": 0.2},
                "iperf3": {"duration_s": 1, "omit_s": 0},
                "retry_backoff_ms": 200,
            },
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(exp1_netns, "ProcessNetnsTopology", FakeTopology),
                patch.object(exp1_netns, "run_one", fake_run_one),
                patch.object(exp1_netns, "_write_csv"),
                patch.object(exp1_netns, "_write_jsonl"),
                patch("builtins.print"),
            ):
                exp1_netns.run_experiment(config, Path(temporary_directory), seeds)

        expected = []
        for block_index in range(10):
            order = sizes[block_index % len(sizes) :] + sizes[: block_index % len(sizes)]
            for order_position, num_agents in enumerate(order):
                for seed in seeds[block_index * 5 : (block_index + 1) * 5]:
                    expected.append(
                        (
                            num_agents,
                            seed,
                            {
                                "run_sequence": len(expected) + 1,
                                "block_index": block_index,
                                "order_position": order_position,
                                "method_order_position": 0,
                            },
                        )
                    )
        self.assertEqual(captured, expected)

    def test_traffic_control_and_background_start_before_task_timing(self) -> None:
        """Including topology traffic preparation in T_form would overstate formation latency."""
        class FakeTopology:
            num_gateways = 4
            last_install_commands: list[dict[str, object]] = []

            def reset_task_routes(self) -> None:
                return None

            def configure_traffic_control(self, *_args: object, **_kwargs: object) -> list[object]:
                return []

            def start_background_traffic(self, *_args: object, **_kwargs: object) -> object:
                traffic = exp1_netns.BackgroundTraffic(enabled=True)
                traffic.preparation_commands = [{"command": ["ip", "route", "replace"]}]
                traffic.readiness_ready = True
                traffic.source_up0_tx_bytes_before = 100
                traffic.source_up0_tx_bytes_after = 125
                traffic.target_up0_rx_bytes_before = 200
                traffic.target_up0_rx_bytes_after = 235
                return traffic

            def plan_task_routes(
                self, method_id: str, edges: object, **_kwargs: object
            ) -> exp1_netns.FormationDeploymentPlan:
                edge_ids = tuple(edge.edge_id for edge in edges)
                command = exp1_netns.DeploymentCommand(
                    "outer", None, ("ip", "route", "replace", "default", "dev", "lo")
                )
                return exp1_netns.FormationDeploymentPlan(
                    method_id=method_id,
                    planning_policy="test_fixture",
                    tie_break="canonical_edge_id",
                    ordered_edge_ids=edge_ids,
                    service_chain_count=0,
                    planning_work_units=len(edge_ids),
                    batches=(exp1_netns.DeploymentBatch("fixture", (command,)),),
                )

            def install_deployment_plan(
                self, plan: exp1_netns.FormationDeploymentPlan
            ) -> tuple[int, int]:
                self.last_install_commands = [
                    {"namespace": command.namespace, "command": list(command.argv)}
                    for batch in plan.batches
                    for command in batch.commands
                ]
                return plan.control_messages, plan.rules_installed

            def verify_ping(self, edges: object, **_kwargs: object) -> tuple[int, list[dict[str, object]]]:
                rows = [
                    {
                        "edge_id": edge.edge_id,
                        "passed": True,
                        "average_rtt_ms": 1.0,
                        "packet_loss_percent": 0.0,
                        "timeout": False,
                    }
                    for edge in edges
                ]
                return len(rows), rows

            def verify_iperf3(self, edges: object, **_kwargs: object) -> tuple[int, float, list[dict[str, object]]]:
                rows = [
                    {
                        "edge_id": edge.edge_id,
                        "passed": True,
                        "throughput_mbps": 1.0,
                        "retransmissions": 0,
                        "reported_duration_s": 1.0,
                    }
                    for edge in edges
                ]
                return len(rows), float(len(rows)), rows

            def stop_background_traffic(self, _traffic: object) -> dict[str, object]:
                return {"enabled": False}

        config = {
            "task": {"edge_ratio": 1.25, "required_throughput_mbps": 0.5},
            "traffic_control": {"background_traffic": {}},
            "verification": {
                "max_attempts": 2,
                "retry_backoff_ms": 200,
                "parallel": False,
                "ping": {
                    "count": 3,
                    "timeout_s": 1,
                    "interval_s": 0.2,
                    "max_packet_loss_percent": 0.0,
                    "max_average_rtt_ms": 300.0,
                },
                "iperf3": {
                    "duration_s": 1,
                    "omit_s": 0,
                    "client_timeout_s": 6,
                    "base_port": 5201,
                },
            },
        }

        row, events = exp1_netns.run_one(FakeTopology(), config, num_agents=4, seed=3)

        self.assertLessEqual(row.traffic_control_finished_at, row.task_received_at)
        self.assertLessEqual(row.traffic_control_started_at, row.task_received_at)
        prepared = [event for event in events if event["stage"] == "BACKGROUND_PREPARED"]
        self.assertEqual(len(prepared), 1)
        self.assertTrue(prepared[0]["details"]["readiness_ready"])
        self.assertGreater(
            prepared[0]["details"].get("source_up0_tx_bytes_after", 0),
            prepared[0]["details"].get("source_up0_tx_bytes_before", 0),
        )
        self.assertGreater(
            prepared[0]["details"].get("target_up0_rx_bytes_after", 0),
            prepared[0]["details"].get("target_up0_rx_bytes_before", 0),
        )
        task_event = next(event for event in events if event["stage"] == "TASK_RECEIVED")
        route_event = next(event for event in events if event["stage"] == "ROUTE_INSTALL_STARTED")
        self.assertLess(prepared[0]["timestamp"], task_event["timestamp"])
        self.assertGreater(route_event["timestamp"], task_event["timestamp"])

    def test_background_preparation_uses_gateway_uplinks_and_bilateral_byte_readiness(self) -> None:
        """Background load must traverse only gateway uplinks and reach the peer gateway."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology.num_agents = 2
        topology.num_gateways = 2
        topology.agent_gateways = [0, 1]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2"]
        topology.agents = [SimpleNamespace(pid=101), SimpleNamespace(pid=102)]
        topology.gateways = [SimpleNamespace(pid=201), SimpleNamespace(pid=202)]
        topology._servers = []
        captured_commands: list[tuple[int | None, tuple[str, ...]]] = []
        topology._parallel_commands = lambda commands, **_kwargs: captured_commands.extend(commands)
        topology._wait_for_listener = lambda *_args, **_kwargs: None
        source_tx_samples = iter((1_000, 1_000, 66_536))
        target_rx_samples = iter((2_000, 2_000, 67_536))
        topology._gateway_up0_tx_bytes = lambda _gateway: next(source_tx_samples)
        topology._gateway_up0_rx_bytes = lambda _gateway: next(target_rx_samples)

        class FakeProcess:
            def poll(self) -> None:
                return None

        with patch.object(exp1_netns.subprocess, "Popen", side_effect=(FakeProcess(), FakeProcess())):
            traffic = topology.start_background_traffic(
                {
                    "enabled_probability": 1.0,
                    "utilization_range": [0.1, 0.1],
                    "port": 5099,
                    "duration_s": 8,
                    "readiness_timeout_s": 0.1,
                    "minimum_payload_bytes": 65_536,
                },
                [
                    SimpleNamespace(endpoint="gateway-0:up0", bandwidth_mbps=40.0),
                    SimpleNamespace(endpoint="gateway-1:up0", bandwidth_mbps=40.0),
                    SimpleNamespace(endpoint="outer:og0", bandwidth_mbps=60.0),
                    SimpleNamespace(endpoint="outer:og1", bandwidth_mbps=60.0),
                    SimpleNamespace(endpoint="agent-0:eth0", bandwidth_mbps=1.0),
                ],
                seed=7,
            )

        self.assertTrue(getattr(traffic, "readiness_ready", False))
        self.assertGreater(
            getattr(traffic, "source_up0_tx_bytes_after", 0),
            getattr(traffic, "source_up0_tx_bytes_before", 0),
        )
        self.assertGreater(
            getattr(traffic, "target_up0_rx_bytes_after", 0),
            getattr(traffic, "target_up0_rx_bytes_before", 0),
        )
        self.assertEqual(getattr(traffic, "minimum_payload_bytes", 0), 65_536)
        self.assertGreaterEqual(getattr(traffic, "source_up0_tx_delta_bytes", 0), 65_536)
        self.assertGreaterEqual(getattr(traffic, "target_up0_rx_delta_bytes", 0), 65_536)
        self.assertEqual(getattr(traffic, "forward_path_endpoints", ()), (
            f"gateway-{traffic.source_gateway_index}:up0",
            f"outer:og{traffic.target_gateway_index}",
        ))
        self.assertAlmostEqual(getattr(traffic, "forward_path_bottleneck_mbps", 0.0), 40.0)
        self.assertAlmostEqual(traffic.target_mbps, 4.0)
        self.assertEqual(len(captured_commands), 4)
        self.assertTrue(
            all("default" not in command for _pid, command in captured_commands)
        )
        self.assertTrue(
            all("eth0" not in command and "ga" not in command for _pid, command in captured_commands)
        )
        self.assertTrue(
            all("10.200." in command[3] for _pid, command in captured_commands)
        )
        commands = [command for _pid, command in captured_commands]
        self.assertIn(
            ("ip", "route", "replace", "10.200.1.2/32", "via", "10.200.0.1", "dev", "up0"),
            commands,
        )
        self.assertIn(
            ("ip", "route", "replace", "10.200.0.2/32", "via", "10.200.1.1", "dev", "up0"),
            commands,
        )

    def test_formal_background_routes_are_disjoint_from_all_task_agent_endpoints(self) -> None:
        """Pre-task background routes must never reach an endpoint used by a business edge."""
        choose_pair = getattr(exp1_netns, "background_gateway_pair", None)
        route_commands = getattr(exp1_netns, "background_gateway_route_commands", None)
        self.assertTrue(callable(choose_pair))
        self.assertTrue(callable(route_commands))
        for num_agents in (4, 8, 12, 16, 20):
            for seed in range(50):
                source_gateway, target_gateway = choose_pair(num_agents, 4, seed)
                commands = route_commands(source_gateway, target_gateway)
                edges = generate_dag(num_agents, 1.25, seed, 0.5)
                business_agent_ips = {
                    f"10.{100 + (index % 4)}.{index}.2"
                    for edge in edges
                    for index in (edge.source_index, edge.target_index)
                }
                destinations = {command[3].removesuffix("/32") for command in commands}
                self.assertTrue(destinations.isdisjoint(business_agent_ips))
                self.assertTrue(all(destination.startswith("10.200.") for destination in destinations))
                self.assertTrue(
                    all("eth0" not in command and not any(item.startswith("ga") for item in command) for command in commands)
                )

    def test_background_readiness_rejects_source_only_uplink_activity(self) -> None:
        """Source TX alone can be ARP/control traffic and must not certify background load."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology._gateway_up0_tx_bytes = lambda _gateway: 150
        topology._gateway_up0_rx_bytes = lambda _gateway: 200

        class LiveProcess:
            def poll(self) -> None:
                return None

        perf_values = iter((0.0, 0.0, 1.0))
        with patch.object(exp1_netns.time, "perf_counter", side_effect=lambda: next(perf_values)):
            with self.assertRaisesRegex(TimeoutError, "minimum payload"):
                topology._wait_for_background_transfer(
                    0,
                    1,
                    LiveProcess(),
                    100,
                    200,
                    minimum_payload_bytes=65_536,
                    timeout_s=0.5,
                )

    def test_background_readiness_rejects_arp_sized_bilateral_deltas(self) -> None:
        """Bidirectional control frames below one payload block must not certify load."""
        topology = object.__new__(exp1_netns.ProcessNetnsTopology)
        topology._gateway_up0_tx_bytes = lambda _gateway: 1_064
        topology._gateway_up0_rx_bytes = lambda _gateway: 2_128

        class LiveProcess:
            def poll(self) -> None:
                return None

        perf_values = iter((0.0, 0.0, 1.0))
        with patch.object(exp1_netns.time, "perf_counter", side_effect=lambda: next(perf_values)):
            with self.assertRaisesRegex(TimeoutError, "minimum payload"):
                topology._wait_for_background_transfer(
                    0,
                    1,
                    LiveProcess(),
                    1_000,
                    2_000,
                    minimum_payload_bytes=65_536,
                    timeout_s=0.5,
                )

    def test_exp1_audit_checks_retries_and_counts_timeouts(self) -> None:
        """Ignoring a retried flow can hide a throughput measurement over its path bottleneck."""
        config = {
            "task": {"num_gateways": 4},
            "audit": {"path_bottleneck": {"relative_tolerance": 0.1, "short_tcp_min_tolerance_mbps": 0.25}},
        }
        profiles = [
            {"endpoint": endpoint, "bandwidth_mbps": 10.0}
            for endpoint in audit_expected_endpoints(4, 4)
        ]
        result = audit_exp1_netns(
            config,
            [
                {"run_id": "run", "num_agents": 4, "stage": "TRAFFIC_CONTROL_FINISHED", "details": {"profiles": profiles}},
                {"run_id": "run", "num_agents": 4, "stage": "IPERF3_COMMAND_RESULT", "details": {"attempt": 1, "timeout": True, "edge_id": "timeout", "source_index": 0, "target_index": 1, "throughput_mbps": 0.0}},
                {"run_id": "run", "num_agents": 4, "stage": "IPERF3_COMMAND_RESULT", "details": {"attempt": 2, "timeout": False, "edge_id": "retry", "source_index": 0, "target_index": 1, "throughput_mbps": 12.0}},
            ],
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["iperf_timeout_records"], 1)
        self.assertEqual(result["iperf_non_timeout_attempts_checked"], 1)

    def test_exp1_audit_rejects_empty_event_coverage(self) -> None:
        """A no-op audit must not certify a run with no traffic-control evidence."""
        result = audit_exp1_netns(
            {
                "task": {"num_gateways": 4},
                "audit": {"path_bottleneck": {"relative_tolerance": 0.1, "short_tcp_min_tolerance_mbps": 0.25}},
            },
            [],
        )
        self.assertFalse(result["passed"])

    def _formal_audit_fixture(self, directory: Path) -> tuple[Path, Path]:
        """Copy the accepted formal artifact set so each audit mutation is isolated."""
        config_path = directory / "formal-config.yaml"
        results_dir = directory / "formal-results"
        shutil.copy("configs/exp1_netns_verified_formation_v2.yaml", config_path)
        shutil.copytree("results/exp1_wcnc_final_v2", results_dir)
        return config_path, results_dir

    @staticmethod
    def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or ()), list(reader)

    @staticmethod
    def _write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _read_events(path: Path) -> list[dict[str, object]]:
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    @staticmethod
    def _write_events(path: Path, events: list[dict[str, object]]) -> None:
        path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )

    def test_exp1_formal_audit_rejects_missing_or_duplicate_grid_rows(self) -> None:
        """A formal audit must reject either side of a broken 5x50 Cartesian grid."""
        with self.subTest("missing"):
            with tempfile.TemporaryDirectory() as temporary_directory:
                config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
                runs_path = results_dir / "raw" / "runs.csv"
                fields, rows = self._read_csv_rows(runs_path)
                self._write_csv_rows(runs_path, fields, rows[1:])
                result = audit_exp1_module.audit_formal_results(config_path, results_dir)
                self.assertFalse(result["passed"])
                self.assertTrue(any("formal grid" in error for error in result["errors"]))
        with self.subTest("duplicate"):
            with tempfile.TemporaryDirectory() as temporary_directory:
                config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
                runs_path = results_dir / "raw" / "runs.csv"
                fields, rows = self._read_csv_rows(runs_path)
                rows[1]["num_agents"] = rows[0]["num_agents"]
                rows[1]["seed"] = rows[0]["seed"]
                self._write_csv_rows(runs_path, fields, rows)
                result = audit_exp1_module.audit_formal_results(config_path, results_dir)
                self.assertFalse(result["passed"])
                self.assertTrue(any("formal grid" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_latin_schedule_drift(self) -> None:
        """A complete grid in the wrong run order is still not the frozen formal schedule."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            runs_path = results_dir / "raw" / "runs.csv"
            fields, rows = self._read_csv_rows(runs_path)
            rows[0]["order_position"] = "4"
            self._write_csv_rows(runs_path, fields, rows)
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("formal schedule" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_configuration_hash_mismatch(self) -> None:
        """One scope hash that disagrees with the frozen config invalidates the artifact set."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            scope_path = results_dir / "raw" / "measurement_scope.json"
            scope = json.loads(scope_path.read_text(encoding="utf-8"))
            scope["configuration_sha256"] = "0" * 64
            scope_path.write_text(json.dumps(scope), encoding="utf-8")
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("configuration hash" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_t_form_drift(self) -> None:
        """The reported T_form must exactly equal its recorded timing boundary."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            runs_path = results_dir / "raw" / "runs.csv"
            fields, rows = self._read_csv_rows(runs_path)
            rows[0]["verified_formation_latency_s"] = "0.0"
            self._write_csv_rows(runs_path, fields, rows)
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("T_form" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_processed_summary_drift(self) -> None:
        """Processed summary values must be a byte-for-byte recomputation from raw rows."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            summary_path = results_dir / "processed" / "summary.csv"
            fields, rows = self._read_csv_rows(summary_path)
            rows[0]["success_rate"] = "0.123"
            self._write_csv_rows(summary_path, fields, rows)
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("processed summary" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_tampered_manifest(self) -> None:
        """A manifest created from an accepted artifact set must detect later hash tampering."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            manifest_path = Path(temporary_directory) / "formal-manifest.json"
            initial = audit_exp1_module.audit_formal_results(
                config_path, results_dir, manifest_path=manifest_path, create_manifest=True
            )
            self.assertTrue(initial["passed"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifact_sha256"]["raw_runs"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = audit_exp1_module.audit_formal_results(
                config_path, results_dir, manifest_path=manifest_path
            )
        self.assertFalse(result["passed"])
        self.assertTrue(any("manifest" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_per_run_tc_multiplicity_drift(self) -> None:
        """Moving one TC event between runs must fail even when the global count remains 250."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            events_path = results_dir / "raw" / "events.jsonl"
            events = self._read_events(events_path)
            tc_indices = [index for index, event in enumerate(events) if event["stage"] == "TRAFFIC_CONTROL_FINISHED"]
            first, second = tc_indices[:2]
            events[second] = copy.deepcopy(events[first])
            self._write_events(events_path, events)
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("TRAFFIC_CONTROL_FINISHED" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_duplicate_or_unknown_terminal_events(self) -> None:
        """Terminal events must be exactly one per known run and never belong to an unknown run."""
        with self.subTest("duplicate"):
            with tempfile.TemporaryDirectory() as temporary_directory:
                config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
                events_path = results_dir / "raw" / "events.jsonl"
                events = self._read_events(events_path)
                terminal = next(event for event in events if event["stage"] == "DATA_PLANE_VERIFIED")
                events.append(copy.deepcopy(terminal))
                self._write_events(events_path, events)
                result = audit_exp1_module.audit_formal_results(config_path, results_dir)
                self.assertFalse(result["passed"])
                self.assertTrue(any("terminal event" in error for error in result["errors"]))
        with self.subTest("unknown"):
            with tempfile.TemporaryDirectory() as temporary_directory:
                config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
                events_path = results_dir / "raw" / "events.jsonl"
                events = self._read_events(events_path)
                terminal = copy.deepcopy(next(event for event in events if event["stage"] == "DATA_PLANE_VERIFIED"))
                terminal["run_id"] = "agents=4:seed=unknown"
                events.append(terminal)
                self._write_events(events_path, events)
                result = audit_exp1_module.audit_formal_results(config_path, results_dir)
                self.assertFalse(result["passed"])
                self.assertTrue(any("unknown run_id" in error for error in result["errors"]))

    def test_exp1_formal_audit_rejects_csv_timing_drift_from_events(self) -> None:
        """Coherently shifting CSV timing boundaries cannot evade the event-timestamp cross-check."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            runs_path = results_dir / "raw" / "runs.csv"
            fields, rows = self._read_csv_rows(runs_path)
            rows[0]["task_received_at"] = str(float(rows[0]["task_received_at"]) + 10.0)
            rows[0]["data_plane_verified_at"] = str(float(rows[0]["data_plane_verified_at"]) + 10.0)
            self._write_csv_rows(runs_path, fields, rows)
            result = audit_exp1_module.audit_formal_results(config_path, results_dir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("TASK_RECEIVED timestamp" in error for error in result["errors"]))

    def test_exp1_formal_audit_does_not_create_or_overwrite_manifest_when_failed(self) -> None:
        """Failed audits must leave both absent and existing manifests untouched."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            events_path = results_dir / "raw" / "events.jsonl"
            events = self._read_events(events_path)
            events = [event for event in events if event["stage"] != "TASK_RECEIVED"]
            self._write_events(events_path, events)
            missing_manifest = Path(temporary_directory) / "missing.json"
            result = audit_exp1_module.audit_formal_results(
                config_path, results_dir, manifest_path=missing_manifest, create_manifest=True
            )
            self.assertFalse(result["passed"])
            self.assertFalse(missing_manifest.exists())
            existing_manifest = Path(temporary_directory) / "existing.json"
            existing_manifest.write_text("preserve-me", encoding="utf-8")
            result = audit_exp1_module.audit_formal_results(
                config_path, results_dir, manifest_path=existing_manifest, create_manifest=True
            )
            self.assertFalse(result["passed"])
            self.assertEqual(existing_manifest.read_text(encoding="utf-8"), "preserve-me")

    def test_exp1_formal_audit_refuses_to_overwrite_existing_manifest(self) -> None:
        """Create mode must fail closed rather than replace a valid-artifact manifest."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path, results_dir = self._formal_audit_fixture(Path(temporary_directory))
            manifest_path = Path(temporary_directory) / "existing.json"
            manifest_path.write_text("preserve-me", encoding="utf-8")
            result = audit_exp1_module.audit_formal_results(
                config_path, results_dir, manifest_path=manifest_path, create_manifest=True
            )
            self.assertFalse(result["passed"])
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), "preserve-me")

    def test_exp1_formal_audit_cli_rejects_invalid_manifest_combinations(self) -> None:
        """Manifest flags are valid only for a formal audit and create mode always names a new file."""
        for arguments in (
            ["audit_exp1_netns.py", "--manifest", "manifest.json"],
            ["audit_exp1_netns.py", "--create-manifest"],
            ["audit_exp1_netns.py", "--formal-results-dir", "results", "--create-manifest"],
        ):
            with self.subTest(arguments=arguments):
                with patch.object(sys, "argv", arguments):
                    with self.assertRaises(SystemExit):
                        audit_exp1_module.main()

    def test_formal_protocol_rejects_noncanonical_cli_overrides(self) -> None:
        """A 50-item but noncanonical formal seed/size override changes the claimed protocol."""
        validator = getattr(exp1_netns, "validate_formal_protocol", None)
        self.assertTrue(callable(validator))
        with Path("configs/exp1_netns_verified_formation_v2.yaml").open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        config["simulation"]["task_sizes"] = [4, 8, 12, 16, 24]
        with self.assertRaisesRegex(ValueError, "task sizes"):
            validator(config, tuple(range(50)))
        config["simulation"]["task_sizes"] = [4, 8, 12, 16, 20]
        config["traffic_control"]["background_traffic"]["minimum_payload_bytes"] = 1
        with self.assertRaisesRegex(ValueError, "minimum_payload_bytes"):
            validator(config, tuple(range(50)))
        config["traffic_control"]["background_traffic"]["minimum_payload_bytes"] = 65_536
        with self.assertRaisesRegex(ValueError, "seeds"):
            validator(config, tuple(range(1, 51)))
        config["simulation"]["schedule"]["block_size"] = 4
        with self.assertRaisesRegex(ValueError, "block_size"):
            validator(config, tuple(range(50)))
        config["simulation"]["schedule"]["block_size"] = 5
        config["simulation"]["schedule"]["blocks"] = 9
        with self.assertRaisesRegex(ValueError, "blocks"):
            validator(config, tuple(range(50)))

    def test_formal_protocol_freezes_all_binding_v2_values(self) -> None:
        """Any claimed-formal binding parameter drift must fail before namespace execution."""
        with Path("configs/exp1_netns_verified_formation_v2.yaml").open(encoding="utf-8") as handle:
            formal = yaml.safe_load(handle)
        exp1_netns.validate_formal_protocol(formal, tuple(range(50)))
        mutations = (
            (("task", "num_gateways"), 3, "task.num_gateways"),
            (("task", "edge_ratio"), 1.5, "task.edge_ratio"),
            (("task", "required_throughput_mbps"), 0.6, "task.required_throughput_mbps"),
            (("traffic_control", "netem", "base_delay_ms"), [6, 20], "traffic_control.netem.base_delay_ms"),
            (("traffic_control", "htb", "bandwidth_mbps"), [40, 101], "traffic_control.htb.bandwidth_mbps"),
            (("traffic_control", "background_traffic", "enabled_probability"), 0.31, "background_traffic.enabled_probability"),
            (("traffic_control", "background_traffic", "protocol"), "tcp", "background_traffic.protocol"),
            (("verification", "parallel"), False, "verification.parallel"),
            (("verification", "ping", "interval_s"), 0.3, "verification.ping.interval_s"),
            (("verification", "iperf3", "base_port"), 5202, "verification.iperf3.base_port"),
            (("audit", "path_bottleneck", "relative_tolerance"), 0.11, "audit.path_bottleneck.relative_tolerance"),
            (("audit", "path_bottleneck", "short_tcp_min_tolerance_mbps"), 0.3, "audit.path_bottleneck.short_tcp_min_tolerance_mbps"),
        )
        for path, replacement, field in mutations:
            altered = copy.deepcopy(formal)
            destination = altered
            for key in path[:-1]:
                destination = destination[key]
            destination[path[-1]] = replacement
            with self.assertRaisesRegex(ValueError, field):
                exp1_netns.validate_formal_protocol(altered, tuple(range(50)))

    def test_pilot_protocol_allows_pilot_seed_and_schedule_values(self) -> None:
        """Pilot provenance is not rejected merely because its seeds and schedule differ."""
        with Path("configs/exp1_netns_verified_formation_pilot_v2.yaml").open(encoding="utf-8") as handle:
            pilot = yaml.safe_load(handle)
        exp1_netns.validate_formal_protocol(pilot, tuple(range(9000, 9005)))

    def test_formal_cli_defaults_to_v2_and_protects_v1_outputs(self) -> None:
        """The v2 formal executable must never write into a v1 result location."""
        arguments = exp1_netns._parser().parse_args([])
        self.assertEqual(arguments.config, "configs/exp1_netns_verified_formation_v2.yaml")
        self.assertEqual(arguments.output_dir, "results/exp1_wcnc_final_v2")
        validator = getattr(exp1_netns, "validate_output_directory", None)
        self.assertTrue(callable(validator))
        with self.assertRaisesRegex(ValueError, "v1"):
            validator(Path("results/exp1_netns"))
        validator(Path("results/exp1_wcnc_final_v2"))

    def test_main_rejects_formal_override_before_entering_user_namespace(self) -> None:
        """An invalid formal CLI must not enter a privileged execution path before rejection."""
        rejected = False
        with (
            patch.object(exp1_netns, "_inside_user_namespace", return_value=False),
            patch.object(exp1_netns, "_reexec_in_user_namespace", return_value=0) as reexec,
            patch.object(
                exp1_netns.sys,
                "argv",
                [
                    "exp1",
                    "--task-sizes",
                    "4,8,12,16,24",
                ],
            ),
        ):
            try:
                exp1_netns.main()
            except ValueError as error:
                rejected = "task sizes" in str(error)
            except SystemExit:
                rejected = False
        self.assertTrue(rejected)
        self.assertFalse(reexec.called)


class ConflictStressScenarioTests(unittest.TestCase):
    def test_frozen_stress_grid_naturally_spans_all_ground_truth_classes(self) -> None:
        generator = CrossLayerStressGenerator()
        config = ConflictScenarioConfig(
            num_agents=10,
            edge_ratio=1.5,
            num_gateways=4,
            cross_gateway_edge_ratio=0.5,
            multi_hop=True,
        )
        by_intensity: dict[float, list[str]] = {}
        for intensity in (0.6, 0.8, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5):
            by_intensity[intensity] = [
                generator.generate(intensity, seed, config).conflict_class
                for seed in range(10)
            ]
        observed = {value for values in by_intensity.values() for value in values}
        self.assertEqual(
            observed,
            {NO_CONFLICT, RESOLVABLE_CONFLICT, UNRESOLVABLE_CONFLICT},
        )
        self.assertTrue(all(value == NO_CONFLICT for value in by_intensity[0.6]))
        self.assertTrue(
            all(value == UNRESOLVABLE_CONFLICT for value in by_intensity[1.5])
        )

    def test_stress_snapshot_is_repeatable_and_method_independent(self) -> None:
        generator = CrossLayerStressGenerator()
        first = generator.generate(1.2, 11)
        second = generator.generate(1.2, 11)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.proposals, second.proposals)
        self.assertEqual(first.ground_truth, second.ground_truth)

    def test_no_global_verification_is_not_layer_wise_selection(self) -> None:
        generator = CrossLayerStressGenerator()
        coordinator = CrossLayerCoordinator()
        differences = 0
        for intensity in (0.8, 1.0, 1.2, 1.3, 1.4):
            for seed in range(10):
                snapshot = generator.generate(intensity, seed)
                independent = coordinator.coordinate(
                    "independent", snapshot.observed_state, snapshot.proposals
                )
                no_verification = coordinator.coordinate(
                    "no_verification", snapshot.observed_state, snapshot.proposals
                )
                differences += (
                    independent.selected_proposals
                    != no_verification.selected_proposals
                )
                self.assertFalse(no_verification.global_check_performed)
                self.assertEqual(
                    no_verification.candidate_combinations,
                    snapshot.ground_truth.candidate_combinations,
                )
        self.assertGreater(differences, 0)


class DemandCapacityRatioScenarioTests(unittest.TestCase):
    @staticmethod
    def _proposal(snapshot, edge_id: str, layer: str, *, keep: bool = True):
        return next(
            proposal
            for proposal in snapshot.proposals
            if proposal.layer == layer
            and edge_id in proposal.affected_edges
            and proposal.is_keep == keep
        )

    def test_v2_uses_three_binary_four_layer_flow_decisions(self) -> None:
        """A one-flow or three-candidate generator would not enumerate 4096 choices."""
        snapshot = DemandCapacityRatioGenerator().generate(1.05, 23)
        state = snapshot.true_state

        self.assertEqual(len(state.constraints), 3)
        self.assertEqual(len(snapshot.proposals), 24)
        self.assertEqual(snapshot.ground_truth.candidate_combinations, 4096)
        self.assertEqual(snapshot.metadata["proposals_per_layer"], 2)
        self.assertEqual(
            set(snapshot.metadata["flow_bottlenecks"].values()),
            {"transport", "network", "physical"},
        )
        self.assertEqual(len(snapshot.base.base.task.app_agents), 10)
        for edge_id, capacities in snapshot.metadata["flow_capacities"].items():
            by_layer = {
                "transport": capacities["transport_admissible_mbps"],
                "network": capacities["network_available_mbps"],
                "physical": capacities["physical_available_mbps"],
            }
            self.assertEqual(
                snapshot.metadata["flow_bottlenecks"][edge_id],
                min(by_layer, key=by_layer.get),
            )
            self.assertAlmostEqual(
                state.transport[edge_id].send_rate_mbps,
                snapshot.metadata["flow_r_req_mbps"][edge_id],
            )
        self.assertEqual(
            {constraint.shared_resource_id for constraint in state.constraints.values()},
            {"gamma-shared-resource"},
        )
        for edge_id, gateways in state.metadata["edge_gateways"].items():
            self.assertNotEqual(gateways[0], gateways[-1], edge_id)
        proposal_counts: dict[tuple[str, str], int] = {}
        for proposal in snapshot.proposals:
            edge_id = next(iter(proposal.affected_edges))
            key = (edge_id, proposal.layer)
            proposal_counts[key] = proposal_counts.get(key, 0) + 1
        self.assertEqual(set(proposal_counts.values()), {2})
        self.assertFalse(
            any(
                item.startswith("shared:")
                for proposal in snapshot.proposals
                for item in proposal.write_set
            )
        )
        self.assertTrue(
            all(
                "transport_rate_mbps" in proposal.parameters
                and "transport_admissible_capacity_mbps" in proposal.parameters
                for proposal in snapshot.proposals
                if proposal.layer == "transport"
            )
        )
        self.assertEqual(
            {
                proposal.action
                for proposal in snapshot.proposals
                if proposal.layer == "transport" and not proposal.is_keep
            },
            {"RESERVE_TRANSPORT_SERVICE"},
        )
        topology = snapshot.base.base
        gateway_by_agent = {
            spec.agent_id: spec.gateway_id for spec in topology.catalog.agents
        }
        cross_gateway = [
            edge
            for edge in topology.task.biz_edges
            if gateway_by_agent[edge.source] != gateway_by_agent[edge.target]
        ]
        expected = tuple(
            edge.edge_id
            for edge in sorted(
                cross_gateway,
                key=lambda edge: (-edge.priority, edge.edge_id),
            )[:3]
        )
        self.assertEqual(tuple(sorted(state.constraints)), tuple(sorted(expected)))
        self.assertEqual(
            snapshot.metadata["critical_flow_selection_criterion"],
            "highest_task_priority_then_canonical_edge_id",
        )
        self.assertEqual(
            snapshot.metadata["flow_priorities"],
            {
                edge.edge_id: edge.priority
                for edge in cross_gateway
                if edge.edge_id in expected
            },
        )

    def test_alc_pair_feasibility_is_edge_local(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 7)
        edge_id, unrelated_id = sorted(snapshot.true_state.constraints)[:2]
        unrelated_app = snapshot.true_state.application[unrelated_id]
        state = replace(
            snapshot.true_state,
            application={
                **snapshot.true_state.application,
                unrelated_id: replace(unrelated_app, required_rate_mbps=1_000.0),
            },
        )
        pair = (
            self._proposal(snapshot, edge_id, "application"),
            self._proposal(snapshot, edge_id, "transport"),
        )

        result = coordinator_module._adjacent_pair_result(
            state, pair, "application", "transport"
        )

        self.assertTrue(result.feasible, result.violations)
        self.assertFalse(
            any(item.endswith(f":{unrelated_id}") for item in result.violations)
        )

    def test_alc_pairs_recognize_each_independent_layer_bottleneck(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 11)
        edge_id = sorted(snapshot.true_state.constraints)[0]
        application = replace(
            snapshot.true_state.application[edge_id], required_rate_mbps=10.0
        )
        transport = replace(
            snapshot.true_state.transport[edge_id],
            send_rate_mbps=9.0,
            admissible_capacity_mbps=8.0,
        )
        network = replace(
            snapshot.true_state.network[edge_id], available_bandwidth_mbps=8.0
        )
        physical = replace(
            snapshot.true_state.physical[edge_id], available_capacity_mbps=8.0
        )
        state = replace(
            snapshot.true_state,
            application={**snapshot.true_state.application, edge_id: application},
            transport={**snapshot.true_state.transport, edge_id: transport},
            network={**snapshot.true_state.network, edge_id: network},
            physical={**snapshot.true_state.physical, edge_id: physical},
        )
        application_proposal = replace(
            self._proposal(snapshot, edge_id, "application"),
            parameters={"application_rate_mbps": 10.0, "observed_version": 2},
        )
        transport_proposal = replace(
            self._proposal(snapshot, edge_id, "transport"),
            parameters={
                "transport_rate_mbps": 9.0,
                "transport_admissible_capacity_mbps": 8.0,
                "observed_version": 2,
            },
        )
        network_proposal = replace(
            self._proposal(snapshot, edge_id, "network"),
            parameters={"network_bandwidth_mbps": 8.0, "observed_version": 2},
        )
        physical_proposal = replace(
            self._proposal(snapshot, edge_id, "physical"),
            parameters={"physical_capacity_mbps": 8.0, "observed_version": 2},
        )

        app_transport = coordinator_module._adjacent_pair_result(
            state,
            (application_proposal, transport_proposal),
            "application",
            "transport",
        )
        transport_network = coordinator_module._adjacent_pair_result(
            state,
            (transport_proposal, network_proposal),
            "transport",
            "network",
        )
        network_physical = coordinator_module._adjacent_pair_result(
            state,
            (network_proposal, physical_proposal),
            "network",
            "physical",
        )

        self.assertEqual(
            set(app_transport.violations),
            {
                f"application_transport_rate:{edge_id}",
                f"application_transport_capacity:{edge_id}",
                f"transport_admissible_capacity:{edge_id}",
            },
        )
        self.assertIn(
            f"application_network_capacity:{edge_id}",
            transport_network.violations,
        )
        self.assertIn(
            f"transport_network_rate:{edge_id}", transport_network.violations
        )
        self.assertIn(
            f"application_physical_capacity:{edge_id}",
            network_physical.violations,
        )
        self.assertIn(
            f"transport_physical_capacity:{edge_id}",
            network_physical.violations,
        )
        self.assertNotIn(
            f"network_physical_capacity:{edge_id}",
            network_physical.violations,
        )

        legacy_state = replace(
            snapshot.true_state,
            metadata={
                key: value
                for key, value in snapshot.true_state.metadata.items()
                if key != "independent_layer_capacities"
            },
            application={
                **snapshot.true_state.application,
                edge_id: replace(application, required_rate_mbps=5.0),
            },
            transport={
                **snapshot.true_state.transport,
                edge_id: replace(
                    transport,
                    send_rate_mbps=5.0,
                    admissible_capacity_mbps=10.0,
                ),
            },
            network={
                **snapshot.true_state.network,
                edge_id: replace(network, available_bandwidth_mbps=20.0),
            },
            physical={
                **snapshot.true_state.physical,
                edge_id: replace(physical, available_capacity_mbps=10.0),
            },
            constraints={
                **snapshot.true_state.constraints,
                edge_id: replace(
                    snapshot.true_state.constraints[edge_id],
                    desired_rate_mbps=5.0,
                ),
            },
        )
        for label, legacy_metadata in (
            ("absent", legacy_state.metadata),
            ("false", {**legacy_state.metadata, "independent_layer_capacities": False}),
        ):
            with self.subTest(independent_layer_capacities=label):
                legacy_result = evaluate_cross_layer_combination(
                    replace(legacy_state, metadata=legacy_metadata)
                )
                self.assertIn(
                    f"network_physical_capacity:{edge_id}",
                    legacy_result.violations,
                )

        legacy_network_physical = coordinator_module._adjacent_pair_result(
            legacy_state,
            (
                replace(
                    self._proposal(snapshot, edge_id, "network"),
                    parameters={
                        "network_bandwidth_mbps": 20.0,
                        "observed_version": 2,
                    },
                ),
                replace(
                    self._proposal(snapshot, edge_id, "physical"),
                    parameters={
                        "physical_capacity_mbps": 10.0,
                        "observed_version": 2,
                    },
                ),
            ),
            "network",
            "physical",
        )
        self.assertIn(
            f"network_physical_capacity:{edge_id}",
            legacy_network_physical.violations,
        )

    def test_no_verification_soft_score_responds_to_ct_and_shared_capacity(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 13)
        keep = tuple(proposal for proposal in snapshot.proposals if proposal.is_keep)
        edge_id = sorted(snapshot.true_state.constraints)[0]
        low_ct_keep = tuple(
            replace(
                proposal,
                parameters={
                    **proposal.parameters,
                    "transport_admissible_capacity_mbps": 0.1,
                },
            )
            if proposal.layer == "transport" and edge_id in proposal.affected_edges
            else proposal
            for proposal in keep
        )
        low_shared = replace(
            snapshot.true_state,
            shared_resource_capacity_mbps={"gamma-shared-resource": 0.1},
        )

        baseline = coordinator_module._declared_joint_objective(snapshot.true_state, keep)
        ct_penalty = coordinator_module._declared_joint_objective(
            snapshot.true_state, low_ct_keep
        )
        shared_penalty = coordinator_module._declared_joint_objective(low_shared, keep)
        decision = CrossLayerCoordinator().coordinate(
            "no_verification", low_shared, snapshot.proposals
        )

        self.assertGreater(ct_penalty[0], baseline[0])
        self.assertGreater(shared_penalty[0], baseline[0])
        self.assertFalse(decision.global_check_performed)
        self.assertEqual(decision.candidate_combinations, 4096)
        self.assertTrue(decision.selected_proposals)

    def test_gamma_is_the_only_controlled_variable_for_a_paired_seed(self) -> None:
        generator = DemandCapacityRatioGenerator()
        low = generator.generate(0.8, 17)
        high = generator.generate(1.3, 17)
        self.assertEqual(
            low.metadata["environment_fingerprint"],
            high.metadata["environment_fingerprint"],
        )
        self.assertEqual(low.base.fingerprint, high.base.fingerprint)
        self.assertEqual(
            low.metadata["random_environment"], high.metadata["random_environment"]
        )
        self.assertEqual(low.metadata["flow_capacities"], high.metadata["flow_capacities"])
        self.assertEqual(low.metadata["flow_bottlenecks"], high.metadata["flow_bottlenecks"])
        self.assertEqual(low.metadata["candidate_resources"], high.metadata["candidate_resources"])
        self.assertEqual(
            tuple((proposal.proposal_id, proposal.action, proposal.write_set) for proposal in low.proposals),
            tuple((proposal.proposal_id, proposal.action, proposal.write_set) for proposal in high.proposals),
        )
        for snapshot, gamma in ((low, 0.8), (high, 1.3)):
            for edge_id, capacities in snapshot.metadata["flow_capacities"].items():
                c_eff = min(
                    capacities["transport_admissible_mbps"],
                    capacities["network_available_mbps"],
                    capacities["physical_available_mbps"],
                )
                self.assertAlmostEqual(capacities["effective_mbps"], c_eff)
                self.assertAlmostEqual(
                    snapshot.metadata["flow_r_req_mbps"][edge_id] / c_eff,
                    gamma,
                )

    def test_transport_admissible_capacity_is_not_the_current_send_rate(self) -> None:
        """Configured service and admissible transport capacity are separate limits."""
        snapshot = DemandCapacityRatioGenerator().generate(0.8, 31)
        edge_id = sorted(snapshot.true_state.constraints)[0]
        application = snapshot.true_state.application[edge_id]
        transport = snapshot.true_state.transport[edge_id]
        capacity_limited = replace(
            snapshot.true_state,
            transport={
                **snapshot.true_state.transport,
                edge_id: replace(
                    transport,
                    send_rate_mbps=application.required_rate_mbps * 2.0,
                    admissible_capacity_mbps=application.required_rate_mbps * 0.5,
                ),
            },
        )
        send_limited = replace(
            snapshot.true_state,
            transport={
                **snapshot.true_state.transport,
                edge_id: replace(
                    transport,
                    send_rate_mbps=application.required_rate_mbps * 0.5,
                    admissible_capacity_mbps=application.required_rate_mbps * 2.0,
                ),
            },
        )

        capacity_result = evaluate_cross_layer_combination(capacity_limited)
        send_result = evaluate_cross_layer_combination(send_limited)

        self.assertFalse(capacity_result.feasible)
        self.assertIn(
            f"application_transport_capacity:{edge_id}",
            capacity_result.violations,
        )
        self.assertFalse(send_result.feasible)
        self.assertIn(f"application_transport_rate:{edge_id}", send_result.violations)

    def test_unresolvable_gamma_is_safely_rejected_or_rolled_back_by_all_methods(self) -> None:
        async def exercise() -> None:
            snapshot = DemandCapacityRatioGenerator().generate(1.3, 20)
            self.assertEqual(snapshot.conflict_class, UNRESOLVABLE_CONFLICT)
            for index, method in enumerate(exp2_demand.METHOD_TO_ENGINE.values(), 1):
                metric, _events, _decision = await run_exp2_case(
                    snapshot,
                    method,
                    run_index=index,
                    coordination_timeout_ms=10_000,
                )
                self.assertTrue(metric.safe_rejection, method)
                self.assertFalse(metric.unsafe_execution, method)
                self.assertTrue(metric.no_partial_commit, method)
                self.assertEqual(metric.stable_version_before, metric.stable_version_after)
                self.assertTrue(
                    metric.decision_rejected
                    or (metric.rollback_triggered and metric.rollback_success),
                    method,
                )

        asyncio.run(exercise())

    def test_main_gamma_grid_naturally_spans_ground_truth_classes(self) -> None:
        generator = DemandCapacityRatioGenerator()
        observed = {
            generator.generate(gamma, seed).conflict_class
            for gamma in (0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3)
            for seed in (0, 20)
        }
        self.assertEqual(
            observed,
            {NO_CONFLICT, RESOLVABLE_CONFLICT, UNRESOLVABLE_CONFLICT},
        )

    def test_v5_formal_protocol_refuses_unfrozen_config(self) -> None:
        with Path("configs/exp2_demand_capacity_ratio_v5.yaml").open(
            encoding="utf-8"
        ) as handle:
            config = yaml.safe_load(handle)
        config["experiment"]["frozen"] = False
        with self.assertRaisesRegex(ValueError, "formal config is not frozen"):
            exp2_demand.validate_protocol(
                config,
                tuple(exp2_demand.METHOD_TO_ENGINE),
                exp2_demand.FORMAL_SEEDS,
                Path(config["experiment"]["output_dir"]),
            )

    def test_pilot_protocol_and_cli_defaults_use_registered_pilot_values(self) -> None:
        config_path = Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml")
        with config_path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        methods = tuple(config["methods"])
        seeds = tuple(range(9000, 9010))
        output = Path(config["experiment"]["output_dir"])

        exp2_demand.validate_protocol(config, methods, seeds, output)
        args, loaded = exp2_demand.parse_runtime_args(
            ["--config", str(config_path)]
        )

        self.assertEqual(loaded, config)
        self.assertEqual(args.seeds, "9000:9009")
        self.assertEqual(args.methods, ",".join(methods))
        self.assertEqual(args.output_dir, str(output))
        with self.assertRaisesRegex(ValueError, "pilot seeds"):
            exp2_demand.validate_protocol(config, methods, tuple(range(10)), output)
        with self.assertRaisesRegex(ValueError, "pilot output"):
            exp2_demand.validate_protocol(
                config, methods, seeds, Path("results/exp2_demand_capacity_v5")
            )

    def test_v2_rejects_v1_output_descendants_before_generation(self) -> None:
        with Path("configs/exp2_demand_capacity_ratio_v5.yaml").open(
            encoding="utf-8"
        ) as handle:
            config = yaml.safe_load(handle)

        with self.assertRaisesRegex(ValueError, "prior results"):
            asyncio.run(
                exp2_demand.run_experiment(
                    config,
                    methods=tuple(exp2_demand.METHOD_TO_ENGINE),
                    seeds=tuple(range(200)),
                    output_dir=Path("results/exp2_demand_capacity/raw/v2"),
                )
            )

    def test_pilot_ceiling_uses_only_ground_truth_class_coverage(self) -> None:
        classes_by_gamma = {
            0.8: [NO_CONFLICT] * 10,
            0.9: [NO_CONFLICT] * 10,
            1.0: [NO_CONFLICT] * 10,
            1.05: [RESOLVABLE_CONFLICT] * 10,
            1.1: [RESOLVABLE_CONFLICT] * 10,
            1.2: [RESOLVABLE_CONFLICT] * 8 + [UNRESOLVABLE_CONFLICT] * 2,
            1.3: [UNRESOLVABLE_CONFLICT] * 10,
        }
        self.assertEqual(
            exp2_demand.select_pilot_ratios(classes_by_gamma),
            (0.8, 0.9, 1.0, 1.05, 1.1, 1.2),
        )
        evidence = [
            {
                "gamma": gamma,
                "seed": seed,
                "conflict_class": (
                    NO_CONFLICT
                    if gamma < 1.05
                    else RESOLVABLE_CONFLICT
                    if gamma < 1.2
                    else RESOLVABLE_CONFLICT
                    if gamma == 1.2 and seed < 9008
                    else UNRESOLVABLE_CONFLICT
                ),
                "candidate_combinations": 4096,
            }
            for gamma in (0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3, 1.4)
            for seed in range(9000, 9010)
        ]
        manifest = {"phase": "pilot", "seeds": list(range(9000, 9010))}
        selection = exp2_demand.build_pilot_selection(
            evidence,
            retained_ratios=(0.8, 0.9, 1.0, 1.05, 1.1, 1.2),
            generator_version="wcnc-final-gamma-v5",
            execution_manifest=manifest,
            execution_manifest_sha256=exp2_demand.sha256_json(manifest),
        )
        self.assertEqual(
            {item["seed"] for item in selection["candidate_evidence"]},
            set(range(9000, 9010)),
        )
        self.assertEqual(selection["highest_retained_gamma"], 1.2)
        self.assertEqual(len(selection["candidate_evidence"]), 80)
        self.assertEqual(sum(item["samples"] for item in selection["class_counts"]), 80)

    def test_execution_provenance_is_persisted_before_method_inputs_are_returned(self) -> None:
        with Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").open(
            encoding="utf-8"
        ) as handle:
            config = yaml.safe_load(handle)
        evidence = [
            {
                "gamma": gamma,
                "seed": seed,
                "conflict_class": (
                    NO_CONFLICT
                    if gamma < 1.05
                    else RESOLVABLE_CONFLICT
                    if gamma < 1.2
                    else UNRESOLVABLE_CONFLICT
                ),
                "candidate_combinations": 4096,
            }
            for gamma in exp2_demand.PILOT_GAMMA_GRID
            for seed in exp2_demand.PILOT_SEEDS
        ]
        retained = (0.8, 0.9, 1.0, 1.05, 1.1, 1.2)
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_dir = Path(temporary_directory)
            manifest, manifest_sha = exp2_demand.persist_execution_provenance(
                raw_dir,
                config=config,
                methods=(),
                seeds=exp2_demand.PILOT_SEEDS,
                ratios=retained,
                output_dir=exp2_demand.PILOT_OUTPUT,
                pilot_evidence=evidence,
            )
            persisted_manifest = json.loads(
                (raw_dir / "execution_manifest.json").read_text(encoding="utf-8")
            )
            selection = json.loads(
                (raw_dir / "pilot_selection.json").read_text(encoding="utf-8")
            )

        self.assertEqual(persisted_manifest, manifest)
        self.assertEqual(manifest_sha, exp2_demand.sha256_json(manifest))
        self.assertEqual(selection["execution_manifest_sha256"], manifest_sha)
        self.assertEqual(selection["retained_ratios"], list(retained))
        self.assertEqual(len(selection["candidate_evidence"]), 80)

    def test_scenario_serialization_keeps_oracle_audit_without_4096_details(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.05, 5)

        row = exp2_demand.compact_scenario_row(snapshot)

        self.assertEqual(row["ground_truth"]["candidate_combinations"], 4096)
        self.assertEqual(row["ground_truth"]["conflict_class"], snapshot.conflict_class)
        self.assertIn("best_feasible_combination", row["ground_truth"])
        self.assertNotIn("evaluated_combinations", row["ground_truth"])
        self.assertIn("oracle_input", row)
        self.assertEqual(
            row["oracle_input_sha256"],
            exp2_demand.sha256_json(row["oracle_input"]),
        )
        self.assertEqual(
            row["oracle_evaluation_sha256"],
            exp2_demand.oracle_evaluation_sha256(snapshot),
        )
        self.assertLess(len(json.dumps(row)), 200_000)

    def test_v2_aggregation_audits_all_per_flow_gamma_definitions(self) -> None:
        capacities = {
            "f0": {
                "transport_admissible_mbps": 10.0,
                "network_available_mbps": 12.0,
                "physical_available_mbps": 13.0,
                "effective_mbps": 10.0,
            },
            "f1": {
                "transport_admissible_mbps": 14.0,
                "network_available_mbps": 10.0,
                "physical_available_mbps": 13.0,
                "effective_mbps": 10.0,
            },
            "f2": {
                "transport_admissible_mbps": 14.0,
                "network_available_mbps": 13.0,
                "physical_available_mbps": 10.0,
                "effective_mbps": 10.0,
            },
        }
        configuration = {
            "experiment": {
                "phase": "formal",
                "generator_version": "wcnc-final-gamma-v2",
                "output_dir": "fixture-output",
            },
            "methods": list(exp2_demand.METHOD_TO_ENGINE),
        }
        manifest = {
            "phase": "formal",
            "generator_version": "wcnc-final-gamma-v2",
            "configuration_sha256": exp2_demand.sha256_json(configuration),
            "methods": list(exp2_demand.METHOD_TO_ENGINE),
            "seeds": [0],
            "ratios": [1.05],
            "output_dir": "fixture-output",
        }
        common = {
            "demand_to_capacity_ratio": 1.05,
            "seed": 0,
            "scenario_fingerprint": exp2_demand.sha256_json("scenario"),
            "environment_fingerprint": exp2_demand.sha256_json("environment"),
            "configuration_sha256": exp2_demand.sha256_json(configuration),
            "execution_manifest_sha256": exp2_demand.sha256_json(manifest),
            "oracle_input_sha256": exp2_demand.sha256_json(
                {"true_state": {"task_id": "task"}, "proposals": []}
            ),
            "oracle_evaluation_sha256": exp2_demand.sha256_json("evaluation"),
            "flow_capacities_json": json.dumps(capacities, sort_keys=True),
            "flow_r_req_mbps_json": json.dumps(
                {"f0": 10.5, "f1": 10.5, "f2": 10.5}, sort_keys=True
            ),
            "flow_bottlenecks_json": json.dumps(
                {"f0": "transport", "f1": "network", "f2": "physical"},
                sort_keys=True,
            ),
            "ground_truth_candidate_combinations": 4096,
            "ground_truth_uses_true_state": True,
            "qos_satisfied": True,
            "conflict_class": RESOLVABLE_CONFLICT,
        }
        rows = [
            {**common, "run_id": f"run:{method}", "method": method}
            for method in exp2_demand.METHOD_TO_ENGINE
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_path = root / "runs.csv"
            scenarios_path = root / "scenarios.jsonl"
            scenario = {
                "pressure": 1.05,
                "seed": 0,
                "scenario_fingerprint": common["scenario_fingerprint"],
                "configuration_sha256": common["configuration_sha256"],
                "execution_manifest_sha256": common[
                    "execution_manifest_sha256"
                ],
                "oracle_input": {
                    "true_state": {"task_id": "task"},
                    "proposals": [],
                },
                "oracle_input_sha256": common["oracle_input_sha256"],
                "oracle_evaluation_sha256": common[
                    "oracle_evaluation_sha256"
                ],
            }

            def write_rows(values) -> None:
                with input_path.open("w", encoding="utf-8", newline="") as handle:
                    if not values:
                        return
                    writer = csv.DictWriter(handle, fieldnames=list(values[0]))
                    writer.writeheader()
                    writer.writerows(values)

            scenarios_path.write_text(json.dumps(scenario) + "\n", encoding="utf-8")
            (root / "configuration.json").write_text(
                json.dumps(configuration), encoding="utf-8"
            )
            (root / "execution_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            write_rows(rows)
            report = aggregate_exp2_demand(
                input_path,
                root / "processed",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertTrue(report["pass"], report)
            invalid_oracle = [dict(item) for item in rows]
            invalid_oracle[0]["ground_truth_candidate_combinations"] = 81
            write_rows(invalid_oracle)
            report = aggregate_exp2_demand(
                input_path,
                root / "bad-oracle",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertFalse(report["pass"])
            self.assertTrue(report["oracle_count_errors"])

            write_rows(rows[:-1])
            missing = aggregate_exp2_demand(
                input_path,
                root / "missing",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertFalse(missing["pass"])
            self.assertTrue(missing["missing_cartesian_rows"])

            write_rows([*rows, rows[0]])
            duplicate = aggregate_exp2_demand(
                input_path,
                root / "duplicate",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertFalse(duplicate["pass"])
            self.assertTrue(duplicate["duplicate_cartesian_rows"])

            drifted = [dict(item) for item in rows]
            drifted[0]["scenario_fingerprint"] = exp2_demand.sha256_json(
                "different"
            )
            write_rows(drifted)
            disagreement = aggregate_exp2_demand(
                input_path,
                root / "disagreement",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertFalse(disagreement["pass"])
            self.assertTrue(disagreement["pairing_errors"])

            input_path.write_text("", encoding="utf-8")
            empty = aggregate_exp2_demand(
                input_path,
                root / "empty",
                1,
                expected_ratios=(1.05,),
                expected_seed_values=(0,),
            )
            self.assertFalse(empty["pass"])
            self.assertTrue(empty["empty_input"])


class CspfAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.links = (
            TrafficEngineeringLink("s-a", "s", "a", 10.0, 2.0, 5.0),
            TrafficEngineeringLink("a-d", "a", "d", 10.0, 2.0, 5.0),
            TrafficEngineeringLink("s-b", "s", "b", 10.0, 4.0, 1.0),
            TrafficEngineeringLink("b-d", "b", "d", 10.0, 4.0, 1.0),
            TrafficEngineeringLink("s-x", "s", "x", 1.0, 0.1, 0.1),
            TrafficEngineeringLink("x-d", "x", "d", 1.0, 0.1, 0.1),
        )

    def test_cspf_prunes_insufficient_bandwidth_and_uses_minimum_delay(self) -> None:
        result = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 20.0, metric="delay"),
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.path, ("s", "a", "d"))
        self.assertIn("s-x", result.pruned_link_ids)
        self.assertIn("x-d", result.pruned_link_ids)

    def test_cspf_uses_fixed_te_metric_but_enforces_delay_bound(self) -> None:
        result = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 9.0, metric="te_cost"),
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.path, ("s", "b", "d"))
        rejected = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 3.0, metric="delay"),
        )
        self.assertFalse(rejected.feasible)
        self.assertEqual(rejected.failure_reason, "no_path_within_delay_bound")

    def test_cspf_checks_delay_after_selecting_the_minimum_cost_path(self) -> None:
        """Catches silently substituting a costlier path when SPF exceeds D_max."""
        links = (
            TrafficEngineeringLink("s-a", "s", "a", 10.0, 6.0, 1.0),
            TrafficEngineeringLink("a-d", "a", "d", 10.0, 6.0, 1.0),
            TrafficEngineeringLink("s-b", "s", "b", 10.0, 4.0, 5.0),
            TrafficEngineeringLink("b-d", "b", "d", 10.0, 4.0, 5.0),
        )

        result = CspfSolver().solve(
            links,
            CspfRequest("flow", "s", "d", 5.0, 10.0, metric="te_cost"),
        )

        self.assertFalse(result.feasible)
        self.assertEqual(result.failure_reason, "no_path_within_delay_bound")

    def test_cspf_planning_does_not_consult_cross_layer_physical_truth(self) -> None:
        """Catches CSPF gaining forbidden physical-layer knowledge while selecting."""
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="PHYSICAL_CAPACITY_DROP",
                    fault_level="post_capacity_to_requirement_0.30",
                    severity=0.30,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            controller, verifier, _provider = snapshot.instantiate()
            stable, formation = await controller.build_task_subnet(
                snapshot.task,
                verifier=verifier,
                run_id=1,
                seed=snapshot.seed,
            )
            self.assertTrue(formation.networking_success)
            event, context = snapshot.apply_fault(controller, stable)

            planning = await CspfNetworkOnlyStrategy().plan(
                controller,
                stable,
                event,
                context,
            )

            self.assertIsNotNone(planning.plan)
            self.assertFalse(planning.safe_rejection)
            self.assertEqual(
                {item.layer for item in planning.selected_proposals},
                {"network"},
            )

        asyncio.run(execute())

    def test_cspf_failure_recovery_authorizes_network_actions_only(self) -> None:
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="LINK_FAILURE",
                    fault_level="capacity_factor_0.0",
                    severity=0.0,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            row, _events, proposals, _probes = await run_one(
                snapshot,
                "cspf",
                run_sequence=1,
            )
            self.assertTrue(row.recovery_success)
            self.assertEqual(row.selected_layers, "network")
            self.assertEqual(row.changed_physical_bindings, 0)
            self.assertTrue(proposals)
            self.assertTrue(
                all(item["proposal"]["layer"] == "network" for item in proposals)
            )
            expected_inputs = {
                "topology",
                "link_up",
                "available_bandwidth",
                "link_delay",
                "te_cost",
                "flow_source",
                "flow_destination",
                "required_bandwidth",
                "maximum_delay",
            }
            self.assertTrue(
                all(
                    set(item["proposal"]["parameters"]["allowed_inputs"])
                    == expected_inputs
                    for item in proposals
                )
            )

        asyncio.run(execute())


class Exp4FormalProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        with Path("configs/exp4_cspf_failure_reconfiguration.yaml").open(
            encoding="utf-8"
        ) as handle:
            self.config = yaml.safe_load(handle)
        self.methods = ("proposed", "full_rebuild", "cspf")
        self.seeds = tuple(range(30))
        self.output = Path("results/exp4_cspf_final")

    def test_formal_protocol_requires_frozen_registered_invocation(self) -> None:
        self.assertTrue(
            hasattr(exp4_failure, "validate_protocol"),
            "formal Exp4 runs need a protocol validator",
        )
        exp4_failure.validate_protocol(
            self.config,
            self.methods,
            self.seeds,
            self.output,
        )
        mutations = (
            (
                "unfrozen",
                {
                    **self.config,
                    "experiment": {
                        **self.config["experiment"],
                        "frozen": False,
                    },
                },
                self.methods,
                self.seeds,
                self.output,
            ),
            ("methods", self.config, ("proposed", "cspf"), self.seeds, self.output),
            ("seeds", self.config, self.methods, tuple(range(29)), self.output),
            (
                "output",
                self.config,
                self.methods,
                self.seeds,
                Path("results/exp4-other"),
            ),
        )
        for label, config, methods, seeds, output in mutations:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    exp4_failure.validate_protocol(config, methods, seeds, output)

    def test_formal_protocol_rejects_non_disruptive_capacity_points(self) -> None:
        self.assertTrue(
            hasattr(exp4_failure, "validate_protocol"),
            "formal Exp4 runs need a protocol validator",
        )
        for sweep_name in (
            "link_post_failure_to_requirement_ratios",
            "physical_post_failure_to_requirement_ratios",
        ):
            config = copy.deepcopy(self.config)
            config["fault_sweeps"][sweep_name] = [0.9, 1.0]
            with self.subTest(sweep=sweep_name):
                with self.assertRaisesRegex(ValueError, "below service requirement"):
                    exp4_failure.validate_protocol(
                        config,
                        self.methods,
                        self.seeds,
                        self.output,
                    )

    def test_physical_failure_is_disruptive_before_recovery(self) -> None:
        async def execute() -> None:
            for sequence, ratio in enumerate((0.90, 0.75, 0.60, 0.45, 0.30), 1):
                snapshot = FailureScenarioGenerator().generate(
                    FailureScenarioConfig(
                        fault_type="PHYSICAL_CAPACITY_DROP",
                        fault_level=f"post_capacity_to_requirement_{ratio:.2f}",
                        severity=ratio,
                        num_agents=20,
                        num_gateways=4,
                    ),
                    3,
                )
                row, _events, _proposals, _probes = await run_one(
                    snapshot,
                    "proposed",
                    run_sequence=sequence,
                )
                self.assertTrue(row.fault_was_disruptive)
                self.assertTrue(row.requirement_violated_before_recovery)
                self.assertLess(
                    row.post_failure_capacity_mbps,
                    row.pre_recovery_requirement_mbps,
                )
                self.assertAlmostEqual(
                    row.pre_recovery_violation_margin_mbps,
                    row.pre_recovery_requirement_mbps
                    - row.post_failure_capacity_mbps,
                )

        asyncio.run(execute())

    def test_cspf_cannot_bypass_shared_failed_physical_access(self) -> None:
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="PHYSICAL_CAPACITY_DROP",
                    fault_level="capacity_factor_0.2",
                    severity=0.2,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            row, _events, _proposals, _probes = await run_one(
                snapshot,
                "cspf",
                run_sequence=1,
            )
            self.assertFalse(row.recovery_success)
            self.assertFalse(row.safe_rejection)
            self.assertTrue(row.rollback_triggered)
            self.assertTrue(row.rollback_success)
            self.assertFalse(row.post_execution_verification_success)
            self.assertEqual(row.selected_layers, "network")
            self.assertEqual(row.changed_physical_bindings, 0)
            self.assertIn("verification failed", row.failure_reason)

        asyncio.run(execute())


if __name__ == "__main__":
    unittest.main()
