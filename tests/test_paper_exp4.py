from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from experiments.exp4_failure import run_exp4
from experiments.paper_protocol import EXPERIMENT_METHODS
from src.controller.paper_failure_recovery import run_paper_failure_method
from src.simulation.paper_failure_scenarios import generate_paper_failure_snapshot


class PaperFailureScenarioTests(unittest.TestCase):
    def test_shared_failure_base_has_final_task_and_topology_size(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "agent_failure",
            1.0,
            seed=3,
            event_id=0,
        )

        self.assertEqual(len(snapshot.task.app_agents), 24)
        self.assertEqual(len(snapshot.task.biz_edges), 36)
        self.assertEqual(len(snapshot.catalog.gateways), 12)

    def test_agent_failure_targets_active_business_agent_with_replacements(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "agent_failure",
            1.0,
            seed=3,
            event_id=0,
        )
        app_specs = {
            item.agent_id: item
            for item in snapshot.catalog.agents
            if item.agent_id in snapshot.task.app_agents
        }

        self.assertIn(snapshot.fault_target, snapshot.task.app_agents)
        self.assertIn(snapshot.fault_target, app_specs)
        self.assertGreaterEqual(len(snapshot.compatible_replacements), 1)
        self.assertLessEqual(len(snapshot.compatible_replacements), 3)
        self.assertTrue(
            all(
                replacement not in snapshot.task.app_agents
                for replacement in snapshot.compatible_replacements
            )
        )

    def test_link_failure_is_on_active_traffic_and_has_an_alternate_path(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "link_failure",
            1.0,
            seed=4,
            event_id=0,
        )

        self.assertTrue(snapshot.failed_link_in_active_task_path)
        self.assertTrue(snapshot.alternate_path_available)
        self.assertNotIn(
            snapshot.failed_link,
            set(zip(snapshot.backup_path, snapshot.backup_path[1:])),
        )

    def test_capacity_severity_is_a_reduction_percentage(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "capacity_degradation",
            0.30,
            seed=5,
            event_id=1,
        )

        self.assertAlmostEqual(snapshot.failure_severity, 0.30)
        self.assertAlmostEqual(
            snapshot.post_capacity_mbps,
            snapshot.pre_capacity_mbps * 0.70,
        )


class PaperFailureStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_failure_exposes_network_only_capability_boundary(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "agent_failure",
            1.0,
            seed=2,
            event_id=0,
        )

        proposed = await run_paper_failure_method(snapshot, "proposed")
        netkeeper = await run_paper_failure_method(snapshot, "netkeeper")
        cspf = await run_paper_failure_method(snapshot, "cspf")
        full = await run_paper_failure_method(snapshot, "full_rebuild")

        self.assertTrue(proposed.success, proposed.failure_reason)
        self.assertTrue(full.success, full.failure_reason)
        self.assertFalse(netkeeper.success)
        self.assertFalse(cspf.success)
        self.assertNotIn("application", netkeeper.selected_layers)
        self.assertNotIn("application", cspf.selected_layers)

    async def test_cspf_is_valid_and_competitive_for_link_failure(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "link_failure",
            1.0,
            seed=1,
            event_id=0,
        )

        proposed = await run_paper_failure_method(snapshot, "proposed")
        cspf = await run_paper_failure_method(snapshot, "cspf")

        self.assertTrue(proposed.success, proposed.failure_reason)
        self.assertTrue(cspf.success, cspf.failure_reason)
        self.assertLessEqual(cspf.recovery_latency_ms, proposed.recovery_latency_ms)
        self.assertEqual(cspf.selected_layers, frozenset({"network"}))

    async def test_netkeeper_actions_remain_network_only(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "capacity_degradation",
            0.30,
            seed=0,
            event_id=0,
        )

        outcome = await run_paper_failure_method(snapshot, "netkeeper")

        self.assertTrue(outcome.selected_layers.issubset({"network"}))
        self.assertTrue(
            outcome.selected_actions.issubset(
                {"SWITCH_ROUTE", "RESERVE_BANDWIDTH", "REBALANCE_TRAFFIC"}
            )
        )

    async def test_cross_layer_methods_complete_capacity_scope_escalation(self) -> None:
        for reduction in (0.10, 0.20, 0.30, 0.40, 0.50):
            with self.subTest(reduction=reduction):
                snapshot = generate_paper_failure_snapshot(
                    "capacity_degradation",
                    reduction,
                    seed=0,
                    event_id=0,
                )
                proposed = await run_paper_failure_method(snapshot, "proposed")
                full = await run_paper_failure_method(snapshot, "full_rebuild")

                self.assertTrue(proposed.success, proposed.failure_reason)
                self.assertTrue(full.success, full.failure_reason)


class PaperExp4RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_grid_writes_complete_paired_final_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = await run_exp4(
                "pilot",
                root,
                seeds=(0,),
                event_ids=(0,),
                capacity_reductions=(10,),
            )

            self.assertEqual(len(rows), (3 + 1) * 4)
            grouped = {}
            for row in rows:
                grouped.setdefault(row.trial_id, set()).add(row.method_id)
                self.assertEqual(row.task_size, 24)
                self.assertEqual(row.num_dag_edges, 36)
                self.assertEqual(row.num_gateways, 12)
            self.assertTrue(grouped)
            self.assertTrue(
                all(methods == set(EXPERIMENT_METHODS["exp4"]) for methods in grouped.values())
            )
            self.assertTrue((root / "raw" / "pilot" / "exp4" / "trials.csv").exists())


if __name__ == "__main__":
    unittest.main()
