from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import experiments.exp1_netns_verified_formation as exp1
import experiments.exp1_transactional_formation as transactional
from experiments.exp1_netns_verified_formation import FormationEdge
from experiments.exp1_transactional_formation import (
    TransactionalFormationRun,
    build_transactional_schedule,
    load_transactional_config,
    run_transactional_experiment,
    run_transactional_trial,
)
from experiments.paper_protocol import TRANSACTIONAL_EXP1_PROTOCOL
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
        owners = []
        for label in labels:
            if method_id == "cspf":
                owners.append(label.removeprefix("cspf_flow:"))
            elif method_id == "global_sfc_embedding":
                parts = label.split(":")
                owners.append(f"chain-{int(parts[1]):03d}:hop:{int(parts[3]):03d}")
            else:
                owners.append("shared")
        batches = tuple(
            SimpleNamespace(label=label, commands=(label,), command_owners=(owner,))
            for label, owner in zip(labels, owners)
        )
        edge_endpoints = {
            "e-0": (0, 1),
            "e-1": (1, 2),
            "e-2": (3, 4),
        }
        path_records = tuple(
            {
                "edge_id": edge_id,
                "path": (
                    f"agent-{edge_endpoints[edge_id][0]}",
                    "gateway-0",
                    f"agent-{edge_endpoints[edge_id][1]}",
                ),
                "path_endpoints": (
                    f"agent-{edge_endpoints[edge_id][0]}:eth0",
                    f"gateway-0:ga{edge_endpoints[edge_id][0]}",
                    f"gateway-0:ga{edge_endpoints[edge_id][1]}",
                    f"agent-{edge_endpoints[edge_id][1]}:eth0",
                ),
                "required_throughput_mbps": 1.0,
                "bottleneck_mbps": 2.0,
                "residual_bottleneck_after_mbps": 1.0,
                "delay_cost_ms": 1.0,
                "max_path_delay_ms": None,
                "tie_break": edge_id,
                "feasible": True,
            }
            for edge_id in ("e-0", "e-1", "e-2")
        )
        return SimpleNamespace(
            method_id=method_id, ordered_edge_ids=tuple(), batches=batches,
            service_placements={index: index for index in range(5)}
            if method_id == "global_sfc_embedding" else None,
            placement_capacity={"agent-0": 2, "agent-1": 2, "agent-2": 2,
                                "agent-3": 2, "agent-4": 2}
            if method_id == "global_sfc_embedding" else None,
            service_chain_count=0,
            path_records=path_records
            if method_id == "global_sfc_embedding" else (),
            sfc_evidence={
                "service_availability": tuple({"node": index, "agent": index, "available": True} for index in range(5)),
                "placement_capacity": {"agent-0": 2, "agent-1": 2, "agent-2": 2,
                                        "agent-3": 2, "agent-4": 2},
                "placement_slot_inventory": {"agent-0": 2, "agent-1": 2, "agent-2": 2,
                                              "agent-3": 2, "agent-4": 2},
                "placement_slot_demand": {"agent-0": 1, "agent-1": 1, "agent-2": 1,
                                           "agent-3": 1, "agent-4": 1},
                "placement_feasible": True,
                "chain_order_valid": True,
                "path_feasible": True,
                "path_records": path_records,
            } if method_id == "global_sfc_embedding" else None,
        )

    @staticmethod
    def _replace_sfc_path_records(
        plan: SimpleNamespace, records: tuple[object, ...]
    ) -> None:
        plan.path_records = records
        plan.sfc_evidence = {**plan.sfc_evidence, "path_records": records}

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
        for wave in waves:
            self.assertEqual(
                len({tx.chain_id for tx in wave}), len(wave),
                "a wave may contain at most one dependency hop per chain",
            )
        self.assertEqual(
            {(tx.chain_id, tx.commands) for tx in waves[0]},
            {
                ("chain-000", ("sfc_chain:000:hop:000:routes",)),
                ("chain-001", ("sfc_chain:001:hop:000:routes",)),
            },
        )
        self.assertEqual(waves[1][0].commands, ("sfc_chain:000:hop:001:routes",))

    def test_baselines_never_call_task_descendant_closure(self) -> None:
        edges = self._edges()
        cspf_plan = self._plan("cspf", tuple(f"cspf_flow:{edge.edge_id}" for edge in edges))
        sfc_plan = self._plan(
            "global_sfc_embedding", (
                "sfc_chain:000:hop:000:routes", "sfc_chain:000:hop:001:routes",
                "sfc_chain:001:hop:000:routes",
            )
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

    def test_global_sfc_requires_all_planning_evidence(self) -> None:
        edges = self._edges()
        valid = {
            "service_availability": (True,),
            "placement_capacity": {"chain-000": 3.0, "chain-001": 1.0},
            "placement_feasible": True,
            "chain_order_valid": True,
            "path_feasible": True,
        }
        cases = {
            "missing evidence": None,
            "missing service availability": {
                key: value for key, value in valid.items() if key != "service_availability"
            },
            "unavailable service": {**valid, "service_availability": (False,)},
            "missing placement capacity": {
                **valid, "placement_capacity": {}
            },
            "insufficient placement": {**valid, "placement_feasible": False},
            "invalid chain ordering": {**valid, "chain_order_valid": False},
            "infeasible path": {**valid, "path_feasible": False},
        }
        for name, evidence in cases.items():
            with self.subTest(name=name):
                plan = self._plan("global_sfc_embedding", ("sfc_chain:000:hop:000:routes",))
                plan.sfc_evidence = evidence
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)

        for field, value in (("service_placements", ()), ("placement_capacity", {})):
            with self.subTest(missing_field=field):
                plan = self._plan("global_sfc_embedding", ("sfc_chain:000:hop:000:routes",))
                setattr(plan, field, value)
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)

    def test_proposed_waves_and_descendant_retry_use_exact_edge_ownership(self) -> None:
        edges = (
            FormationEdge("e-0", 0, 1, 1.0),
            FormationEdge("e-1", 1, 2, 1.0),
            FormationEdge("e-2", 2, 3, 1.0),
        )
        batches = (
            SimpleNamespace(
                label="task_dag_parallel_batch",
                commands=("shared", "ancestor", "failed", "descendant", "sibling"),
                command_owners=("shared", "e-0", "e-1", "e-2", "e-other"),
            ),
        )
        plan = SimpleNamespace(
            method_id="proposed", ordered_edge_ids=("e-0", "e-1", "e-2"), batches=batches
        )

        waves = build_method_transactions("proposed", plan, edges)
        retry = build_retry_transactions("proposed", "e-1", plan, edges)

        self.assertEqual([[tx.logical_edge_id for tx in wave] for wave in waves], [[None], ["e-0"], ["e-1"], ["e-2"]])
        self.assertEqual(waves[0][0].scope_kind, "shared")
        self.assertEqual(waves[0][0].commands, ("shared",))
        self.assertEqual(waves[1][0].commands, ("ancestor",))
        self.assertEqual(waves[2][0].commands, ("failed",))
        self.assertEqual(waves[3][0].commands, ("descendant",))
        self.assertEqual([tx.logical_edge_id for wave in retry for tx in wave], ["e-1", "e-2"])
        self.assertEqual([tx.commands for wave in retry for tx in wave], [("failed",), ("descendant",)])

    def test_cspf_retry_uses_exact_ownership_without_substring_collisions(self) -> None:
        edges = (FormationEdge("e-1", 0, 1, 1.0), FormationEdge("e-10", 1, 2, 1.0))
        plan = SimpleNamespace(
            method_id="cspf", ordered_edge_ids=("e-1", "e-10"),
            batches=(
                SimpleNamespace(label="cspf_flow:e-1", commands=("one",), command_owners=("e-1",)),
                SimpleNamespace(label="cspf_flow:e-10", commands=("ten",), command_owners=("e-10",)),
            ),
        )

        retry = build_retry_transactions("cspf", "e-1", plan, edges)

        self.assertEqual(retry[0][0].commands, ("one",))

    def test_cspf_rejects_empty_owned_flow_payload(self) -> None:
        edge = FormationEdge("e-empty", 0, 1, 1.0)
        plan = SimpleNamespace(
            method_id="cspf", ordered_edge_ids=("e-empty",),
            batches=(SimpleNamespace(
                label="cspf_flow:e-empty", commands=(), command_owners=()
            ),),
        )

        with self.assertRaisesRegex(ValueError, "no owned commands"):
            build_method_transactions("cspf", plan, (edge,))

    @staticmethod
    def _sfc_topology_and_profiles(
        *, bandwidth_mbps: float = 10.0, base_delay_ms: float = 1.0
    ) -> tuple[exp1.ProcessNetnsTopology, tuple[exp1.TrafficControlProfile, ...]]:
        topology = exp1.ProcessNetnsTopology(2, 2)
        topology.agents = [SimpleNamespace(pid=100), SimpleNamespace(pid=101)]
        topology.gateways = [SimpleNamespace(pid=200), SimpleNamespace(pid=201)]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2"]
        topology.agent_gateways = [0, 1]
        endpoints = (
            "agent-0:eth0", "gateway-0:ga0", "gateway-0:up0", "outer:og0",
            "outer:og1", "gateway-1:up0", "gateway-1:ga1", "agent-1:eth0",
        )
        return topology, tuple(
            exp1.TrafficControlProfile(
                endpoint=endpoint, namespace_pid=None, interface="eth0",
                base_delay_ms=base_delay_ms, jitter_ms=0.0,
                packet_loss_percent=0.0, bandwidth_mbps=bandwidth_mbps,
                queue_limit_packets=100,
            ) for endpoint in endpoints
        )

    def test_global_sfc_planner_rejects_bandwidth_and_delay_path_failures(self) -> None:
        edge = FormationEdge("e-path", 0, 1, 1.0)
        topology, profiles = self._sfc_topology_and_profiles(bandwidth_mbps=0.5)
        with self.assertRaisesRegex(RuntimeError, "pruned e-path"):
            topology.plan_task_routes("global_sfc_embedding", (edge,), profiles=profiles)
        topology, profiles = self._sfc_topology_and_profiles(base_delay_ms=2.0)
        with self.assertRaisesRegex(RuntimeError, "path delay"):
            topology.plan_task_routes(
                "global_sfc_embedding", (edge,), profiles=profiles, max_path_delay_ms=10.0
            )

    def test_global_sfc_planner_emits_independent_evidence_for_valid_path(self) -> None:
        edge = FormationEdge("e-path", 0, 1, 1.0)
        topology, profiles = self._sfc_topology_and_profiles()
        plan = topology.plan_task_routes(
            "global_sfc_embedding", (edge,), profiles=profiles, max_path_delay_ms=20.0
        )
        waves = build_method_transactions("global_sfc_embedding", plan, (edge,))
        self.assertTrue(plan.sfc_evidence["placement_slot_inventory"])
        self.assertEqual(plan.sfc_evidence["placement_slot_demand"], {"agent-0": 1, "agent-1": 1})
        self.assertEqual(plan.sfc_evidence["path_records"], plan.path_records)
        self.assertEqual(waves[0][0].commands, tuple(
            command for batch in plan.batches if batch.label.startswith("sfc_chain:")
            for command in batch.commands
        ))

    def test_global_sfc_rejects_dead_assigned_agent_from_observable_inventory(self) -> None:
        edge = FormationEdge("e-dead", 0, 1, 1.0)
        topology, profiles = self._sfc_topology_and_profiles()
        topology.agents[1] = SimpleNamespace(
            pid=101, process=SimpleNamespace(poll=lambda: 1)
        )
        plan = topology.plan_task_routes(
            "global_sfc_embedding", (edge,), profiles=profiles, max_path_delay_ms=20.0
        )
        with self.assertRaisesRegex(ValueError, "service availability"):
            build_method_transactions("global_sfc_embedding", plan, (edge,))

    def test_global_sfc_rejects_incomplete_slot_demand_and_substantive_path_errors(self) -> None:
        edges = self._edges()
        plan = self._plan("global_sfc_embedding", ("sfc_chain:000:hop:000:routes",))
        for demand in ({}, {"agent-0": 1}):
            with self.subTest(demand=demand):
                plan.sfc_evidence = {**plan.sfc_evidence, "placement_slot_demand": demand}
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)
        valid_records = list(plan.path_records)
        for name, changes in (
            ("bare", {"path": None}),
            ("demand", {"required_throughput_mbps": 9.0}),
            ("negative residual", {"residual_bottleneck_after_mbps": -1.0}),
            ("inconsistent residual", {"residual_bottleneck_after_mbps": 0.5}),
            ("delay bound", {"max_path_delay_ms": 0.0}),
        ):
            with self.subTest(path_error=name):
                records = [dict(record) for record in valid_records]
                records[0].update(changes)
                plan.path_records = tuple(records)
                plan.sfc_evidence = {**self._plan(
                    "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
                ).sfc_evidence, "path_records": tuple(records)}
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)

    def test_global_sfc_rejects_arbitrary_path_and_endpoint_evidence(self) -> None:
        edges = self._edges()
        plan = self._plan("global_sfc_embedding", ("sfc_chain:000:hop:000:routes",))
        records = [dict(record) for record in plan.path_records]
        records[0].update({"path": ("x",), "path_endpoints": ("y",)})
        self._replace_sfc_path_records(plan, tuple(records))

        with self.assertRaises(ValueError):
            build_method_transactions("global_sfc_embedding", plan, edges)

    def test_global_sfc_rejects_reversed_or_mismatched_endpoint_order(self) -> None:
        edges = self._edges()
        valid_plan = self._plan(
            "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
        )
        for name, endpoints in (
            ("reversed", tuple(reversed(valid_plan.path_records[0]["path_endpoints"]))),
            (
                "owner order mismatch",
                (
                    "agent-0:eth0",
                    "agent-1:eth0",
                    "gateway-0:ga0",
                    "agent-1:eth0",
                ),
            ),
        ):
            with self.subTest(name=name):
                plan = self._plan(
                    "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
                )
                records = [dict(record) for record in plan.path_records]
                records[0]["path_endpoints"] = endpoints
                self._replace_sfc_path_records(plan, tuple(records))
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)

    def test_global_sfc_rejects_duplicate_or_non_mapping_path_records(self) -> None:
        edges = self._edges()
        for name, extra_record in (
            ("duplicate", dict(self._plan(
                "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
            ).path_records[0])),
            ("non-mapping", "not-a-path-record"),
        ):
            with self.subTest(name=name):
                plan = self._plan(
                    "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
                )
                records = (*plan.path_records, extra_record)
                self._replace_sfc_path_records(plan, records)
                with self.assertRaises(ValueError):
                    build_method_transactions("global_sfc_embedding", plan, edges)

    def test_global_sfc_rejects_boolean_numeric_evidence(self) -> None:
        edges = self._edges()
        for name in ("path demand", "slot inventory"):
            with self.subTest(name=name):
                plan = self._plan(
                    "global_sfc_embedding", ("sfc_chain:000:hop:000:routes",)
                )
                if name == "path demand":
                    records = [dict(record) for record in plan.path_records]
                    records[0]["required_throughput_mbps"] = True
                    self._replace_sfc_path_records(plan, tuple(records))
                else:
                    plan.sfc_evidence = {
                        **plan.sfc_evidence,
                        "placement_slot_inventory": {
                            **plan.sfc_evidence["placement_slot_inventory"],
                            "agent-0": True,
                        },
                    }
                with self.assertRaises(ValueError):
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

    def test_same_transaction_prepare_is_serialized_without_blocking_other_ids(self) -> None:
        class BlockingExecutor(RecordingExecutor):
            def __init__(self):
                super().__init__()
                self.stage_started = threading.Event()
                self.release_stage = threading.Event()
                self.stage_calls = 0

            def stage(self, transaction, ack_timeout_ms):
                self.stage_calls += 1
                if transaction.transaction_id == "txn-1":
                    self.stage_started.set()
                    self.release_stage.wait(1.0)
                return super().stage(transaction, ack_timeout_ms)

        executor = BlockingExecutor()
        engine = FormationTransactionEngine(executor, ack_timeout_ms=200)
        other_transaction = FormationTransaction(
            transaction_id="txn-2",
            attempt_index=0,
            expected_versions=(),
            commands=("ip route replace 10.0.1.2/32",),
            affected_objects=("edge-1",),
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(engine.prepare, self.transaction)
            self.assertTrue(executor.stage_started.wait(1.0))
            second = pool.submit(engine.prepare, self.transaction)
            other = pool.submit(engine.prepare, other_transaction)
            self.assertTrue(other.result(timeout=0.2).accepted)
            time.sleep(0.02)
            executor.release_stage.set()
            outcomes = []
            for future in (first, second):
                try:
                    outcomes.append(future.result())
                except RuntimeError as error:
                    outcomes.append(error)

        self.assertEqual(executor.stage_calls, 2)
        self.assertEqual(sum(isinstance(item, TransactionAttempt) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, RuntimeError) for item in outcomes), 1)
        self.assertEqual(len(engine.attempts), 2)

    def test_concurrent_unique_transactions_record_every_prepare_and_commit(self) -> None:
        engine = FormationTransactionEngine(RecordingExecutor(), ack_timeout_ms=200)
        transactions = tuple(
            FormationTransaction(
                transaction_id=f"txn-{index}",
                attempt_index=0,
                expected_versions=(),
                commands=(f"command-{index}",),
                affected_objects=(f"edge-{index}",),
            )
            for index in range(64)
        )

        with ThreadPoolExecutor(max_workers=16) as pool:
            prepared = tuple(pool.map(engine.prepare, transactions))
            committed = tuple(
                pool.map(lambda tx: engine.commit(tx.transaction_id), transactions)
            )

        self.assertTrue(all(item.accepted for item in prepared + committed))
        self.assertEqual(len(engine.attempts), 128)
        self.assertEqual(
            len({(item.transaction_id, item.operation) for item in engine.attempts}),
            128,
        )


