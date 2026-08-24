from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path

import yaml

import experiments.exp4_failure_reconfiguration as exp4
from experiments.exp4_failure_reconfiguration import run_one
from src.controller.cspf import CspfRequest, CspfSolver, TrafficEngineeringLink
from src.controller.failure_recovery import CspfNetworkOnlyStrategy
from src.simulation.failure_scenario_generator import (
    FailureScenarioConfig,
    FailureScenarioGenerator,
)


class CspfAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.links = (
            TrafficEngineeringLink("s-a", "s", "a", 10.0, 2.0, 5.0),
            TrafficEngineeringLink("a-d", "a", "d", 10.0, 2.0, 5.0),
            TrafficEngineeringLink("s-b", "s", "b", 10.0, 4.0, 1.0),
            TrafficEngineeringLink("b-d", "b", "d", 10.0, 4.0, 1.0),
            TrafficEngineeringLink("s-x", "s", "x", 1.0, 0.1, 0.1),
            TrafficEngineeringLink("x-d", "x", "d", 1.0, 0.1, 0.1),
        )

    def test_prunes_insufficient_bandwidth_and_uses_minimum_delay(self) -> None:
        result = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 20.0, metric="delay"),
        )

        self.assertTrue(result.feasible)
        self.assertEqual(result.path, ("s", "a", "d"))
        self.assertIn("s-x", result.pruned_link_ids)
        self.assertIn("x-d", result.pruned_link_ids)

    def test_uses_fixed_te_metric_and_enforces_delay_after_spf(self) -> None:
        accepted = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 9.0, metric="te_cost"),
        )
        rejected = CspfSolver().solve(
            self.links,
            CspfRequest("flow", "s", "d", 5.0, 3.0, metric="delay"),
        )

        self.assertTrue(accepted.feasible)
        self.assertEqual(accepted.path, ("s", "b", "d"))
        self.assertFalse(rejected.feasible)
        self.assertEqual(rejected.failure_reason, "no_path_within_delay_bound")

    def test_does_not_substitute_a_costlier_path_when_spf_exceeds_dmax(self) -> None:
        links = (
            TrafficEngineeringLink("s-a", "s", "a", 10.0, 6.0, 1.0),
            TrafficEngineeringLink("a-d", "a", "d", 10.0, 6.0, 1.0),
            TrafficEngineeringLink("s-b", "s", "b", 10.0, 4.0, 5.0),
            TrafficEngineeringLink("b-d", "b", "d", 10.0, 4.0, 5.0),
        )

        result = CspfSolver().solve(
            links,
            CspfRequest("flow", "s", "d", 5.0, 10.0, metric="te_cost"),
        )

        self.assertFalse(result.feasible)
        self.assertEqual(result.failure_reason, "no_path_within_delay_bound")


