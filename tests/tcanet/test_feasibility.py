"""Joint feasibility tests (paper Sec. III-A, Eq. 3)."""
from __future__ import annotations

import unittest
from dataclasses import replace

from src.tcanet.candidates import CandidateAction
from src.tcanet.feasibility import evaluate_joint_decision, project_state
from src.tcanet.spec import Layer
from tests.tcanet.common import built_subnet, mini_task, mini_world


def _switch_mode(dep_id: str, mode: str, overhead: float, loss: float):
    return CandidateAction(
        action_id=f"transport:SWITCH_MODE:{dep_id}:{mode}",
        layer=Layer.TRANSPORT,
        action="SWITCH_MODE",
        target=dep_id,
        read_fields=frozenset({f"transport:mode:{dep_id}"}),
        write_fields=frozenset({f"transport:mode:{dep_id}"}),
        parameters={
            "mode": mode,
            "overhead_factor": overhead,
            "loss_factor": loss,
        },
    )


def _adjust_rate(dep_id: str, rate: float):
    return CandidateAction(
        action_id=f"application:ADJUST_RATE:{dep_id}:{rate:g}",
        layer=Layer.APPLICATION,
        action="ADJUST_RATE",
        target=dep_id,
        read_fields=frozenset({f"application:rate:{dep_id}"}),
        write_fields=frozenset({f"application:rate:{dep_id}"}),
        parameters={"rate_mbps": rate},
    )


def _reroute(dep_id: str, path):
    return CandidateAction(
        action_id=f"network:REROUTE:{dep_id}:{'|'.join(path)}",
        layer=Layer.NETWORK,
        action="REROUTE",
        target=dep_id,
        read_fields=frozenset({f"network:path:{dep_id}"}),
        write_fields=frozenset({f"network:path:{dep_id}"}),
        parameters={"gateway_path": tuple(path)},
    )


def _rebind(dep_id: str, role: str, agent_id: str):
    return CandidateAction(
        action_id=f"physical:REBIND:{dep_id}:{agent_id}",
        layer=Layer.PHYSICAL,
        action="REBIND_SUPPORT",
        target=dep_id,
        read_fields=frozenset({f"binding:{role}:{dep_id}"}),
        write_fields=frozenset({f"binding:{role}:{dep_id}"}),
        parameters={"role": role, "agent_id": agent_id},
    )


class Eq3TransportOverheadTests(unittest.TestCase):
    """The paper's example: 25 Mbps x 1.2 overhead = 30 > 28 available."""

    def setUp(self) -> None:
        self.world = mini_world()
        self.task = mini_task(self.world, d1_demand=25.0)
        self.subnet = built_subnet(self.task, self.world)
        # d1 uses the direct LC path? mini solver picks GA->GB->GC (10ms)
        # or GA->GC (15ms): delay metric prefers LA+LB, so d1 rides LA/LB.

    def test_reliable_mode_breaks_shared_resource(self) -> None:
        result = evaluate_joint_decision(
            self.task, self.world, self.subnet,
            (_switch_mode("d1", "reliable", 1.2, 0.5),),
        )
        self.assertFalse(result.feasible)
        self.assertIn("shared_resource:link:LA", result.violations)

    def test_standard_mode_fits(self) -> None:
        result = evaluate_joint_decision(self.task, self.world, self.subnet, ())
        self.assertTrue(result.feasible, result.violations)

    def test_protected_load_is_deducted(self) -> None:
        projection = project_state(self.task, self.world, self.subnet, ())
        self.assertAlmostEqual(
            projection.resource_capacity_mbps["link:LA"], 28.0
        )
        # 25 Mbps task load on LA leaves 3 Mbps residual.
        self.assertAlmostEqual(projection.resource_load_mbps["link:LA"], 25.0)

    def test_reroute_onto_constrained_link_violates_eq3(self) -> None:
        # LC only has 20 Mbps capacity: 25 Mbps cannot move there.
        result = evaluate_joint_decision(
            self.task, self.world, self.subnet,
            (_reroute("d1", ("GA", "GC")),),
        )
        self.assertIn("shared_resource:link:LC", result.violations)


class CompatibilityTests(unittest.TestCase):
    def test_write_conflict_detected(self) -> None:
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        result = evaluate_joint_decision(
            task, world, subnet,
            (_adjust_rate("d1", 5.0), _adjust_rate("d1", 7.0)),
        )
        self.assertFalse(result.feasible)
        self.assertTrue(
            any(v.startswith("write_conflict:") for v in result.violations)
        )

    def test_rebind_to_unavailable_agent_rejected(self) -> None:
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        world.support_agents["physical-GC"] = replace(
            world.support_agents["physical-GC"], online=False
        )
        result = evaluate_joint_decision(
            task, world, subnet, (_rebind("d1", "p", "physical-GC"),)
        )
        self.assertFalse(result.feasible)
        self.assertTrue(
            any(v.startswith("executor_unavailable:") for v in result.violations)
        )

    def test_offline_binding_requires_rebind(self) -> None:
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        bound = subnet.bindings.binding("d1").p_agent_id
        world.support_agents[bound] = replace(
            world.support_agents[bound], online=False
        )
        no_fix = evaluate_joint_decision(task, world, subnet, ())
        self.assertTrue(
            any(v.startswith("binding_offline:d1") for v in no_fix.violations)
        )
        alt = next(
            agent_id
            for agent_id in world.support_agents
            if agent_id.startswith("physical-") and agent_id != bound
        )
        fixed = evaluate_joint_decision(
            task, world, subnet, (_rebind("d1", "p", alt),)
        )
        self.assertFalse(
            any(v.startswith("binding_offline:d1") for v in fixed.violations)
        )


class HardRequirementTests(unittest.TestCase):
    def test_throughput_hard_violation(self) -> None:
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        result = evaluate_joint_decision(
            task, world, subnet, (_adjust_rate("d1", 3.0),)
        )
        self.assertIn("throughput_hard:d1", result.violations)

    def test_projection_applies_overhead_and_bottleneck(self) -> None:
        world = mini_world()
        task = mini_task(world, d1_demand=25.0)
        subnet = built_subnet(task, world)
        projection = project_state(
            task, world, subnet,
            (_switch_mode("d1", "reliable", 1.2, 0.5),),
        )
        item = projection.dependencies["d1"]
        self.assertAlmostEqual(item.effective_demand_mbps, 30.0)
        self.assertAlmostEqual(item.delivered_mbps, min(30.0, 28.0))


if __name__ == "__main__":
    unittest.main()
