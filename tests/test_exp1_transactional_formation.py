from __future__ import annotations

import hashlib
import json
import csv
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

import experiments.exp1_netns_verified_formation as exp1
import experiments.exp1_transactional_formation as transactional
from scripts.validate_wcnc_final_v3_exp1_nominal import (
    select_nominal_candidate,
    validate_completed_nominal_artifacts,
    verify_nominal_selection_receipt,
)
from experiments.exp1_netns_verified_formation import FormationEdge
from experiments.exp1_transactional_formation import (
    TransactionalFormationRun,
    build_transactional_schedule,
    load_transactional_config,
    run_transactional_experiment,
    run_transactional_trial,
)
from experiments.paper_protocol import TRANSACTIONAL_EXP1_PROTOCOL
from scripts.aggregate_wcnc_final_v3 import (
    _write_transactional_paired_differences,
    aggregate_transactional,
)
from scripts.normalize_wcnc_final_v3_exp1_transactional import _validate_attempt_events
from scripts.publish_wcnc_final_v3_staging import (
    publish_immutable_tree,
    validate_staged_experiment,
)
from src.metrics.exp2_robustness import RobustnessRunMetrics
from src.metrics.paper import PaperTrial
from src.controller.formation_transactions import (
    CommandResult,
    FaultClass,
    FormationFaultSchedule,
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


class NetlinkJsonCompatibilityTests(unittest.TestCase):
    def test_empty_policy_table_output_is_an_empty_route_list(self) -> None:
        self.assertEqual(exp1._ip_json("", "policy table routes"), [])
        self.assertEqual(exp1._ip_json("\n", "policy table routes"), [])

    def test_legacy_ip_rule_text_preserves_activation_fields(self) -> None:
        rules = exp1._ip_rules(
            "0:\tfrom all lookup local\n"
            "10042:\tfrom all to 10.101.1.2 lookup 23456\n"
            "32766:\tfrom all lookup main\n",
            "policy table rules",
        )
        self.assertIn(
            {"priority": "10042", "to": "10.101.1.2", "table": "23456"},
            rules,
        )

    def test_legacy_ip_route_text_is_stable_readback_evidence(self) -> None:
        routes = exp1._ip_routes(
            "10.101.1.2 via 10.100.0.1 dev eth0  proto static\n",
            "policy table routes",
        )
        self.assertEqual(
            routes,
            [{"raw": "10.101.1.2 via 10.100.0.1 dev eth0 proto static"}],
        )


class NominalValidatorCliTests(unittest.TestCase):
    def test_validator_script_imports_repository_modules_without_pythonpath(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            (
                sys.executable,
                str(repository / "scripts" / "validate_wcnc_final_v3_exp1_nominal.py"),
                "--help",
            ),
            cwd=repository,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class TransactionalCliTests(unittest.TestCase):
    def test_unprivileged_reexec_runs_the_transactional_module(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                transactional.sys,
                "argv",
                [
                    "exp1_transactional",
                    "--config",
                    "configs/exp1_transactional_formation_pilot_v1.yaml",
                    "--output-dir",
                    directory,
                ],
            ),
            patch.object(transactional.nominal, "_inside_user_namespace", return_value=False),
            patch.object(transactional.nominal, "_network_namespace_inode", return_value=321),
            patch.object(transactional.nominal.os, "geteuid", return_value=1000),
            patch.object(transactional.nominal.sys, "executable", "python"),
            patch.object(
                transactional.nominal.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            with self.assertRaisesRegex(SystemExit, "0"):
                transactional.main()

        self.assertEqual(
            run.call_args.args[0],
            (
                "unshare",
                "--user",
                "--map-root-user",
                "--net",
                "--fork",
                "python",
                "-m",
                "experiments.exp1_transactional_formation",
                "--config",
                "configs/exp1_transactional_formation_pilot_v1.yaml",
                "--output-dir",
                directory,
            ),
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
    def test_stage_accepts_legacy_rule_readback_from_old_iproute2(self) -> None:
        topology = exp1.ProcessNetnsTopology(2, 1)
        topology.agents = [SimpleNamespace(pid=100), SimpleNamespace(pid=101)]
        topology.gateways = [SimpleNamespace(pid=200)]
        topology.agent_ips = ["10.100.0.2", "10.100.1.2"]
        topology.agent_gateways = [0, 0]
        topology._parallel_commands = lambda *_args, **_kwargs: None

        def readback(_pid, *command, **_kwargs):
            if command[:5] == ("ip", "-j", "route", "show", "table"):
                return SimpleNamespace(stdout="")
            if command == ("ip", "-j", "rule", "show"):
                return SimpleNamespace(
                    stdout=(
                        "0:\tfrom all lookup local\n"
                        "32766:\tfrom all lookup main\n"
                        "32767:\tfrom all lookup default\n"
                    )
                )
            raise AssertionError(command)

        topology._run_ns = readback
        backend = exp1.NetnsPolicyTableBackend(topology)
        command = exp1.DeploymentCommand(
            "agent-0",
            100,
            (
                "ip", "route", "replace", "10.100.1.2/32", "via",
                "10.100.0.1", "dev", "eth0",
            ),
        )

        staged = backend.stage("txn-legacy-rules", (command,))

        self.assertTrue(staged.accepted, staged.reason)

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

    def test_stage_timeout_returns_at_deadline_then_flushes_explicitly(self) -> None:
        class DeadlineTopology:
            def __init__(self) -> None:
                self.commands: list[tuple[tuple[int | None, tuple[str, ...]], ...]] = []
                self.delay_cleanup = True

            def _run_ns(self, *_args, **_kwargs):
                return SimpleNamespace(stdout="[]")

            def _parallel_commands(self, commands, *, check, timeout=None, deadline=None):
                items = tuple(commands)
                self.commands.append(items)
                if any(command[:3] == ("ip", "route", "replace") for _, command in items):
                    threading.Event().wait(max(0.0, (deadline or time.perf_counter()) - time.perf_counter()))
                    raise subprocess.TimeoutExpired("ip", 0.0)
                if self.delay_cleanup:
                    threading.Event().wait(0.08)

        topology = DeadlineTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)
        started = time.perf_counter()

        staged = backend.stage("txn-timeout", (self._command(),), ack_timeout_ms=30)
        elapsed = time.perf_counter() - started

        self.assertFalse(staged.accepted)
        self.assertLess(elapsed, 0.07)
        self.assertIn("txn-timeout", backend._transactions)
        self.assertTrue(backend._used_tables)
        self.assertFalse(any(
            command[:3] == ("ip", "route", "flush")
            for batch in topology.commands for _, command in batch
        ))

        topology.delay_cleanup = False
        flushed = backend.flush("txn-timeout")

        self.assertTrue(flushed.accepted)
        self.assertFalse(backend._used_tables)

    def test_engine_prepare_deadline_keeps_post_readback_for_explicit_abort(self) -> None:
        class DeadlineTopology:
            def __init__(self) -> None:
                self.timed_out = False
                self.readback_calls = 0

            def _run_ns(self, *_args, **_kwargs):
                self.readback_calls += 1
                if self.timed_out:
                    # An engine fallback readback after the stage deadline would
                    # be observable as an additional 80 ms pre-return delay.
                    threading.Event().wait(0.08)
                return SimpleNamespace(stdout="[]")

            def _parallel_commands(self, commands, *, check, timeout=None, deadline=None):
                if any(
                    command[:3] == ("ip", "route", "replace")
                    for _, command in tuple(commands)
                ):
                    threading.Event().wait(
                        max(0.0, (deadline or time.perf_counter()) - time.perf_counter())
                    )
                    self.timed_out = True
                    raise subprocess.TimeoutExpired("ip", 0.0)

        class BackendExecutor:
            def __init__(self, backend) -> None:
                self.backend = backend
                self.stage_result: StageResult | None = None

            def stage(self, transaction, ack_timeout_ms):
                self.stage_result = self.backend.stage(
                    transaction.transaction_id,
                    transaction.commands,
                    ack_timeout_ms=ack_timeout_ms,
                )
                return self.stage_result

            def activate(self, transaction_id):
                return self.backend.activate(transaction_id)

            def flush(self, transaction_id):
                return self.backend.flush(transaction_id)

            def readback(self, transaction_id):
                return self.backend.readback(transaction_id)

        topology = DeadlineTopology()
        backend = exp1.NetnsPolicyTableBackend(topology)
        executor = BackendExecutor(backend)
        engine = FormationTransactionEngine(executor, ack_timeout_ms=30)
        transaction = FormationTransaction(
            transaction_id="txn-engine-deadline",
            attempt_index=0,
            expected_versions=(),
            commands=(self._command(),),
            affected_objects=("edge-0",),
        )

        started = time.perf_counter()
        prepared = engine.prepare(transaction)
        prepare_elapsed = time.perf_counter() - started
        reads_after_prepare = topology.readback_calls

        self.assertFalse(prepared.accepted)
        self.assertLess(prepare_elapsed, 0.07)
        self.assertIsNotNone(executor.stage_result)
        assert executor.stage_result is not None
        self.assertIsNone(executor.stage_result.readback_after)
        self.assertEqual(
            executor.stage_result.readback_after_evidence,
            "post-readback-unavailable-due-to-prepare-deadline",
        )
        self.assertIn("post-readback unavailable due to prepare deadline", prepared.reason)
        self.assertTrue(prepared.readback_after_fingerprint)

        aborted = engine.abort(transaction.transaction_id)

        self.assertTrue(aborted.accepted)
        self.assertGreater(topology.readback_calls, reads_after_prepare)
        expected_cleanup_readback = hashlib.sha256(
            json.dumps(
                executor.stage_result.readback_before,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(aborted.readback_after_fingerprint, expected_cleanup_readback)
        self.assertFalse(backend._used_tables)

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
            "txn-cumulative", (self._command(),), ack_timeout_ms=70
        )
        elapsed = time.perf_counter() - started

        self.assertFalse(staged.accepted)
        self.assertIn("post-stage readback failed", staged.reason)
        self.assertLess(elapsed, 0.14)
        self.assertGreaterEqual(len(topology.read_timeouts), 2)
        self.assertGreater(topology.read_timeouts[0], topology.read_timeouts[-1])

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

    def _table_route(self, edge: FormationEdge, owner: int) -> exp1.DeploymentCommand:
        return exp1.DeploymentCommand(
            f"agent-{owner}", 100 + owner + self.pid_offset,
            (
                "ip", "route", "replace", "table", str(100 + owner),
                f"10.0.{edge.target_index}.2/32", "via", f"10.0.{owner}.1",
                "dev", "eth0",
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
                            (self._table_route(edge, edge.source_index),),
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
                                        "lookup", str(100 + edge.source_index),
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
            transaction_events = [
                event for event in events
                if event["stage"].startswith("TRANSACTION_")
            ]
            self.assertEqual(row.attempt_count, len(transaction_events))
            self.assertEqual(
                row.prepare_attempts,
                sum(event["stage"] == "TRANSACTION_PREPARE" for event in events),
            )
            self.assertEqual(
                row.commit_attempts,
                sum(event["stage"] == "TRANSACTION_COMMIT" for event in events),
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
            run_bytes = (raw / "runs.csv").read_bytes()
            attempt_bytes = (raw / "attempts.jsonl").read_bytes()
            with (raw / "runs.csv").open(encoding="utf-8", newline="") as handle:
                persisted_rows = list(csv.DictReader(handle))

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
        self.assertEqual(rows[0].result_mode, "measured_netns")
        self.assertTrue(rows[0].scenario_fingerprint)
        self.assertEqual(scope["runs_csv_sha256"], hashlib.sha256(run_bytes).hexdigest())
        self.assertEqual(scope["attempts_jsonl_sha256"], hashlib.sha256(attempt_bytes).hexdigest())
        self.assertEqual(scope["row_count"], 1)
        events = [json.loads(line) for line in attempt_lines]
        first_attempt = _validate_attempt_events(persisted_rows, events)
        self.assertFalse(first_attempt[rows[0].run_id])
        self.assertGreater(len(events), 1)
        self.assertEqual([event["event_sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event["run_sequence"] == 1 for event in events))
        self.assertTrue(all(event["method_id"] == "proposed" for event in events))
        for event in events:
            self.assertEqual(event["success"], rows[0].success)
            self.assertEqual(event["timeout"], rows[0].timeout)
            self.assertEqual(event["failure_stage"], rows[0].failure_stage)
            self.assertEqual(event["failure_reason"], rows[0].failure_reason)

    def test_interrupted_prefix_resumes_exactly_once(self) -> None:
        """Resume must validate and append only the missing frozen invocation suffix."""
        calls = 0

        def interrupting_factory(num_agents, num_gateways):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulated host interruption")
            return _RecordingTransactionalTopology(num_agents, num_gateways)

        overrides = {
            "seeds": (9000, 9001), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            with self.assertRaises(KeyboardInterrupt):
                run_transactional_experiment(
                    self.pilot, output, topology_factory=interrupting_factory,
                    execution_commit=commit, **overrides,
                )

            rows = run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, resume=True, require_complete_grid=True,
                **overrides,
            )
            with (output / "raw" / "runs.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                persisted = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 2)
        self.assertEqual(len(persisted), 2)
        self.assertEqual(len({row["run_id"] for row in persisted}), 2)

    def test_resume_rejects_tampered_prefix_and_commit_mismatch(self) -> None:
        """Neither a mutated prefix nor data from another execution commit is resumable."""
        overrides = {
            "seeds": (9000,), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, require_complete_grid=True, **overrides,
            )
            with self.assertRaisesRegex(RuntimeError, "commit"):
                run_transactional_experiment(
                    self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                    execution_commit="b" * 40, resume=True, **overrides,
                )
            runs_path = output / "raw" / "runs.csv"
            with runs_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["method_id"] = "cspf"
            with runs_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(RuntimeError, "prefix|schedule|checkpoint|shorter"):
                run_transactional_experiment(
                    self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                    execution_commit=commit, resume=True, **overrides,
                )

    def test_complete_resume_is_validation_only(self) -> None:
        """A complete authenticated grid must not execute or rewrite another trial."""
        overrides = {
            "seeds": (9000,), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, require_complete_grid=True, **overrides,
            )
            before = {
                path.name: path.read_bytes() for path in (output / "raw").iterdir()
            }
            checkpoint = json.loads(before["measurement_scope.json"])
            self.assertEqual(checkpoint["runs_csv_bytes"], len(before["runs.csv"]))
            self.assertEqual(
                checkpoint["attempts_jsonl_bytes"], len(before["attempts.jsonl"]),
            )
            self.assertEqual(
                checkpoint["runs_csv_sha256"], hashlib.sha256(before["runs.csv"]).hexdigest(),
            )
            self.assertEqual(
                checkpoint["attempts_jsonl_sha256"],
                hashlib.sha256(before["attempts.jsonl"]).hexdigest(),
            )

            def forbidden_factory(*_args):
                raise AssertionError("complete resume executed a new topology")

            rows = run_transactional_experiment(
                self.pilot, output, topology_factory=forbidden_factory,
                execution_commit=commit, resume=True, require_complete_grid=True,
                **overrides,
            )
            after = {
                path.name: path.read_bytes() for path in (output / "raw").iterdir()
            }

        self.assertEqual(len(rows), 1)
        self.assertEqual(before, after)

    def test_resume_quarantines_events_appended_after_last_checkpoint(self) -> None:
        """An events-only crash tail is evidence, not an authenticated trial."""
        overrides = {
            "seeds": (9000,), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, require_complete_grid=True, **overrides,
            )
            events = output / "raw" / "attempts.jsonl"
            authenticated = events.read_bytes()
            with events.open("ab") as handle:
                handle.write(b'{"crash":"events-only"}\n')

            rows = run_transactional_experiment(
                self.pilot, output, topology_factory=lambda *_: (_ for _ in ()).throw(
                    AssertionError("complete prefix should not rerun")
                ),
                execution_commit=commit, resume=True, require_complete_grid=True,
                **overrides,
            )
            quarantines = list((output / "recovery").glob("quarantine-*"))
            metadata = json.loads((quarantines[0] / "metadata.json").read_text())
            restored = events.read_bytes()

        self.assertEqual(len(rows), 1)
        self.assertEqual(restored, authenticated)
        self.assertEqual(len(quarantines), 1)
        self.assertIn("attempts.jsonl", metadata["artifacts"])

    def test_resume_quarantines_row_and_event_tail_before_scope_replace(self) -> None:
        """A row-before-scope crash restores both files to the authenticated prefix."""
        overrides = {
            "seeds": (9000,), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, require_complete_grid=True, **overrides,
            )
            runs = output / "raw" / "runs.csv"; attempts = output / "raw" / "attempts.jsonl"
            run_prefix = runs.read_bytes(); event_prefix = attempts.read_bytes()
            with runs.open("ab") as handle: handle.write(run_prefix.splitlines(keepends=True)[-1])
            with attempts.open("ab") as handle: handle.write(b'{"crash":"row-before-scope"}\n')

            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, resume=True, require_complete_grid=True,
                **overrides,
            )
            run_after = runs.read_bytes(); event_after = attempts.read_bytes()
            quarantine_count = len(list((output / "recovery").glob("quarantine-*")))

        self.assertEqual(run_after, run_prefix)
        self.assertEqual(event_after, event_prefix)
        self.assertEqual(quarantine_count, 1)

    def test_resume_rejects_shorter_authenticated_artifact(self) -> None:
        overrides = {
            "seeds": (9000,), "task_sizes": (4,),
            "methods": ("proposed",),
            "scenario_classes": ("command_rejection",),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); commit = "a" * 40
            (output / "execution_commit.txt").write_text(commit + "\n", encoding="utf-8")
            run_transactional_experiment(
                self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                execution_commit=commit, require_complete_grid=True, **overrides,
            )
            attempts = output / "raw" / "attempts.jsonl"
            attempts.write_bytes(attempts.read_bytes()[:-1])
            with self.assertRaisesRegex(RuntimeError, "shorter|checkpoint"):
                run_transactional_experiment(
                    self.pilot, output, topology_factory=_RecordingTransactionalTopology,
                    execution_commit=commit, resume=True, **overrides,
                )

    def test_logical_scenario_fingerprint_includes_frozen_transaction_policy(self) -> None:
        original = transactional._logical_scenario_fingerprint(
            self.formal, "command_rejection", 7, 8,
        )
        changed = dict(self.formal)
        changed["transaction"] = {
            **self.formal["transaction"],
            "max_attempts": int(self.formal["transaction"]["max_attempts"]) + 1,
        }

        self.assertNotEqual(
            original,
            transactional._logical_scenario_fingerprint(
                changed, "command_rejection", 7, 8,
            ),
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
            ("ip", "route", "replace", "table", "100", "10.0.1.2/32", "via", "10.0.0.1", "dev", "eth0"),
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
        unrelated_activation = exp1.DeploymentCommand(
            "agent-0", 100,
            ("ip", "rule", "add", "priority", "10001", "to", "10.0.99.2/32", "lookup", "100"),
        )
        with self.assertRaisesRegex(ValueError, "does not correspond"):
            transactional._method_owned_waves((
                (replace(sfc, commands=(route, unrelated_activation)),),
            ))

    def test_phase_aware_default_output_uses_the_loaded_protocol_phase(self) -> None:
        self.assertEqual(
            transactional._default_output_dir(self.formal),
            Path(self.formal["output"]["provenance"]),
        )
        self.assertEqual(
            transactional._default_output_dir(self.pilot),
            Path(self.pilot["output"]["provenance"]),
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

    def test_prepare_timer_includes_parallel_future_error(self) -> None:
        def delayed_prepare_failure(*_args, **_kwargs):
            threading.Event().wait(0.02)
            raise RuntimeError("prepare future exploded")

        with patch.object(
            transactional, "_parallel_apply", side_effect=delayed_prepare_failure
        ):
            prepare_row, _events = run_transactional_trial(
                _RecordingTransactionalTopology(4, 4), self.formal,
                method_id="proposed", scenario_class="command_rejection",
                seed=0, num_agents=4,
            )
        self.assertEqual(prepare_row.failure_stage, "PREPARE")
        self.assertGreaterEqual(prepare_row.prepare_latency_ms, 15.0)

    def test_commit_timer_includes_parallel_future_error(self) -> None:
        original_parallel_apply = transactional._parallel_apply
        calls = 0

        def fail_second_parallel_apply(function, items):
            nonlocal calls
            calls += 1
            if calls == 2:
                threading.Event().wait(0.02)
                raise RuntimeError("commit future exploded")
            return original_parallel_apply(function, items)

        with patch.object(
            transactional, "_parallel_apply", new=fail_second_parallel_apply
        ):
            commit_row, _events = run_transactional_trial(
                _RecordingTransactionalTopology(4, 4), self.formal,
                method_id="proposed", scenario_class="command_rejection",
                seed=0, num_agents=4,
            )
        self.assertEqual(commit_row.failure_stage, "COMMIT")
        self.assertGreaterEqual(commit_row.commit_latency_ms, 15.0)

    def test_parallel_commit_exception_collects_and_rolls_back_later_accepts(self) -> None:
        topology = _RecordingTransactionalTopology(4, 4)
        original_commit = FormationTransactionEngine.commit
        later_commit_finished = threading.Event()
        failing_id = "cspf:flow:edge-000-00-01:0"

        def commit_with_first_future_failure(engine, transaction_id):
            if transaction_id == failing_id:
                self.assertTrue(later_commit_finished.wait(1.0))
                raise RuntimeError("forced first commit future failure")
            attempt = original_commit(engine, transaction_id)
            if attempt.accepted:
                later_commit_finished.set()
            return attempt

        def no_target_fault(*_args, **_kwargs):
            return FormationFaultSchedule(
                FaultClass.COMMAND_REJECTION,
                4,
                0,
                "not-a-formation-edge",
                0,
            )

        with (
            patch.object(transactional, "build_fault_schedule", new=no_target_fault),
            patch.object(
                FormationTransactionEngine,
                "commit",
                new=commit_with_first_future_failure,
            ),
        ):
            row, events = run_transactional_trial(
                topology,
                self.formal,
                method_id="cspf",
                scenario_class="command_rejection",
                seed=0,
                num_agents=4,
            )

        committed = {
            str(event["details"]["transaction_id"])
            for event in events
            if event["stage"] == "TRANSACTION_COMMIT"
            and bool(event["details"]["accepted"])
        }
        rolled_back = {
            str(event["details"]["transaction_id"])
            for event in events
            if event["stage"] == "TRANSACTION_ROLLBACK"
            and bool(event["details"]["accepted"])
        }

        self.assertFalse(row.success)
        self.assertEqual(row.failure_stage, "COMMIT")
        self.assertTrue(committed)
        self.assertSetEqual(rolled_back, committed)
        self.assertEqual(row.rollback_count, len(committed))
        self.assertEqual(topology.activated, set())

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

    def test_post_complete_verifier_failure_does_not_extend_closed_exposure(self) -> None:
        topology = _RecordingTransactionalTopology(4, 4)

        def delayed_verify_failure(*_args, **_kwargs):
            threading.Event().wait(0.05)
            raise RuntimeError("verifier exploded after complete commit")

        topology.verify_ping = delayed_verify_failure
        row, _events = run_transactional_trial(
            topology, self.formal, method_id="cspf",
            scenario_class="command_rejection", seed=0, num_agents=4,
        )

        self.assertFalse(row.success)
        self.assertEqual(row.failure_stage, "FINAL_VERIFY")
        self.assertLess(row.partial_state_exposure_ms, 30.0)

    def test_commit_rejection_rolls_back_later_accepted_peer(self) -> None:
        class RejectionBeforeAcceptanceTopology(_RecordingTransactionalTopology):
            def __init__(self) -> None:
                super().__init__(4, 4)
                self.commit_calls = 0

            def activate_transaction(self, transaction_id):
                self.commit_calls += 1
                if self.commit_calls == 1:
                    return CommandResult(accepted=False, reason="first commit rejected")
                return super().activate_transaction(transaction_id)

        topology = RejectionBeforeAcceptanceTopology()
        row, events = run_transactional_trial(
            topology, self.formal, method_id="cspf",
            scenario_class="command_rejection", seed=0, num_agents=4,
        )

        self.assertFalse(row.success)
        self.assertEqual(row.failure_stage, "COMMIT")
        self.assertEqual(row.rollback_count, row.commit_attempts - 1)
        self.assertGreater(row.rollback_count, 0)
        self.assertFalse(topology.activated)
        self.assertEqual(
            len([event for event in events if event["stage"] == "TRANSACTION_ROLLBACK"]),
            row.rollback_count,
        )

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


class TransactionalAggregationTests(unittest.TestCase):
    @staticmethod
    def _rows() -> list[dict[str, object]]:
        return [
            {
                "scenario_class": "command_rejection", "num_agents": 4,
                "seed": seed, "fault_schedule_fingerprint": "shared-fault",
                "method_id": method, "success": success, "timeout": timeout,
                "verified_correct": success,
                "attempt_count": attempts, "commit_attempts": 1,
                "method_owned_formation_latency_ms": latency,
                "time_to_correct_formation_ms": correct_time,
                "rollback_scope_objects": rollback_scope,
                "wasted_rule_commands": wasted, "partial_state_exposure_ms": exposure,
            }
            for seed, method, success, timeout, attempts, latency, correct_time,
            rollback_scope, wasted, exposure in (
                (0, "proposed", True, False, 1, 5.0, 8.0, '["edge-0"]', 0, 0.0),
                (0, "cspf", True, False, 2, 7.0, 10.0, '["edge-0", "edge-1"]', 3, 2.0),
                (0, "global_sfc_embedding", True, False, 1, 8.0, 12.0, '[]', 1, 1.0),
                (1, "proposed", False, True, 2, 6.0, None, '["edge-1"]', 2, 3.0),
                (1, "cspf", False, True, 2, 9.0, None, '["edge-0"]', 4, 4.0),
                (1, "global_sfc_embedding", False, True, 2, 10.0, None, '["edge-0"]', 2, 5.0),
            )
        ]

    def test_failed_attempt_time_and_waste_are_not_dropped(self) -> None:
        """Removing failed trials would hide actual method-owned work and waste."""
        rows = aggregate_transactional(self._rows())

        def find(metric: str, method: str) -> dict[str, object]:
            return next(
                row for row in rows
                if row["metric"] == metric and row["method_id"] == method
            )

        metric = find("method_owned_formation_latency_ms", "cspf")
        self.assertEqual(metric["denominator"], 2)
        self.assertGreater(float(find("wasted_rule_commands", "cspf")["estimate"]), 0)

    def test_time_to_correct_is_conditional_and_adjacent_to_success(self) -> None:
        """Correct-formation time excludes unverified failures but retains its base N."""
        rows = aggregate_transactional(self._rows())

        def find(metric: str, method: str) -> dict[str, object]:
            return next(
                row for row in rows
                if row["metric"] == metric and row["method_id"] == method
            )

        self.assertEqual(find("success_rate", "proposed")["denominator"], 2)
        self.assertEqual(find("time_to_correct_formation_ms", "proposed")["numerator"], 1)

    def test_aggregation_rejects_divergent_paired_fault_fingerprints(self) -> None:
        """A broken pairing key must not silently create a partial comparison."""
        rows = self._rows()
        rows[1]["fault_schedule_fingerprint"] = "different-fault"

        with self.assertRaisesRegex(ValueError, "fault fingerprints"):
            aggregate_transactional(rows)

    def test_paired_output_reports_explicit_zero_valid_conditional_pairs(self) -> None:
        """Conditional metrics must show excluded pairs instead of disappearing."""
        rows = [
            {
                "scenario_class": "command_rejection", "num_agents": 4,
                "seed": 0, "fault_schedule_fingerprint": "shared",
                "method_id": method, "success": False, "timeout": True,
                "verified_correct": False, "attempt_count": 2,
                "method_owned_formation_latency_ms": 1.0,
                "time_to_correct_formation_ms": None,
                "rollback_scope_objects": "[]", "wasted_rule_commands": 1,
                "partial_state_exposure_ms": 1.0,
            }
            for method in ("proposed", "cspf", "global_sfc_embedding")
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paired_differences.csv"
            _write_transactional_paired_differences(
                rows, path, Path("trials.csv"), "a" * 64,
            )
            with path.open(encoding="utf-8", newline="") as handle:
                output = list(csv.DictReader(handle))
        metric = next(
            row for row in output
            if row["method_id"] == "cspf"
            and row["metric"] == "time_to_correct_formation_ms"
        )
        self.assertEqual(metric["eligible_pairs"], "1")
        self.assertEqual(metric["paired_trials"], "0")
        self.assertEqual(metric["excluded_pairs"], "1")
        self.assertEqual(metric["estimate"], "")
        self.assertEqual(metric["exclusion_reason"], "not_successful_verified_both")


class RemoteLauncherContractTests(unittest.TestCase):
    METHODS = ("proposed", "cspf", "global_sfc_embedding")
    SIZES = (4, 8, 12, 16, 20)

    def _staged_rows(self, experiment: str, seeds: tuple[int, ...]) -> list[dict[str, str]]:
        repo = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((repo / "configs/wcnc_final_v3.yaml").read_text())
        rows: list[dict[str, str]] = []
        if experiment == "exp2":
            points = [("", gamma, 0) for gamma in config["exp2"]["gamma"]]
            methods_by_failure = {"": tuple(config["exp2"]["methods"])}
        elif experiment == "exp3":
            points = [("", scope, event) for scope in config["exp3"]["affected_dependency_scope_percent"] for event in range(5)]
            methods_by_failure = {"": tuple(config["exp3"]["methods"])}
        else:
            points = []
            methods_by_failure = config["exp4"]["methods_by_failure"]
            for failure_type, key in (
                ("link_failure", "link_affected_flow_ratio"),
                ("agent_failure", "agent_dependency_closure_ratio"),
                ("capacity_degradation", "capacity_ratio"),
            ):
                points.extend((failure_type, value, 0) for value in config["exp4"][key])
        for failure_type, point, event in points:
            for seed in seeds:
                scenario = f"{experiment}:{failure_type}:{point}:{seed}:{event}"
                for method in methods_by_failure[failure_type]:
                    if experiment == "exp2":
                        expected_fields = {
                            field.name for field in fields(RobustnessRunMetrics)
                        } | {
                            "protocol_id", "execution_mode_detail", "method_id", "gamma",
                            "environment_fingerprint", "observation_fingerprint",
                            "oracle_fingerprint", "pre_verification_correct_decision",
                            "pre_verification_feasible", "unsafe_proposal_before_verification",
                            "verification_rescued", "search_timeout",
                        }
                    else:
                        expected_fields = {field.name for field in fields(PaperTrial)} | {
                            "execution_mode_detail"
                        }
                    row = {field: "" for field in expected_fields}
                    row.update({
                        "protocol_id": "wcnc_final_v3", "experiment": experiment,
                        "seed": str(seed), "event_id": str(event), "method_id": method,
                        "method": method, "scenario_fingerprint": hashlib.sha256(scenario.encode()).hexdigest(),
                        "topology_fingerprint": "a" * 64, "qos_fingerprint": "b" * 64,
                        "event_fingerprint": "c" * 64, "failure_type": failure_type,
                        "gamma": str(point) if experiment == "exp2" else "",
                        "affected_scope_bucket_percent": str(point) if experiment == "exp3" else "",
                        "failure_severity": str(point) if experiment == "exp4" else "",
                        "result_mode": "transactional_simulation", "success": "true",
                        "timeout": "false", "failure_reason": "", "mode": "paper",
                        "trial_id": scenario, "method_label": method,
                        "method_source": "fixture", "adapted": "false",
                        "execution_mode_detail": "producer-simulation",
                    })
                    for boolean in (
                        "ground_truth_conflict", "ground_truth_resolvable",
                        "ground_truth_uses_true_state", "pre_execution_feasibility_checked",
                        "conflict_detected", "conflict_resolved", "qos_satisfied",
                        "infeasible_configuration", "decision_rejected", "safe_rejection",
                        "unsafe_execution", "transaction_attempted", "rollback_triggered",
                        "rollback_success", "no_partial_commit", "stale_state_detected",
                        "stale_proposal_rejected", "proposal_regenerated",
                        "pre_verification_correct_decision", "pre_verification_feasible",
                        "unsafe_proposal_before_verification", "verification_rescued",
                        "search_timeout",
                    ):
                        if boolean in row: row[boolean] = "false"
                    for integer in (
                        "ground_truth_feasible_combinations", "stale_ms", "proposals_per_layer",
                        "num_proposals", "num_layers_observed", "num_raw_combinations",
                        "num_pruned_combinations", "num_evaluated_combinations",
                        "num_feasible_combinations", "stable_version_before",
                        "stable_version_after", "proposal_generated_version", "execution_version",
                        "read_set_version", "control_messages", "control_bytes",
                    ):
                        if integer in row: row[integer] = "0"
                    for number in (
                        "conflict_pressure", "noise_ratio", "coordination_latency_ms",
                        "feasibility_latency_ms", "transaction_latency_ms", "total_latency_ms",
                        "peak_memory_mb",
                    ):
                        if number in row: row[number] = "0.0"
                    row["conflict_class"] = row.get("conflict_class") and "NO_CONFLICT"
                    row["post_execution_verification_result"] = row.get("post_execution_verification_result") and "PASS"
                    if experiment == "exp3":
                        row["affected_scope_ratio"] = str(float(point) / 100.0)
                        row["reconfiguration_latency_ms"] = "1.0"
                        row["modification_scope_ratio"] = "0.1"
                        row["unaffected_disturbance_ratio"] = "0.0"
                    if experiment == "exp4":
                        row[{
                            "link_failure": "affected_flow_ratio",
                            "agent_failure": "dependency_closure_ratio",
                            "capacity_degradation": "post_fault_capacity_ratio",
                        }[failure_type]] = str(point)
                        row["recovery_latency_ms"] = "1.0"
                        row["modification_scope_ratio"] = "0.1"
                        row["unaffected_disturbance_ratio"] = "0.0"
                    rows.append({key: value for key, value in row.items() if key in expected_fields})
        return rows

    @staticmethod
    def _write_staged_rows(root: Path, experiment: str, rows: list[dict[str, str]]) -> Path:
        raw = root / "raw" / experiment
        raw.mkdir(parents=True)
        with (raw / "trials.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(rows[0]), lineterminator="\n")
            writer.writeheader(); writer.writerows(rows)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
        ).stdout.strip()
        (raw / "execution_commit.txt").write_text(head + "\n", encoding="utf-8")
        return raw

    def test_exp2_exp4_staging_validation_and_immutable_publication_are_behavioral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for experiment in ("exp2", "exp3", "exp4"):
                rows = self._staged_rows(experiment, (0, 1))
                source = self._write_staged_rows(base / experiment, experiment, rows)
                head = (source / "execution_commit.txt").read_text().strip()
                validate_staged_experiment(
                    source, experiment, seeds=(0, 1), expected_commit=head,
                )
                (source / "producer-extra.json").write_text("{}\n", encoding="utf-8")
                target = base / "canonical" / experiment
                target.mkdir(parents=True)
                (target / "preserved.txt").write_text("old\n", encoding="utf-8")
                publish_immutable_tree(source, target, experiment=experiment)
                self.assertEqual((target / "preserved.txt").read_text(), "old\n")
                self.assertEqual((target / "producer-extra.json").read_text(), "{}\n")
                self.assertTrue((target / "execution_commit.txt").is_file())
                self.assertTrue((target / "publication_complete.json").is_file())

    def test_exp4_staging_accepts_targeted_and_realized_severity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = self._staged_rows("exp4", (0,))
            for row in rows:
                if row["failure_type"] == "link_failure":
                    row["affected_flow_ratio"] = "0.1111111111111111"
                if row["failure_type"] == "agent_failure":
                    row["dependency_closure_ratio"] = "0.20833333333333334"
                if (
                    row["failure_type"] == "capacity_degradation"
                    and row["post_fault_capacity_ratio"] == "1.1"
                ):
                    row["failure_severity"] = "-0.1"
            source = self._write_staged_rows(Path(directory), "exp4", rows)
            head = (source / "execution_commit.txt").read_text().strip()
            validate_staged_experiment(
                source, "exp4", seeds=(0,), expected_commit=head,
            )

    def test_staging_validation_rejects_missing_duplicate_and_inapplicable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); rows = self._staged_rows("exp4", (0,))
            source = self._write_staged_rows(base / "missing", "exp4", rows[:-1])
            with self.assertRaisesRegex(ValueError, "grid|row"):
                validate_staged_experiment(source, "exp4", seeds=(0,), expected_commit=(source / "execution_commit.txt").read_text().strip())
            source = self._write_staged_rows(base / "duplicate", "exp4", rows + [rows[0]])
            with self.assertRaisesRegex(ValueError, "duplicate|grid"):
                validate_staged_experiment(source, "exp4", seeds=(0,), expected_commit=(source / "execution_commit.txt").read_text().strip())
            altered = [dict(row) for row in rows]; altered[0]["method_id"] = "sfc_restoration"
            source = self._write_staged_rows(base / "inapplicable", "exp4", altered)
            with self.assertRaisesRegex(ValueError, "method|grid"):
                validate_staged_experiment(source, "exp4", seeds=(0,), expected_commit=(source / "execution_commit.txt").read_text().strip())

    def test_staging_validation_rejects_schema_domains_nonfinite_and_fake_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); rows = self._staged_rows("exp3", (0,))
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
            ).stdout.strip()
            unknown = [dict(row) for row in rows]
            unknown[0]["invented_metric"] = "1"
            source = self._write_staged_rows(base / "schema", "exp3", unknown)
            with self.assertRaisesRegex(ValueError, "schema"):
                validate_staged_experiment(source, "exp3", seeds=(0,), expected_commit=head)

            nonfinite = [dict(row) for row in rows]
            nonfinite[0]["reconfiguration_latency_ms"] = "nan"
            source = self._write_staged_rows(base / "nonfinite", "exp3", nonfinite)
            with self.assertRaisesRegex(ValueError, "finite|numeric|latency"):
                validate_staged_experiment(source, "exp3", seeds=(0,), expected_commit=head)

            invalid_bool = [dict(row) for row in rows]
            invalid_bool[0]["success"] = "yes"
            source = self._write_staged_rows(base / "boolean", "exp3", invalid_bool)
            with self.assertRaisesRegex(ValueError, "boolean|success"):
                validate_staged_experiment(source, "exp3", seeds=(0,), expected_commit=head)

            source = self._write_staged_rows(base / "commit", "exp3", rows)
            (source / "execution_commit.txt").write_text("a" * 40 + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "commit"):
                validate_staged_experiment(source, "exp3", seeds=(0,), expected_commit=head)

    def test_exp2_staging_retains_pre_metric_failure_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = self._staged_rows("exp2", (0,))
            failed = rows[0]
            for field in tuple(failed):
                if field not in {
                    "protocol_id", "seed", "gamma", "method_id",
                    "scenario_fingerprint", "environment_fingerprint",
                    "observation_fingerprint", "oracle_fingerprint", "result_mode",
                    "success", "timeout", "failure_reason",
                }:
                    failed[field] = ""
            failed.update(success="false", timeout="false", failure_reason="RuntimeError:fixture")
            source = self._write_staged_rows(Path(directory), "exp2", rows)
            head = (source / "execution_commit.txt").read_text().strip()

            validate_staged_experiment(
                source, "exp2", seeds=(0,), expected_commit=head,
            )

    def test_staging_requires_all_plotted_metric_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
            ).stdout.strip()
            cases = (
                ("exp2", "coordination_latency_ms", False),
                ("exp3", "modification_scope_ratio", False),
                ("exp3", "unaffected_disturbance_ratio", False),
                ("exp3", "reconfiguration_latency_ms", True),
                ("exp4", "modification_scope_ratio", False),
                ("exp4", "unaffected_disturbance_ratio", False),
                ("exp4", "recovery_latency_ms", True),
            )
            for index, (experiment, metric, successful_only) in enumerate(cases):
                rows = self._staged_rows(experiment, (0,))
                if successful_only:
                    rows[0]["success"] = "true"
                rows[0][metric] = ""
                source = self._write_staged_rows(base / f"case-{index}", experiment, rows)
                with self.subTest(experiment=experiment, metric=metric), self.assertRaisesRegex(
                    ValueError, metric,
                ):
                    validate_staged_experiment(
                        source, experiment, seeds=(0,), expected_commit=head,
                    )

    def test_immutable_tree_preflights_all_files_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "source"; target = base / "target"
            source.mkdir(); target.mkdir()
            (source / "a.txt").write_text("new-a\n", encoding="utf-8")
            (source / "z.txt").write_text("new-z\n", encoding="utf-8")
            (target / "z.txt").write_text("old-z\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                publish_immutable_tree(source, target, experiment="exp2")
            self.assertFalse((target / "a.txt").exists())
            self.assertEqual((target / "z.txt").read_text(), "old-z\n")

    def test_partial_publication_has_no_completion_marker_and_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); source = base / "source"; target = base / "target"
            source.mkdir(); (source / "a.txt").write_text("a\n"); (source / "b.txt").write_text("b\n")
            real_copy = __import__("shutil").copy2
            calls = 0

            def interrupted_copy(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2: raise OSError("simulated publication crash")
                return real_copy(src, dst)

            with patch("scripts.publish_wcnc_final_v3_staging.shutil.copy2", side_effect=interrupted_copy):
                with self.assertRaisesRegex(OSError, "publication crash"):
                    publish_immutable_tree(source, target, experiment="exp2")
            self.assertFalse((target / "publication_complete.json").exists())

            publish_immutable_tree(source, target, experiment="exp2")

            self.assertEqual((target / "a.txt").read_text(), "a\n")
            self.assertEqual((target / "b.txt").read_text(), "b\n")
            self.assertTrue((target / "publication_complete.json").is_file())

    @staticmethod
    def _configuration_hash(config_path: Path) -> str:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _nominal_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        repo = Path(__file__).resolve().parents[1]
        config = repo / "configs" / "exp1_netns_verified_formation_v3.yaml"
        configuration_hash = self._configuration_hash(config)
        raw = root / "raw"
        raw.mkdir(parents=True)
        runs = raw / "runs.csv"
        field_names = tuple(field.name for field in fields(exp1.NetnsFormationRun))
        rows = []
        events = []
        config_payload = yaml.safe_load(config.read_text(encoding="utf-8"))
        schedule = exp1.build_experiment_schedule(config_payload, tuple(range(50)))
        for scheduled in schedule:
            fingerprint = f"scenario-{scheduled.num_agents}-{scheduled.seed}"
            method_order = exp1.counterbalanced_method_order(
                self.METHODS, scheduled.seed
            )
            for method_position, method in enumerate(method_order):
                sequence = (scheduled.run_sequence - 1) * 3 + method_position + 1
                run_id = (
                    f"agents={scheduled.num_agents}:seed={scheduled.seed}:method={method}"
                )
                base = sequence * 10.0
                times = {
                    "traffic_control_started_at": base - 0.2,
                    "traffic_control_finished_at": base - 0.1,
                    "task_received_at": base,
                    "mapping_finished_at": base + 0.1,
                    "route_install_started_at": base + 0.1,
                    "route_install_finished_at": base + 0.2,
                    "activation_finished_at": base + 0.21,
                    "verification_started_at": base + 0.21,
                    "ping_verify_started_at": base + 0.21,
                    "ping_verify_finished_at": base + 0.3,
                    "iperf_verify_started_at": base + 0.3,
                    "data_plane_verified_at": base + 0.4,
                }
                edges = max(1, scheduled.num_agents - 1)
                row = {
                    "run_id": run_id,
                    "num_agents": scheduled.num_agents,
                    "seed": scheduled.seed,
                    "method_id": method,
                    "method_label": {"proposed": "Ours", "cspf": "CSPF-based Formation", "global_sfc_embedding": "Global SFC Embedding"}[method],
                    "scenario_fingerprint": fingerprint,
                    "configuration_sha256": configuration_hash,
                    "result_mode": "real_linux_netns_veth_tc_data_plane",
                    "run_sequence": sequence,
                    "block_index": scheduled.block_index,
                    "order_position": scheduled.order_position,
                    "method_order_position": method_position,
                    **times,
                    "num_business_edges": edges,
                    "num_gateways": 4,
                    "control_messages": 1,
                    "rules_installed": edges,
                    "mapping_latency_s": 0.1,
                    "traffic_control_latency_s": 0.1,
                    "route_install_latency_s": 0.1,
                    "ping_verification_latency_s": 0.09,
                    "iperf3_verification_latency_s": 0.1,
                    "verification_latency_s": 0.19,
                    "verified_formation_latency_s": 0.4,
                    "ping_edges_total": edges,
                    "ping_edges_passed": edges,
                    "ping_attempts": 1,
                    "ping_retried_edges": 0,
                    "ping_mean_rtt_ms": 1.0,
                    "ping_max_packet_loss_percent": 0.0,
                    "ping_timeouts": 0,
                    "iperf_flows_total": edges,
                    "iperf_flows_passed": edges,
                    "iperf_attempts": 1,
                    "iperf_retried_flows": 0,
                    "iperf_retransmissions": 0,
                    "iperf_reported_duration_s": 2.0,
                    "aggregate_receiver_throughput_mbps": float(edges),
                    "retry_backoff_ms": 200,
                    "retry_count": 0,
                    "background_traffic_enabled": False,
                    "background_utilization": 0.0,
                    "background_target_mbps": 0.0,
                    "tc_profile_count": 0,
                    "formation_timing_valid": True,
                    "formation_failed": "false",
                    "success": "true",
                    "failure_stage": "",
                    "failure_reason": "",
                }
                self.assertEqual(set(row), set(field_names))
                rows.append(row)
                identity = {
                    "run_id": run_id, "run_sequence": sequence,
                    "block_index": scheduled.block_index,
                    "order_position": scheduled.order_position,
                    "method_order_position": method_position,
                    "seed": scheduled.seed, "method_id": method,
                    "num_agents": scheduled.num_agents,
                }
                run_events = [
                    {**identity, "timestamp": base - 0.05,
                     "stage": "BACKGROUND_PREPARED", "details": {"enabled": False}},
                    {**identity, "timestamp": times["task_received_at"],
                     "stage": "TASK_RECEIVED", "details": {}},
                    {**identity, "timestamp": times["mapping_finished_at"],
                     "stage": "MAPPING_FINISHED", "details": {"business_edges": edges}},
                    {**identity, "timestamp": times["traffic_control_started_at"],
                     "stage": "TRAFFIC_CONTROL_STARTED", "details": {"implementation": "Linux tc HTB + netem"}},
                    {**identity, "timestamp": times["traffic_control_finished_at"],
                     "stage": "TRAFFIC_CONTROL_FINISHED", "details": {"profiles": []}},
                    {**identity, "timestamp": times["route_install_started_at"],
                     "stage": "ROUTE_INSTALL_STARTED", "details": {
                         "control_messages": 1, "rules_installed": edges,
                         "deployment_batches": [{"label": "fixture", "command_count": edges}],
                         "commands": [
                             {"batch_index": 0, "batch_label": "fixture",
                              "namespace": "agent-0", "command": ["ip", "route", "replace"]}
                             for _ in range(edges)
                         ],
                     }},
                    {**identity, "timestamp": times["route_install_finished_at"],
                     "stage": "ROUTE_INSTALL_FINISHED", "details": {}},
                    {**identity, "timestamp": times["activation_finished_at"],
                     "stage": "ACTIVATION_FINISHED", "details": {"background_traffic": {"enabled": False}}},
                    {**identity, "timestamp": times["ping_verify_started_at"],
                     "stage": "PING_VERIFY_STARTED", "details": {}},
                    {**identity, "timestamp": times["ping_verify_finished_at"],
                     "stage": "PING_VERIFY_FINISHED", "details": {"passed": edges, "total": edges}},
                    {**identity, "timestamp": times["iperf_verify_started_at"],
                     "stage": "IPERF3_VERIFY_STARTED", "details": {}},
                    {**identity, "timestamp": times["data_plane_verified_at"],
                     "stage": "DATA_PLANE_VERIFIED", "details": {"passed": edges, "total": edges, "failure_reason": ""}},
                    {**identity, "timestamp": times["data_plane_verified_at"],
                     "stage": "BACKGROUND_TRAFFIC_RESULT", "details": {"enabled": False}},
                ]
                for edge_index in range(edges):
                    edge_id = f"edge-{edge_index:03d}"
                    command_identity = {
                        "run_id": run_id, "run_sequence": sequence,
                        "block_index": scheduled.block_index,
                        "order_position": scheduled.order_position,
                        "seed": scheduled.seed, "num_agents": scheduled.num_agents,
                    }
                    run_events.append({
                        **command_identity, "timestamp": times["ping_verify_finished_at"],
                        "stage": "PING_COMMAND_RESULT", "details": {
                            "edge_id": edge_id, "source_index": edge_index,
                            "target_index": edge_index + 1, "attempt": 1,
                            "command": ["ping"], "returncode": 0,
                            "duration_s": 0.01, "packet_loss_percent": 0.0,
                            "average_rtt_ms": 1.0, "timeout": False,
                            "passed": True, "stdout": "", "stderr": "",
                        },
                    })
                    run_events.append({
                        **command_identity, "timestamp": times["data_plane_verified_at"],
                        "stage": "IPERF3_COMMAND_RESULT", "details": {
                            "edge_id": edge_id, "source_index": edge_index,
                            "target_index": edge_index + 1, "attempt": 1,
                            "command": ["iperf3"], "returncode": 0,
                            "duration_s": 2.0, "reported_duration_s": 2.0,
                            "throughput_mbps": 1.0, "retransmissions": 0,
                            "required_throughput_mbps": 0.5, "passed": True,
                            "timeout": False, "error": "", "stdout": "", "stderr": "",
                        },
                    })
                events.extend(sorted(run_events, key=lambda item: (item["timestamp"], item["stage"])))
        with runs.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=field_names, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        attempts = raw / "events.jsonl"
        attempts.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        scope = raw / "measurement_scope.json"
        scope.write_text(json.dumps({
            "configuration_sha256": configuration_hash,
            "seeds": list(range(50)),
            "task_sizes": list(self.SIZES),
            "methods": list(self.METHODS),
            "schedule": [asdict(item) for item in schedule],
            "schedule_policy": "five-seed blocks with Latin task-size rotation",
            "method_schedule_policy": "paired-seed deterministic Latin rotation",
        }), encoding="utf-8")
        return runs, attempts, scope, config

    def test_complete_nominal_grid_can_continue_despite_trial_failures(self) -> None:
        """A nonzero nominal runner is acceptable only after its artifacts pass audit."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["failure_stage"] = "DATA_PLANE_VERIFICATION"
            rows[0]["success"] = "false"
            rows[0]["formation_failed"] = "true"
            rows[0]["failure_reason"] = "data_plane_verification_failed:ping=3/3:iperf3=2/3"
            rows[0]["iperf_flows_passed"] = "2"
            rows[0]["aggregate_receiver_throughput_mbps"] = "2.0"
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            for event in payloads:
                if event["run_id"] == rows[0]["run_id"] and event["stage"] == "DATA_PLANE_VERIFIED":
                    event["stage"] = "FORMATION_FAILED"
                    event["details"] = {
                        "passed": 2, "total": 3,
                        "failure_reason": rows[0]["failure_reason"],
                    }
                if (
                    event["run_id"] == rows[0]["run_id"]
                    and event["stage"] == "IPERF3_COMMAND_RESULT"
                    and event["details"]["edge_id"] == "edge-002"
                ):
                    event["details"]["passed"] = False
                    event["details"]["throughput_mbps"] = 0.0
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in payloads),
                encoding="utf-8",
            )

            result = validate_completed_nominal_artifacts(runs, events, scope, config)

        self.assertTrue(result.complete)
        self.assertEqual(result.raw_rows, 750)
        self.assertEqual(result.event_runs, 750)

    def test_incomplete_or_fabricated_nominal_artifacts_fail_closed(self) -> None:
        """Deleting a grid row or its terminal event must block continuation."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))[:-1]
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "750|grid"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

            runs, events, scope, config = self._nominal_fixture(Path(directory) / "fake")
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            payloads = [item for item in payloads if not (
                item["run_id"] == "agents=20:seed=49:method=global_sfc_embedding"
                and item["stage"] == "DATA_PLANE_VERIFIED"
            )]
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in payloads),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "terminal event"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

    def test_nominal_validator_rejects_invocation_sequence_drift(self) -> None:
        """A complete Cartesian set with fabricated execution order is not canonical."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["run_sequence"] = rows[1]["run_sequence"]
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)

            with self.assertRaisesRegex(ValueError, "sequence|invocation"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

    def test_nominal_validator_rejects_fabricated_two_event_stream(self) -> None:
        """Counts alone must not authenticate events lacking measured causal stages."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                first = next(csv.DictReader(handle))
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            kept = [
                item for item in payloads
                if item["run_id"] != first["run_id"]
                or item["stage"] in {"TASK_RECEIVED", "DATA_PLANE_VERIFIED"}
            ]
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in kept),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "mandatory|causal"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

    def test_nominal_validator_rejects_event_identity_and_timestamp_drift(self) -> None:
        """An event from another run or a fabricated row timestamp must fail audit."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            payloads[0]["seed"] = 999
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in payloads),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "identity"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

            runs, events, scope, config = self._nominal_fixture(Path(directory) / "time")
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            target = next(item for item in payloads if item["stage"] == "MAPPING_FINISHED")
            target["timestamp"] += 0.01
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in payloads),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "timestamp|monotonic"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

    def test_nominal_validator_requires_exact_producer_schema_and_command_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            runs, events, scope, config = self._nominal_fixture(base / "schema")
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            reduced_fields = [name for name in rows[0] if name != "ping_mean_rtt_ms"]
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=reduced_fields, extrasaction="ignore")
                writer.writeheader(); writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "schema"):
                validate_completed_nominal_artifacts(runs, events, scope, config)

            for mutation, pattern in (("missing", "PING_COMMAND_RESULT|command"), ("duplicate", "IPERF3_COMMAND_RESULT|duplicate"), ("identity", "identity"), ("primary_method", "identity|method"), ("summary", "summary|RTT"), ("latency", "latency|timing")):
                runs, events, scope, config = self._nominal_fixture(base / mutation)
                payloads = [json.loads(line) for line in events.read_text().splitlines()]
                first_run = payloads[0]["run_id"]
                if mutation == "missing":
                    removed = False
                    kept = []
                    for event in payloads:
                        if not removed and event["run_id"] == first_run and event["stage"] == "PING_COMMAND_RESULT":
                            removed = True; continue
                        kept.append(event)
                    payloads = kept
                elif mutation == "duplicate":
                    duplicate = next(event for event in payloads if event["run_id"] == first_run and event["stage"] == "IPERF3_COMMAND_RESULT")
                    payloads.insert(payloads.index(duplicate) + 1, dict(duplicate))
                elif mutation == "identity":
                    target = next(event for event in payloads if event["run_id"] == first_run and event["stage"] == "PING_COMMAND_RESULT")
                    del target["seed"]
                elif mutation == "primary_method":
                    target = next(event for event in payloads if event["run_id"] == first_run and event["stage"] == "TASK_RECEIVED")
                    del target["method_id"]
                elif mutation == "summary":
                    with runs.open(encoding="utf-8", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    rows[0]["ping_mean_rtt_ms"] = "9.0"
                    with runs.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                        writer.writeheader(); writer.writerows(rows)
                else:
                    with runs.open(encoding="utf-8", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    rows[0]["route_install_latency_s"] = "9.0"
                    with runs.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                        writer.writeheader(); writer.writerows(rows)
                events.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in payloads), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, pattern):
                    validate_completed_nominal_artifacts(runs, events, scope, config)

    def test_nominal_validator_accepts_authentic_preparation_failure_shape(self) -> None:
        """Producer keeps edge totals but has no ping/iperf command rows on preparation failure."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            row = rows[0]; run_id = row["run_id"]
            row.update({
                "control_messages": "0", "rules_installed": "0",
                "ping_edges_passed": "0", "ping_attempts": "0",
                "ping_retried_edges": "0", "ping_mean_rtt_ms": "0.0",
                "ping_max_packet_loss_percent": "100.0", "ping_timeouts": "0",
                "iperf_flows_passed": "0", "iperf_attempts": "0",
                "iperf_retried_flows": "0", "iperf_retransmissions": "0",
                "iperf_reported_duration_s": "0.0",
                "aggregate_receiver_throughput_mbps": "0.0", "retry_count": "0",
                "failure_stage": "PREPARATION", "formation_timing_valid": "false",
                "verified_formation_latency_s": "",
                "formation_failed": "true", "success": "false",
                "failure_reason": "RuntimeError:background preparation failed",
            })
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader(); writer.writerows(rows)
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            changed = []
            for event in payloads:
                if event["run_id"] != run_id:
                    changed.append(event); continue
                if event["stage"] in {"PING_COMMAND_RESULT", "IPERF3_COMMAND_RESULT"}:
                    continue
                if event["stage"] == "BACKGROUND_PREPARED":
                    event["stage"] = "BACKGROUND_PREPARATION_FAILED"
                if event["stage"] == "ROUTE_INSTALL_STARTED":
                    event["details"]["control_messages"] = 0
                    event["details"]["rules_installed"] = 0
                    event["details"]["commands"] = []
                if event["stage"] == "PING_VERIFY_FINISHED":
                    event["details"]["passed"] = 0
                if event["stage"] == "DATA_PLANE_VERIFIED":
                    event["stage"] = "FORMATION_FAILED"
                    event["details"].update(passed=0, failure_reason=row["failure_reason"])
                changed.append(event)
            events.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in changed),
                encoding="utf-8",
            )

            result = validate_completed_nominal_artifacts(runs, events, scope, config)

        self.assertTrue(result.complete)

    def test_nominal_validator_accepts_partial_route_install_evidence(self) -> None:
        """A thrown install can retain completed command evidence without returned totals."""
        with tempfile.TemporaryDirectory() as directory:
            runs, events, scope, config = self._nominal_fixture(Path(directory))
            with runs.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            row = rows[0]; run_id = row["run_id"]
            row.update({
                "control_messages": "0", "rules_installed": "0",
                "ping_edges_passed": "0", "ping_attempts": "0",
                "ping_retried_edges": "0", "ping_mean_rtt_ms": "0.0",
                "ping_max_packet_loss_percent": "100.0", "ping_timeouts": "0",
                "iperf_flows_passed": "0", "iperf_attempts": "0",
                "iperf_retried_flows": "0", "iperf_retransmissions": "0",
                "iperf_reported_duration_s": "0.0",
                "aggregate_receiver_throughput_mbps": "0.0", "retry_count": "0",
                "failure_stage": "ROUTE_INSTALLATION", "formation_failed": "true",
                "success": "false", "failure_reason": "RuntimeError:install failed",
            })
            with runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader(); writer.writerows(rows)
            payloads = [json.loads(line) for line in events.read_text().splitlines()]
            changed = []
            for event in payloads:
                if event["run_id"] != run_id:
                    changed.append(event); continue
                if event["stage"] in {"PING_COMMAND_RESULT", "IPERF3_COMMAND_RESULT"}:
                    continue
                if event["stage"] == "ROUTE_INSTALL_STARTED":
                    event["details"]["control_messages"] = 0
                    event["details"]["rules_installed"] = 0
                    event["details"]["commands"] = event["details"]["commands"][:1]
                if event["stage"] == "PING_VERIFY_FINISHED": event["details"]["passed"] = 0
                if event["stage"] == "DATA_PLANE_VERIFIED":
                    event["stage"] = "FORMATION_FAILED"
                    event["details"].update(passed=0, failure_reason=row["failure_reason"])
                changed.append(event)
            events.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in changed), encoding="utf-8")

            result = validate_completed_nominal_artifacts(runs, events, scope, config)

        self.assertTrue(result.complete)

    def test_nominal_selection_skips_invalid_candidate_and_binds_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); invalid = root / "exp1_netns_staging_v3"
            invalid.mkdir()
            valid = root / "exp1_netns_staging_v3_attempts" / "attempt-001"
            runs, events, scope, config = self._nominal_fixture(valid)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
            ).stdout.strip()
            (valid / "execution_commit.txt").write_text(head + "\n", encoding="utf-8")
            receipt = root / "environment" / "exp1_nominal_selection.json"

            selected = select_nominal_candidate(
                root, (invalid, valid), config, receipt, repo=Path("."),
            )
            verified = verify_nominal_selection_receipt(
                root, receipt, config, repo=Path("."),
            )

        self.assertEqual(selected, valid.resolve())
        self.assertEqual(verified, valid.resolve())

    def test_nominal_selection_rejects_outside_path_and_receipt_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(directory)
            outside = Path(outside_dir) / "attempt-001"
            runs, events, scope, config = self._nominal_fixture(outside)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=True,
            ).stdout.strip()
            (outside / "execution_commit.txt").write_text(head + "\n", encoding="utf-8")
            receipt = root / "environment" / "selection.json"
            with self.assertRaisesRegex(ValueError, "outside|confined"):
                select_nominal_candidate(root, (outside,), config, receipt, repo=Path("."))

            valid = root / "exp1_netns_staging_v3_attempts" / "attempt-001"
            runs, events, scope, config = self._nominal_fixture(valid)
            (valid / "execution_commit.txt").write_text(head + "\n", encoding="utf-8")
            select_nominal_candidate(root, (valid,), config, receipt, repo=Path("."))
            scope.write_text(scope.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "receipt|hash"):
                verify_nominal_selection_receipt(root, receipt, config, repo=Path("."))

    def test_launcher_orders_transactional_smoke_pilot_formal_before_exp2(self) -> None:
        """Reordering Exp2 ahead of the measured transactional arm breaks provenance."""
        script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text(encoding="utf-8")
        positions = [
            script.index("run_step exp1_transactional_smoke"),
            script.index("run_step exp1_transactional_pilot"),
            script.index("run_step exp1_transactional_formal"),
            script.index("run_step exp1_transactional_normalize"),
            script.index("run_step exp2"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("--seeds 9000:9001", script)
        self.assertIn("--task-sizes 4", script)
        self.assertIn("--scenario-classes command_rejection", script)
        self.assertIn("--methods proposed,cspf,global_sfc_embedding", script)
        self.assertIn("configs/exp1_transactional_formation_pilot_v1.yaml", script)
        self.assertIn("configs/exp1_transactional_formation_v1.yaml", script)
        self.assertIn("--require-complete-grid", script)

    def test_launcher_preserves_separate_arm_provenance_and_failure_status(self) -> None:
        """Sharing commit evidence or masking transactional failure invalidates the arms."""
        script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text(encoding="utf-8")
        self.assertIn('raw/exp1/execution_commit.txt', script)
        self.assertIn('raw/exp1_transactional/execution_commit.txt', script)
        self.assertIn("validate_wcnc_final_v3_exp1_nominal.py", script)
        self.assertNotIn("run_exp1_transactional ||", script)
        self.assertNotIn("exp1_transactional_formal ||", script)

    def test_launcher_plots_before_final_manifest_audit(self) -> None:
        """The final manifest cannot bind figure hashes if audit runs before plotting."""
        script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text(encoding="utf-8")
        self.assertLess(
            script.index("run_always figures"),
            script.index("run_always final_audit"),
        )
        self.assertIn("--write-manifest", script[script.index("run_always final_audit"):])

    def test_launcher_requires_explicit_twenty_figure_families(self) -> None:
        """A count-only check can accept missing figures plus unrelated stale files."""
        script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text(encoding="utf-8")
        expected = (
            "exp1_conditional_verified_latency_ms",
            "exp1_route_install_latency_ms",
            "exp2_feasible_qos_satisfaction_rate",
            "exp2_pre_verification_correct_decision_rate",
            "exp3_success_rate",
            "exp3_conditional_verified_latency_ms",
            "exp3_modification_scope_ratio",
            "exp4_link_failure_success_rate",
            "exp4_link_failure_conditional_verified_latency_ms",
            "exp4_link_failure_modification_scope_ratio",
            "exp4_agent_failure_success_rate",
            "exp4_agent_failure_conditional_verified_latency_ms",
            "exp4_agent_failure_modification_scope_ratio",
            "exp4_capacity_degradation_success_rate",
            "exp4_capacity_degradation_conditional_verified_latency_ms",
            "exp4_capacity_degradation_modification_scope_ratio",
            "exp1_transactional_method_owned_formation_latency_ms",
            "exp1_transactional_time_to_correct_formation_ms",
            "exp1_transactional_rollback_scope_objects",
            "exp1_transactional_wasted_rule_commands",
        )
        for stem in expected:
            self.assertIn(stem, script)
        self.assertIn("exp1_transactional_success_rate", script)
        self.assertNotIn('[[ "$count" -eq 16 ]]', script)

    def test_step_markers_bind_commit_protocol_command_and_artifact(self) -> None:
        """A stale marker must not skip a changed command or changed artifact."""
        script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text(encoding="utf-8")
        for field in (
            "git_commit=$current_commit", "protocol_signature=$protocol_signature",
            "command_signature=$command_signature", "artifact_hash=$current_artifact_hash",
        ):
            self.assertIn(field, script)
