from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

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
    build_method_transactions,
    build_retry_transactions,
    build_fault_schedule,
    retry_scope_for_method,
    task_descendant_closure,
)


class MethodTransactionAdapterTests(unittest.TestCase):
    @staticmethod
    def _plan(method_id: str, labels: tuple[str, ...]) -> SimpleNamespace:
        batches = tuple(SimpleNamespace(label=label, commands=(label,)) for label in labels)
        return SimpleNamespace(method_id=method_id, ordered_edge_ids=tuple(), batches=batches)

    @staticmethod
    def _edges() -> tuple[FormationEdge, ...]:
        return (
            FormationEdge("e-0", 0, 1, 1.0),
            FormationEdge("e-1", 1, 2, 1.0),
            FormationEdge("e-2", 3, 4, 1.0),
        )

    def test_cspf_independent_flows_are_in_one_parallel_wave(self) -> None:
        edges = self._edges()
        plan = self._plan("cspf", tuple(f"cspf_flow:{edge.edge_id}" for edge in edges))

        waves = build_method_transactions("cspf", plan, edges)

        self.assertEqual(len(waves), 1)
        self.assertEqual({tx.scope_kind for tx in waves[0]}, {"flow"})
        self.assertEqual([tx.logical_edge_id for tx in waves[0]], [edge.edge_id for edge in edges])

    def test_sfc_parallelizes_chains_but_preserves_hop_order(self) -> None:
        edges = self._edges()
        plan = self._plan(
            "global_sfc_embedding",
            (
                "sfc_chain:000:hop:000:routes",
                "sfc_chain:001:hop:000:routes",
                "sfc_chain:000:hop:001:routes",
            ),
        )

        waves = build_method_transactions("global_sfc_embedding", plan, edges)

        self.assertEqual(len(waves), 2)
        self.assertEqual(
            [(tx.chain_id, tx.hop_index) for wave in waves for tx in wave],
            [("chain-000", 0), ("chain-001", 0), ("chain-000", 1)],
        )
        for chain_id in {tx.chain_id for wave in waves for tx in wave}:
            hops = [tx.hop_index for wave in waves for tx in wave if tx.chain_id == chain_id]
            self.assertEqual(hops, list(range(len(hops))))
        self.assertEqual(
            [tx.chain_id for tx in waves[0]], ["chain-000", "chain-001"]
        )
        self.assertEqual(len({tx.chain_id for tx in waves[0]}), len(waves[0]))

    def test_baselines_never_call_task_descendant_closure(self) -> None:
        edges = self._edges()
        cspf_plan = self._plan("cspf", tuple(f"cspf_flow:{edge.edge_id}" for edge in edges))
        sfc_plan = self._plan(
            "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
        )

        with patch(
            "src.controller.formation_transactions.task_descendant_closure",
            side_effect=AssertionError,
        ):
            build_retry_transactions("cspf", "e-1", cspf_plan, edges)
            build_retry_transactions("global_sfc_embedding", "e-1", sfc_plan, edges)

    def test_global_sfc_rejects_invalid_hop_order_before_building_transactions(self) -> None:
        edges = self._edges()
        plan = self._plan(
            "global_sfc_embedding", ("sfc_chain:000:hop:001:routes",)
        )

        with self.assertRaisesRegex(ValueError, "invalid hop order"):
            build_method_transactions("global_sfc_embedding", plan, edges)

    def test_cspf_reserves_residual_capacity_before_parallel_flow_deployment(self) -> None:
        topology = exp1.ProcessNetnsTopology(3, 2)
        topology.agent_gateways = [0, 1, 0]
        edges = (
            FormationEdge("e-0", 0, 1, 1.0),
            FormationEdge("e-1", 1, 2, 1.0),
        )
        endpoints = {
            f"agent-{index}:eth0" for index in range(3)
        } | {
            "gateway-0:ga0", "gateway-1:ga1", "gateway-0:ga2",
            "gateway-0:up0", "gateway-1:up0", "outer:og0", "outer:og1",
        }
        profiles = tuple(
            exp1.TrafficControlProfile(
                endpoint=endpoint, namespace_pid=None, interface="eth0",
                base_delay_ms=1.0, jitter_ms=0.0, packet_loss_percent=0.0,
                bandwidth_mbps=1.5, queue_limit_packets=100,
            )
            for endpoint in endpoints
        )
        with self.assertRaisesRegex(RuntimeError, "pruned e-1"):
            topology._cspf_path_records(edges, profiles, max_path_delay_ms=None)


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

    def test_prepare_audit_uses_backend_kernel_snapshot(self) -> None:
        class SnapshotExecutor(RecordingExecutor):
            def stage(self, transaction, ack_timeout_ms):
                self.staged[transaction.transaction_id] = transaction.commands
                return StageResult(
                    accepted=True,
                    readback_before=({"routes": ["before"]},),
                    readback_after=({"routes": ["after"]},),
                )

        engine = FormationTransactionEngine(SnapshotExecutor(), ack_timeout_ms=200)
        prepared = engine.prepare(self.transaction)

        expected = hashlib.sha256(
            json.dumps(({"routes": ["before"]},), sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        ).hexdigest()
        self.assertEqual(prepared.readback_before_fingerprint, expected)

    def test_post_mutation_readback_failure_is_audited_as_rejected(self) -> None:
        class ReadbackFailureExecutor(RecordingExecutor):
            def readback(self, transaction_id):
                raise RuntimeError("kernel readback unavailable")

        engine = FormationTransactionEngine(ReadbackFailureExecutor(), ack_timeout_ms=200)
        prepared = engine.prepare(self.transaction)

        self.assertFalse(prepared.accepted)
        self.assertEqual(prepared.phase, TransactionPhase.REJECTED)
        self.assertIn("kernel readback unavailable", prepared.reason)
        self.assertTrue(prepared.readback_after_fingerprint)


class NetnsPolicyTableBackendTests(unittest.TestCase):
    def test_stages_routes_before_activation_and_flushes_table(self) -> None:
        topology = exp1.ProcessNetnsTopology(3, 2)
        topology.agents = [SimpleNamespace(pid=100 + index) for index in range(3)]
        topology.gateways = [SimpleNamespace(pid=200 + index) for index in range(2)]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2", "10.100.2.2"]
        topology.agent_gateways = [0, 1, 0]
        backend = exp1.NetnsPolicyTableBackend(topology)
        executed: list[tuple[tuple[int | None, tuple[str, ...]], ...]] = []
        topology._parallel_commands = lambda commands, *, check, timeout=None: executed.append(tuple(commands))
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
        self.assertEqual(executed[2][0][1][:4], ("ip", "route", "flush", "table"))

    class _KernelTopology:
        def __init__(self) -> None:
            self.routes: dict[tuple[int, str], list[dict[str, object]]] = {}
            self.rules: dict[int, list[dict[str, object]]] = {}
            self.executed: list[tuple[tuple[tuple[int | None, tuple[str, ...]], ...], bool, float | None]] = []
            self.readback_calls = 0
            self.fail_cleanup = False
            self.timeout_stage = False
            self.strict_missing_rule_deletion = False
            self.canonical_host_rule = False

        def _run_ns(self, pid, *command, **_kwargs):
            self.readback_calls += 1
            if command[:5] == ("ip", "-j", "route", "show", "table"):
                return SimpleNamespace(stdout=json.dumps(self.routes.get((pid, command[5]), [])))
            if command == ("ip", "-j", "rule", "show"):
                return SimpleNamespace(stdout=json.dumps(self.rules.get(pid, [])))
            raise AssertionError(f"unexpected readback command: {command}")

        def _parallel_commands(self, commands, *, check, timeout=None):
            items = tuple(commands)
            self.executed.append((items, check, timeout))
            if self.timeout_stage and any(item[1][:3] == ("ip", "route", "replace") for item in items):
                raise subprocess.TimeoutExpired("ip", timeout)
            if self.fail_cleanup and any(item[1][:3] == ("ip", "route", "flush") for item in items):
                raise subprocess.CalledProcessError(1, "ip")
            for pid, command in items:
                assert pid is not None
                if command[:4] == ("ip", "route", "replace", "table"):
                    table = command[4]
                    self.routes.setdefault((pid, table), []).append({"dst": command[5]})
                elif command[:3] == ("ip", "route", "flush"):
                    self.routes[(pid, command[4])] = []
                elif command[:3] == ("ip", "rule", "add"):
                    destination = command[6]
                    if self.canonical_host_rule and destination.endswith("/32"):
                        destination = destination.removesuffix("/32")
                    self.rules.setdefault(pid, []).append(
                        {"priority": int(command[4]), "to": destination, "table": command[8]}
                    )
                elif command[:3] == ("ip", "rule", "del"):
                    matching = [
                        rule for rule in self.rules.get(pid, [])
                        if str(rule.get("priority")) == command[4] and str(rule.get("table")) == command[8]
                    ]
                    if self.strict_missing_rule_deletion and not matching:
                        raise subprocess.CalledProcessError(2, command)
                    self.rules[pid] = [
                        rule for rule in self.rules.get(pid, [])
                        if not (str(rule.get("priority")) == command[4] and str(rule.get("table")) == command[8])
                    ]

    @staticmethod
    def _command() -> exp1.DeploymentCommand:
        return exp1.DeploymentCommand(
            "agent-0", 100,
            ("ip", "route", "replace", "10.101.1.2/32", "via", "10.100.0.1", "dev", "eth0"),
        )

    def test_flush_restores_snapshot_and_reads_kernel_after_cleanup(self) -> None:
        topology = self._KernelTopology()
        topology.canonical_host_rule = True
        backend = exp1.NetnsPolicyTableBackend(topology)

        staged = backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)
        backend.activate("txn-1")
        reads_before_flush = topology.readback_calls
        flushed = backend.flush("txn-1")

        self.assertTrue(staged.accepted)
        self.assertTrue(flushed.accepted)
        self.assertEqual(flushed.readback_after, staged.readback_before)
        self.assertGreater(topology.readback_calls, reads_before_flush)
        self.assertEqual(backend.readback("txn-1"), staged.readback_before)
        self.assertTrue(any(
            command[:3] == ("ip", "rule", "del")
            for commands, _, _ in topology.executed for _, command in commands
        ))
        self.assertFalse(exp1._same_rule_destination("10.101.1.3", "10.101.1.2/32"))
        self.assertFalse(exp1._same_rule_destination("10.101.1.0/24", "10.101.1.2/32"))

    def test_cleanup_command_failure_is_reported_and_state_is_retained(self) -> None:
        topology = self._KernelTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)
        backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)
        backend.activate("txn-1")
        topology.fail_cleanup = True

        flushed = backend.flush("txn-1")

        self.assertFalse(flushed.accepted)
        self.assertIn("CalledProcessError", flushed.reason)
        self.assertTrue(backend.readback("txn-1")[0]["routes"])

    def test_rule_only_collision_rejects_an_inactive_table(self) -> None:
        topology = self._KernelTopology()
        topology.rules[100] = [{"priority": 1, "table": "23456"}]
        backend = exp1.NetnsPolicyTableBackend(topology)
        backend._allocate_table = lambda *_args: 23456

        staged = backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)

        self.assertFalse(staged.accepted)
        self.assertIn("not inactive", staged.reason)
        self.assertEqual(len(topology.executed), 0)

    def test_stage_deadline_reaches_commands_and_timeout_rejects_prepare(self) -> None:
        topology = self._KernelTopology()
        topology.timeout_stage = True
        backend = exp1.NetnsPolicyTableBackend(topology)

        staged = backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)

        self.assertFalse(staged.accepted)
        self.assertIn("TimeoutExpired", staged.reason)
        self.assertTrue(all(timeout == 0.2 for _, _, timeout in topology.executed))

    def test_abort_of_unactivated_prepare_skips_absent_rule_deletion_and_releases_table(self) -> None:
        topology = self._KernelTopology()
        topology.strict_missing_rule_deletion = True
        topology.canonical_host_rule = True
        backend = exp1.NetnsPolicyTableBackend(topology)
        staged = backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)
        table_id = staged.readback_before[0]["table_id"]

        aborted = backend.flush("txn-1")

        self.assertTrue(aborted.accepted)
        self.assertFalse(any(
            command[:3] == ("ip", "rule", "del")
            for commands, _, _ in topology.executed for _, command in commands
        ))
        self.assertNotIn(table_id, backend._used_tables)

    def test_stage_failure_cleanup_skips_absent_rule_deletion(self) -> None:
        topology = self._KernelTopology()
        topology.timeout_stage = True
        topology.strict_missing_rule_deletion = True
        backend = exp1.NetnsPolicyTableBackend(topology)

        staged = backend.stage("txn-1", (self._command(),), ack_timeout_ms=200)

        self.assertFalse(staged.accepted)
        self.assertNotIn("cleanup: CalledProcessError", staged.reason)
        self.assertFalse(any(
            command[:3] == ("ip", "rule", "del")
            for commands, _, _ in topology.executed for _, command in commands
        ))


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