class NetnsPolicyTableBackendTests(unittest.TestCase):
    def test_stages_routes_before_activation_and_flushes_table(self) -> None:
        topology = exp1.ProcessNetnsTopology(3, 2)
        topology.agents = [SimpleNamespace(pid=100 + index) for index in range(3)]
        topology.gateways = [SimpleNamespace(pid=200 + index) for index in range(2)]
        topology.agent_ips = ["10.100.0.2", "10.101.1.2", "10.100.2.2"]
        topology.agent_gateways = [0, 1, 0]
        backend = exp1.NetnsPolicyTableBackend(topology)
        executed: list[tuple[tuple[int | None, tuple[str, ...]], ...]] = []
        topology._parallel_commands = lambda commands, *, check, timeout=None, deadline=None: executed.append(tuple(commands))
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
            self.deadlines: list[float | None] = []
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

        def _parallel_commands(self, commands, *, check, timeout=None, deadline=None):
            items = tuple(commands)
            self.executed.append((items, check, timeout))
            self.deadlines.append(deadline)
            if self.timeout_stage and any(item[1][:3] == ("ip", "route", "replace") for item in items):
                remaining = max(0.0, deadline - time.perf_counter()) if deadline else timeout
                raise subprocess.TimeoutExpired("ip", remaining)
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
        self.assertIsNotNone(topology.deadlines[0])
        self.assertGreater(topology.deadlines[0], time.perf_counter())

    def test_stage_uses_one_absolute_deadline_for_all_concrete_commands(self) -> None:
        topology = self._KernelTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)

        staged = backend.stage("txn-absolute", (self._command(),), ack_timeout_ms=200)

        self.assertTrue(staged.accepted)
        self.assertEqual(len(topology.deadlines), 1)
        self.assertIsNotNone(topology.deadlines[0])
        self.assertGreater(topology.deadlines[0], time.perf_counter())

    def test_duplicate_concurrent_stage_reserves_one_transaction(self) -> None:
        topology = self._KernelTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)
        iteration_started = threading.Event()
        release_iteration = threading.Event()

        class BlockingCommands:
            def __init__(self):
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                if self.iterations == 1:
                    iteration_started.set()
                    release_iteration.wait(1.0)
                return iter((NetnsPolicyTableBackendTests._command(),))

        commands = BlockingCommands()
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(backend.stage, "txn-concurrent", commands)
            self.assertTrue(iteration_started.wait(1.0))
            second = pool.submit(backend.stage, "txn-concurrent", commands)
            other = pool.submit(backend.stage, "txn-independent", commands)
            self.assertTrue(other.result(timeout=0.2).accepted)
            time.sleep(0.02)
            release_iteration.set()
            results = (first.result(), second.result())

        self.assertEqual(sum(item.accepted for item in results), 1)
        self.assertEqual(len(backend._transactions), 2)
        self.assertEqual(len(backend._used_tables), 2)

    def test_concurrent_transactions_allocate_unique_policy_tables(self) -> None:
        topology = self._KernelTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = tuple(
                pool.map(
                    lambda index: backend.stage(
                        f"txn-{index}", (self._command(),), ack_timeout_ms=200
                    ),
                    range(64),
                )
            )

        self.assertTrue(all(item.accepted for item in results))
        tables = [
            next(iter(staged.table_by_pid.values()))
            for staged in backend._transactions.values()
        ]
        self.assertEqual(len(tables), 64)
        self.assertEqual(len(set(tables)), 64)

    def test_prepare_deadline_covers_pre_stage_and_post_stage_readback(self) -> None:
        class CumulativeTopology:
            def __init__(self):
                self.read_timeouts = []
                self.read_calls = 0
                self.timed_out = False

            def _run_ns(self, pid, *command, timeout=None, **_kwargs):
                self.read_calls += 1
                self.read_timeouts.append(timeout)
                delay = 0.025 if self.read_calls <= 3 else 0.0
                if timeout is not None and timeout < delay:
                    threading.Event().wait(max(0.0, timeout))
                    self.timed_out = True
                    raise subprocess.TimeoutExpired(command, timeout)
                threading.Event().wait(delay)
                return SimpleNamespace(stdout="[]")

            def _parallel_commands(
                self, commands, *, check, timeout=None, deadline=None
            ):
                if deadline is not None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("stage", 0.0)
                    threading.Event().wait(min(0.015, remaining))

        topology = CumulativeTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)
        started = time.perf_counter()

        staged = backend.stage(
            "txn-cumulative", (self._command(),), ack_timeout_ms=80
        )
        elapsed = time.perf_counter() - started

        self.assertFalse(staged.accepted)
        self.assertTrue(topology.timed_out)
        self.assertIn("post-stage readback failed", staged.reason)
        self.assertLess(elapsed, 0.14)
        self.assertGreater(topology.read_timeouts[0], topology.read_timeouts[2])

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


