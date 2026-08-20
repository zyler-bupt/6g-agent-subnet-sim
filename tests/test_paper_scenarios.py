from __future__ import annotations

import unittest

from src.simulation.paper_scenarios import (
    generate_formation_snapshot,
    generate_mesh_topology,
)


class PaperMeshTopologyTests(unittest.TestCase):
    """Catch topology drift that would invalidate cross-method pairing."""

    def test_mesh_is_connected_deterministic_and_within_registered_ranges(self) -> None:
        first = generate_mesh_topology(seed=7, num_gateways=12)
        second = generate_mesh_topology(seed=7, num_gateways=12)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.gateway_ids), 12)
        self.assertTrue(first.connected)
        degrees = first.degrees()
        average_degree = sum(degrees.values()) / len(degrees)
        self.assertGreaterEqual(average_degree, 3.0)
        self.assertLessEqual(average_degree, 4.0)
        self.assertTrue(all(2 <= value <= 5 for value in degrees.values()))
        self.assertEqual(len(first.links), len({link.link_id for link in first.links}))
        for link in first.links:
            self.assertGreaterEqual(link.delay_ms, 5.0)
            self.assertLessEqual(link.delay_ms, 30.0)
            self.assertGreaterEqual(link.bandwidth_mbps, 50.0)
            self.assertLessEqual(link.bandwidth_mbps, 200.0)
            self.assertGreaterEqual(link.loss_percent, 0.0)
            self.assertLessEqual(link.loss_percent, 1.0)
            self.assertGreaterEqual(link.jitter_ms, 0.0)
            self.assertLessEqual(link.jitter_ms, 5.0)

    def test_all_gateway_paths_use_links_present_in_the_mesh(self) -> None:
        topology = generate_mesh_topology(seed=9, num_gateways=12)
        links = {
            frozenset((link.source, link.target))
            for link in topology.links
        }

        self.assertEqual(len(topology.gateway_paths), 12 * 12)
        for (source, target), path in topology.gateway_paths.items():
            self.assertEqual(path[0], source)
            self.assertEqual(path[-1], target)
            for left, right in zip(path, path[1:]):
                self.assertIn(frozenset((left, right)), links)


class PaperFormationScenarioTests(unittest.IsolatedAsyncioTestCase):
    """Catch chain-like or unexecutable Task DAG generation."""

    def test_modular_tasks_have_forks_merges_density_and_cross_gateway_share(self) -> None:
        for task_size in (8, 12, 16, 20, 24, 28, 32):
            with self.subTest(task_size=task_size):
                snapshot = generate_formation_snapshot(
                    task_size=task_size,
                    seed=11,
                    event_id=0,
                )
                indegree = {agent_id: 0 for agent_id in snapshot.task.app_agents}
                outdegree = {agent_id: 0 for agent_id in snapshot.task.app_agents}
                positions = {
                    agent_id: index
                    for index, agent_id in enumerate(snapshot.task.app_agents)
                }
                for edge in snapshot.task.biz_edges:
                    self.assertLess(positions[edge.source], positions[edge.target])
                    outdegree[edge.source] += 1
                    indegree[edge.target] += 1

                fork_ratio = sum(value >= 2 for value in outdegree.values()) / task_size
                average_outdegree = len(snapshot.task.biz_edges) / task_size
                self.assertGreaterEqual(fork_ratio, 0.20)
                self.assertLessEqual(fork_ratio, 0.30)
                self.assertTrue(any(value >= 2 for value in indegree.values()))
                self.assertGreaterEqual(average_outdegree, 1.5)
                self.assertLessEqual(average_outdegree, 2.0)
                self.assertGreaterEqual(snapshot.cross_gateway_edge_ratio, 0.60)
                self.assertLessEqual(snapshot.cross_gateway_edge_ratio, 0.70)

    def test_events_share_topology_but_keep_distinct_scenario_identity(self) -> None:
        first = generate_formation_snapshot(24, seed=5, event_id=0)
        replay = generate_formation_snapshot(24, seed=5, event_id=0)
        second_event = generate_formation_snapshot(24, seed=5, event_id=1)

        self.assertEqual(first.fingerprint, replay.fingerprint)
        self.assertEqual(first.topology.fingerprint, second_event.topology.fingerprint)
        self.assertEqual(first.task.biz_edges, second_event.task.biz_edges)
        self.assertEqual(first.agent_gateway_mapping, second_event.agent_gateway_mapping)
        self.assertNotEqual(first.event_fingerprint, second_event.event_fingerprint)

    async def test_generated_task_builds_through_existing_transaction_and_verifier(self) -> None:
        snapshot = generate_formation_snapshot(12, seed=3, event_id=0)
        controller, verifier, _provider = snapshot.instantiate()

        subnet, metrics = await controller.build_task_subnet(
            snapshot.task,
            verifier=verifier,
            run_id=1,
            seed=snapshot.seed,
        )

        self.assertTrue(metrics.networking_success, metrics.failure_reason)
        self.assertEqual(len(subnet.business_edges), len(snapshot.task.biz_edges))
        self.assertEqual(subnet.version, 1)


if __name__ == "__main__":
    unittest.main()
