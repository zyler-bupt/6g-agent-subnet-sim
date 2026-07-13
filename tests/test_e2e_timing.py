from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from src.controller.networking import AgentController
from src.e2e.build import build_task_subnet_e2e
from src.e2e.flowgen import NetnsFlowHealthVerifier, NetnsFlowgenManager
from src.e2e.installers import (
    NetnsGatewayInstaller,
    SimulatedGatewayInstaller,
    rescue_netns_gateway_targets,
)
from src.e2e.recovery import SyntheticFaultActuator, measure_e2e_recovery
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


class E2ETimingTests(unittest.TestCase):
    def test_controller_metric_keeps_explicit_compatibility_alias(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            _subnet, metrics = await controller.build_task_subnet(rescue_task())
            self.assertGreaterEqual(metrics.controller_build_ms, 0.0)
            self.assertEqual(metrics.controller_build_ms, metrics.networking_latency_ms)

        asyncio.run(run())

    def test_e2e_build_includes_install_and_business_verification(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            controller = AgentController(build_rescue_topology(provider))
            installer = SimulatedGatewayInstaller(
                control_rtt_ms=0.0,
                rule_install_ms=0.0,
                ack_ms=0.0,
                jitter_ms=0.0,
            )
            verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
            _subnet, metrics, install, verify = await build_task_subnet_e2e(
                "建立应急救援无人机任务通信子网",
                controller,
                provider,
                installer=installer,
                verifier=verifier,
            )
            self.assertTrue(metrics.success)
            self.assertTrue(install.ok)
            self.assertTrue(verify.ok)
            self.assertEqual(metrics.checked_edges, 3)
            self.assertEqual(metrics.passed_edges, 3)
            self.assertEqual(metrics.updated_rule_count, 6)
            self.assertGreaterEqual(metrics.e2e_build_ms, metrics.controller_build_ms)

        asyncio.run(run())

    def test_business_verification_rejects_insufficient_throughput(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=8.0)
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            result = await SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0).verify(subnet)
            self.assertFalse(result.ok)
            self.assertLess(result.passed_edges, result.checked_edges)

        asyncio.run(run())

    def test_all_synthetic_faults_measure_fault_to_restore(self) -> None:
        async def run(fault_type: str) -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            controller = AgentController(build_rescue_topology(provider))
            installer = SimulatedGatewayInstaller(
                control_rtt_ms=0.0,
                rule_install_ms=0.0,
                ack_ms=0.0,
                jitter_ms=0.0,
            )
            verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
            subnet, build, _install, _verify = await build_task_subnet_e2e(
                rescue_task(),
                controller,
                provider,
                installer=installer,
                verifier=verifier,
            )
            self.assertTrue(build.success)
            result = await measure_e2e_recovery(
                scenario="rescue",
                run_id=1,
                controller=controller,
                subnet=subnet,
                provider=provider,
                installer=installer,
                verifier=verifier,
                actuator=SyntheticFaultActuator(provider),
                fault_type=fault_type,
                healthy_windows=3,
                sample_interval_s=0.0,
                detection_timeout_s=1.0,
                recovery_timeout_s=1.0,
            )
            self.assertTrue(result.incremental_success, result.errors)
            self.assertGreaterEqual(result.e2e_recovery_ms, result.fault_detect_ms)
            self.assertGreaterEqual(result.e2e_recovery_ms, result.elastic_recovery_ms)
            self.assertGreaterEqual(result.estimated_interruption_ms, 0.0)
            self.assertEqual(result.healthy_windows, 3)

        for fault_type in ("link_degrade", "agent_offline", "gateway_rule_loss"):
            with self.subTest(fault_type=fault_type):
                asyncio.run(run(fault_type))

    def test_flowgen_recovery_verifier_uses_active_tcp_flow(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            manager = NetnsFlowgenManager(
                gateway_namespaces={
                    "gw-ue": "h-term",
                    "gw-mec": "h-edge",
                    "gw-cloud": "h-cloud",
                },
                gateway_ips={
                    "gw-ue": "10.10.1.2",
                    "gw-mec": "10.10.2.2",
                    "gw-cloud": "10.10.3.2",
                },
            )
            targets = manager._targets_for(subnet)
            verifier = NetnsFlowHealthVerifier(targets)
            ping = (
                "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
                "rtt min/avg/max/mdev = 2.000/2.000/2.000/0.000 ms\n"
            )
            ss = (
                "ESTAB 0 0 10.10.1.2:40000 10.10.2.2:9401\n"
                " cubic rtt:2.0/0.2 bytes_sent:1000000 bytes_retrans:0 "
                "delivery_rate 100Mbps\n"
            )

            async def fake_run(_namespace, command, *_args):
                return ping if command == "ping" else ss

            verifier._run = fake_run
            result = await verifier.verify(subnet)
            self.assertTrue(result.ok)
            self.assertEqual(result.mode, "netns-flowgen")
            self.assertEqual(result.passed_edges, 3)

        asyncio.run(run())

    def test_flowgen_process_preserves_sudo_tty_and_uses_own_process_group(self) -> None:
        provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
        controller = AgentController(build_rescue_topology(provider))

        async def subnet_for_test():
            subnet, _ = await controller.build_task_subnet(rescue_task())
            return subnet

        subnet = asyncio.run(subnet_for_test())
        manager = NetnsFlowgenManager(
            gateway_namespaces={
                "gw-ue": "h-term",
                "gw-mec": "h-edge",
                "gw-cloud": "h-cloud",
            },
            gateway_ips={
                "gw-ue": "10.10.1.2",
                "gw-mec": "10.10.2.2",
                "gw-cloud": "10.10.3.2",
            },
            sudo=True,
        )
        target = next(iter(manager._targets_for(subnet).values()))
        with patch("src.e2e.flowgen.subprocess.Popen") as popen:
            manager._popen(
                target.target_namespace,
                "server",
                "--host",
                "0.0.0.0",
                "--port",
                str(target.port),
            )
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[:2], ["sudo", "-n"])
        self.assertIsNone(options["stdin"])
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertIs(options["preexec_fn"], os.setpgrp)
        self.assertNotIn("start_new_session", options)

    def test_flowgen_stop_signals_root_process_group_through_sudo(self) -> None:
        manager = NetnsFlowgenManager(
            gateway_namespaces={},
            gateway_ips={},
            sudo=True,
        )
        process = Mock(pid=12345)
        process.poll.return_value = None
        completed = Mock(returncode=0, stderr="")
        with patch("src.e2e.flowgen.subprocess.run", return_value=completed) as run:
            manager._signal_process_group(process, signal.SIGTERM)
        self.assertEqual(
            run.call_args.args[0],
            ["sudo", "-n", "/bin/kill", "-TERM", "--", "-12345"],
        )

    def test_full_rebuild_recovery_baseline_is_executed(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            controller = AgentController(build_rescue_topology(provider))
            installer = SimulatedGatewayInstaller(
                control_rtt_ms=0.0,
                rule_install_ms=0.0,
                ack_ms=0.0,
                jitter_ms=0.0,
            )
            verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
            subnet, build, _install, _verify = await build_task_subnet_e2e(
                rescue_task(),
                controller,
                provider,
                installer=installer,
                verifier=verifier,
            )
            self.assertTrue(build.success)
            result = await measure_e2e_recovery(
                scenario="rescue",
                run_id=1,
                controller=controller,
                subnet=subnet,
                provider=provider,
                installer=installer,
                verifier=verifier,
                actuator=SyntheticFaultActuator(provider),
                fault_type="gateway_rule_loss",
                healthy_windows=2,
                sample_interval_s=0.0,
                detection_timeout_s=1.0,
                recovery_timeout_s=1.0,
                recovery_mode="full_rebuild",
            )
            self.assertTrue(result.incremental_success, result.errors)
            self.assertEqual(result.strategy, "full_rebuild")
            self.assertEqual(result.changed_gateway_count, 3)

        asyncio.run(run())

    def test_confirmed_agent_recovery_does_not_probe_blackholed_path(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            failed_agent = "nagent-gw-mec"
            self.assertTrue(controller.gateways["gw-mec"].fail_agent(failed_agent))

            with patch.object(
                controller,
                "run_agent_loop",
                side_effect=AssertionError("failed path must not be probed during apply"),
            ):
                adjustment = await controller.apply_failed_support_recovery(
                    subnet,
                    {failed_agent},
                    timestamp=1.0,
                )

            self.assertEqual(adjustment.strategy, "support_agent_replace")
            self.assertEqual(adjustment.changed_agents, 1)

        asyncio.run(run())

    def test_netns_installer_maps_forward_rules_to_idempotent_routes(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            installer = NetnsGatewayInstaller(rescue_netns_gateway_targets())
            commands = installer._commands_for_entries(
                installer.targets["gw-ue"],
                subnet.gateway_routes["gw-ue"],
            )
            rendered = [" ".join(command) for command, _mutation in commands]
            self.assertTrue(any("route replace 10.10.2.2/32" in item for item in rendered))
            self.assertTrue(any("route get 10.10.1.2" in item for item in rendered))
            self.assertEqual(sum(1 for _command, mutation in commands if mutation), 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