class _RecordingTransactionalTopology:
    def __init__(
        self, num_agents: int, num_gateways: int, *,
        fail_verify: bool = False, pid_offset: int = 0,
        common_delay_s: float = 0.0, cleanup_delay_s: float = 0.0,
        fail_abort: bool = False, cleanup_error: bool = False,
        teardown_error: bool = False,
    ) -> None:
        self.num_agents = num_agents
        self.num_gateways = min(num_gateways, num_agents)
        self.fail_verify = fail_verify
        self.pid_offset = pid_offset
        self.common_delay_s = common_delay_s
        self.cleanup_delay_s = cleanup_delay_s
        self.fail_abort = fail_abort
        self.cleanup_error = cleanup_error
        self.teardown_error = teardown_error
        self.planner_inputs: list[dict[str, object]] = []
        self.common_installs: list[tuple[exp1.DeploymentCommand, ...]] = []
        self.staged: dict[str, tuple[object, ...]] = {}
        self.activated: set[str] = set()
        self.verifier_calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self.teardown_error:
            raise RuntimeError("topology teardown exploded")
        return None

    def reset_task_routes(self) -> None:
        self.staged.clear()
        self.activated.clear()

    def configure_traffic_control(self, _config, *, seed):
        return ()

    def start_background_traffic(self, _config, _profiles, *, seed):
        return object()

    def stop_background_traffic(self, _background):
        threading.Event().wait(self.cleanup_delay_s)
        if self.cleanup_error:
            raise RuntimeError("background cleanup exploded")
        return {"enabled": False}

    def common_infrastructure_commands(self):
        return (
            exp1.DeploymentCommand(
                "gateway-0", 200 + self.pid_offset,
                ("ip", "route", "replace", "default", "via", "10.200.0.1", "dev", "up0"),
            ),
            exp1.DeploymentCommand(
                "outer", None,
                ("ip", "route", "replace", "10.100.0.2/32", "via", "10.200.0.2", "dev", "og0"),
            ),
        )

    def install_common_infrastructure(self, commands):
        commands = tuple(commands)
        self.assert_common(commands)
        threading.Event().wait(self.common_delay_s)
        self.common_installs.append(commands)
        return len(commands)

    def assert_common(self, commands):
        if commands != self.common_infrastructure_commands():
            raise AssertionError("method-specific common infrastructure")

    def _route(self, edge: FormationEdge, owner: int) -> exp1.DeploymentCommand:
        return exp1.DeploymentCommand(
            f"agent-{owner}", 100 + owner + self.pid_offset,
            (
                "ip", "route", "replace", f"10.0.{edge.target_index}.2/32",
                "via", f"10.0.{owner}.1", "dev", "eth0",
            ),
        )

    def plan_task_routes(self, method_id, edges, *, profiles, max_path_delay_ms):
        self.planner_inputs.append(
            {
                "method_id": method_id,
                "edges": tuple(edge.edge_id for edge in edges),
                "profiles": tuple(profiles),
                "max_path_delay_ms": max_path_delay_ms,
            }
        )
        ordered = tuple(sorted(edges, key=lambda edge: edge.edge_id))
        common = self.common_infrastructure_commands()
        if method_id == "proposed":
            owned = tuple(self._route(edge, edge.source_index) for edge in ordered)
            batches = (
                exp1.DeploymentBatch(
                    "task_dag_parallel_batch", common + owned,
                    ("shared",) * len(common) + tuple(edge.edge_id for edge in ordered),
                ),
            )
            chains = ()
        elif method_id == "cspf":
            batches = (
                exp1.DeploymentBatch(
                    "cspf_shared_infrastructure", common, ("shared",) * len(common)
                ),
                *(
                    exp1.DeploymentBatch(
                        f"cspf_flow:{edge.edge_id}",
                        (self._route(edge, edge.source_index),),
                        (edge.edge_id,),
                    )
                    for edge in ordered
                ),
            )
            chains = ()
        else:
            chains = exp1._service_chains(ordered)
            chain_batches = []
            for chain_index, chain in enumerate(chains):
                for hop_index, edge in enumerate(chain):
                    owner = f"chain-{chain_index:03d}:hop:{hop_index:03d}"
                    chain_batches.append(
                        exp1.DeploymentBatch(
                            f"sfc_chain:{chain_index:03d}:hop:{hop_index:03d}:routes",
                            (self._route(edge, edge.source_index),),
                            (owner,),
                        )
                    )
                    chain_batches.append(
                        exp1.DeploymentBatch(
                            f"sfc_chain:{chain_index:03d}:hop:{hop_index:03d}:activate",
                            (
                                exp1.DeploymentCommand(
                                    f"agent-{edge.source_index}",
                                    100 + edge.source_index + self.pid_offset,
                                    (
                                        "ip", "rule", "add", "priority", "10000",
                                        "to", f"10.0.{edge.target_index}.2/32",
                                        "lookup", "100",
                                    ),
                                ),
                            ),
                            (owner,),
                        )
                    )
            batches = (
                exp1.DeploymentBatch(
                    "sfc_shared_infrastructure", common, ("shared",) * len(common)
                ),
                *chain_batches,
            )
        placements = {index: index for index in range(self.num_agents)}
        capacities = {f"agent-{index}": 2 for index in range(self.num_agents)}
        path_records = tuple(
            {
                "edge_id": edge.edge_id,
                "path": (f"agent-{edge.source_index}", "gateway-0", f"agent-{edge.target_index}"),
                "path_endpoints": (
                    f"agent-{edge.source_index}:eth0", "gateway-0:in",
                    "gateway-0:out", f"agent-{edge.target_index}:eth0",
                ),
                "required_throughput_mbps": edge.required_throughput_mbps,
                "bottleneck_mbps": edge.required_throughput_mbps + 1.0,
                "residual_bottleneck_after_mbps": 1.0,
                "delay_cost_ms": 1.0,
                "max_path_delay_ms": max_path_delay_ms,
                "tie_break": edge.edge_id,
                "feasible": True,
            }
            for edge in ordered
        )
        evidence = None
        if method_id == "global_sfc_embedding":
            evidence = {
                "service_availability": tuple(
                    {"node": index, "agent": index, "available": True}
                    for index in range(self.num_agents)
                ),
                "placement_capacity": capacities,
                "placement_slot_inventory": capacities,
                "placement_slot_demand": {
                    f"agent-{index}": 1 for index in range(self.num_agents)
                },
                "placement_feasible": True,
                "chain_order_valid": True,
                "path_feasible": True,
                "path_records": path_records,
            }
        return exp1.FormationDeploymentPlan(
            method_id=method_id,
            planning_policy=f"recording-{method_id}",
            tie_break="canonical_edge_id",
            ordered_edge_ids=tuple(edge.edge_id for edge in ordered),
            service_chain_count=len(chains),
            planning_work_units=len(ordered),
            batches=batches,
            path_records=path_records if method_id == "global_sfc_embedding" else (),
            service_placements=placements if method_id == "global_sfc_embedding" else None,
            placement_capacity=capacities if method_id == "global_sfc_embedding" else None,
            sfc_evidence=evidence,
        )

    def stage_transaction_commands(self, transaction_id, commands, *, ack_timeout_ms):
        commands = tuple(commands)
        if any(
            command.namespace in {"outer", "gateway-0"}
            or command.argv[:3] != ("ip", "route", "replace")
            for command in commands
        ):
            raise AssertionError("unsupported shared command reached policy backend")
        self.staged[transaction_id] = commands
        return StageResult(
            accepted=True,
            commands_attempted=len(commands),
            readback_after=tuple({"command": str(command.argv)} for command in commands),
        )

    def activate_transaction(self, transaction_id):
        if transaction_id not in self.staged:
            return CommandResult(accepted=False, reason="not staged")
        self.activated.add(transaction_id)
        return CommandResult(accepted=True, commands_attempted=1, readback_after=self.read_transaction_state(transaction_id))

    def abort_transaction(self, transaction_id):
        if self.fail_abort:
            return CommandResult(
                accepted=False,
                reason="kernel cleanup failed",
                readback_after=self.read_transaction_state(transaction_id),
            )
        self.staged.pop(transaction_id, None)
        self.activated.discard(transaction_id)
        return CommandResult(accepted=True, commands_attempted=1, readback_after=())

    def read_transaction_state(self, transaction_id):
        return tuple(
            {"command": str(command.argv), "active": transaction_id in self.activated}
            for command in self.staged.get(transaction_id, ())
        )

    def verify_ping(self, edges, **kwargs):
        if self.fail_verify:
            raise RuntimeError("verifier exploded")
        self.verifier_calls.append(("ping", dict(kwargs)))
        return len(edges), [
            {"edge_id": edge.edge_id, "passed": True, "timeout": False}
            for edge in edges
        ]

    def verify_iperf3(self, edges, **kwargs):
        self.verifier_calls.append(("iperf3", dict(kwargs)))
        return len(edges), float(len(edges)), [
            {"edge_id": edge.edge_id, "passed": True, "timeout": False}
            for edge in edges
        ]


class TransactionalRunnerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.formal = load_transactional_config(
            cls.root / "configs" / "exp1_transactional_formation_v1.yaml"
        )
        cls.pilot = load_transactional_config(
            cls.root / "configs" / "exp1_transactional_formation_pilot_v1.yaml"
        )

    def test_frozen_grids_and_protocol_registration_are_exact(self) -> None:
        self.assertEqual(len(build_transactional_schedule(self.formal)), 2250)
        self.assertEqual(len(build_transactional_schedule(self.pilot)), 900)
        self.assertEqual(self.formal["experiment"]["phase"], "formal")
        self.assertEqual(self.pilot["experiment"]["phase"], "pilot")
        self.assertEqual(self.formal["transaction"], self.pilot["transaction"])
        self.assertEqual(self.formal["verification"], self.pilot["verification"])
        self.assertEqual(
            TRANSACTIONAL_EXP1_PROTOCOL["formal_config"],
            "configs/exp1_transactional_formation_v1.yaml",
        )

    def test_pilot_differs_only_by_phase_seeds_and_output_provenance(self) -> None:
        import yaml

        formal_source = yaml.safe_load(
            (self.root / "configs" / "exp1_transactional_formation_v1.yaml")
            .read_text(encoding="utf-8")
        )
        pilot_source = yaml.safe_load(
            (self.root / "configs" / "exp1_transactional_formation_pilot_v1.yaml")
            .read_text(encoding="utf-8")
        )

        def differing_paths(left, right, prefix=()):
            if isinstance(left, dict) and isinstance(right, dict):
                return {
                    path
                    for key in left.keys() | right.keys()
                    for path in differing_paths(left.get(key), right.get(key), prefix + (key,))
                }
            return set() if left == right else {prefix}

        self.assertEqual(
            differing_paths(formal_source, pilot_source),
            {
                ("experiment", "phase"),
                ("simulation", "seeds"),
                ("output", "provenance"),
            },
        )

    def test_override_schedule_is_the_invocation_cartesian_grid(self) -> None:
        schedule = build_transactional_schedule(
            self.pilot,
            seeds=(9000, 9001),
            task_sizes=(4,),
            methods=("proposed", "cspf", "global_sfc_embedding"),
            scenario_classes=("command_rejection",),
        )

        self.assertEqual(len(schedule), 6)
        self.assertEqual(
            {
                (item.seed, item.num_agents, item.method_id, item.scenario_class)
                for item in schedule
            },
            {
                (seed, 4, method, "command_rejection")
                for seed in (9000, 9001)
                for method in ("proposed", "cspf", "global_sfc_embedding")
            },
        )

    def test_fault_is_hidden_shared_infrastructure_is_common_and_verifier_is_paired(self) -> None:
        paired = []
        topologies = []
        for method_index, method in enumerate(
            ("proposed", "cspf", "global_sfc_embedding")
        ):
            topology = _RecordingTransactionalTopology(4, 4, pid_offset=method_index * 1000)
            row, events = run_transactional_trial(
                topology,
                self.formal,
                method_id=method,
                scenario_class="command_rejection",
                seed=0,
                num_agents=4,
            )
            paired.append(row)
            topologies.append(topology)
            self.assertTrue(row.success)
            self.assertEqual(len(topology.common_installs), 1)
            self.assertNotIn("fault_schedule", topology.planner_inputs[0])
            self.assertEqual(
                len([event for event in events if event["stage"] == "FAULT_INJECTED"]),
                1,
            )
            self.assertTrue(topology.verifier_calls)

        self.assertEqual(len({row.fault_schedule_fingerprint for row in paired}), 1)
        self.assertEqual(len({row.verifier_fingerprint for row in paired}), 1)
        self.assertEqual(len({row.common_infrastructure_fingerprint for row in paired}), 1)

    def test_ack_timeout_waits_for_a_real_deadline_then_retries(self) -> None:
        topology = _RecordingTransactionalTopology(4, 4)
        started = time.perf_counter()

        row, events = run_transactional_trial(
            topology,
            self.formal,
            method_id="cspf",
            scenario_class="prepare_ack_timeout",
            seed=0,
            num_agents=4,
        )

        self.assertTrue(row.success)
        self.assertGreaterEqual(time.perf_counter() - started, 0.18)
        injected = [event for event in events if event["stage"] == "FAULT_INJECTED"]
        self.assertEqual(injected[0]["details"]["mechanism"], "threading.Event.wait")

    def test_exception_is_retained_and_invocation_grid_is_persisted(self) -> None:
        def topology_factory(num_agents, num_gateways):
            return _RecordingTransactionalTopology(num_agents, num_gateways, fail_verify=True)

        with tempfile.TemporaryDirectory() as directory:
            rows = run_transactional_experiment(
                self.formal,
                Path(directory),
                seeds=(7,),
                task_sizes=(4,),
                methods=("proposed",),
                scenario_classes=("command_rejection",),
                require_complete_grid=True,
                topology_factory=topology_factory,
            )
            raw = Path(directory) / "raw"
            scope = json.loads((raw / "measurement_scope.json").read_text(encoding="utf-8"))
            attempt_lines = (raw / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
            run_lines = (raw / "runs.csv").read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].success)
        self.assertIn("verifier exploded", rows[0].failure_reason)
        self.assertEqual(len(run_lines), 2)
        self.assertTrue(attempt_lines)
        self.assertEqual(scope["phase"], "formal")
        self.assertEqual(scope["invocation_grid"]["seeds"], [7])
        self.assertEqual(scope["invocation_grid"]["expected_rows"], 1)
        self.assertNotEqual(scope["invocation_grid"], scope["frozen_config_grid"])
        self.assertNotEqual(
            scope["configuration_sha256"], scope["invocation_grid_sha256"]
        )

    def test_override_safety_rejects_formal_default_output_and_existing_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "pilot-output"
            overrides = {"seeds": (9000,)}
            with self.assertRaisesRegex(RuntimeError, "formal"):
                transactional._validate_cli_invocation(
                    self.formal,
                    output_dir,
                    has_overrides=True,
                    output_was_explicit=True,
                )
            with self.assertRaisesRegex(RuntimeError, "explicit noncanonical"):
                transactional._validate_cli_invocation(
                    self.pilot,
                    transactional.DEFAULT_OUTPUT_DIR,
                    has_overrides=True,
                    output_was_explicit=True,
                )
            (output_dir / "raw").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "existing raw"):
                transactional._validate_cli_invocation(
                    self.pilot,
                    output_dir,
                    has_overrides=True,
                    output_was_explicit=True,
                )
            with self.assertRaisesRegex(RuntimeError, "existing raw"):
                run_transactional_experiment(
                    self.pilot,
                    output_dir,
                    seeds=(9000,),
                    task_sizes=(4,),
                    methods=("cspf",),
                    scenario_classes=("command_rejection",),
                )
            self.assertEqual(overrides["seeds"], (9000,))

    def test_method_owned_filter_only_omits_known_sfc_activation_rules(self) -> None:
        route = exp1.DeploymentCommand(
            "agent-0", 100,
            ("ip", "route", "replace", "10.0.1.2/32", "via", "10.0.0.1", "dev", "eth0"),
        )
        unknown = exp1.DeploymentCommand(
            "agent-0", 100, ("ip", "route", "del", "10.0.1.2/32"),
        )
        activation = exp1.DeploymentCommand(
            "agent-0", 100,
            ("ip", "rule", "add", "priority", "10000", "to", "10.0.1.2/32", "lookup", "100"),
        )
        for method_id in ("proposed", "cspf"):
            transaction = FormationTransaction(
                transaction_id=f"{method_id}:bad", attempt_index=0,
                expected_versions=(), commands=(route, unknown),
                affected_objects=("e-0",), scope_kind="task", logical_edge_id="e-0",
            )
            with self.subTest(method_id=method_id), self.assertRaisesRegex(ValueError, "unsupported"):
                transactional._method_owned_waves(((transaction,),))
        sfc = FormationTransaction(
            transaction_id="sfc:known", attempt_index=0,
            expected_versions=(), commands=(route, activation),
            affected_objects=("chain-000",), scope_kind="chain", chain_id="chain-000",
        )
        self.assertEqual(
            transactional._method_owned_waves(((sfc,),))[0][0].commands, (route,)
        )

    def test_exception_paths_accumulate_started_stage_durations(self) -> None:
        cases = (
            ("plan_task_routes", "planning_latency_ms", "PLAN"),
            ("install_common_infrastructure", "common_infrastructure_latency_ms", "COMMON_INFRASTRUCTURE"),
            ("verify_ping", "final_verification_latency_ms", "FINAL_VERIFY"),
        )
        for method_name, duration_name, stage in cases:
            topology = _RecordingTransactionalTopology(4, 4)

            def delayed_failure(*_args, **_kwargs):
                threading.Event().wait(0.02)
                raise RuntimeError(f"{method_name} exploded")

            setattr(topology, method_name, delayed_failure)
            row, _events = run_transactional_trial(
                topology, self.formal, method_id="cspf",
                scenario_class="command_rejection", seed=0, num_agents=4,
            )
            with self.subTest(stage=stage):
                self.assertFalse(row.success)
                self.assertEqual(row.failure_stage, stage)
                self.assertGreaterEqual(getattr(row, duration_name), 15.0)

    def test_measured_scopes_exclude_common_setup_and_post_verify_cleanup(self) -> None:
        topology = _RecordingTransactionalTopology(
            4, 4, common_delay_s=0.05, cleanup_delay_s=0.05
        )
        wall_started = time.perf_counter()

        row, _events = run_transactional_trial(
            topology, self.formal, method_id="cspf",
            scenario_class="command_rejection", seed=3, num_agents=4,
        )
        wall_ms = (time.perf_counter() - wall_started) * 1000.0

        self.assertTrue(row.success)
        self.assertGreater(row.time_to_correct_formation_ms, row.method_owned_formation_latency_ms)
        self.assertGreaterEqual(
            row.time_to_correct_formation_ms - row.method_owned_formation_latency_ms,
            40.0,
        )
        self.assertGreaterEqual(wall_ms - row.time_to_correct_formation_ms, 40.0)

    def test_failed_peer_abort_stops_replan_and_final_verification(self) -> None:
        topology = _RecordingTransactionalTopology(4, 4, fail_abort=True)

        row, events = run_transactional_trial(
            topology, self.formal, method_id="proposed",
            scenario_class="command_rejection", seed=0, num_agents=4,
        )

        self.assertFalse(row.success)
        self.assertFalse(row.infrastructure_cleanup_success)
        self.assertEqual(row.failure_stage, "ABORT_OR_ROLLBACK")
        self.assertIn("kernel cleanup failed", row.cleanup_failure_reason)
        self.assertTrue(row.leaked_state_fingerprint)
        self.assertEqual(len(topology.planner_inputs), 1)
        self.assertEqual(topology.verifier_calls, [])
        self.assertTrue(any(event["stage"] == "TRANSACTION_ABORT" for event in events))

    def test_failed_rollback_is_retained_with_original_verifier_failure(self) -> None:
        topology = _RecordingTransactionalTopology(
            4, 4, fail_abort=True, fail_verify=True
        )

        row, events = run_transactional_trial(
            topology, self.formal, method_id="cspf",
            scenario_class="command_rejection", seed=0, num_agents=4,
        )

        self.assertFalse(row.success)
        self.assertFalse(row.infrastructure_cleanup_success)
        self.assertEqual(row.failure_stage, "FINAL_VERIFY")
        self.assertIn("verifier exploded", row.failure_reason)
        self.assertIn("kernel cleanup failed", row.cleanup_failure_reason)
        self.assertTrue(row.leaked_state_fingerprint)
        self.assertTrue(any(event["stage"] == "TRANSACTION_ROLLBACK" for event in events))

    def test_background_cleanup_failure_retains_first_verified_timestamp(self) -> None:
        topology = _RecordingTransactionalTopology(4, 4, cleanup_error=True)

        row, events = run_transactional_trial(
            topology, self.formal, method_id="cspf",
            scenario_class="command_rejection", seed=0, num_agents=4,
        )

        self.assertFalse(row.success)
        self.assertTrue(row.verified_correct)
        self.assertIsNotNone(row.time_to_correct_formation_ms)
        self.assertFalse(row.infrastructure_cleanup_success)
        self.assertEqual(row.failure_stage, "BACKGROUND_CLEANUP")
        self.assertIn("background cleanup exploded", row.cleanup_failure_reason)
        self.assertTrue(any(event["stage"] == "BACKGROUND_CLEANUP_FAILED" for event in events))

    def test_teardown_failure_appends_to_attempts_without_replacing_trial(self) -> None:
        def topology_factory(num_agents, num_gateways):
            return _RecordingTransactionalTopology(
                num_agents, num_gateways, teardown_error=True
            )

        with tempfile.TemporaryDirectory() as directory:
            rows = run_transactional_experiment(
                self.formal,
                Path(directory),
                seeds=(7,),
                task_sizes=(4,),
                methods=("cspf",),
                scenario_classes=("command_rejection",),
                require_complete_grid=True,
                topology_factory=topology_factory,
            )
            raw = Path(directory) / "raw"
            events = [
                json.loads(line)
                for line in (raw / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        row = rows[0]
        self.assertGreater(row.num_business_edges, 0)
        self.assertTrue(row.fault_schedule_fingerprint)
        self.assertTrue(row.verified_correct)
        self.assertIsNotNone(row.time_to_correct_formation_ms)
        self.assertFalse(row.infrastructure_cleanup_success)
        self.assertIn("topology teardown exploded", row.cleanup_failure_reason)
        self.assertTrue(any(event["stage"] == "FAULT_INJECTED" for event in events))
        self.assertTrue(any(event["stage"] == "TOPOLOGY_TEARDOWN_FAILED" for event in events))

    def test_successful_baseline_retries_measure_partial_state_exposure(self) -> None:
        for method_id in ("cspf", "global_sfc_embedding"):
            topology = _RecordingTransactionalTopology(4, 4)

            row, _events = run_transactional_trial(
                topology, self.formal, method_id=method_id,
                scenario_class="command_rejection", seed=0, num_agents=4,
            )

            with self.subTest(method_id=method_id):
                self.assertTrue(row.success)
                self.assertGreater(row.partial_state_exposure_ms, 0.0)

    def test_run_schema_is_immutable(self) -> None:
        names = set(TransactionalFormationRun.__dataclass_fields__)
        self.assertTrue(
            {
                "prepare_attempts", "commit_attempts", "rollback_scope_objects",
                "wasted_rule_commands", "partial_state_exposure_ms",
                "planning_latency_ms", "prepare_latency_ms", "commit_latency_ms",
                "rollback_replan_latency_ms", "final_verification_latency_ms",
                "configuration_sha256", "verifier_fingerprint",
            }.issubset(names)
        )
        topology = _RecordingTransactionalTopology(4, 4)
        row, _events = run_transactional_trial(
            topology, self.formal, method_id="proposed",
            scenario_class="command_rejection", seed=1, num_agents=4,
        )
        with self.assertRaises(FrozenInstanceError):
            row.success = False  # type: ignore[misc]
