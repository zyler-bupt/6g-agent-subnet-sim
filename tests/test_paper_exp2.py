from __future__ import annotations

import inspect
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from experiments.exp2_conflict import run_exp2
from experiments.paper_protocol import EXPERIMENT_METHODS, METHODS
from scripts.aggregate_results import aggregate_experiment
from src.controller.paper_cross_layer_coordination import coordinate_paper
from src.metrics.paper import PaperTrial
from src.simulation.paper_conflicts import (
    PaperConflictType,
    generate_conflict_snapshot,
    solve_paper_feasibility_oracle,
)


class ConflictGeneratorTests(unittest.TestCase):
    def test_conflict_density_counts_business_edges(self) -> None:
        snapshot = generate_conflict_snapshot(40, seed=2, event_id=0)

        self.assertEqual(len(snapshot.task.app_agents), 20)
        self.assertGreaterEqual(len(snapshot.task.biz_edges), 28)
        self.assertLessEqual(len(snapshot.task.biz_edges), 32)
        self.assertEqual(len(snapshot.catalog.gateways), 10)
        self.assertEqual(
            len(snapshot.conflicted_edge_ids),
            round(0.40 * len(snapshot.task.biz_edges)),
        )

    def test_conflict_mix_contains_all_four_types_and_is_not_all_cascaded(self) -> None:
        kinds = []
        for event_id in range(20):
            snapshot = generate_conflict_snapshot(60, seed=4, event_id=event_id)
            kinds.extend(snapshot.conflict_types.values())

        counts = Counter(kinds)
        self.assertEqual(set(counts), set(PaperConflictType))
        self.assertLess(counts[PaperConflictType.CASCADED], sum(counts.values()))

    def test_oracle_has_no_method_parameter_and_matches_exact_action_space(self) -> None:
        self.assertNotIn(
            "method",
            inspect.signature(solve_paper_feasibility_oracle).parameters,
        )
        snapshot = generate_conflict_snapshot(50, seed=3, event_id=1)

        replay = solve_paper_feasibility_oracle(snapshot.state, snapshot.proposals)

        self.assertEqual(replay, snapshot.oracle)
        self.assertEqual(replay.candidate_combinations, 81)

    def test_pilot_truth_mix_is_between_85_and_90_percent_solvable(self) -> None:
        snapshots = [
            generate_conflict_snapshot(density, seed, event_id)
            for density in (0, 10, 20, 30, 40, 50, 60)
            for seed in range(5)
            for event_id in range(2)
        ]

        solvable_rate = sum(item.oracle.feasible for item in snapshots) / len(snapshots)
        self.assertGreaterEqual(solvable_rate, 0.85)
        self.assertLessEqual(solvable_rate, 0.90)


class PaperCoordinationTests(unittest.TestCase):
    def test_all_methods_receive_identical_candidate_action_fingerprint(self) -> None:
        snapshot = generate_conflict_snapshot(40, seed=6, event_id=0)

        fingerprints = {
            method_id: coordinate_paper(method_id, snapshot).proposal_fingerprint
            for method_id in EXPERIMENT_METHODS["exp2"]
        }

        self.assertEqual(len(set(fingerprints.values())), 1)

    def test_sanet_uses_dynamic_weights_without_hard_feasibility_arbitration(self) -> None:
        snapshot = generate_conflict_snapshot(40, seed=6, event_id=0)

        with patch(
            "src.controller.paper_cross_layer_coordination."
            "evaluate_cross_layer_combination",
            side_effect=AssertionError("SANet-DW* may not hard-filter candidates"),
        ):
            outcome = coordinate_paper("sanet_dw", snapshot)

        self.assertEqual(outcome.policy, "sanet_dynamic_weight_global_objective")
        self.assertAlmostEqual(sum(outcome.layer_weights.values()), 1.0)
        self.assertFalse(outcome.hard_arbitration_performed)

    def test_proposed_safely_rejects_or_commits_exactly_verified_action(self) -> None:
        for event_id in range(8):
            snapshot = generate_conflict_snapshot(60, seed=0, event_id=event_id)
            outcome = coordinate_paper("proposed", snapshot)
            if snapshot.oracle.feasible:
                self.assertTrue(outcome.success, outcome.failure_reason)
                self.assertTrue(outcome.qos_satisfied)
            else:
                self.assertFalse(outcome.success)
                self.assertTrue(outcome.safe_rejection)
                self.assertEqual(outcome.rollback_count, 0)

    def test_common_transaction_verifier_safely_rejects_infeasible_actions(self) -> None:
        snapshot = next(
            generate_conflict_snapshot(60, seed=seed, event_id=event_id)
            for seed in range(10)
            for event_id in range(5)
            if not generate_conflict_snapshot(60, seed=seed, event_id=event_id).oracle.feasible
        )

        for method_id in EXPERIMENT_METHODS["exp2"]:
            with self.subTest(method_id=method_id):
                outcome = coordinate_paper(method_id, snapshot)
                self.assertFalse(outcome.success)
                self.assertTrue(outcome.safe_rejection)


