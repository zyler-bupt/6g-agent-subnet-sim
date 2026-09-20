"""Modification-scope accounting and scenario recovery tests (paper Eq. 10)."""
from __future__ import annotations

import asyncio
import unittest

from src.tcanet.metrics import (
    MetricSummary,
    TrialRecord,
    load_factor,
    modification_stats,
    summarize,
)
from src.tcanet.scenario import (
    E1_DEMAND_HIGH,
    build_world,
    rescue_task,
    run_coordination,
    run_formation,
    run_gateway_recovery,
    run_support_recovery,
    with_demand,
)


class ModificationStatsTests(unittest.TestCase):
    def test_parameter_only_decision_has_zero_mod(self) -> None:
        world = build_world()
        task = rescue_task(world)
        formation = asyncio.run(run_formation(task, world))
        high = with_demand(task, "e1", E1_DEMAND_HIGH)
        result = run_coordination(high, world, formation.subnet)
        self.assertTrue(result.recovered)
        stats = modification_stats(formation.subnet, result.subnet)
        self.assertEqual(stats.changed, 0)
        self.assertEqual(stats.ratio, 0.0)

    def test_reroute_counts_replacement_once(self) -> None:
        world = build_world()
        task = rescue_task(world)
        formation = asyncio.run(run_formation(task, world))
        high = with_demand(task, "e1", E1_DEMAND_HIGH)
        coord = run_coordination(high, world, formation.subnet)
        result = run_gateway_recovery(high, world, coord.subnet)
        self.assertTrue(result.recovered)
        stats = modification_stats(coord.subnet, result.subnet)
        # One path record + FT records at (e1,G2)->(e1,G3) pairs.
        self.assertEqual(stats.changed_paths, 1)
        self.assertEqual(stats.changed_forwarding, 3)
        self.assertEqual(stats.changed_bindings, 0)
        # installed = 4 paths + 8 FT pairs + 4 bindings.
        self.assertEqual(stats.installed, 16)
        self.assertAlmostEqual(stats.ratio, 4.0 / 16.0)

    def test_binding_records_counted_per_dependency(self) -> None:
        world = build_world()
        task = rescue_task(world)
        formation = asyncio.run(run_formation(task, world))
        high = with_demand(task, "e1", E1_DEMAND_HIGH)
        coord = run_coordination(high, world, formation.subnet)
        gw = run_gateway_recovery(high, world, coord.subnet)
        result = run_support_recovery(high, world, gw.subnet, demo_retry=False)
        self.assertTrue(result.recovered)
        stats = modification_stats(gw.subnet, result.subnet)
        self.assertEqual(stats.changed_bindings, 3)
        self.assertEqual(stats.changed_paths, 0)


class ScenarioEpisodesTests(unittest.TestCase):
    def test_full_scenario_version_timeline(self) -> None:
        world = build_world()
        task = rescue_task(world)
        formation = asyncio.run(run_formation(task, world))
        self.assertEqual(formation.subnet.version, 1)
        high = with_demand(task, "e1", E1_DEMAND_HIGH)

        coord = run_coordination(high, world, formation.subnet)
        self.assertTrue(coord.recovered)
        self.assertEqual(coord.subnet.version, 2)
        # Cross-layer coordination keeps the rate and avoids Eq.3 overflow.
        self.assertIn("SWITCH_MODE", coord.attempts[-1].selected_label)

        gw = run_gateway_recovery(high, world, coord.subnet)
        self.assertTrue(gw.recovered)
        self.assertEqual(gw.subnet.version, 3)
        self.assertEqual(
            gw.subnet.paths["e1"].gateway_path, ("G1", "G3", "G4")
        )

        support = run_support_recovery(high, world, gw.subnet, demo_retry=True)
        self.assertTrue(support.recovered)
        self.assertEqual(support.subnet.version, 4)
        # The B_r loop retried after the first window rejected.
        self.assertEqual(len(support.attempts), 2)
        self.assertEqual(support.attempts[0].outcome, "window_rejected")
        self.assertEqual(support.attempts[1].outcome, "accepted")
        for record in support.subnet.bindings.records.values():
            self.assertNotEqual(record.p_agent_id, "physical-G4")

    def test_gateway_failure_takes_agents_down(self) -> None:
        world = build_world()
        task = rescue_task(world)
        self.assertTrue(all(a.online for a in world.support_agents.values()))
        from src.tcanet.scenario import fail_gateway

        fail_gateway(world, "G2")
        self.assertFalse(world.gateway_online("G2"))
        for agent_id, agent in world.support_agents.items():
            if agent.gateway_id == "G2":
                self.assertFalse(agent.online)
            else:
                self.assertTrue(agent.online)


class MetricSummaryTests(unittest.TestCase):
    def test_recovery_latency_only_successful_trials(self) -> None:
        trials = (
            TrialRecord("t", True, 100.0),
            TrialRecord("t", False, None),
            TrialRecord("t", True, 200.0),
        )
        summary = summarize(trials, modification_ratios=(0.25, 0.5))
        self.assertEqual(summary.success_rate, 2.0 / 3.0)
        self.assertEqual(summary.mean_recovery_latency_ms, 150.0)
        self.assertAlmostEqual(summary.mean_modification_ratio, 0.375)

    def test_empty_summary(self) -> None:
        summary = summarize(())
        self.assertEqual(summary.success_rate, 0.0)
        self.assertIsNone(summary.mean_recovery_latency_ms)

    def test_load_factor_reference(self) -> None:
        self.assertAlmostEqual(load_factor(6.5, 10.0), 0.65)
        self.assertEqual(load_factor(1.0, 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
