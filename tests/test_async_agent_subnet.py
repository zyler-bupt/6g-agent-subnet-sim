from __future__ import annotations

import asyncio
import unittest

from src.agents.base import exponential_forecast
from src.agents.phy_agent import PhyAgentStub
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator, RiskWeights
from src.core.models import AgentLayer
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.bus import AsyncMessageBus
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


class AsyncAgentSubnetTests(unittest.TestCase):
    def test_message_bus_round_trip(self) -> None:
        async def run() -> None:
            bus = AsyncMessageBus()
            from src.core.models import Message, MessageKind

            await bus.send(Message(kind=MessageKind.MEMBER_CONFIRM, source="c", target="gw"))
            received = await bus.receive("gw", timeout=0.1)
            self.assertEqual(received.source, "c")

        asyncio.run(run())

    def test_synthetic_metrics_range(self) -> None:
        provider = SyntheticMetricProvider()
        snapshot = provider.snapshot("task", "agent", 1.0)
        self.assertGreater(snapshot.available_bandwidth_mbps, 0)
        self.assertGreaterEqual(snapshot.loss_rate, 0)
        self.assertLessEqual(snapshot.loss_rate, 0.35)

    def test_rescue_subnet_maps_business_edges_to_trans_and_net(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, metrics = await controller.build_task_subnet(rescue_task())
            self.assertTrue(metrics.networking_success)
            self.assertEqual(len(subnet.sessions), 3)
            self.assertEqual(len(subnet.trans_agents), 3)
            self.assertEqual(len(subnet.net_agents), 3)
            self.assertEqual(subnet.phy_agents, set())
            self.assertTrue(all(ack.accepted for ack in subnet.gateway_acks))

        asyncio.run(run())

    def test_agent_loop_produces_three_layer_predictions(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            predictions = await controller.run_agent_loop(subnet, 2.0)
            layers = {item.layer for item in predictions}
            self.assertIn(AgentLayer.APPLICATION, layers)
            self.assertIn(AgentLayer.TRANSPORT, layers)
            self.assertIn(AgentLayer.NETWORK, layers)
            self.assertNotIn(AgentLayer.PHYSICAL, layers)

        asyncio.run(run())

    def test_physical_stub_is_noop_and_lambda_h_defaults_zero(self) -> None:
        provider = SyntheticMetricProvider()
        gateway = build_rescue_topology(provider)["gw-ue"]
        stub = gateway.agents["pagent-stub-ue"]
        self.assertIsInstance(stub, PhyAgentStub)
        task = rescue_task()
        self.assertEqual(stub.predict(task), [])
        self.assertEqual(RiskWeights().lambda_h, 0.0)

    def test_risk_and_minimal_adjustment(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            task = rescue_task()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(task)
            provider.inject_event(task.task_id, "bearer_degradation", severity=1.0)
            before, after, adjustment = await controller.evaluate_and_adjust(subnet, 4.0)
            self.assertGreaterEqual(before, 0.0)
            self.assertLessEqual(after, before + 0.5)
            self.assertIn(adjustment.strategy, {"local_tuning", "support_session_retune", "no_adjustment"})

        asyncio.run(run())

    def test_tier2_replaces_failed_support_with_local_standby(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            task = rescue_task()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(task)
            # gw-mec has a standby network-bearer Agent -> local tier-2 replacement.
            controller.gateways["gw-mec"].fail_agent("nagent-gw-mec")
            _, _, adjustment = await controller.evaluate_and_adjust(subnet, 4.0, {"nagent-gw-mec"})
            self.assertEqual(adjustment.strategy, "support_agent_replace")
            self.assertEqual(adjustment.changed_agents, 1)
            self.assertIn("nagent-gw-mec-standby", subnet.net_agents)
            self.assertNotIn("nagent-gw-mec", subnet.net_agents)

        asyncio.run(run())

    def test_tier3_reroutes_when_no_local_standby(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            task = rescue_task()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(task)
            # gw-ue has no standby -> must reroute the flow's network support cross-subnet.
            controller.gateways["gw-ue"].fail_agent("nagent-gw-ue")
            _, _, adjustment = await controller.evaluate_and_adjust(subnet, 4.0, {"nagent-gw-ue"})
            self.assertEqual(adjustment.strategy, "communication_reroute")
            self.assertEqual(adjustment.operations.get("reroute"), 1)
            self.assertNotIn("nagent-gw-ue", subnet.net_agents)

        asyncio.run(run())

    def test_failure_auto_detection_triggers_without_risk(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            task = rescue_task()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(task)
            controller.gateways["gw-mec"].fail_agent("nagent-gw-mec")
            # No failed_agents passed and no risk event: controller must auto-detect F_m.
            before, _, adjustment = await controller.evaluate_and_adjust(subnet, 4.0)
            self.assertLessEqual(before, task.qos.risk_threshold)
            self.assertEqual(adjustment.strategy, "support_agent_replace")

        asyncio.run(run())

    def test_forecast_rejects_empty_history(self) -> None:
        with self.assertRaises(ValueError):
            exponential_forecast([], 3)


if __name__ == "__main__":
    unittest.main()

