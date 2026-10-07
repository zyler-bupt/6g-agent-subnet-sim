"""paper_fig1 scenario: duplex access, alternates, scoped recovery."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.tcanet.candidates import coordination_candidates
from src.tcanet.closure import demand_change, gateway_failure, support_failure
from src.tcanet.executor import stage_decision
from src.tcanet.feasibility import evaluate_joint_decision
from src.tcanet.scenario import fail_gateway, fail_support_agent, run_formation, with_demand
from src.tcanet.scenario_fig1 import build_world, fig1_task, load
from src.tcanet.verify import RecoveryController, observe_state, projected_measurements
from tests.tcanet.common import mini_world


def _formed():
    world = build_world()
    task = fig1_task(world)
    subnet = asyncio.run(run_formation(task, world)).subnet
    return world, task, subnet


def _l3_lossy(task):
    """Measurement source reporting 10% loss on any path through L3."""

    def measure(staged, world, dep_ids):
        observations = projected_measurements(staged, world, dep_ids, task=task)
        return tuple(
            replace(obs, loss_rate=0.10)
            if "L3" in staged.subnet.paths[obs.dep_id].link_ids
            else obs
            for obs in observations
        )

    return measure


class Fig1ScenarioTests(unittest.TestCase):
    def test_load_returns_fresh_world_and_task(self) -> None:
        world, task = load("paper_fig1")
        self.assertEqual(world.access_model, "duplex")
        self.assertEqual(
            [dep.dep_id for dep in task.dag.dependencies], ["e1", "e2", "e3"]
        )
        with self.assertRaises(KeyError):
            load("nope")

    def test_formation_paths_and_forwarding(self) -> None:
        _world, _task, subnet = _formed()
        self.assertEqual(subnet.paths["e1"].gateway_path, ("G1", "G2", "G4"))
        self.assertEqual(subnet.paths["e2"].gateway_path, ("G3", "G4"))
        self.assertEqual(subnet.paths["e3"].gateway_path, ("G4", "G3"))
        self.assertEqual(len(subnet.forwarding), 7)

    def test_gateway_failure_scope_excludes_e3(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(set(result.closure.initial), {"e1"})
        self.assertEqual(result.closure.final, frozenset({"e1", "e2"}))
        # J ties between L6 and G3 detours; the smaller M (4 < 5) wins.
        self.assertEqual(result.subnet.paths["e1"].gateway_path, ("G1", "G3", "G4"))
        self.assertEqual(result.subnet.paths["e3"], subnet.paths["e3"])
        self.assertEqual(result.attempts[0].selection.selected.modification_scope, 4)

    def test_rejected_assess_falls_back_to_second_alternate(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        controller = RecoveryController(measure=_l3_lossy(task))
        result = asyncio.run(
            controller.recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(
            [attempt.outcome for attempt in result.attempts],
            ["window_rejected", "accepted"],
        )
        self.assertEqual(result.subnet.paths["e1"].gateway_path, ("G1", "G4"))

    def test_demand_increase_rejects_reliable_mode_on_l1(self) -> None:
        world, task, subnet = _formed()
        task = with_demand(task, "e1", 25.0)
        candidates = coordination_candidates(
            task, world, subnet, observe_state(task, world, subnet),
            focus_dep_ids=("e1",),
        )
        reliable = next(
            action
            for actions in candidates.values()
            for action in actions
            if action.action_id == "transport:SWITCH_MODE:e1:reliable"
        )
        result = evaluate_joint_decision(task, world, subnet, (reliable,))
        self.assertFalse(result.feasible)
        self.assertIn("shared_resource:link:L1", result.violations)  # 25x1.2=30>28

    def test_demand_change_recovers(self) -> None:
        world, task, subnet = _formed()
        task = with_demand(task, "e1", 25.0)
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, demand_change("e1", 25 / 15))
        )
        self.assertTrue(result.recovered, result.error)

    def test_support_failure_rebinds_only_e3(self) -> None:
        world, task, subnet = _formed()
        fail_support_agent(world, "physical-G4")
        result = asyncio.run(
            RecoveryController().recover(
                task, subnet, world, support_failure("physical-G4")
            )
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(result.closure.final, frozenset({"e3"}))
        self.assertNotEqual(
            result.subnet.bindings.binding("e3").p_agent_id, "physical-G4"
        )
        self.assertEqual(result.subnet.paths, subnet.paths)

    def test_rerouted_path_keeps_link_ids_for_next_episode(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertEqual(result.subnet.paths["e1"].link_ids, ("L3", "L5"))
        staged = stage_decision(task, subnet, (), world)
        self.assertEqual(staged.subnet.paths["e1"].link_ids, ("L1", "L2"))

    def test_shared_access_model_is_default(self) -> None:
        world = mini_world()
        self.assertEqual(
            world.access_resource_ids("GA", "GC"), ("access:GA", "access:GC")
        )


if __name__ == "__main__":
    unittest.main()
