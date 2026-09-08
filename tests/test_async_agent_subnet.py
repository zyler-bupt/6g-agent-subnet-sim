from __future__ import annotations

import asyncio
import unittest

from src.agents.base import exponential_forecast
from src.agents.phy_agent import PhyAgent
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator, RiskWeights
from src.core.gateway import Gateway
from src.core.models import AgentLayer, FlowMatch, GatewayRouteAction, GatewayRouteEntry
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

    def test_rescue_subnet_maps_business_edges_to_all_four_layers(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, metrics = await controller.build_task_subnet(rescue_task())
            self.assertTrue(metrics.networking_success)
            self.assertEqual(len(subnet.sessions), 3)
            self.assertEqual(len(subnet.trans_agents), 3)
            self.assertEqual(len(subnet.net_agents), 3)
            self.assertEqual(
                subnet.phy_agents,
                {"pagent-gw-ue", "pagent-gw-mec", "pagent-gw-cloud"},
            )
            self.assertEqual(len(subnet.physical_bindings), 6)
            self.assertEqual(len(subnet.session_supports), 3)
            self.assertEqual(len(subnet.path_supports), 3)
            self.assertEqual(len(subnet.agent_acks), 13)
            self.assertTrue(all(ack.accepted for ack in subnet.gateway_acks))
            self.assertTrue(all(ack.accepted for ack in subnet.agent_acks))

        asyncio.run(run())

    def test_agentcard_and_gateway_install_acks_are_auditable(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())

            app_ack = next(ack for ack in subnet.agent_acks if ack.agent_id == "agent-drone-capture")
            self.assertEqual(app_ack.gateway_id, "gw-ue")
            self.assertEqual(app_ack.purpose, "app_member")
            self.assertEqual(app_ack.ip, "10.10.1.2")
            self.assertEqual(app_ack.port, 9101)

            support_ack = next(ack for ack in subnet.agent_acks if ack.agent_id == "nagent-gw-ue")
            self.assertEqual(support_ack.purpose, "network_bearer")
            self.assertIn("network_bearer", support_ack.capabilities)

            ue_ack = next(ack for ack in subnet.gateway_acks if ack.gateway_id == "gw-ue")
            self.assertTrue(ue_ack.accepted)
            self.assertEqual(ue_ack.operation, "install")
            self.assertEqual(ue_ack.session_count, 2)
            self.assertEqual(ue_ack.route_count, 2)
            self.assertIn("task-rescue-001-sess-1", ue_ack.installed_session_ids)
            self.assertIn("task-rescue-001-sess-3", ue_ack.installed_session_ids)
            self.assertTrue(all(route_id.startswith("task-rescue-001:") for route_id in ue_ack.installed_route_ids))

        asyncio.run(run())

    def test_gateway_rejects_unavailable_local_delivery_agent(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            gateway = build_rescue_topology(provider)["gw-mec"]
            action = GatewayRouteAction(
                mode="local_delivery",
                allow=True,
                local_agent="missing-agent",
                local_agent_ip="10.10.2.2",
                local_agent_port=9999,
            )
            entry = GatewayRouteEntry(
                task_id="task-x",
                session_id="sess-x",
                gateway_id="gw-mec",
                flow_id="a->b",
                match=FlowMatch(src_agent="a", dst_agent="b", flow_type="video"),
                action=action,
                t_agent_id="tagent-gw-mec",
                n_agent_id="nagent-gw-mec",
            )
            ack = await gateway.install_subnet(rescue_task(), [], [entry])
            self.assertFalse(ack.accepted)
            self.assertEqual(ack.operation, "install")
            self.assertEqual(ack.reason, "local_agent_unavailable:missing-agent")
            self.assertEqual(gateway.installed_route_table(), [])

        asyncio.run(run())

    def test_support_bindings_match_route_table(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            session_id = "task-rescue-001-sess-1"

            session = next(item for item in subnet.sessions if item.session_id == session_id)
            session_support = subnet.session_supports[session_id]
            path_support = subnet.path_supports[session_support.path_support_id]
            route = next(
                entry
                for entry in controller.gateways["gw-ue"].installed_route_table()
                if entry.session_id == session_id
            )

            self.assertEqual(session_support.t_agent_id, session.t_agent_id)
            self.assertEqual(session_support.path_id, session.path_id)
            self.assertEqual(path_support.n_agent_id, session.n_agent_id)
            self.assertEqual(path_support.gateway_path, session.gateway_path)
            self.assertEqual(path_support.monitored_links, (("gw-ue", "gw-mec"),))
            self.assertEqual(route.session_support_id, session_support.support_id)
            self.assertEqual(route.path_support_id, path_support.support_id)

        asyncio.run(run())

    def test_gateway_route_table_hides_remote_agent_ip(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            session_id = "task-rescue-001-sess-1"

            ue_routes = {
                entry.session_id: entry
                for entry in controller.gateways["gw-ue"].installed_route_table()
            }
            mec_routes = {
                entry.session_id: entry
                for entry in controller.gateways["gw-mec"].installed_route_table()
            }

            self.assertEqual(len(controller.gateways["gw-ue"].installed_route_table()), 2)
            self.assertEqual(len(controller.gateways["gw-mec"].installed_route_table()), 2)
            self.assertIn(session_id, ue_routes)
            self.assertIn(session_id, mec_routes)
            self.assertTrue(
                any(entry.session_id == session_id for entry in subnet.gateway_routes["gw-ue"])
            )

            forward = ue_routes[session_id]
            self.assertEqual(forward.action.mode, "forward_to_gateway")
            self.assertEqual(forward.action.next_hop_gateway, "gw-mec")
            self.assertEqual(forward.action.next_hop_gateway_ip, "10.10.2.2")
            self.assertIsNone(forward.action.local_agent)
            self.assertIsNone(forward.action.local_agent_ip)

            local = mec_routes[session_id]
            self.assertEqual(local.action.mode, "local_delivery")
            self.assertEqual(local.action.local_agent, "agent-edge-recognition")
            self.assertEqual(local.action.local_agent_ip, "10.10.2.2")
            self.assertEqual(local.action.local_agent_port, 9201)
            self.assertIsNone(local.action.next_hop_gateway_ip)

        asyncio.run(run())

    def test_gateway_route_table_installs_multihop_transit_entries(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            gateways = build_rescue_topology(provider)
            gateways["gw-relay"] = Gateway(
                gateway_id="gw-relay",
                subnet_id="relay-subnet",
                node="relay-node",
                gateway_ip="10.10.9.2",
            )
            controller = AgentController(
                gateways,
                gateway_paths={
                    ("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec"),
                },
            )
            subnet, _ = await controller.build_task_subnet(rescue_task())
            session_id = "task-rescue-001-sess-1"
            session = next(item for item in subnet.sessions if item.session_id == session_id)

            self.assertEqual(session.gateway_path, ("gw-ue", "gw-relay", "gw-mec"))
            self.assertIn("gw-relay", subnet.involved_gateways)
            self.assertIn(session_id, controller.gateways["gw-relay"].installed_sessions)

            ue_route = next(
                entry
                for entry in controller.gateways["gw-ue"].installed_route_table()
                if entry.session_id == session_id
            )
            relay_route = next(
                entry
                for entry in controller.gateways["gw-relay"].installed_route_table()
                if entry.session_id == session_id
            )
            mec_route = next(
                entry
                for entry in controller.gateways["gw-mec"].installed_route_table()
                if entry.session_id == session_id
            )

            self.assertEqual(ue_route.action.mode, "forward_to_gateway")
            self.assertEqual(ue_route.action.next_hop_gateway, "gw-relay")
            self.assertEqual(ue_route.action.next_hop_gateway_ip, "10.10.9.2")
            self.assertIsNone(ue_route.action.local_agent_ip)
            self.assertEqual(ue_route.hop_index, 0)

            self.assertEqual(relay_route.action.mode, "forward_to_gateway")
            self.assertEqual(relay_route.action.next_hop_gateway, "gw-mec")
            self.assertEqual(relay_route.action.next_hop_gateway_ip, "10.10.2.2")
            self.assertIsNone(relay_route.action.local_agent_ip)
            self.assertEqual(relay_route.hop_index, 1)

            self.assertEqual(mec_route.action.mode, "local_delivery")
            self.assertEqual(mec_route.action.local_agent, "agent-edge-recognition")
            self.assertEqual(mec_route.action.local_agent_ip, "10.10.2.2")
            self.assertEqual(mec_route.hop_index, 2)
            session_support = subnet.session_supports[session_id]
            path_support = subnet.path_supports[session_support.path_support_id]
            self.assertEqual(path_support.gateway_path, ("gw-ue", "gw-relay", "gw-mec"))
            self.assertEqual(path_support.monitored_links, (("gw-ue", "gw-relay"), ("gw-relay", "gw-mec")))
            self.assertEqual(relay_route.path_support_id, path_support.support_id)

        asyncio.run(run())

    def test_agent_loop_produces_four_layer_predictions(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            controller = AgentController(build_rescue_topology(provider))
            subnet, _ = await controller.build_task_subnet(rescue_task())
            predictions = await controller.run_agent_loop(subnet, 2.0)
            layers = {item.layer for item in predictions}
            self.assertIn(AgentLayer.APPLICATION, layers)
            self.assertIn(AgentLayer.TRANSPORT, layers)
            self.assertIn(AgentLayer.NETWORK, layers)
            self.assertIn(AgentLayer.PHYSICAL, layers)

        asyncio.run(run())

    def test_physical_agent_reports_and_proposes_without_direct_mutation(self) -> None:
        provider = SyntheticMetricProvider()
        gateway = build_rescue_topology(provider)["gw-ue"]
        physical = gateway.agents["pagent-gw-ue"]
        self.assertIsInstance(physical, PhyAgent)
        task = rescue_task()
        predictions = physical.predict(task)
        self.assertEqual(
            {item.metric for item in predictions},
            {"phy_reliability", "phy_available_capacity_mbps"},
        )
        bindings_before = dict(physical.resource_bindings)
        action = physical.propose_for_edge(18.0, currently_bound=False)
        physical.execute(task, action)
        self.assertIn(
            action.action_type,
            {"KEEP_RESOURCE", "ALLOCATE_RESOURCE", "RELEASE_RESOURCE", "SWITCH_ACCESS"},
        )
        self.assertEqual(physical.resource_bindings, bindings_before)
        self.assertGreater(RiskWeights().lambda_h, 0.0)

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
