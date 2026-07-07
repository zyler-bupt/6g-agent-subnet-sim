from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from experiments.real_run import (
    _apply_path_support_targets,
    _build_link_target_profile,
    _build_target_profile,
    _evaluate_adjustment,
    _evaluate_rebuild_baseline,
    _render_real_report,
)
from src.controller.elastic import ElasticAdjuster
from src.controller.networking import AgentController
from src.metrics.synthetic import SyntheticMetricProvider
from src.metrics.netns import NetnsMetricProvider, NetnsTarget
from src.metrics.parsers import parse_iperf3_json, parse_ping, parse_ss_ti
from src.metrics.trace import TraceMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import GatewaySpec, build_rescue_topology, build_topology_from_catalog, rescue_topology_catalog, support_agent_specs


PING_SAMPLE = """
PING 10.10.3.2 (10.10.3.2) 56(84) bytes of data.

--- 10.10.3.2 ping statistics ---
50 packets transmitted, 45 received, 10% packet loss, time 49153ms
rtt min/avg/max/mdev = 80.008/80.029/80.062/0.286 ms
"""

SS_SAMPLE = """
ESTAB 0 0 10.10.1.2:43122 10.10.3.2:9000
	 cubic wscale:7,7 rto:204 rtt:80.1/0.3 ato:40 mss:1448 pmtu:1500 rcvmss:536 advmss:1448 cwnd:23 bytes_sent:1000000 bytes_retrans:5000 delivery_rate 95.3Mbps
"""


