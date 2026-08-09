from __future__ import annotations

import unittest
from copy import deepcopy

from experiments.exp2_cross_layer_robustness import (
    _coordinate_measured,
    _raw_combination_count,
    run_case,
)
from scripts.aggregate_exp2_robustness import _mcnemar, _wilcoxon
from scripts.audit_exp2_robustness import audit_ground_truth_independence
from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.controller.ground_truth import GroundTruthSolver
from src.core.models import to_jsonable
from src.simulation.conflict_robustness import (
    UNRESOLVABLE_CASES,
    UNRESOLVABLE_CONFLICT,
    ConflictRobustnessGenerator,
)


class GroundTruthNonTrivialityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = ConflictRobustnessGenerator()

    def test_ground_truth_independence_report(self) -> None:
        report = audit_ground_truth_independence()
        self.assertEqual("PASS", report["status"])
        self.assertFalse(report["same_objective"])
        self.assertFalse(report["same_tie_break"])
        self.assertFalse(report["direct_ground_truth_result_read"])
        self.assertEqual("exact feasible-combination search", report["search_type"])

    def test_oracle_and_proposed_do_not_mutate_shared_inputs(self) -> None:
        snapshot = self.generator.base_case("application_capacity", 1.2, 2)
        state_before = deepcopy(to_jsonable(snapshot.true_state))
        proposals_before = deepcopy(to_jsonable(snapshot.proposals))
        GroundTruthSolver().solve(snapshot.true_state, snapshot.proposals)
        CrossLayerCoordinator().coordinate(
            "proposed", snapshot.observed_state, snapshot.proposals
        )
        self.assertEqual(state_before, to_jsonable(snapshot.true_state))
        self.assertEqual(proposals_before, to_jsonable(snapshot.proposals))

    def test_proposed_does_not_always_select_oracle_optimum(self) -> None:
        differences = 0
        for seed in range(5):
            snapshot = self.generator.base_case(
                "application_capacity", 0.8, seed
            )
            result = CrossLayerCoordinator().coordinate(
                "proposed", snapshot.observed_state, snapshot.proposals
            )
            selected = tuple(item.proposal_id for item in result.selected_proposals)
            differences += selected != snapshot.ground_truth.best_feasible_combination
        self.assertGreater(differences, 0)

    def test_independent_and_no_verification_select_differently(self) -> None:
        snapshot = self.generator.base_case("application_capacity", 0.8, 0)
        coordinator = CrossLayerCoordinator()
        independent = coordinator.coordinate(
            "independent", snapshot.observed_state, snapshot.proposals
        )
        no_verification = coordinator.coordinate(
            "no_verification", snapshot.observed_state, snapshot.proposals
        )
        self.assertNotEqual(
            tuple(item.proposal_id for item in independent.selected_proposals),
            tuple(item.proposal_id for item in no_verification.selected_proposals),
        )
        self.assertEqual(
            "independent_layer_maximum_utility",
            independent.details["selection_policy"],
        )
        self.assertEqual(
            "declared_joint_score_without_feasibility",
            no_verification.details["selection_policy"],
        )


class RobustnessScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = ConflictRobustnessGenerator()

    def test_all_unresolvable_cases_have_no_feasible_combination(self) -> None:
        for case in UNRESOLVABLE_CASES:
            with self.subTest(case=case):
                snapshot = self.generator.unresolvable(case, 0)
                self.assertEqual(UNRESOLVABLE_CONFLICT, snapshot.conflict_class)
                self.assertFalse(snapshot.ground_truth.feasible_combinations)

    def test_proposed_rejects_unresolvable_case_before_execution(self) -> None:
        snapshot = self.generator.unresolvable("shared_resource_shortage", 1)
        result = CrossLayerCoordinator().coordinate(
            "proposed", snapshot.observed_state, snapshot.proposals
        )
        self.assertTrue(result.global_check_performed)
        self.assertFalse(result.selected_proposals)
        self.assertEqual(0, result.details["feasible_combinations"])

    def test_noise_ground_truth_uses_true_state(self) -> None:
        snapshot = self.generator.noisy(
            "network_physical", 0.4, 0.2, 3
        )
        expected = GroundTruthSolver().solve(
            snapshot.true_state, snapshot.proposals
        )
        self.assertEqual(expected, snapshot.ground_truth)
        self.assertNotEqual(
            to_jsonable(snapshot.true_state),
            to_jsonable(snapshot.observed_state),
        )

    def test_noise_values_are_clipped_to_legal_ranges(self) -> None:
        snapshot = self.generator.noisy(
            "application_capacity", 1.2, 0.2, 8
        )
        self.assertTrue(
            all(item.required_rate_mbps > 0 for item in snapshot.observed_state.application.values())
        )
        self.assertTrue(
            all(0.0 <= item.reliability <= 1.0 for item in snapshot.observed_state.physical.values())
        )

    def test_stale_proposal_version_is_rejected(self) -> None:
        snapshot = self.generator.stale(100, 0)
        self.assertEqual(1, snapshot.metadata["proposal_generated_version"])
        self.assertEqual(2, snapshot.metadata["execution_version"])
        result = CrossLayerCoordinator().coordinate(
            "proposed", snapshot.observed_state, snapshot.proposals
        )
        self.assertTrue(result.conflict_detected)
        self.assertFalse(result.selected_proposals)

    def test_missing_layer_is_not_filled_with_true_observation(self) -> None:
        snapshot = self.generator.missing_layer("physical", 0)
        self.assertNotIn("physical", {item.layer for item in snapshot.proposals})
        edge_id = next(iter(snapshot.true_state.physical))
        self.assertNotEqual(
            snapshot.true_state.physical[edge_id],
            snapshot.observed_state.physical[edge_id],
        )
        self.assertEqual(
            "conservative_bound", snapshot.metadata["missing_value_policy"]
        )

    def test_proposed_safely_rejects_missing_layer(self) -> None:
        snapshot = self.generator.missing_layer("network", 0)
        result = CrossLayerCoordinator().coordinate(
            "proposed", snapshot.observed_state, snapshot.proposals
        )
        self.assertFalse(result.selected_proposals)
        self.assertTrue(result.details["safe_rejection"])

    def test_proposal_combination_growth_is_n_to_four(self) -> None:
        for count in range(1, 6):
            snapshot = self.generator.proposal_scale(count, 0)
            self.assertEqual(count**4, _raw_combination_count(snapshot.proposals))

    def test_coordination_timeout_is_reported(self) -> None:
        snapshot = self.generator.proposal_scale(5, 0)
        _result, _memory, timed_out = _coordinate_measured(
            "proposed", snapshot, 0.000001
        )
        self.assertTrue(timed_out)

    def test_same_seed_produces_same_noisy_observation(self) -> None:
        left = self.generator.noisy("transport_network", 0.85, 0.1, 9)
        right = self.generator.noisy("transport_network", 0.85, 0.1, 9)
        self.assertEqual(left.fingerprint, right.fingerprint)


class RobustnessTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresolvable_proposed_keeps_stable_version(self) -> None:
        snapshot = ConflictRobustnessGenerator().unresolvable(
            "physical_capacity_absolute", 0
        )
        metric, _events, _decision = await run_case(
            snapshot,
            "proposed",
            run_index=1,
            coordination_timeout_ms=1000,
        )
        self.assertTrue(metric.safe_rejection)
        self.assertFalse(metric.transaction_attempted)
        self.assertEqual(metric.stable_version_before, metric.stable_version_after)
        self.assertTrue(metric.no_partial_commit)

    async def test_unresolvable_independent_rolls_back(self) -> None:
        snapshot = ConflictRobustnessGenerator().unresolvable(
            "reliability_impossible", 0
        )
        metric, _events, _decision = await run_case(
            snapshot,
            "independent",
            run_index=1,
            coordination_timeout_ms=1000,
        )
        self.assertTrue(metric.transaction_attempted)
        self.assertTrue(metric.rollback_triggered)
        self.assertTrue(metric.rollback_success)
        self.assertTrue(metric.no_partial_commit)


class PairedStatisticsTests(unittest.TestCase):
    def test_exact_mcnemar_uses_paired_binary_samples(self) -> None:
        pairs = [
            ({"qos_satisfied": "True"}, {"qos_satisfied": "False"})
            for _ in range(10)
        ]
        result = _mcnemar(pairs, "qos_satisfied")
        self.assertEqual("exact_McNemar", result["paired_test"])
        self.assertEqual(10, result["sample_size"])
        self.assertLess(result["p_value"], 0.01)

    def test_wilcoxon_uses_paired_latency_samples(self) -> None:
        pairs = [
            (
                {"coordination_latency_ms": str(index + 2)},
                {"coordination_latency_ms": str(index)},
            )
            for index in range(1, 11)
        ]
        result = _wilcoxon(pairs, "coordination_latency_ms")
        self.assertIn("Wilcoxon", result["paired_test"])
        self.assertEqual(2.0, result["mean_difference"])
        self.assertEqual(10, result["sample_size"])


if __name__ == "__main__":
    unittest.main()
