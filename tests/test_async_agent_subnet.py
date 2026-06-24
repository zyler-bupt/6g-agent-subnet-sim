from __future__ import annotations

import asyncio
import unittest

from src.agents.base import exponential_forecast
from src.agents.phy_agent import PhyAgentStub
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator, RiskWeights
from src.core.models import AgentLayer
from src.metrics.mock import MockMetricProvider
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

    def test_mock_metrics_range(self) -> None:
        provider = MockMetricProvider()
        snapshot = provider.snapshot("task", "agent", 1.0)
        self.assertGreater(snapshot.available_bandwidth_mbps, 0)
        self.assertGreaterEqual(snapshot.loss_rate, 0)
        self.assertLessEqual(snapshot.loss_rate, 0.35)

    def test_rescue_subnet_maps_business_edges_to_trans_and_net(self) -> None:
        async def run() -> None:
            provider = MockMetricProvider()
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
            provider = MockMetricProvider()
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
        provider = MockMetricProvider()
        gateway = build_rescue_topology(provider)["gw-ue"]
        stub = gateway.agents["pagent-stub-ue"]
        self.assertIsInstance(stub, PhyAgentStub)
        task = rescue_task()
        self.assertEqual(stub.predict(task), [])
        self.assertEqual(RiskWeights().lambda_h, 0.0)

    def test_risk_and_minimal_adjustment(self) -> None:
        async def run() -> None:
            provider = MockMetricProvider()
            task = rescue_task()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(task)
            provider.inject_event(task.task_id, "bearer_degradation", severity=1.0)
            before, after, adjustment = await controller.evaluate_and_adjust(subnet, 4.0)
            self.assertGreaterEqual(before, 0.0)
            self.assertLessEqual(after, before + 0.5)
            self.assertIn(adjustment.strategy, {"local_tuning", "support_session_retune", "no_adjustment"})

        asyncio.run(run())

    def test_forecast_rejects_empty_history(self) -> None:
        with self.assertRaises(ValueError):
            exponential_forecast([], 3)


if __name__ == "__main__":
    unittest.main()

