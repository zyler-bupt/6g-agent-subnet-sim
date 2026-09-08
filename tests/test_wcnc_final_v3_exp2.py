from __future__ import annotations

import unittest
from unittest.mock import patch

from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.simulation.demand_capacity_ratio_v3 import HeterogeneousDemandCapacityGenerator
from src.simulation.conflict_robustness import ConflictRobustnessGenerator
from experiments.exp2_cross_layer_robustness import run_case


class WcncFinalV3Exp2Tests(unittest.TestCase):
    def test_seed_fixed_heterogeneity_is_paired_across_gamma(self) -> None:
        generator = HeterogeneousDemandCapacityGenerator()
        low = generator.generate(0.9, 17)
        high = generator.generate(1.2, 17)
        self.assertEqual(low.metadata["epsilon_by_flow"], high.metadata["epsilon_by_flow"])
        epsilon = low.metadata["epsilon_by_flow"]
        self.assertAlmostEqual(sum(epsilon.values()) / len(epsilon), 1.0)
        self.assertTrue(all(0.85 <= value <= 1.15 for value in epsilon.values()))
        for snapshot, gamma in ((low, 0.9), (high, 1.2)):
            for flow_id, required in snapshot.metadata["flow_r_req_mbps"].items():
                effective = snapshot.metadata["flow_capacities"][flow_id]["effective_mbps"]
                self.assertAlmostEqual(required, gamma * effective * epsilon[flow_id])

    def test_online_state_is_separate_from_oracle_state(self) -> None:
        snapshot = HeterogeneousDemandCapacityGenerator().generate(1.0, 23)
        self.assertIsNot(snapshot.true_state, snapshot.observed_state)
        self.assertEqual(snapshot.metadata["true_state_schema_version"], "exp2-true-v1")
        self.assertEqual(snapshot.metadata["observed_state_schema_version"], "exp2-observed-v1")
        self.assertNotIn("ground_truth_resolvable", snapshot.observed_state.metadata)

    def test_proposed_search_budget_is_enforced(self) -> None:
        snapshot = HeterogeneousDemandCapacityGenerator().generate(1.2, 31)
        result = CrossLayerCoordinator().coordinate(
            "proposed", snapshot.observed_state, snapshot.proposals,
            max_combinations=3,
        )
        self.assertLessEqual(result.candidate_combinations, 3)
        self.assertTrue(result.details["search_budget_exhausted"])

    def test_sanet_dynamic_weight_selection_does_not_call_hard_feasibility(self) -> None:
        snapshot = HeterogeneousDemandCapacityGenerator().generate(1.1, 41)
        with patch(
            "src.controller.cross_layer_coordinator.evaluate_cross_layer_combination",
            side_effect=AssertionError("hard feasibility leaked into SANet selection"),
        ):
            result = CrossLayerCoordinator().coordinate(
                "sanet_dw", snapshot.observed_state, snapshot.proposals
            )
        self.assertFalse(result.global_check_performed)
        self.assertEqual(result.details["selection_policy"], "sanet_inspired_dynamic_weight_soft_objective")

    def test_observation_noise_can_change_online_decision_without_changing_oracle(self) -> None:
        generator = ConflictRobustnessGenerator()
        clean = generator.noisy("application_capacity", 1.2, 0.0, 4)
        noisy = generator.noisy("application_capacity", 1.2, 0.5, 4)
        self.assertEqual(clean.true_state, noisy.true_state)
        self.assertEqual(clean.ground_truth, noisy.ground_truth)
        coordinator = CrossLayerCoordinator()
        clean_ids = tuple(item.proposal_id for item in coordinator.coordinate("sanet_dw", clean.observed_state, clean.proposals).selected_proposals)
        noisy_ids = tuple(item.proposal_id for item in coordinator.coordinate("sanet_dw", noisy.observed_state, noisy.proposals).selected_proposals)
        self.assertNotEqual(clean_ids, noisy_ids)


class WcncFinalV3Exp2TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_canonical_soft_methods_emit_complete_metrics(self) -> None:
        snapshot = HeterogeneousDemandCapacityGenerator().generate(0.9, 51)
        for index, method in enumerate(("sanet_dw", "weighted_sum"), 1):
            metric, _events, decision = await run_case(
                snapshot, method, run_index=index, coordination_timeout_ms=10_000
            )
            self.assertEqual(metric.method, method)
            self.assertIn("pre_verification_correct_decision", decision)


if __name__ == "__main__":
    unittest.main()
