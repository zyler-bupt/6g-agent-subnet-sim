"""Affected-set closure tests (paper Eq. 8)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from src.tcanet.closure import (
    demand_change,
    dependency_relations,
    endpoint_failure,
    expand_affected_set,
    gateway_failure,
    initial_affected_set,
    link_failure,
    qos_change,
    support_failure,
)
from src.tcanet.spec import Dependency, Endpoint, TaskDAG
from tests.tcanet.common import built_subnet, mini_task, mini_world


class InitialAffectedSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = mini_world()
        self.task = mini_task(self.world)
        self.subnet = built_subnet(self.task, self.world)

    def test_demand_change(self) -> None:
        affected = initial_affected_set(
            self.task, self.subnet, self.world, demand_change("d1", 1.5)
        )
        self.assertEqual(affected, {"d1": "demand change"})

    def test_qos_change(self) -> None:
        affected = initial_affected_set(
            self.task, self.subnet, self.world, qos_change("d1")
        )
        self.assertEqual(affected, {"d1": "QoS change"})

    def test_endpoint_failure(self) -> None:
        affected = initial_affected_set(
            self.task, self.subnet, self.world, endpoint_failure("a")
        )
        self.assertEqual(affected, {"d1": "failed endpoint"})

    def test_link_failure(self) -> None:
        # d1 rides LA+LB (GA->GB->GC, 10ms), so failing LA or LB hits it
        # and failing the direct LC does not.
        affected = initial_affected_set(
            self.task, self.subnet, self.world, link_failure("LC")
        )
        self.assertEqual(affected, {})
        affected = initial_affected_set(
            self.task, self.subnet, self.world, link_failure("LB")
        )
        self.assertEqual(affected, {"d1": "path traverses failed link"})

    def test_gateway_failure_catches_paths_and_bindings(self) -> None:
        # GB is on d1's path AND hosts d1's rebound network agent.
        binding = self.subnet.bindings.binding("d1")
        rebound = replace(binding, n_agent_id="network-GB")
        records = dict(self.subnet.bindings.records)
        records["d1"] = rebound
        subnet = replace(self.subnet, bindings=type(self.subnet.bindings)(records))
        affected = initial_affected_set(
            self.task, subnet, self.world, gateway_failure("GB")
        )
        self.assertEqual(set(affected), {"d1"})

    def test_support_failure(self) -> None:
        agent_id = self.subnet.bindings.binding("d1").p_agent_id
        affected = initial_affected_set(
            self.task, self.subnet, self.world, support_failure(agent_id)
        )
        self.assertEqual(affected, {"d1": "failed supporting agent"})


class DependencyRelationTests(unittest.TestCase):
    def _task_with_three_deps(self, world):
        a = Endpoint("a", "a", "GA")
        b = Endpoint("b", "b", "GB")
        c = Endpoint("c", "c", "GC")
        deps = (
            Dependency("d1", "a", "b", "flow", 5.0),
            Dependency("d2", "b", "c", "flow", 5.0),
            Dependency("d3", "a", "c", "flow", 5.0),
        )
        dag = TaskDAG("chain", "chain", (a, b, c), deps)
        world.endpoints = {ep.agent_id: ep for ep in dag.endpoints}
        from src.tcanet.spec import TaskSpecification, HardRequirements

        return TaskSpecification(
            dag=dag,
            hard=HardRequirements(
                min_throughput_mbps=1.0, max_delay_ms=100.0, max_loss_rate=0.2
            ),
        )

    def test_shared_link_pulls_distant_dag_edges(self) -> None:
        """d1 and d3 are non-adjacent in the DAG but share link LA."""
        world = mini_world()
        task = self._task_with_three_deps(world)
        subnet = built_subnet(task, world)
        # CSPF sends d3 over LA+LB as well (10ms beats LC's 15ms).
        self.assertEqual(
            subnet.paths["d3"].gateway_path, ("GA", "GB", "GC")
        )
        relations = dependency_relations(task, subnet)
        self.assertIn(("d1", "d3"), relations.resource_pairs)

        result = expand_affected_set({"d3": "demand change"}, relations)
        # d3 pulls d1 (shared LA / access GA) and d2 (shared LB / access GC).
        self.assertEqual(result.final, {"d1", "d2", "d3"})
        self.assertTrue(result.expanded_beyond_initial)
        self.assertEqual(len(result.rounds), 1)

    def test_fixpoint_terminates(self) -> None:
        world = mini_world()
        task = self._task_with_three_deps(world)
        subnet = built_subnet(task, world)
        relations = dependency_relations(task, subnet)
        result = expand_affected_set({"d2": "x"}, relations)
        self.assertEqual(result.final, {"d1", "d2", "d3"})

    def test_shared_binding_is_config_dependency(self) -> None:
        world = mini_world()
        task = self._task_with_three_deps(world)
        subnet = built_subnet(task, world)
        relations = dependency_relations(task, subnet)
        # d1 and d3 both bind the agents at GA -> D^cfg pair (they also
        # share GB's forwarding state; either reason is a valid D^cfg edge).
        self.assertIn(("d1", "d3"), relations.config_pairs)
        self.assertIn(
            relations.config_pairs.get(("d1", "d3")),
            {
                "shared gateway forwarding state",
                "shared supporting-agent binding",
            },
        )


if __name__ == "__main__":
    unittest.main()
