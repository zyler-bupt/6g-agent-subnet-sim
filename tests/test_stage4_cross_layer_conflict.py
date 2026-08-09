from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from dataclasses import replace
from time import perf_counter

from experiments.exp2_cross_layer_conflict import run_compound_method
from scripts.aggregate_exp2 import _conditional_rate, _negative_condition_rate
from src.agents.layer_proposals import collect_layer_proposals
from src.controller.authorized_actions import (
    AuthorizedActionExecutor,
    _compile_authorized_target,
)
from src.controller.conflicts import (
    ConflictType,
    conflicts_from_violations,
    detect_write_set_conflicts,
)
from src.controller.cross_layer_coordinator import CrossLayerCoordinator
from src.controller.feasibility import evaluate_cross_layer_combination
from src.controller.ground_truth import GroundTruthSolver, proposals_for_ids
from src.core.cross_layer import AuthorizedAction
from src.core.events import EventInjector, EventStage
from src.core.models import to_jsonable
from src.simulation.conflict_scenario_generator import ConflictScenarioGenerator


class ProposalAndFeasibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = ConflictScenarioGenerator()

    def test_layer_proposal_does_not_mutate_state(self) -> None:
        snapshot = self.generator.generate("application_capacity", 1.2, 3)
        state = snapshot.cross_layer_state
        before = deepcopy(to_jsonable(state))
        proposals = collect_layer_proposals(state)
        self.assertEqual(before, to_jsonable(state))
        self.assertEqual(4, len({proposal.layer for proposal in proposals}))
        self.assertTrue(all(proposal.read_set for proposal in proposals))

    def test_physical_layer_participates_in_feasibility(self) -> None:
        snapshot = self.generator.generate("network_physical", 0.5, 0)
        independent = proposals_for_ids(
            snapshot.proposals,
            snapshot.ground_truth.independent_proposal_ids,
        )
        result = evaluate_cross_layer_combination(
            snapshot.cross_layer_state,
            independent,
        )
        self.assertFalse(result.feasible)
        self.assertTrue(
            any(
                value.startswith("network_physical")
                for value in result.violations
            )
        )

    def test_application_capacity_conflict(self) -> None:
        snapshot = self.generator.generate("application_capacity", 1.4, 0)
        self.assertTrue(snapshot.ground_truth.ground_truth_conflict)
        records = detect_constraint_types(snapshot)
        self.assertIn(ConflictType.APPLICATION_CAPACITY.value, records)

    def test_transport_network_conflict(self) -> None:
        snapshot = self.generator.generate("transport_network", 0.95, 0)
        self.assertTrue(snapshot.ground_truth.ground_truth_conflict)
        self.assertTrue(
            any(
                value.startswith("latency_qos")
                for value in snapshot.ground_truth.independent_violations
            )
        )

    def test_network_physical_conflict(self) -> None:
        snapshot = self.generator.generate("network_physical", 0.5, 0)
        self.assertTrue(snapshot.ground_truth.ground_truth_conflict)
        self.assertIn(ConflictType.NETWORK_PHYSICAL.value, detect_constraint_types(snapshot))

    def test_write_set_conflict(self) -> None:
        snapshot = self.generator.generate("application_capacity", 0.6, 0)
        left = next(
            proposal
            for proposal in snapshot.proposals
            if proposal.layer == "application" and not proposal.is_keep
        )
        right = next(
            proposal
            for proposal in snapshot.proposals
            if proposal.layer == "transport" and not proposal.is_keep
        )
        left = replace(
            left,
            write_set=frozenset({"flow:shared"}),
            parameters={
                **left.parameters,
                "write_values": {"flow:shared": "application"},
            },
        )
        right = replace(
            right,
            write_set=frozenset({"flow:shared"}),
            parameters={
                **right.parameters,
                "write_values": {"flow:shared": "transport"},
            },
        )
        records = detect_write_set_conflicts((left, right))
        self.assertEqual(1, len(records))
        self.assertEqual(ConflictType.WRITE_SET.value, records[0].conflict_type)


class StrategyAndGroundTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = ConflictScenarioGenerator()
        self.snapshot = self.generator.generate("application_capacity", 1.4, 2)
        self.coordinator = CrossLayerCoordinator()

    def test_independent_layer_uses_same_proposals(self) -> None:
        before = tuple(proposal.proposal_id for proposal in self.snapshot.proposals)
        result = self.coordinator.coordinate(
            "independent",
            self.snapshot.cross_layer_state,
            self.snapshot.proposals,
        )
        self.assertTrue(
            set(item.proposal_id for item in result.selected_proposals) <= set(before)
        )
        self.assertEqual(before, tuple(item.proposal_id for item in self.snapshot.proposals))

    def test_adjacent_layer_has_no_global_check(self) -> None:
        result = self.coordinator.coordinate(
            "adjacent",
            self.snapshot.cross_layer_state,
            self.snapshot.proposals,
        )
        self.assertFalse(result.global_check_performed)
        self.assertGreater(result.pairwise_checks, 0)
        self.assertIsNone(result.selected_feasibility)

    def test_no_verification_skips_pre_execution_check(self) -> None:
        result = self.coordinator.coordinate(
            "no_verification",
            self.snapshot.cross_layer_state,
            self.snapshot.proposals,
        )
        self.assertFalse(result.global_check_performed)
        self.assertEqual(0.0, result.feasibility_latency_ms)
        self.assertIsNone(result.selected_feasibility)

    def test_ground_truth_is_independent_from_method(self) -> None:
        solver = GroundTruthSolver()
        expected = solver.solve(
            self.snapshot.cross_layer_state,
            self.snapshot.proposals,
        )
        for method in ("proposed", "independent", "adjacent", "no_verification"):
            self.coordinator.coordinate(
                method,
                self.snapshot.cross_layer_state,
                self.snapshot.proposals,
            )
            actual = solver.solve(
                self.snapshot.cross_layer_state,
                self.snapshot.proposals,
            )
            self.assertEqual(expected, actual)

    def test_conflict_detection_metrics(self) -> None:
        rows = [
            {"ground_truth_conflict": "True", "conflict_detected": "True"},
            {"ground_truth_conflict": "True", "conflict_detected": "False"},
            {"ground_truth_conflict": "False", "conflict_detected": "False"},
        ]
        self.assertEqual(
            0.5,
            _conditional_rate(rows, "ground_truth_conflict", "conflict_detected"),
        )

    def test_false_alarm_metrics(self) -> None:
        rows = [
            {"ground_truth_conflict": "False", "conflict_detected": "True"},
            {"ground_truth_conflict": "False", "conflict_detected": "False"},
            {"ground_truth_conflict": "True", "conflict_detected": "True"},
        ]
        self.assertEqual(
            0.5,
            _negative_condition_rate(
                rows,
                "ground_truth_conflict",
                "conflict_detected",
            ),
        )

    def test_scenario_fingerprint_identical_across_methods(self) -> None:
        fingerprints = []
        for method in ("proposed", "independent", "adjacent", "no_verification"):
            self.coordinator.coordinate(
                method,
                self.snapshot.cross_layer_state,
                self.snapshot.proposals,
            )
            fingerprints.append(self.snapshot.fingerprint)
        self.assertEqual(1, len(set(fingerprints)))

    def test_rejected_proposal_reason_is_logged(self) -> None:
        result = self.coordinator.coordinate(
            "proposed",
            self.snapshot.cross_layer_state,
            self.snapshot.proposals,
        )
        selected = {item.proposal_id for item in result.selected_proposals}
        expected = {
            item.proposal_id
            for item in self.snapshot.proposals
            if item.proposal_id not in selected
        }
        self.assertEqual(expected, set(result.rejected_proposals))
        self.assertTrue(all(result.rejected_proposals.values()))


class TransactionalExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.generator = ConflictScenarioGenerator()

    async def _execute(self, method: str, scenario: str = "application_capacity"):
        pressure = 1.4 if scenario == "application_capacity" else 0.95
        snapshot = self.generator.generate(scenario, pressure, 4)
        controller, verifier, _provider = snapshot.instantiate()
        stable, formation = await controller.build_task_subnet(
            snapshot.base.task,
            verifier=verifier,
        )
        self.assertTrue(formation.networking_success)
        coordination = CrossLayerCoordinator().coordinate(
            method,
            snapshot.cross_layer_state,
            snapshot.proposals,
        )
        event = EventInjector(prefix="test-exp2").cross_layer_conflict(
            stable.task.task_id,
            scenario=snapshot.scenario,
            pressure=snapshot.pressure,
            scenario_fingerprint=snapshot.fingerprint,
        )
        execution = await AuthorizedActionExecutor(controller, verifier).execute(
            stable,
            snapshot.cross_layer_state,
            coordination,
            event,
            run_id=1,
            seed=snapshot.seed,
            received_at=perf_counter(),
        )
        return snapshot, controller, stable, execution

    async def test_all_actions_require_controller_authorization(self) -> None:
        snapshot, controller, stable, _execution = await self._execute("proposed")
        bad = AuthorizedAction(
            proposal_id="bad",
            task_id=stable.task.task_id,
            layer="application",
            action="INCREASE_APPLICATION_RATE",
            authorized_by="aAgent",
            target_objects=frozenset({"edge"}),
        )
        with self.assertRaises(PermissionError):
            _compile_authorized_target(
                stable,
                snapshot.cross_layer_state,
                (bad,),
            )

    async def test_all_methods_share_transaction_executor(self) -> None:
        for method in ("proposed", "independent", "adjacent", "no_verification"):
            _snapshot, _controller, _stable, execution = await self._execute(method)
            components = {record.component for record in execution.transaction.event_log}
            self.assertIn("TransactionExecutor", components)
            self.assertTrue(execution.transaction.staged_gateway_ids)

    async def test_failed_execution_rolls_back(self) -> None:
        snapshot, controller, stable, execution = await self._execute("independent")
        self.assertFalse(execution.transaction.success)
        self.assertTrue(execution.transaction.rollback_triggered)
        self.assertTrue(execution.transaction.rollback_success)
        for gateway_id in stable.involved_gateways:
            gateway = controller.gateways[gateway_id]
            self.assertEqual(1, gateway.get_stable_version(snapshot.base.task.task_id))
            self.assertIsNone(gateway.get_staged_version(snapshot.base.task.task_id))

    async def test_compound_timeline_is_monotonic(self) -> None:
        snapshots = tuple(
            self.generator.generate("compound", pressure, 5)
            for pressure in (0.0, 0.5, 1.0)
        )
        _metrics, events, _proposals, _conflicts, timeline = await run_compound_method(
            snapshots,
            "proposed",
            run_index=1,
        )
        self.assertEqual([0, 1, 2], [item.time_step for item in timeline])
        self.assertTrue(
            all(
                item.action_executed_at >= item.conflict_detected_at
                for item in timeline
            )
        )
        by_event: dict[str, list[float]] = {}
        for row in events:
            event_id = str(row.get("event_id", "coordination"))
            by_event.setdefault(event_id, []).append(float(row["timestamp"]))
        self.assertTrue(all(values == sorted(values) for values in by_event.values()))


def detect_constraint_types(snapshot) -> set[str]:
    proposals = proposals_for_ids(
        snapshot.proposals,
        snapshot.ground_truth.independent_proposal_ids,
    )
    records = conflicts_from_violations(
        proposals,
        snapshot.ground_truth.independent_violations,
    )
    return {record.conflict_type for record in records}


if __name__ == "__main__":
    unittest.main()
