from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from experiments.exp1_netns_verified_formation import FormationEdge
from src.controller.formation_transactions import (
    FaultClass,
    TransactionAttempt,
    TransactionPhase,
    build_fault_schedule,
    retry_scope_for_method,
    task_descendant_closure,
)


class FormationTransactionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.edges = (
            FormationEdge("e-b", 1, 2, 1.0),
            FormationEdge("e-d", 2, 3, 1.0),
            FormationEdge("e-a", 0, 1, 1.0),
            FormationEdge("e-c", 1, 4, 1.0),
        )

    def test_fault_schedule_is_paired_and_method_blind(self) -> None:
        left = build_fault_schedule("stale_version", 12, 7, self.edges)
        right = build_fault_schedule("stale_version", 12, 7, self.edges)

        self.assertEqual(left, right)
        self.assertEqual(left.fault_class, FaultClass.STALE_VERSION)
        self.assertNotIn("method", left.fingerprint_payload())

    def test_fault_schedule_selection_is_independent_of_input_order(self) -> None:
        forward = build_fault_schedule("command_rejection", 12, 7, self.edges)
        reverse = build_fault_schedule("command_rejection", 12, 7, tuple(reversed(self.edges)))

        self.assertEqual(forward, reverse)

    def test_proposed_uses_descendants_but_baselines_do_not(self) -> None:
        self.assertEqual(
            task_descendant_closure("e-b", self.edges),
            frozenset({"e-b", "e-d"}),
        )
        self.assertEqual(
            retry_scope_for_method("proposed", "e-b", self.edges),
            frozenset({"e-b", "e-d"}),
        )
        self.assertEqual(
            retry_scope_for_method("cspf", "e-b", self.edges),
            frozenset({"e-b"}),
        )
        self.assertEqual(
            retry_scope_for_method("global_sfc_embedding", "e-b", self.edges),
            frozenset({"chain:e-a,e-b,e-d"}),
        )

    def test_global_scope_selects_one_hop_ordered_path_at_forks_and_joins(self) -> None:
        fork_join_edges = (
            FormationEdge("z-input", 0, 1, 1.0),
            FormationEdge("a-input", 5, 1, 1.0),
            FormationEdge("m-failed", 1, 2, 1.0),
            FormationEdge("z-output", 2, 3, 1.0),
            FormationEdge("a-output", 2, 4, 1.0),
            FormationEdge("z-tail", 3, 6, 1.0),
            FormationEdge("a-tail", 4, 7, 1.0),
        )

        self.assertEqual(
            retry_scope_for_method("global_sfc_embedding", "m-failed", fork_join_edges),
            frozenset({"chain:a-input,m-failed,a-output,a-tail"}),
        )

    def test_transaction_attempt_is_immutable_and_rejects_invalid_timing(self) -> None:
        attempt = TransactionAttempt(
            transaction_id="txn-1",
            attempt_index=0,
            operation="prepare",
            phase=TransactionPhase.PREPARED,
            accepted=True,
            started_ns=10,
            ended_ns=12,
            affected_objects=("e-b",),
            commands_attempted=1,
        )

        self.assertEqual(attempt.affected_objects, ("e-b",))
        with self.assertRaises(FrozenInstanceError):
            attempt.accepted = False  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "ended_ns"):
            TransactionAttempt(
                "txn-1", 0, "prepare", TransactionPhase.REJECTED, False, 12, 10
            )

    def test_transaction_attempt_normalizes_mutable_affected_objects(self) -> None:
        affected_objects = ["e-b"]
        attempt = TransactionAttempt(
            transaction_id="txn-1",
            attempt_index=0,
            operation="prepare",
            phase=TransactionPhase.NEW,
            accepted=False,
            started_ns=0,
            ended_ns=1,
            affected_objects=affected_objects,  # type: ignore[arg-type]
        )
        affected_objects.append("e-d")

        self.assertEqual(attempt.affected_objects, ("e-b",))

    def test_transaction_attempt_rejects_negative_counters_and_timestamps(self) -> None:
        cases = (
            ("attempt_index", {"attempt_index": -1}),
            ("started_ns", {"started_ns": -1}),
            ("ended_ns", {"ended_ns": -1}),
            ("commands_attempted", {"commands_attempted": -1}),
        )
        for field, overrides in cases:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                TransactionAttempt(
                    transaction_id="txn-1",
                    attempt_index=overrides.get("attempt_index", 0),
                    operation="prepare",
                    phase=TransactionPhase.NEW,
                    accepted=False,
                    started_ns=overrides.get("started_ns", 0),
                    ended_ns=overrides.get("ended_ns", 1),
                    commands_attempted=overrides.get("commands_attempted", 0),
                )
