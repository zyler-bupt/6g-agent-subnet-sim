from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from experiments.exp4_failure import run_exp4
from experiments.paper_protocol import EXPERIMENT_FAILURE_METHODS, EXPERIMENT_METHODS
from scripts.sanity_check_results import check_results
from src.controller.paper_failure_recovery import (
    PaperFailureVerifier,
    PaperSfcRestorationStrategy,
    run_paper_failure_method,
)
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

    def test_capacity_sweep_keeps_the_same_fault_target_for_a_seed(self) -> None:
        snapshots = [
            generate_paper_failure_snapshot(
                "capacity_degradation", 1.0 - ratio,
                seed=7, event_id=0, capacity_ratio=ratio,
            )
            for ratio in (1.1, 1.0, 0.9, 0.75, 0.6)
        ]

        self.assertEqual(len({item.topology_fingerprint for item in snapshots}), 1)
        self.assertEqual(len({item.qos_fingerprint for item in snapshots}), 1)
        self.assertEqual(len({item.base.target_edge_id for item in snapshots}), 1)
        self.assertEqual(len({item.failed_link for item in snapshots}), 1)


class PaperFailureStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_failure_verification_does_not_invent_capacity_failure(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "agent_failure",
            1.0,
            seed=0,
            event_id=0,
        )
        controller, formation_verifier, _ = snapshot.instantiate()
        stable, formation = await controller.build_task_subnet(
            snapshot.task,
            verifier=formation_verifier,
            run_id=0,
            seed=0,
        )
        self.assertTrue(formation.networking_success, formation.failure_reason)
        event, context = snapshot.apply_fault(controller, stable)
        event = replace(event, occurred_at=0.0)
        planning = await PaperSfcRestorationStrategy().plan(
            controller,
            stable,
            event,
            context,
        )
        self.assertIsNotNone(planning.plan)

        result = await PaperFailureVerifier(snapshot, stable, context).verify(
            planning.plan.target_state
        )

        errors = [item.error for item in result.edge_results if not item.ok]
        self.assertNotIn("post_failure_capacity_exceeded", errors)
        self.assertTrue(result.ok, errors)

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

    async def test_agent_recovery_records_changed_agent_and_path_objects(self) -> None:
        snapshot = generate_paper_failure_snapshot(
            "agent_failure",
            1.0,
            seed=2,
            event_id=0,
        )

        proposed = await run_paper_failure_method(snapshot, "proposed")

        self.assertTrue(proposed.success, proposed.failure_reason)
        self.assertGreaterEqual(proposed.changed_agents, 1)
        self.assertGreaterEqual(proposed.changed_paths, 1)
        expected = (
            proposed.changed_rules
            + proposed.changed_paths
            + proposed.changed_agents
        ) / (
            proposed.total_rule_objects + proposed.total_paths + proposed.total_agents
        )
        self.assertEqual(proposed.modification_scope_ratio, expected)

    async def test_full_rebuild_scope_covers_each_main_failure_recovery(self) -> None:
        for failure_type, severity in (
            ("agent_failure", 1.0),
            ("link_failure", 1.0),
            ("capacity_degradation", 0.30),
        ):
            with self.subTest(failure_type=failure_type):
                snapshot = generate_paper_failure_snapshot(
                    failure_type,
                    severity,
                    seed=0,
                    event_id=0,
                )

                proposed = await run_paper_failure_method(snapshot, "proposed")
                full = await run_paper_failure_method(snapshot, "full_rebuild")

                self.assertTrue(proposed.success, proposed.failure_reason)
                self.assertTrue(full.success, full.failure_reason)
                self.assertEqual(full.changed_paths, full.total_paths)
                self.assertEqual(full.changed_agents, full.total_agents)
                self.assertAlmostEqual(full.rule_change_ratio, 1.0)
                self.assertLessEqual(full.modification_scope_ratio, 1.0)
                self.assertGreaterEqual(
                    full.modification_scope_ratio,
                    proposed.modification_scope_ratio,
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
    async def test_rows_carry_failure_modification_scope_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = await run_exp4(
                "pilot",
                Path(directory),
                seeds=(0,),
                event_ids=(0,),
                capacity_reductions=(10,),
            )

        successful_rows = [row for row in rows if row.success]
        self.assertTrue(successful_rows)
        self.assertTrue(
            all(
                row.total_paths is not None
                and row.changed_paths is not None
                and row.total_agents is not None
                and row.changed_agents is not None
                and row.modification_scope_ratio is not None
                for row in successful_rows
            )
        )

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

            # Frozen Exp4 (v3): each failure type is compared against its OWN
            # method set (never a common list). With seeds={0}, events={0},
            # capacity_reductions={10}: 3 main failure points + 1 capacity
            # stress point, each evaluated with its per-fault method set.
            expected = (
                len(EXPERIMENT_FAILURE_METHODS["exp4"]["link_failure"])
                + len(EXPERIMENT_FAILURE_METHODS["exp4"]["agent_failure"])
                + len(EXPERIMENT_FAILURE_METHODS["exp4"]["capacity_degradation"]) * 2
            )
            self.assertEqual(len(rows), expected)
            grouped = {}
            for row in rows:
                grouped.setdefault(row.trial_id, set()).add(row.method_id)
                self.assertEqual(row.task_size, 24)
                self.assertEqual(row.num_dag_edges, 36)
                self.assertEqual(row.num_gateways, 12)
            self.assertTrue(grouped)
            # Every trial's method set must match its own failure type's set.
            for trial_id, methods in grouped.items():
                ft = next(
                    row.failure_type
                    for row in rows
                    if row.trial_id == trial_id
                )
                self.assertEqual(
                    methods,
                    set(EXPERIMENT_FAILURE_METHODS["exp4"][ft]),
                    msg=f"trial {trial_id} (ft={ft}) used wrong method set",
                )
            self.assertTrue((root / "raw" / "pilot" / "exp4" / "trials.csv").exists())

            findings = check_results(rows, experiment="exp4")
            incomplete = [
                item for item in findings if item.code == "INCOMPLETE_METHOD_PAIR"
            ]
            self.assertEqual(incomplete, [])


if __name__ == "__main__":
    unittest.main()
