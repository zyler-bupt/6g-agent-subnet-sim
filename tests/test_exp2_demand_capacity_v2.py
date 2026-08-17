from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

import experiments.exp2_demand_capacity_ratio as exp2
import scripts.aggregate_exp2_demand_capacity as exp2_aggregate
from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.simulation.conflict_robustness import (
    NO_CONFLICT,
    RESOLVABLE_CONFLICT,
    UNRESOLVABLE_CONFLICT,
)
from src.simulation.demand_capacity_ratio import (
    SHARED_RESOURCE_ID,
    DemandCapacityRatioGenerator,
)


class DemandCapacityMechanismV3Tests(unittest.TestCase):
    def test_satisfaction_confidence_interval_uses_wilson_score(self) -> None:
        stats = exp2_aggregate._stats([1.0] * 93 + [0.0] * 7, "rate")

        self.assertAlmostEqual(stats["rate_lower95"], 0.8625032899)
        self.assertAlmostEqual(stats["rate_upper95"], 0.9656811756)

    def test_paired_method_comparison_uses_exact_mcnemar_test(self) -> None:
        self.assertTrue(hasattr(exp2_aggregate, "exact_mcnemar_pvalue"))
        self.assertAlmostEqual(
            exp2_aggregate.exact_mcnemar_pvalue(10, 0),
            0.001953125,
        )

    def test_shared_trunk_is_seeded_exogenous_capacity_not_gamma_cliff(self) -> None:
        generator = DemandCapacityRatioGenerator()
        low = generator.generate(0.8, 41)
        high = generator.generate(1.3, 41)

        utilization = low.metadata["shared_trunk_utilization"]
        effective_sum = sum(
            values["effective_mbps"]
            for values in low.metadata["flow_capacities"].values()
        )
        capacity = low.true_state.shared_resource_capacity_mbps[
            SHARED_RESOURCE_ID
        ]

        self.assertGreaterEqual(utilization, 0.75)
        self.assertLessEqual(utilization, 0.90)
        self.assertAlmostEqual(capacity, effective_sum / utilization)
        self.assertEqual(
            low.true_state.shared_resource_capacity_mbps,
            high.true_state.shared_resource_capacity_mbps,
        )
        self.assertEqual(
            low.metadata["environment_fingerprint"],
            high.metadata["environment_fingerprint"],
        )

    def test_route_access_coupling_is_seeded_not_forced_in_every_scenario(self) -> None:
        generator = DemandCapacityRatioGenerator()
        observed: set[bool] = set()
        for seed in range(20):
            low = generator.generate(1.05, seed)
            high = generator.generate(1.3, seed)
            low_flags = {
                edge_id: bool(resources["requires_access_switch"])
                for edge_id, resources in low.metadata["candidate_resources"].items()
            }
            high_flags = {
                edge_id: bool(resources["requires_access_switch"])
                for edge_id, resources in high.metadata["candidate_resources"].items()
            }
            self.assertEqual(low_flags, high_flags)
            observed.update(low_flags.values())

        self.assertEqual(observed, {False, True})

    def test_actions_use_dimensioned_resource_and_quality_costs(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.1, 7)
        changed = [proposal for proposal in snapshot.proposals if not proposal.is_keep]

        self.assertEqual(
            {
                proposal.action
                for proposal in changed
                if proposal.layer == "transport"
            },
            {"RESERVE_TRANSPORT_SERVICE"},
        )
        for proposal in changed:
            self.assertIn("normalized_action_cost", proposal.parameters)
            self.assertAlmostEqual(
                proposal.expected_cost,
                proposal.parameters["normalized_action_cost"],
            )
            if proposal.layer == "application":
                self.assertGreater(proposal.parameters["quality_loss_fraction"], 0.0)
            else:
                self.assertGreater(proposal.parameters["reserved_increment_mbps"], 0.0)
                self.assertGreater(
                    proposal.parameters["reserved_increment_fraction"], 0.0
                )

    def test_ground_truth_conflict_is_relative_to_current_keep_state(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.2, 0)
        proposals = {proposal.proposal_id: proposal for proposal in snapshot.proposals}

        self.assertTrue(snapshot.ground_truth.ground_truth_conflict)
        self.assertTrue(
            all(
                proposals[proposal_id].is_keep
                for proposal_id in snapshot.ground_truth.independent_proposal_ids
            )
        )

    def test_four_selectors_do_not_collapse_to_one_decision_rule(self) -> None:
        generator = DemandCapacityRatioGenerator()
        coordinator = CrossLayerCoordinator()
        observed_policies: dict[str, set[str]] = {
            method: set()
            for method in ("proposed", "adjacent", "independent", "no_verification")
        }
        distinct_selections = False

        for gamma in (1.05, 1.1, 1.2):
            for seed in range(6):
                snapshot = generator.generate(gamma, seed)
                selections = {}
                for method in observed_policies:
                    decision = coordinator.coordinate(
                        method, snapshot.observed_state, snapshot.proposals
                    )
                    observed_policies[method].add(
                        str(decision.details["selection_policy"])
                    )
                    selections[method] = tuple(
                        proposal.proposal_id
                        for proposal in decision.selected_proposals
                    )
                distinct_selections |= len(set(selections.values())) >= 2

        self.assertTrue(distinct_selections)
        self.assertEqual(
            {next(iter(values)) for values in observed_policies.values()},
            {
                "global_hard_feasibility_common_objective",
                "adjacent_pair_common_objective",
                "independent_layer_local_objective",
                "global_common_objective_without_hard_verification",
            },
        )

    def test_alc_propagates_an_upstream_boundary_choice_downstream(self) -> None:
        snapshot = DemandCapacityRatioGenerator().generate(1.2, 0)
        coordinator = CrossLayerCoordinator()
        independent = coordinator.coordinate(
            "independent", snapshot.observed_state, snapshot.proposals
        )
        alc = coordinator.coordinate(
            "adjacent", snapshot.observed_state, snapshot.proposals
        )
        independent_network_actions = {
            next(iter(proposal.affected_edges)): proposal.action
            for proposal in independent.selected_proposals
            if proposal.layer == "network"
        }
        alc_by_edge_layer = {
            (next(iter(proposal.affected_edges)), proposal.layer): proposal
            for proposal in alc.selected_proposals
        }
        reserved_edges = {
            edge_id
            for edge_id, action in independent_network_actions.items()
            if action == "RESERVE_BANDWIDTH"
        }

        self.assertTrue(reserved_edges)
        for edge_id in reserved_edges:
            network = alc_by_edge_layer[(edge_id, "network")]
            physical = alc_by_edge_layer[(edge_id, "physical")]
            self.assertEqual(network.action, "RESERVE_BANDWIDTH")
            required_access = snapshot.true_state.metadata[
                "route_access_requirements"
            ][edge_id][network.parameters["selected_route"]]
            self.assertEqual(physical.parameters["access_id"], required_access)

    def test_pilot_protocol_forbids_method_execution(self) -> None:
        config_path = Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(config["methods"], [])
        exp2.validate_protocol(
            config,
            (),
            exp2.PILOT_SEEDS,
            exp2.PILOT_OUTPUT,
        )
        with self.assertRaisesRegex(ValueError, "ground-truth-only"):
            exp2.validate_protocol(
                config,
                ("ours",),
                exp2.PILOT_SEEDS,
                exp2.PILOT_OUTPUT,
            )

    def test_pilot_run_never_calls_comparison_method(self) -> None:
        config = yaml.safe_load(
            Path("configs/exp2_demand_capacity_ratio_pilot_v5.yaml").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot"
            local = copy.deepcopy(config)
            local["experiment"]["output_dir"] = str(output)
            with (
                patch.object(exp2, "PILOT_OUTPUT", output),
                patch.object(
                    exp2,
                    "run_case",
                    new=AsyncMock(side_effect=AssertionError("method executed")),
                ),
                patch.object(
                    exp2,
                    "select_pilot_ratios",
                    return_value=(0.8,),
                ),
            ):
                rows = exp2.run_ground_truth_pilot(
                    local,
                    seeds=exp2.PILOT_SEEDS[:2],
                    output_dir=output,
                    validate_registered_seeds=False,
                    require_clean_source=False,
                )

        self.assertTrue(rows)
        self.assertTrue(all("method" not in row for row in rows))

    def test_main_range_ends_at_highest_mixed_feasibility_point(self) -> None:
        classes_by_gamma = {
            0.8: [NO_CONFLICT] * 10,
            0.9: [NO_CONFLICT] * 10,
            1.0: [RESOLVABLE_CONFLICT] * 10,
            1.1: [RESOLVABLE_CONFLICT] * 9 + [UNRESOLVABLE_CONFLICT],
            1.2: [UNRESOLVABLE_CONFLICT] * 10,
        }

        self.assertEqual(
            exp2.select_pilot_ratios(classes_by_gamma),
            (0.8, 0.9, 1.0, 1.1),
        )
        without_mixed = copy.deepcopy(classes_by_gamma)
        without_mixed[1.1] = [RESOLVABLE_CONFLICT] * 10
        with self.assertRaisesRegex(ValueError, "mixed feasibility endpoint"):
            exp2.select_pilot_ratios(without_mixed)

    def test_formal_protocol_rejects_changed_pilot_selection_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pilot_config_path = root / "pilot.yaml"
            pilot_config = {
                "experiment": {"generator_version": "wcnc-final-gamma-v5"},
                "methods": [],
            }
            pilot_config_path.write_text(
                yaml.safe_dump(pilot_config, sort_keys=False), encoding="utf-8"
            )
            selection_path = root / "pilot_selection.json"
            selection_path.write_text(
                '{"generator_version":"wcnc-final-gamma-v5",'
                '"configuration_sha256":"'
                + exp2.sha256_json(pilot_config)
                + '","retained_ratios":[0.8,0.9,1.0,1.1]}\n',
                encoding="utf-8",
            )
            digest = exp2.sha256_file(selection_path)
            config = yaml.safe_load(
                Path("configs/exp2_demand_capacity_ratio_v5.yaml").read_text(
                    encoding="utf-8"
                )
            )
            config["experiment"]["pilot_selection_path"] = str(selection_path)
            config["experiment"]["pilot_selection_sha256"] = digest
            config["experiment"]["pilot_config_path"] = str(pilot_config_path)
            config["experiment"]["pilot_config_sha256"] = exp2.sha256_file(
                pilot_config_path
            )
            config["experiment"]["frozen"] = True
            config["demand_capacity"]["ratios"] = [0.8, 0.9, 1.0, 1.1]

            config["experiment"]["pilot_selection_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "pilot selection SHA-256"):
                exp2.validate_formal_pilot_binding(config)


if __name__ == "__main__":
    unittest.main()
