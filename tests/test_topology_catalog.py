from __future__ import annotations

import asyncio
import unittest

from src.controller.networking import AgentController
from src.core.models import AgentLayer, AgentRole
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import (
    GatewaySpec,
    build_rescue_topology,
    build_topology_from_catalog,
    rescue_topology_catalog,
    support_agent_specs,
)


class TopologyCatalogTests(unittest.TestCase):
    def test_rescue_topology_preserves_existing_agent_addresses(self) -> None:
        provider = SyntheticMetricProvider()
        gateways = build_rescue_topology(provider)

        self.assertEqual(set(gateways), {"gw-ue", "gw-mec", "gw-cloud"})
        self.assertEqual(gateways["gw-ue"].gateway_ip, "10.10.1.2")
        self.assertEqual(gateways["gw-mec"].gateway_ip, "10.10.2.2")
        self.assertEqual(gateways["gw-cloud"].gateway_ip, "10.10.3.2")

        drone = gateways["gw-ue"].agents["agent-drone-capture"].card
        self.assertEqual(drone.layer, AgentLayer.APPLICATION)
        self.assertEqual(drone.role, AgentRole.BUSINESS)
        self.assertEqual(drone.ip, "10.10.1.2")
        self.assertEqual(drone.port, 9101)
        self.assertEqual(drone.endpoint, "sim://gw-ue/agent-drone-capture")

        edge = gateways["gw-mec"].agents["agent-edge-recognition"].card
        self.assertEqual(edge.ip, "10.10.2.2")
        self.assertEqual(edge.port, 9201)
        self.assertIn("person_detection", edge.capabilities)

        standby = gateways["gw-cloud"].agents["nagent-gw-cloud-standby"].card
        self.assertEqual(standby.layer, AgentLayer.NETWORK)
        self.assertEqual(standby.ip, "10.10.3.2")
        self.assertIsNone(standby.port)

    def test_catalog_can_add_relay_gateway_with_support_agents(self) -> None:
        provider = SyntheticMetricProvider()
        relay = GatewaySpec(
            gateway_id="gw-relay",
            subnet_id="relay-subnet",
            node="relay-node",
            gateway_ip="10.10.9.2",
        )
        catalog = rescue_topology_catalog().with_gateway(relay).with_agents(
            *support_agent_specs("gw-relay", standby_network=True)
        )
        gateways = build_topology_from_catalog(catalog, provider)

        self.assertIn("gw-relay", gateways)
        relay_gateway = gateways["gw-relay"]
        self.assertEqual(relay_gateway.subnet_id, "relay-subnet")
        self.assertEqual(relay_gateway.node, "relay-node")
        self.assertEqual(relay_gateway.gateway_ip, "10.10.9.2")
        self.assertIn("tagent-gw-relay", relay_gateway.agents)
        self.assertIn("nagent-gw-relay", relay_gateway.agents)
        self.assertIn("nagent-gw-relay-standby", relay_gateway.agents)

        tagent = relay_gateway.agents["tagent-gw-relay"].card
        self.assertEqual(tagent.layer, AgentLayer.TRANSPORT)
        self.assertEqual(tagent.subnet_id, "relay-subnet")
        self.assertEqual(tagent.node, "relay-node")
        self.assertEqual(tagent.ip, "10.10.9.2")
        self.assertIn("transport_session", tagent.capabilities)

        nagent = relay_gateway.agents["nagent-gw-relay"].card
        self.assertEqual(nagent.layer, AgentLayer.NETWORK)
        self.assertEqual(nagent.ip, "10.10.9.2")
        self.assertIn("network_bearer", nagent.capabilities)

    def test_catalog_relay_gateway_can_receive_multihop_routes_and_ack(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            catalog = _catalog_with_relay()
            gateways = build_topology_from_catalog(catalog, provider)
            controller = AgentController(
                gateways,
                gateway_paths={("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec")},
            )

            subnet, metrics = await controller.build_task_subnet(rescue_task())
            session_id = "task-rescue-001-sess-1"
            relay_ack = next(ack for ack in subnet.gateway_acks if ack.gateway_id == "gw-relay")
            relay_route = next(
                route
                for route in controller.gateways["gw-relay"].installed_route_table()
                if route.session_id == session_id
            )

            self.assertTrue(metrics.networking_success)
            self.assertTrue(relay_ack.accepted)
            self.assertEqual(relay_ack.session_count, 1)
            self.assertEqual(relay_ack.route_count, 1)
            self.assertEqual(relay_route.action.mode, "forward_to_gateway")
            self.assertEqual(relay_route.action.next_hop_gateway, "gw-mec")
            self.assertEqual(relay_route.action.next_hop_gateway_ip, "10.10.2.2")

        asyncio.run(run())

    def test_path_support_agent_is_selected_from_given_gateway_path(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider()
            gateways = build_topology_from_catalog(_catalog_with_relay(), provider)
            gateways["gw-ue"].fail_agent("nagent-gw-ue")
            gateways["gw-mec"].fail_agent("nagent-gw-mec")
            controller = AgentController(
                gateways,
                gateway_paths={("gw-ue", "gw-mec"): ("gw-ue", "gw-relay", "gw-mec")},
            )

            subnet, metrics = await controller.build_task_subnet(rescue_task())
            session_id = "task-rescue-001-sess-1"
            session = next(item for item in subnet.sessions if item.session_id == session_id)
            session_support = subnet.session_supports[session_id]
            path_support = subnet.path_supports[session_support.path_support_id]
            support_ack = next(
                ack
                for ack in subnet.agent_acks
                if ack.agent_id == "nagent-gw-relay" and ack.purpose == "network_bearer"
            )

            self.assertTrue(metrics.networking_success)
            self.assertEqual(session.gateway_path, ("gw-ue", "gw-relay", "gw-mec"))
            self.assertEqual(session.n_agent_id, "nagent-gw-relay")
            self.assertEqual(path_support.n_agent_id, "nagent-gw-relay")
            self.assertEqual(path_support.gateway_path, session.gateway_path)
            self.assertEqual(support_ack.gateway_id, "gw-relay")

        asyncio.run(run())


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


if __name__ == "__main__":
    unittest.main()