class RealMetricsStage3Tests(unittest.TestCase):
    def test_parse_ping_loss_and_rtt(self) -> None:
        stats = parse_ping(PING_SAMPLE)
        self.assertAlmostEqual(stats.loss_rate, 0.10)
        self.assertAlmostEqual(stats.rtt_avg_ms, 80.029)
        self.assertAlmostEqual(stats.jitter_ms, 0.286)

    def test_parse_iperf3_json_uses_receiver_throughput(self) -> None:
        output = json.dumps(
            {
                "end": {
                    "sum_sent": {
                        "bits_per_second": 101_000_000,
                        "retransmits": 3,
                    },
                    "sum_received": {
                        "bits_per_second": 98_500_000,
                    },
                }
            }
        )
        stats = parse_iperf3_json(output)
        self.assertAlmostEqual(stats.sender_mbps, 101.0)
        self.assertAlmostEqual(stats.receiver_mbps, 98.5)
        self.assertEqual(stats.retransmits, 3)
        self.assertAlmostEqual(stats.throughput_mbps, 98.5)

    def test_parse_ss_tcp_info(self) -> None:
        info = parse_ss_ti(SS_SAMPLE)
        self.assertAlmostEqual(info.srtt_ms, 80.1)
        self.assertAlmostEqual(info.rtt_var_ms, 0.3)
        self.assertAlmostEqual(info.rto_ms, 204.0)
        self.assertAlmostEqual(info.snd_cwnd, 23.0)
        self.assertAlmostEqual(info.delivery_rate_mbps, 95.3)
        self.assertAlmostEqual(info.retransmission_rate, 0.005)

    def test_trace_provider_overrides_application_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "traffic.npy"
            np.save(path, np.array([1.0, 2.0, 3.0], dtype=np.float32))
            provider = TraceMetricProvider(
                path,
                base_provider=SyntheticMetricProvider(),
                target_mean_mbps=12.0,
                sample_interval_s=1.0,
            )
            first = provider.snapshot("task", "agent", 0.0)
            third = provider.snapshot("task", "agent", 2.0)
            self.assertAlmostEqual(first.app_rate_mbps, 6.0)
            self.assertAlmostEqual(third.app_rate_mbps, 18.0)

    def test_netns_provider_describes_per_agent_targets(self) -> None:
        default = NetnsTarget("h-term", "10.10.3.2", label="default")
        edge = NetnsTarget("h-term", "10.10.2.2", label="terminal -> edge")
        provider = NetnsMetricProvider(
            targets={"nagent-gw-ue": edge},
            default_target=default,
        )

        self.assertEqual(provider.describe_target("nagent-gw-ue")["target_ip"], "10.10.2.2")
        self.assertEqual(provider.describe_target("unknown-agent")["target_ip"], "10.10.3.2")

    def test_real_run_rescue_profile_maps_agents_to_multiple_links(self) -> None:
        targets = _build_target_profile(_Args(), NetnsTarget("h-term", "10.10.3.2"))
        self.assertEqual(targets["nagent-gw-ue"].namespace, "h-term")
        self.assertEqual(targets["nagent-gw-ue"].target_ip, "10.10.2.2")
        self.assertEqual(targets["nagent-gw-mec"].namespace, "h-edge")
        self.assertEqual(targets["nagent-gw-mec"].target_ip, "10.10.3.2")
        self.assertEqual(targets["nagent-gw-cloud"].namespace, "h-cloud")
        self.assertEqual(targets["nagent-gw-cloud"].target_ip, "10.10.1.2")

    def test_path_support_targets_update_nagent_measurement_links(self) -> None:
        import asyncio

        async def run() -> None:
            args = _Args()
            default = NetnsTarget("h-term", "10.10.3.2", label="default")
            provider = NetnsMetricProvider(
                targets=_build_target_profile(args, default),
                default_target=default,
            )
            controller = AgentController(build_rescue_topology(SyntheticMetricProvider()))
            subnet, _ = await controller.build_task_subnet(rescue_task())

            bindings = _apply_path_support_targets(
                provider,
                subnet,
                _build_link_target_profile(args),
                controller,
            )

            self.assertEqual(provider.describe_target("nagent-gw-ue")["namespace"], "h-term")
            self.assertEqual(provider.describe_target("nagent-gw-ue")["target_ip"], "10.10.2.2")
            self.assertEqual(bindings["nagent-gw-ue"]["selected_link"], ["gw-ue", "gw-mec"])
            self.assertEqual(bindings["nagent-gw-mec"]["selected_link"], ["gw-mec", "gw-cloud"])
            self.assertEqual(bindings["nagent-gw-cloud"]["selected_link"], ["gw-cloud", "gw-ue"])

        asyncio.run(run())

    def test_path_support_targets_follow_given_relay_path_agent(self) -> None:
        import asyncio

        async def run() -> None:
            args = _Args()
            relay_target = NetnsTarget("h-relay", "10.10.2.2", label="relay -> edge path support")
            link_targets = _build_link_target_profile(args)
            link_targets[("gw-relay", "gw-mec")] = relay_target
            provider = NetnsMetricProvider(
                targets={},
                default_target=NetnsTarget("h-term", "10.10.3.2", label="default"),
            )
            gateways = build_topology_from_catalog(_catalog_with_relay(), SyntheticMetricProvider())
            gateways["gw-ue"].fail_agent("nagent-gw-ue")
            gateways["gw-mec"].fail_agent("nagent-gw-mec")
            controller = AgentController(
                gateways,
                gateway_paths={("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec")},
            )
            subnet, _ = await controller.build_task_subnet(rescue_task())

            bindings = _apply_path_support_targets(provider, subnet, link_targets, controller)

            self.assertEqual(provider.describe_target("nagent-gw-relay")["namespace"], "h-relay")
            self.assertEqual(provider.describe_target("nagent-gw-relay")["target_ip"], "10.10.2.2")
            self.assertEqual(bindings["nagent-gw-relay"]["selected_link"], ["gw-relay", "gw-mec"])

        asyncio.run(run())

    def test_real_adjustment_summary_reports_controller_decision(self) -> None:
        import asyncio

        async def run() -> None:
            provider = SyntheticMetricProvider()
            gateways = build_topology_from_catalog(_catalog_with_relay(), provider)
            controller = AgentController(
                gateways,
                gateway_paths={("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec")},
            )
            task = rescue_task()
            subnet, _ = await controller.build_task_subnet(task)
            provider.inject_event(task.task_id, "bearer_degradation", severity=1.3)

            summary = await _evaluate_adjustment(controller, subnet, timestamp=4.0)

            self.assertIn(summary["strategy"], {"local_tuning", "support_session_retune"})
            self.assertIn("risk_before", summary)
            self.assertIn("risk_after", summary)
            self.assertIn("service_interruption_ms", summary)
            self.assertIsInstance(summary["actions"], list)
            self.assertIsInstance(summary["changed_sessions"], list)

        asyncio.run(run())

    def test_elastic_retune_preserves_given_gateway_path(self) -> None:
        import asyncio

        async def run() -> None:
            provider = SyntheticMetricProvider()
            gateways = build_topology_from_catalog(_catalog_with_relay(), provider)
            controller = AgentController(
                gateways,
                gateway_paths={("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec")},
            )
            subnet, _ = await controller.build_task_subnet(rescue_task())

            result = ElasticAdjuster().choose_minimal_adjustment(
                subnet,
                risk_before=2.0,
                risk_after_local_actions=2.0,
            )

            self.assertEqual(result.strategy, "support_session_retune")
            changed = {
                session.session_id: session
                for session in result.changed_sessions
            }
            retuned = changed["task-rescue-001-sess-1"]
            self.assertEqual(retuned.gateway_path, ("gw-ue", "gw-relay", "gw-mec"))
            self.assertEqual(retuned.path_id, "gw-ue->gw-relay->gw-mec")
            self.assertEqual(retuned.status, "retuned")
            self.assertEqual(result.changed_gateways, 4)

        asyncio.run(run())

    def test_full_rebuild_baseline_summary_reports_rebuild_cost(self) -> None:
        import asyncio

        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())

            summary = await _evaluate_rebuild_baseline(controller, subnet, timestamp=4.0)

            self.assertEqual(summary["strategy"], "full_rebuild")
            self.assertEqual(summary["operations"]["member_confirm"], 4)
            self.assertEqual(summary["operations"]["session_setup"], 3)
            self.assertEqual(summary["operations"]["gateway_install"], 3)
            self.assertGreater(summary["service_interruption_ms"], 0)

        asyncio.run(run())

    def test_real_report_renders_demo_sections(self) -> None:
        text = _render_real_report(_fake_real_run_result())

        self.assertIn("真实测试床闭环演示", text)
        self.assertIn("nAgent 真实测量链路绑定", text)
        self.assertIn("事件前后真实感知", text)
        self.assertIn("Controller 最小弹性调整", text)
        self.assertIn("最小调整 vs 全量重建", text)
        self.assertIn("tier2 会话重调", text)
        self.assertIn("全量重建", text)