class PaperConflictMetricTests(unittest.TestCase):
    def test_conditional_metrics_use_exact_solvable_and_infeasible_denominators(self) -> None:
        rows = _conditional_fixture()
        with tempfile.TemporaryDirectory() as directory:
            summary = aggregate_experiment(
                rows,
                Path(directory) / "summary.csv",
                bootstrap_iterations=100,
            )

        by_metric = {item.metric: item for item in summary}
        fsr = by_metric["feasible_solution_rate_percent"]
        qsr = by_metric["qos_satisfaction_rate_percent"]
        srr = by_metric["safe_rejection_rate_percent"]
        self.assertEqual((fsr.numerator, fsr.denominator), (1, 2))
        self.assertEqual((qsr.numerator, qsr.denominator), (1, 2))
        self.assertEqual((srr.numerator, srr.denominator), (1, 1))


class PaperExp2RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_grid_writes_complete_paired_final_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = await run_exp2(
                "pilot",
                root,
                seeds=(0,),
                event_ids=(0,),
                conflict_densities=(0, 40),
            )

            self.assertEqual(len(rows), 2 * 4)
            grouped = {}
            for row in rows:
                grouped.setdefault(row.trial_id, set()).add(row.method_id)
                self.assertEqual(row.task_size, 20)
                self.assertGreaterEqual(row.num_dag_edges, 28)
                self.assertLessEqual(row.num_dag_edges, 32)
                self.assertEqual(row.num_gateways, 10)
                self.assertGreater(row.resolution_latency_ms, 0.0)
            self.assertTrue(
                all(
                    methods == set(EXPERIMENT_METHODS["exp2"])
                    for methods in grouped.values()
                )
            )
            self.assertTrue((root / "raw" / "pilot" / "exp2" / "trials.csv").exists())


def _conditional_fixture() -> list[dict[str, object]]:
    rows = []
    for event_id, feasible, success, qos, rejection in (
        (0, True, True, True, False),
        (1, True, False, False, False),
        (2, False, False, False, True),
    ):
        method = METHODS["proposed"]
        row = PaperTrial(
            experiment="exp2",
            mode="pilot",
            trial_id=f"fixture-{event_id}",
            seed=event_id,
            event_id=event_id,
            method_id=method.method_id,
            method_label=method.label,
            method_source=method.reference,
            adapted=method.adapted,
            topology_fingerprint="topology",
            scenario_fingerprint=f"scenario-{event_id}",
            qos_fingerprint="qos",
            event_fingerprint=f"event-{event_id}",
            series="conflict_density",
            conflict_density=0.40,
            conflict_type="mixed",
            ground_truth_feasible=feasible,
            resolution_latency_ms=1.0,
            success=success,
            qos_satisfied=qos,
            safe_rejection=rejection,
        )
        rows.append(row.__dict__)
    return rows


if __name__ == "__main__":
    unittest.main()
