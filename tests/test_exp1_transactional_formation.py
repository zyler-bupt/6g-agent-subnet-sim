from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import experiments.exp1_netns_verified_formation as exp1
from experiments.exp1_netns_verified_formation import FormationEdge
from src.controller.formation_transactions import (
    CommandResult,
    FaultClass,
    FormationTransaction,
    FormationTransactionEngine,
    StageResult,
    TransactionAttempt,
    TransactionPhase,
    build_fault_schedule,
    retry_scope_for_method,
    task_descendant_closure,
)


class RecordingExecutor:
    """In-memory executor that exposes externally observable transaction state."""

    def __init__(self, *, reject_prepare: bool = False) -> None:
        self.reject_prepare = reject_prepare
        self.staged: dict[str, tuple[object, ...]] = {}
        self.activated: list[str] = []

    def stage(
        self, transaction: FormationTransaction, ack_timeout_ms: int
    ) -> StageResult:
        if self.reject_prepare:
            return StageResult(accepted=False, reason="prepare rejected")
        self.staged[transaction.transaction_id] = transaction.commands
        return StageResult(
            accepted=True,
            commands_attempted=len(transaction.commands),
            affected_objects=transaction.affected_objects,
        )

    def activate(self, transaction_id: str) -> CommandResult:
        self.activated.append(transaction_id)
        return CommandResult(accepted=True)

    def flush(self, transaction_id: str) -> CommandResult:
        self.staged.pop(transaction_id, None)
        return CommandResult(accepted=True)

    def readback(self, transaction_id: str) -> tuple[dict[str, object], ...]:
        commands = self.staged.get(transaction_id, ())
        return tuple({"command": str(command)} for command in commands)


class FormationTransactionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transaction = FormationTransaction(
            transaction_id="txn-1",
            attempt_index=0,
            expected_versions=(("gateway-0", 1),),
            commands=("ip route replace 10.0.0.2/32",),
            affected_objects=("edge-0",),
        )

    def test_rejected_prepare_cannot_commit(self) -> None:
        executor = RecordingExecutor(reject_prepare=True)
        engine = FormationTransactionEngine(executor, ack_timeout_ms=200)

        prepared = engine.prepare(self.transaction)

        self.assertFalse(prepared.accepted)
        with self.assertRaisesRegex(RuntimeError, "not prepared"):
            engine.commit(self.transaction.transaction_id)
        self.assertEqual(executor.activated, [])

    def test_abort_flushes_staged_state_and_readback_proves_it(self) -> None:
        executor = RecordingExecutor()
        engine = FormationTransactionEngine(executor, ack_timeout_ms=200)

        engine.prepare(self.transaction)
        aborted = engine.abort(self.transaction.transaction_id)

        self.assertTrue(aborted.accepted)
        self.assertEqual(aborted.phase, TransactionPhase.ABORTED)
        self.assertEqual(executor.readback(self.transaction.transaction_id), ())

    def test_rollback_flushes_activated_state_with_a_distinct_phase(self) -> None:
        executor = RecordingExecutor()
        engine = FormationTransactionEngine(executor, ack_timeout_ms=200)

        engine.prepare(self.transaction)
        engine.commit(self.transaction.transaction_id)
        rolled_back = engine.rollback(self.transaction.transaction_id)

        self.assertTrue(rolled_back.accepted)
        self.assertEqual(rolled_back.phase, TransactionPhase.ROLLED_BACK)
        self.assertEqual(executor.readback(self.transaction.transaction_id), ())


class NetnsPolicyTableBackendTests(unittest.TestCase):
    def test_stages_routes_before_activation_and_flushes_table(self) -> None:
        topology = exp1.ProcessNetnsTopology(3, 2)
        topology.agents = [SimpleNamespace(pid=100 + index) for index in range(3)]
        topology.gateways = [SimpleNamespace(pid=200 + index) for index in range(2)]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2", "10.100.2.2"]
        topology.agent_gateways = [0, 1, 0]
        backend = exp1.NetnsPolicyTableBackend(topology)
        executed: list[tuple[tuple[int | None, tuple[str, ...]], ...]] = []
        topology._parallel_commands = lambda commands, *, check: executed.append(tuple(commands))
        topology._run_ns = lambda *_args, **_kwargs: SimpleNamespace(stdout="[]")
        command = exp1.DeploymentCommand(
            "agent-0",
            100,
            (
                "ip", "route", "replace", "10.101.1.2/32", "via",
                "10.100.0.1", "dev", "eth0",
            ),
        )

        staged = backend.stage("txn-1", (command,))
        activated = backend.activate("txn-1")
        flushed = backend.flush("txn-1")

        self.assertTrue(staged.accepted)
        self.assertTrue(activated.accepted)
        self.assertTrue(flushed.accepted)
        staged_command = executed[0][0][1]
        self.assertEqual(staged_command[:4], ("ip", "route", "replace", "table"))
        self.assertIn("lookup", executed[1][0][1])
        self.assertEqual(executed[2][0][1][:4], ("ip", "rule", "del", "priority"))
        self.assertEqual(executed[2][1][1][:4], ("ip", "route", "flush", "table"))


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