class CspfRecoveryBoundaryTests(unittest.TestCase):
    def test_te_topology_is_paired_across_capacity_levels_for_the_same_seed(self) -> None:
        generator = FailureScenarioGenerator()
        profiles = []
        for ratio in (0.90, 0.45, 0.00):
            snapshot = generator.generate(
                FailureScenarioConfig(
                    fault_type="LINK_FAILURE",
                    fault_level=f"post_capacity_to_requirement_{ratio:.2f}",
                    severity=ratio,
                    num_agents=20,
                    num_gateways=4,
                ),
                17,
            )
            profiles.append(snapshot.cspf_te_link_profiles())

        self.assertEqual(profiles[0], profiles[1])
        self.assertEqual(profiles[0], profiles[2])

    def test_planning_does_not_consult_cross_layer_physical_truth(self) -> None:
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="PHYSICAL_CAPACITY_DROP",
                    fault_level="post_capacity_to_requirement_0.30",
                    severity=0.30,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            controller, verifier, _provider = snapshot.instantiate()
            stable, formation = await controller.build_task_subnet(
                snapshot.task,
                verifier=verifier,
                run_id=1,
                seed=snapshot.seed,
            )
            self.assertTrue(formation.networking_success)
            event, context = snapshot.apply_fault(controller, stable)

            planning = await CspfNetworkOnlyStrategy().plan(
                controller,
                stable,
                event,
                context,
            )

            self.assertIsNotNone(planning.plan)
            self.assertFalse(planning.safe_rejection)
            self.assertEqual(
                {item.layer for item in planning.selected_proposals},
                {"network"},
            )

        asyncio.run(execute())

    def test_each_flow_receives_the_path_selected_for_its_session(self) -> None:
        """Catches collapsing distinct same-endpoint CSPF decisions to one path."""
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="LINK_FAILURE",
                    fault_level="post_capacity_to_requirement_0.90",
                    severity=0.90,
                    num_agents=20,
                    num_gateways=4,
                ),
                9001,
            )
            controller, verifier, _provider = snapshot.instantiate()
            stable, formation = await controller.build_task_subnet(
                snapshot.task,
                verifier=verifier,
                run_id=1,
                seed=snapshot.seed,
            )
            self.assertTrue(formation.networking_success)
            event, context = snapshot.apply_fault(controller, stable)

            planning = await CspfNetworkOnlyStrategy().plan(
                controller,
                stable,
                event,
                context,
            )

            self.assertIsNotNone(planning.plan)
            selected_paths = planning.selected_proposals[0].parameters["paths"]
            target_paths = {
                session.session_id: session.gateway_path
                for session in planning.plan.target_state.sessions
            }
            self.assertGreater(len(set(map(tuple, selected_paths.values()))), 1)
            for session_id, path in selected_paths.items():
                self.assertEqual(target_paths[session_id], tuple(path))

        asyncio.run(execute())

    def test_recovery_authorizes_only_network_routing_actions(self) -> None:
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="LINK_FAILURE",
                    fault_level="post_capacity_to_requirement_0.00",
                    severity=0.0,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            row, _events, proposals, _probes = await run_one(
                snapshot,
                "cspf",
                run_sequence=1,
            )

            self.assertTrue(row.recovery_success)
            self.assertEqual(row.selected_layers, "network")
            self.assertEqual(row.changed_physical_bindings, 0)
            self.assertTrue(proposals)
            self.assertTrue(proposals[0]["selected"])
            self.assertTrue(proposals[0]["authorized"])
            self.assertTrue(proposals[0]["proposal"]["parameters"]["paths"])
            expected_inputs = {
                "topology",
                "link_up",
                "available_bandwidth",
                "link_delay",
                "te_cost",
                "flow_source",
                "flow_destination",
                "required_bandwidth",
                "maximum_delay",
            }
            for item in proposals:
                self.assertEqual(item["proposal"]["layer"], "network")
                self.assertEqual(
                    set(item["proposal"]["parameters"]["allowed_inputs"]),
                    expected_inputs,
                )

        asyncio.run(execute())

    def test_each_physical_capacity_failure_is_disruptive_before_recovery(self) -> None:
        async def execute() -> None:
            for sequence, ratio in enumerate((0.90, 0.75, 0.60, 0.45, 0.30), 1):
                snapshot = FailureScenarioGenerator().generate(
                    FailureScenarioConfig(
                        fault_type="PHYSICAL_CAPACITY_DROP",
                        fault_level=f"post_capacity_to_requirement_{ratio:.2f}",
                        severity=ratio,
                        num_agents=20,
                        num_gateways=4,
                    ),
                    3,
                )
                row, _events, _proposals, _probes = await run_one(
                    snapshot,
                    "proposed",
                    run_sequence=sequence,
                )
                self.assertTrue(row.fault_was_disruptive)
                self.assertTrue(row.requirement_violated_before_recovery)
                self.assertLess(
                    row.post_failure_capacity_mbps,
                    row.pre_recovery_requirement_mbps,
                )
                self.assertAlmostEqual(
                    row.pre_recovery_violation_margin_mbps,
                    row.pre_recovery_requirement_mbps
                    - row.post_failure_capacity_mbps,
                )

        asyncio.run(execute())

    def test_cspf_cannot_bypass_shared_failed_physical_access(self) -> None:
        async def execute() -> None:
            snapshot = FailureScenarioGenerator().generate(
                FailureScenarioConfig(
                    fault_type="PHYSICAL_CAPACITY_DROP",
                    fault_level="post_capacity_to_requirement_0.20",
                    severity=0.20,
                    num_agents=20,
                    num_gateways=4,
                ),
                0,
            )
            row, _events, _proposals, _probes = await run_one(
                snapshot,
                "cspf",
                run_sequence=1,
            )

            self.assertFalse(row.recovery_success)
            self.assertFalse(row.safe_rejection)
            self.assertTrue(row.rollback_triggered)
            self.assertTrue(row.rollback_success)
            self.assertFalse(row.post_execution_verification_success)
            self.assertEqual(row.selected_layers, "network")
            self.assertEqual(row.changed_physical_bindings, 0)
            self.assertIn("verification failed", row.failure_reason)

        asyncio.run(execute())


class Exp4FormalProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        with Path("configs/exp4_cspf_failure_reconfiguration.yaml").open(
            encoding="utf-8"
        ) as handle:
            self.config = yaml.safe_load(handle)
        self.methods = ("proposed", "full_rebuild", "cspf")
        self.seeds = tuple(range(30))
        self.output = Path("results/exp4_cspf_final")

    def test_requires_the_frozen_registered_invocation(self) -> None:
        exp4.validate_protocol(
            self.config,
            self.methods,
            self.seeds,
            self.output,
        )
        mutations = (
            (
                {**self.config, "experiment": {**self.config["experiment"], "frozen": False}},
                self.methods,
                self.seeds,
                self.output,
            ),
            (self.config, ("proposed", "cspf"), self.seeds, self.output),
            (self.config, self.methods, tuple(range(29)), self.output),
            (self.config, self.methods, self.seeds, Path("results/exp4-other")),
        )
        for config, methods, seeds, output in mutations:
            with self.subTest(methods=methods, seeds=len(seeds), output=output):
                with self.assertRaises(ValueError):
                    exp4.validate_protocol(config, methods, seeds, output)

    def test_rejects_non_disruptive_capacity_points(self) -> None:
        for sweep_name in (
            "link_post_failure_to_requirement_ratios",
            "physical_post_failure_to_requirement_ratios",
        ):
            config = copy.deepcopy(self.config)
            config["fault_sweeps"][sweep_name] = [0.9, 1.0]
            with self.subTest(sweep=sweep_name):
                with self.assertRaisesRegex(ValueError, "below service requirement"):
                    exp4.validate_protocol(
                        config,
                        self.methods,
                        self.seeds,
                        self.output,
                    )


if __name__ == "__main__":
    unittest.main()