class _Args:
    target_profile = "rescue"
    term_namespace = "h-term"
    edge_namespace = "h-edge"
    cloud_namespace = "h-cloud"
    term_ip = "10.10.1.2"
    edge_ip = "10.10.2.2"
    cloud_ip = "10.10.3.2"
    iperf_port = 5201
    ping_count = 5
    ping_interval_s = 0.2
    iperf_seconds = 1
    command_timeout_s = 8.0
    sudo = False


def _catalog_with_relay():
    relay = GatewaySpec(
        gateway_id="gw-relay",
        subnet_id="relay-subnet",
        node="relay-node",
        gateway_ip="10.10.9.2",
    )
    return rescue_topology_catalog().with_gateway(relay).with_agents(
        *support_agent_specs("gw-relay")
    )


def _fake_real_run_result():
    baseline_sense = {
        "step": 0,
        "timestamp": 0.0,
        "agent_id": "nagent-gw-ue",
        "agent_name": "network bearer",
        "layer": "net",
        "measurement_target": {
            "label": "terminal -> edge video stream",
            "namespace": "h-term",
            "target_ip": "10.10.2.2",
        },
        "values": {
            "latency_ms": 0.03,
            "loss_rate": 0.0,
            "available_bandwidth_mbps": 100.0,
            "utilization": 0.16,
        },
    }
    event_sense = {
        **baseline_sense,
        "timestamp": 10.0,
        "values": {
            "latency_ms": 80.0,
            "loss_rate": 0.05,
            "available_bandwidth_mbps": 30.0,
            "utilization": 0.53,
        },
    }
    adjustment = {
        "risk_before": 1.2,
        "risk_after": 0.8,
        "strategy": "support_session_retune",
        "changed_agents": 0,
        "changed_edges": 3,
        "changed_gateways": 3,
        "service_interruption_ms": 63.0,
        "operations": {"local_tune": 2, "session_setup": 3, "gateway_install": 3},
        "actions": [
            {
                "agent_id": "nagent-gw-ue",
                "action_type": "raise_monitoring_and_bearer_advice",
            }
        ],
        "changed_sessions": [],
    }
    return {
        "task_id": "task-rescue-001",
        "target_profile": "rescue",
        "history_steps": 10,
        "horizon": 5,
        "flowgen_control_file": "/tmp/6g-agent-testbed/flowgen-control.json",
        "path_measurement_bindings": {
            "nagent-gw-ue": {
                "gateway_path": ["gw-ue", "gw-mec"],
                "selected_link": ["gw-ue", "gw-mec"],
                "measurement_target": {
                    "label": "terminal -> edge video stream",
                    "namespace": "h-term",
                    "target_ip": "10.10.2.2",
                },
            }
        },
        "baseline": {"risk": 0.1, "sense_log": [baseline_sense]},
        "event": {"risk": 1.2, "sense_log": [event_sense]},
        "adjustment": adjustment,
        "full_rebuild_baseline": {
            **adjustment,
            "strategy": "full_rebuild",
            "service_interruption_ms": 99.0,
            "operations": {"member_confirm": 4, "session_setup": 3, "gateway_install": 3},
        },
    }


if __name__ == "__main__":
    unittest.main()
