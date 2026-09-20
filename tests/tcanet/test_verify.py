"""Verification window ``W_m`` and bounded recovery tests (paper Sec. IV-C)."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.tcanet.closure import link_failure
from src.tcanet.executor import execute_staged, stage_decision
from src.tcanet.spec import GatewayLink
from src.tcanet.verify import (
    DepObservation,
    RecoveryController,
    evaluate_window,
    projected_measurements,
)
from tests.tcanet.common import built_subnet, mini_task, mini_world


def _good_obs(dep_id: str, at_ms: float = 50.0) -> DepObservation:
    return DepObservation(
        dep_id=dep_id, observed_at_ms=at_ms,
        throughput_mbps=10.0, delay_ms=12.0, loss_rate=0.002,
    )


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = mini_world()
        self.task = mini_task(self.world)
        self.subnet = built_subnet(self.task, self.world)

    def _staged(self, actions=()):
        staged = stage_decision(self.task, self.subnet, actions)
        execution = asyncio.run(execute_staged(staged, self.world))
        return staged, execution

    def test_observed_and_satisfied_accepts(self) -> None:
        staged, execution = self._staged()
        result = evaluate_window(
            self.task, self.world, staged, execution, ("d1",),
            (_good_obs("d1"),),
        )
        self.assertTrue(result.accepted)
        kinds = [event.kind for event in result.timeline]
        self.assertIn("installed", kinds)
        self.assertIn("observed", kinds)

    def test_missing_observation_is_pending_then_expired(self) -> None:
        staged, execution = self._staged()
        result = evaluate_window(
            self.task, self.world, staged, execution, ("d1",), ()
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.pending, ("d1",))
        self.assertTrue(
            any(event.kind == "expired" for event in result.timeline)
        )

    def test_late_observation_counts_as_missing(self) -> None:
        staged, execution = self._staged()
        result = evaluate_window(
            self.task, self.world, staged, execution, ("d1",),
            (_good_obs("d1", at_ms=5000.0),),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.pending, ("d1",))

    def test_violating_measurement_rejects(self) -> None:
        staged, execution = self._staged()
        bad = DepObservation(
            dep_id="d1", observed_at_ms=50.0,
            throughput_mbps=10.0, delay_ms=999.0, loss_rate=0.002,
        )
        result = evaluate_window(
            self.task, self.world, staged, execution, ("d1",), (bad,)
        )
        self.assertFalse(result.accepted)
        self.assertIn("delay_hard:d1", result.violations)

    def test_failed_execution_rejects_immediately(self) -> None:
        staged, execution = self._staged()
        failed = replace(execution, ok=False)
        result = evaluate_window(
            self.task, self.world, staged, failed, ("d1",), (_good_obs("d1"),)
        )
        self.assertFalse(result.accepted)
        self.assertTrue(
            any(v.startswith("installation_failed") for v in result.violations)
        )

    def test_reroute_withdraws_superseded_rule(self) -> None:
        from tests.tcanet.test_feasibility import _reroute

        staged, execution = self._staged(
            (_reroute("d1", ("GA", "GB", "GC")),)
        )
        # Same gateway pair set -> no superseded rules on this detour.
        self.assertEqual(staged.superseded_rules, frozenset())
        direct = stage_decision(
            self.task, self.subnet,
            (_reroute("d1", ("GA", "GC")),),
        )
        self.assertTrue(direct.superseded_rules)


class RecoveryControllerTests(unittest.TestCase):
    def _broken_world(self):
        """mini world with the direct LC link torn down after building."""
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        links = tuple(
            replace(link, up=False) if link.link_id == "LA" else link
            for link in world.graph.links
        )
        world.graph = replace(world.graph, links=links)
        return task, world, subnet

    def test_recovery_succeeds_on_alternate_path(self) -> None:
        task, world, subnet = self._broken_world()
        controller = RecoveryController()
        result = asyncio.run(
            controller.recover(task, subnet, world, link_failure("LA"))
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(result.subnet.version, subnet.version + 1)
        self.assertTrue(result.subnet.accepted)
        self.assertEqual(
            result.subnet.paths["d1"].gateway_path, ("GA", "GC")
        )

    def test_bounded_attempts_exhausted(self) -> None:
        task, world, subnet = self._broken_world()

        def always_late(staged, world, dep_ids):
            return tuple(
                DepObservation(dep_id=dep_id, observed_at_ms=99999.0)
                for dep_id in dep_ids
            )

        controller = RecoveryController(max_attempts=2, measure=always_late)
        result = asyncio.run(
            controller.recover(task, subnet, world, link_failure("LA"))
        )
        self.assertFalse(result.recovered)
        self.assertEqual(len(result.attempts), 2)
        # The first attempt selected the only alternate and was excluded;
        # the second found no remaining feasible alternative.
        self.assertEqual(result.attempts[0].outcome, "window_expired_pending")
        self.assertTrue(result.attempts[0].excluded_after)
        self.assertEqual(result.attempts[1].outcome, "no_feasible_decision")

    def test_no_feasible_alternative_reports_failure(self) -> None:
        """Both path options dead -> recovery fails without installing."""
        world = mini_world()
        task = mini_task(world)
        subnet = built_subnet(task, world)
        links = tuple(
            replace(link, up=False) for link in world.graph.links
        )
        world.graph = replace(world.graph, links=links)
        controller = RecoveryController(max_attempts=3)
        result = asyncio.run(
            controller.recover(task, subnet, world, link_failure("LA"))
        )
        self.assertFalse(result.recovered)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].outcome, "no_feasible_decision")
        self.assertIsNone(result.subnet)
        self.assertIsNone(result.recovery_latency_ms)


if __name__ == "__main__":
    unittest.main()
