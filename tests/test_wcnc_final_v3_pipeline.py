from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.exp1_netns_verified_formation import FormationEdge, _plan_canonical_formation
from experiments.paper_protocol import EXPERIMENT_FAILURE_METHODS, stable_fingerprint
from experiments.exp1_transactional_formation import (
    _grid_payload,
    _logical_scenario_fingerprint,
    build_transactional_schedule,
    load_transactional_config,
    run_transactional_experiment,
)
from src.simulation.paper_failure_scenarios import generate_paper_failure_snapshot
from scripts.aggregate_wcnc_final_v3 import aggregate_experiment
from scripts.normalize_wcnc_final_v3_exp1 import _configuration_sha256, normalize
from scripts.normalize_wcnc_final_v3_exp1_transactional import (
    _validate_attempt_events,
    normalize_transactional,
)
from scripts.plot_wcnc_final_v3 import load
from scripts.audit_wcnc_final_v3 import (
    audit,
    build_manifest,
    exp1_canonical_grid_errors,
    exp1_paired_eligibility_errors,
)


class WcncFinalV3PipelineTests(unittest.TestCase):
    @staticmethod
    def _causal_transactional_stream() -> tuple[dict[str, str], list[dict[str, object]]]:
        row = {
            "run_id": "causal-run", "run_sequence": "1", "method_id": "proposed",
            "scenario_class": "stale_version", "seed": "0", "num_agents": "4",
            "success": "True", "timeout": "False", "failure_stage": "",
            "failure_reason": "", "verified_correct": "True",
            "infrastructure_cleanup_success": "True", "cleanup_failure_reason": "",
            "logical_fault_target": "edge-0@gateway-0",
            "fault_schedule_fingerprint": stable_fingerprint({
                "fault_class": "stale_version", "num_agents": 4, "seed": 0,
                "logical_edge_id": "edge-0", "gateway_index": 0,
                "reject_attempt": 1,
            }),
            "attempt_count": "4", "prepare_attempts": "2",
            "commit_attempts": "1", "rollback_count": "0",
        }
        identity = {
            "run_id": row["run_id"], "run_sequence": 1, "method_id": "proposed",
            "scenario_class": "stale_version", "seed": 0, "num_agents": 4,
            "success": True, "timeout": False, "failure_stage": "",
            "failure_reason": "",
        }

        def transaction(
            operation: str, transaction_id: str, attempt_index: int,
            *, accepted: bool, phase: str,
        ) -> dict[str, object]:
            return {
                **identity, "stage": f"TRANSACTION_{operation.upper()}",
                "details": {
                    "transaction_id": transaction_id,
                    "attempt_index": attempt_index, "operation": operation,
                    "phase": phase, "accepted": accepted,
                    "started_ns": 1, "ended_ns": 2,
                    "affected_objects": ["edge-0"], "commands_attempted": 1,
                    "reason": "fixture fault" if not accepted else "",
                    "readback_before_fingerprint": "r" * 64,
                    "readback_after_fingerprint": "s" * 64,
                },
            }

        events = [
            {**identity, "stage": "TASK_RECEIVED", "details": {}},
            {**identity, "stage": "PLAN_FINISHED", "details": {
                "method_id": "proposed", "fault_schedule_visible": False,
            }},
            {**identity, "stage": "COMMON_INFRASTRUCTURE_INSTALLED", "details": {
                "fingerprint": "c" * 64, "provenance": "fixture",
                "command_count": 1, "method_owned": False,
            }},
            {**identity, "stage": "FAULT_INJECTED", "details": {
                "fault_class": "stale_version", "fault_attempt": 1,
                "logical_edge_id": "edge-0", "gateway_index": 0,
                "mechanism": "observed_version_mismatch", "reason": "fixture fault",
            }},
            transaction("prepare", "tx-0", 0, accepted=False, phase="rejected"),
            transaction("abort", "tx-0", 0, accepted=True, phase="aborted"),
            transaction("prepare", "tx-1", 1, accepted=True, phase="prepared"),
            transaction("commit", "tx-1", 1, accepted=True, phase="committed"),
            {**identity, "stage": "VERIFIED_CORRECT", "details": {}},
            {**identity, "stage": "TERMINAL_ROW_READY", "details": {
                "success": True, "timeout": False, "failure_stage": "",
                "failure_reason": "", "verified_correct": True,
            }},
        ]
        WcncFinalV3PipelineTests._resequence_events(events)
        return row, events

    @staticmethod
    def _resequence_events(events: list[dict[str, object]]) -> None:
        for sequence, event in enumerate(events, start=1):
            event["event_sequence"] = sequence
            event["timestamp"] = float(sequence)

    def test_transactional_event_causality_rejects_invalid_operation_graphs(self) -> None:
        cases = (
            "commit_without_prepare", "commit_after_abort", "rollback_without_commit",
            "duplicate_prepare", "duplicate_commit", "success_without_accepted_commit",
            "wrong_fault_mechanism", "missing_fault", "fault_after_rejection",
            "transaction_before_common", "verified_before_commit",
        )
        for case in cases:
            with self.subTest(case=case):
                row, events = self._causal_transactional_stream()
                row = copy.deepcopy(row)
                events = copy.deepcopy(events)
                if case == "commit_without_prepare":
                    del events[6]
                    row["attempt_count"] = "3"
                    row["prepare_attempts"] = "1"
                elif case == "commit_after_abort":
                    abort = copy.deepcopy(events[5])
                    abort["details"]["transaction_id"] = "tx-1"
                    abort["details"]["attempt_index"] = 1
                    events.insert(7, abort)
                    row["attempt_count"] = "5"
                elif case == "rollback_without_commit":
                    events[7]["stage"] = "TRANSACTION_ROLLBACK"
                    events[7]["details"].update({
                        "operation": "rollback", "phase": "rolled_back",
                    })
                    row["commit_attempts"] = "0"
                    row["rollback_count"] = "1"
                elif case == "duplicate_prepare":
                    events.insert(7, copy.deepcopy(events[6]))
                    row["attempt_count"] = "5"
                    row["prepare_attempts"] = "3"
                elif case == "duplicate_commit":
                    events.insert(8, copy.deepcopy(events[7]))
                    row["attempt_count"] = "5"
                    row["commit_attempts"] = "2"
                elif case == "success_without_accepted_commit":
                    events[7]["details"].update({
                        "accepted": False, "phase": "rejected",
                        "reason": "commit rejected",
                    })
                elif case == "wrong_fault_mechanism":
                    events[3]["details"]["mechanism"] = "executor_rejection"
                elif case == "missing_fault":
                    del events[3]
                elif case == "fault_after_rejection":
                    fault = events.pop(3)
                    events.insert(5, fault)
                elif case == "transaction_before_common":
                    common = events.pop(2)
                    events.insert(5, common)
                else:
                    verified = events.pop(8)
                    events.insert(7, verified)
                self._resequence_events(events)

                with self.assertRaises(ValueError):
                    _validate_attempt_events([row], events)

    def test_transactional_event_causality_accepts_retry_and_pre_prepare_failure(self) -> None:
        row, events = self._causal_transactional_stream()
        first_attempt = _validate_attempt_events([row], events)
        self.assertFalse(first_attempt[row["run_id"]])

        failed_row = copy.deepcopy(row)
        failed_row.update({
            "success": "False", "failure_stage": "PLAN",
            "failure_reason": "RuntimeError: plan failed", "verified_correct": "False",
            "attempt_count": "0", "prepare_attempts": "0", "commit_attempts": "0",
        })
        identity = {
            "run_id": failed_row["run_id"], "run_sequence": 1,
            "method_id": failed_row["method_id"],
            "scenario_class": failed_row["scenario_class"], "seed": 0,
            "num_agents": 4, "success": False, "timeout": False,
            "failure_stage": "PLAN", "failure_reason": "RuntimeError: plan failed",
        }
        failed_events = [
            {**identity, "stage": "TASK_RECEIVED", "details": {}},
            {**identity, "stage": "TRIAL_FAILED", "details": {
                "failure_stage": "PLAN", "reason": "RuntimeError: plan failed",
            }},
            {**identity, "stage": "TERMINAL_ROW_READY", "details": {
                "success": False, "timeout": False, "failure_stage": "PLAN",
                "failure_reason": "RuntimeError: plan failed", "verified_correct": False,
            }},
        ]
        self._resequence_events(failed_events)

        pre_prepare = _validate_attempt_events([failed_row], failed_events)
        self.assertFalse(pre_prepare[failed_row["run_id"]])

    def test_successful_transaction_on_non_target_does_not_satisfy_fault_trial(self) -> None:
        row, events = self._causal_transactional_stream()
        del events[3:6]
        for event in events:
            if event["stage"] in {"TRANSACTION_PREPARE", "TRANSACTION_COMMIT"}:
                event["details"]["attempt_index"] = 0
                event["details"]["affected_objects"] = ["edge-1"]
        row.update({
            "attempt_count": "2", "prepare_attempts": "1",
            "commit_attempts": "1",
        })
        self._resequence_events(events)

        with self.assertRaisesRegex(ValueError, "fault-target prepare"):
            _validate_attempt_events([row], events)

    def test_target_prepare_requires_fault_injection(self) -> None:
        row, events = self._causal_transactional_stream()
        del events[3]
        self._resequence_events(events)

        with self.assertRaisesRegex(ValueError, "exactly one injection"):
            _validate_attempt_events([row], events)

    def test_target_prepare_rejects_duplicate_fault_injection(self) -> None:
        row, events = self._causal_transactional_stream()
        events.insert(4, copy.deepcopy(events[3]))
        self._resequence_events(events)

        with self.assertRaisesRegex(ValueError, "FAULT_INJECTED is duplicated"):
            _validate_attempt_events([row], events)

    def test_target_fault_injection_accepts_rejected_initial_prepare_and_retry(self) -> None:
        row, events = self._causal_transactional_stream()

        first_attempt = _validate_attempt_events([row], events)

        self.assertFalse(first_attempt[row["run_id"]])

    def _write_task4_shaped_transactional_artifacts(self, root: Path) -> tuple[Path, Path]:
        config = load_transactional_config(
            Path("configs/exp1_transactional_formation_v1.yaml")
        )
        runs = root / "runs.csv"
        attempts = root / "attempts.jsonl"
        scope = root / "measurement_scope.json"
        fields = (
            "protocol_id", "arm_id", "phase", "result_mode",
            "execution_mode_detail", "run_id", "run_sequence", "scenario_class",
            "scenario_fingerprint", "seed", "num_agents", "num_gateways",
            "num_business_edges", "method_id", "fault_schedule_fingerprint",
            "logical_fault_target", "observation_version_fingerprint",
            "verifier_fingerprint", "common_infrastructure_fingerprint",
            "common_infrastructure_provenance", "configuration_sha256",
            "attempt_count", "prepare_attempts", "commit_attempts", "rollback_count",
            "rollback_scope_objects", "wasted_rule_commands",
            "partial_state_exposure_ms", "planning_latency_ms",
            "common_infrastructure_latency_ms", "prepare_latency_ms",
            "commit_latency_ms", "rollback_replan_latency_ms",
            "method_owned_formation_latency_ms", "final_verification_latency_ms",
            "time_to_correct_formation_ms", "verified_correct",
            "infrastructure_cleanup_success", "cleanup_failure_reason",
            "leaked_state_fingerprint", "success", "timeout", "failure_stage",
            "failure_reason",
        )
        events = []
        with runs.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for scheduled in build_transactional_schedule(config):
                run_id = (
                    f"agents={scheduled.num_agents}:seed={scheduled.seed}:"
                    f"scenario={scheduled.scenario_class}:method={scheduled.method_id}"
                )
                row = {
                    "protocol_id": "wcnc_final_v3", "arm_id": "exp1_transactional_v1",
                    "phase": "formal", "result_mode": "measured_netns",
                    "execution_mode_detail": "real_linux_netns_transactional_formation",
                    "run_id": run_id, "run_sequence": scheduled.run_sequence,
                    "scenario_class": scheduled.scenario_class,
                    "scenario_fingerprint": _logical_scenario_fingerprint(
                        config, scheduled.scenario_class, scheduled.seed,
                        scheduled.num_agents,
                    ),
                    "seed": scheduled.seed, "num_agents": scheduled.num_agents,
                    "num_gateways": 4, "num_business_edges": 2,
                    "method_id": scheduled.method_id,
                    "fault_schedule_fingerprint": stable_fingerprint({
                        "fault_class": scheduled.scenario_class,
                        "num_agents": scheduled.num_agents, "seed": scheduled.seed,
                        "logical_edge_id": "edge-0", "gateway_index": 0,
                        "reject_attempt": 1,
                    }),
                    "logical_fault_target": "edge-0@gateway-0",
                    "observation_version_fingerprint": "o" * 64,
                    "verifier_fingerprint": "v" * 64,
                    "common_infrastructure_fingerprint": "c" * 64,
                    "common_infrastructure_provenance": "runner",
                    "configuration_sha256": stable_fingerprint(config),
                    "attempt_count": 4, "prepare_attempts": 2, "commit_attempts": 1,
                    "rollback_count": 0, "rollback_scope_objects": "[]",
                    "wasted_rule_commands": 0, "partial_state_exposure_ms": 0.0,
                    "planning_latency_ms": 1.0, "common_infrastructure_latency_ms": 1.0,
                    "prepare_latency_ms": 1.0, "commit_latency_ms": 1.0,
                    "rollback_replan_latency_ms": 0.0,
                    "method_owned_formation_latency_ms": 3.0,
                    "final_verification_latency_ms": 1.0,
                    "time_to_correct_formation_ms": 4.0,
                    "verified_correct": True, "infrastructure_cleanup_success": True,
                    "cleanup_failure_reason": "", "leaked_state_fingerprint": "",
                    "success": True, "timeout": False, "failure_stage": "",
                    "failure_reason": "",
                }
                writer.writerow(row)
                identity = {
                    "run_id": run_id, "run_sequence": scheduled.run_sequence,
                    "method_id": scheduled.method_id,
                    "scenario_class": scheduled.scenario_class,
                    "seed": scheduled.seed, "num_agents": scheduled.num_agents,
                    "success": True, "timeout": False, "failure_stage": "",
                    "failure_reason": "",
                }
                events.extend((
                    {**identity, "event_sequence": 1, "timestamp": 1.0,
                     "stage": "TASK_RECEIVED", "details": {}},
                    {**identity, "event_sequence": 2, "timestamp": 2.0,
                     "stage": "PLAN_FINISHED", "details": {
                         "method_id": scheduled.method_id,
                         "fault_schedule_visible": False,
                     }},
                    {**identity, "event_sequence": 3, "timestamp": 3.0,
                     "stage": "COMMON_INFRASTRUCTURE_INSTALLED", "details": {
                         "fingerprint": "c" * 64, "provenance": "runner",
                         "command_count": 1, "method_owned": False,
                     }},
                    {**identity, "event_sequence": 4, "timestamp": 4.0,
                     "stage": "FAULT_INJECTED", "details": {
                         "fault_class": scheduled.scenario_class,
                         "fault_attempt": 1, "logical_edge_id": "edge-0",
                         "gateway_index": 0,
                         "mechanism": {
                             "stale_version": "observed_version_mismatch",
                             "prepare_ack_timeout": "threading.Event.wait",
                             "command_rejection": "executor_rejection",
                         }[scheduled.scenario_class],
                         "reason": "fixture fault",
                     }},
                    {**identity, "event_sequence": 5, "timestamp": 5.0,
                     "stage": "TRANSACTION_PREPARE", "details": {
                         "transaction_id": "tx-0", "attempt_index": 0,
                         "operation": "prepare", "phase": "rejected",
                         "accepted": False, "started_ns": 1, "ended_ns": 2,
                         "affected_objects": ["edge-0"], "commands_attempted": 1,
                         "reason": "fixture fault", "readback_before_fingerprint": "r" * 64,
                         "readback_after_fingerprint": "s" * 64,
                     }},
                    {**identity, "event_sequence": 6, "timestamp": 6.0,
                     "stage": "TRANSACTION_ABORT", "details": {
                         "transaction_id": "tx-0", "attempt_index": 0,
                         "operation": "abort", "phase": "aborted",
                         "accepted": True, "started_ns": 3, "ended_ns": 4,
                         "affected_objects": ["edge-0"], "commands_attempted": 1,
                         "reason": "", "readback_before_fingerprint": "s" * 64,
                         "readback_after_fingerprint": "t" * 64,
                     }},
                    {**identity, "event_sequence": 7, "timestamp": 7.0,
                     "stage": "TRANSACTION_PREPARE", "details": {
                         "transaction_id": "tx-1", "attempt_index": 1,
                         "operation": "prepare", "phase": "prepared",
                         "accepted": True, "started_ns": 5, "ended_ns": 6,
                         "affected_objects": ["edge-0"], "commands_attempted": 1,
                         "reason": "", "readback_before_fingerprint": "r" * 64,
                         "readback_after_fingerprint": "s" * 64,
                     }},
                    {**identity, "event_sequence": 8, "timestamp": 8.0,
                     "stage": "TRANSACTION_COMMIT", "details": {
                         "transaction_id": "tx-1", "attempt_index": 1,
                         "operation": "commit", "phase": "committed",
                         "accepted": True, "started_ns": 3, "ended_ns": 4,
                         "affected_objects": ["edge-0"], "commands_attempted": 1,
                         "reason": "", "readback_before_fingerprint": "s" * 64,
                         "readback_after_fingerprint": "t" * 64,
                     }},
                    {**identity, "event_sequence": 9, "timestamp": 9.0,
                     "stage": "VERIFIED_CORRECT", "details": {}},
                    {**identity, "event_sequence": 10, "timestamp": 10.0,
                     "stage": "TERMINAL_ROW_READY", "details": {
                         "success": True, "timeout": False, "failure_stage": "",
                         "failure_reason": "", "verified_correct": True,
                     }},
                ))
        attempts.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        grid = _grid_payload(build_transactional_schedule(config))
        scope.write_text(json.dumps({
            "protocol_id": "wcnc_final_v3", "arm_id": "exp1_transactional_v1",
            "phase": "formal", "configuration_sha256": stable_fingerprint(config),
            "frozen_config_grid": grid, "invocation_grid": grid,
            "invocation_grid_sha256": stable_fingerprint(grid),
            "grid_source": "frozen_config", "completed_rows": 2250,
            "row_count": 2250, "event_count": len(events),
            "runs_csv_sha256": hashlib.sha256(runs.read_bytes()).hexdigest(),
            "attempts_jsonl_sha256": hashlib.sha256(attempts.read_bytes()).hexdigest(),
        }, sort_keys=True), encoding="utf-8")
        return runs, attempts

    def test_transactional_normalizer_accepts_runner_shaped_artifacts_and_emits_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, attempts = self._write_task4_shaped_transactional_artifacts(root)
            target = root / "trials.csv"
            rows = normalize_transactional(runs, target, attempts_path=attempts)
            metrics = aggregate_experiment(
                "exp1_transactional", target, root / "metrics.csv"
            )
            manifest = json.loads(
                (root / "normalization_manifest.json").read_text(encoding="utf-8")
            )
            target_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        self.assertEqual(len(rows), 2250)
        self.assertTrue(metrics)
        self.assertEqual({row["result_mode"] for row in rows}, {"measured_netns"})
        self.assertTrue(all(row["scenario_fingerprint"] for row in rows))
        self.assertEqual({row["first_attempt_commit"] for row in rows}, {"false"})
        self.assertEqual(manifest["row_count"], 2250)
        self.assertEqual(manifest["trials_sha256"], target_sha256)

    def test_transactional_normalizer_rejects_tampered_provenance_streams_and_config(self) -> None:
        cases = (
            "scope", "runs", "attempts", "terminal_only", "run_sequence",
            "event_outcome", "malformed", "duplicate_sequence", "reordered",
            "run_block_reordered", "row_sequence", "scenario_fingerprint",
            "count_mismatch", "prepare_count_mismatch", "commit_count_mismatch",
            "arbitrary_stage", "illegal_operation", "illegal_phase",
            "fault_mismatch", "cleanup_mismatch", "schema_boolean", "config",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runs, attempts = self._write_task4_shaped_transactional_artifacts(root)
                scope_path = root / "measurement_scope.json"
                scope = json.loads(scope_path.read_text(encoding="utf-8"))
                config_path: Path | None = None
                if case == "scope":
                    scope["row_count"] = 0
                elif case == "runs":
                    runs.write_bytes(runs.read_bytes() + b"\n")
                elif case == "attempts":
                    attempts.write_bytes(attempts.read_bytes() + b"\n")
                elif case in {
                    "row_sequence", "scenario_fingerprint", "count_mismatch",
                    "prepare_count_mismatch", "commit_count_mismatch", "schema_boolean",
                }:
                    with runs.open(encoding="utf-8", newline="") as handle:
                        run_rows = list(csv.DictReader(handle))
                        fields = list(run_rows[0])
                    if case == "row_sequence":
                        run_rows[0]["run_sequence"] = "999"
                    elif case == "scenario_fingerprint":
                        run_rows[0]["scenario_fingerprint"] = "x" * 64
                    elif case == "count_mismatch":
                        run_rows[0]["attempt_count"] = "99"
                    elif case == "prepare_count_mismatch":
                        run_rows[0]["prepare_attempts"] = "99"
                    elif case == "commit_count_mismatch":
                        run_rows[0]["commit_attempts"] = "99"
                    else:
                        run_rows[0]["success"] = "maybe"
                    with runs.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(run_rows)
                    scope["runs_csv_sha256"] = hashlib.sha256(runs.read_bytes()).hexdigest()
                    if case in {"row_sequence", "schema_boolean"}:
                        events = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines()]
                        for event in events:
                            if event["run_id"] == run_rows[0]["run_id"]:
                                if case == "row_sequence":
                                    event["run_sequence"] = 999
                                else:
                                    event["success"] = "maybe"
                                    if event["stage"] == "TERMINAL_ROW_READY":
                                        event["details"]["success"] = "maybe"
                        attempts.write_text(
                            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                            encoding="utf-8",
                        )
                        scope["attempts_jsonl_sha256"] = hashlib.sha256(attempts.read_bytes()).hexdigest()
                elif case in {
                    "terminal_only", "run_sequence", "malformed",
                    "event_outcome", "duplicate_sequence", "reordered",
                    "run_block_reordered", "arbitrary_stage", "illegal_operation",
                    "illegal_phase", "fault_mismatch", "cleanup_mismatch",
                }:
                    events = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines()]
                    if case == "terminal_only":
                        events = [event for event in events if event["stage"] == "TERMINAL_ROW_READY"]
                        for event in events:
                            event["event_sequence"] = 1
                    elif case == "run_sequence":
                        events[0]["run_sequence"] = 999
                    elif case == "event_outcome":
                        events[0]["success"] = False
                    elif case == "arbitrary_stage":
                        events[0]["stage"] = "ARBITRARY_REVIEWER_STAGE"
                    elif case == "illegal_operation":
                        events[4]["details"]["operation"] = "rollback"
                    elif case == "illegal_phase":
                        events[4]["details"]["phase"] = "committed"
                    elif case == "fault_mismatch":
                        events[3]["details"]["fault_class"] = "not_the_row_class"
                    elif case == "cleanup_mismatch":
                        events[0]["stage"] = "BACKGROUND_CLEANUP_FAILED"
                        events[0]["details"] = {"reason": "fabricated cleanup"}
                    elif case == "malformed":
                        del events[0]["timestamp"]
                    elif case == "duplicate_sequence":
                        events[1]["event_sequence"] = 1
                    elif case == "run_block_reordered":
                        events[0:20] = events[10:20] + events[0:10]
                    else:
                        events[0], events[1] = events[1], events[0]
                    attempts.write_text(
                        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                        encoding="utf-8",
                    )
                    scope["event_count"] = len(events)
                    scope["attempts_jsonl_sha256"] = hashlib.sha256(attempts.read_bytes()).hexdigest()
                else:
                    config_path = root / "altered_formal.yaml"
                    config_path.write_bytes(
                        Path("configs/exp1_transactional_formation_v1.yaml").read_bytes()
                        + b"\n"
                    )
                scope_path.write_text(json.dumps(scope, sort_keys=True), encoding="utf-8")
                with self.assertRaises(ValueError):
                    normalize_transactional(
                        runs, root / "trials.csv",
                        config_path or Path("configs/exp1_transactional_formation_v1.yaml"),
                        attempts,
                    )

    def test_transactional_normalizer_accepts_complete_producer_construction_failures(self) -> None:
        config = load_transactional_config(
            Path("configs/exp1_transactional_formation_v1.yaml")
        )

        def fail_topology(_num_agents: int, _num_gateways: int) -> object:
            raise RuntimeError("reviewed topology construction failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            produced = run_transactional_experiment(
                config,
                root / "producer",
                require_complete_grid=True,
                topology_factory=fail_topology,
            )
            raw = root / "producer" / "raw"
            normalized = normalize_transactional(
                raw / "runs.csv", root / "trials.csv",
                attempts_path=raw / "attempts.jsonl",
                scope_path=raw / "measurement_scope.json",
            )
            events = [
                json.loads(line)
                for line in (raw / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(produced), 2250)
        self.assertEqual(len(normalized), 2250)
        self.assertEqual({row.attempt_count for row in produced}, {0})
        self.assertEqual(
            {row.logical_fault_target for row in produced},
            {"unavailable_before_topology"},
        )
        paired_fingerprints: dict[tuple[str, int, int], set[str]] = {}
        for row in produced:
            key = (row.scenario_class, row.num_agents, row.seed)
            paired_fingerprints.setdefault(key, set()).add(
                row.fault_schedule_fingerprint
            )
        self.assertTrue(
            all(len(fingerprints) == 1 for fingerprints in paired_fingerprints.values())
        )
        self.assertEqual(
            [event["stage"] for event in events[:2]],
            ["TRIAL_FAILED", "TERMINAL_ROW_READY"],
        )

    def test_transactional_aggregation_rejects_bare_canonical_grid_without_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, attempts = self._write_task4_shaped_transactional_artifacts(root)
            target = root / "trials.csv"
            normalize_transactional(runs, target, attempts_path=attempts)
            (root / "normalization_manifest.json").unlink()
            with self.assertRaisesRegex(ValueError, "normalization manifest"):
                aggregate_experiment("exp1_transactional", target, root / "metrics.csv")

    def test_transactional_aggregation_validates_schema_and_manifest_counts(self) -> None:
        for case in ("schema", "event_count", "schema_hash"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runs, attempts = self._write_task4_shaped_transactional_artifacts(root)
                target = root / "trials.csv"
                normalize_transactional(runs, target, attempts_path=attempts)
                manifest_path = root / "normalization_manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if case == "schema":
                    with target.open(encoding="utf-8", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                        fields = list(rows[0])
                    rows[0]["success"] = "maybe"
                    with target.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(rows)
                    manifest["trials_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                elif case == "event_count":
                    manifest["event_count"] = 0
                else:
                    manifest["raw_schema_file_sha256"] = "0" * 64
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaises(ValueError):
                    aggregate_experiment("exp1_transactional", target, root / "metrics.csv")

    def test_transactional_normalizer_requires_exact_2250_grid(self) -> None:
        """A formal transactional artifact cannot be normalized from a partial run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete_runs = root / "runs.csv"
            attempts = root / "attempts.jsonl"
            target = root / "trials.csv"
            fields = (
                "protocol_id", "arm_id", "phase", "run_id", "scenario_class",
                "seed", "num_agents", "method_id", "fault_schedule_fingerprint",
                "configuration_sha256", "result_mode", "success", "timeout",
                "failure_reason",
            )
            with incomplete_runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "protocol_id": "wcnc_final_v3",
                        "arm_id": "exp1_transactional_v1",
                        "phase": "formal",
                        "run_id": f"run-{method}",
                        "scenario_class": "command_rejection",
                        "seed": 0,
                        "num_agents": 4,
                        "method_id": method,
                        "fault_schedule_fingerprint": "f" * 64,
                        "configuration_sha256": "not-reached",
                        "result_mode": "real_linux_netns_transactional_formation",
                        "success": True,
                        "timeout": False,
                        "failure_reason": "",
                    })
            attempts.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "2250"):
                normalize_transactional(
                    incomplete_runs,
                    target,
                    Path("configs/exp1_transactional_formation_v1.yaml"),
                )

    def test_transactional_formal_aggregation_rejects_partial_raw(self) -> None:
        """The paper aggregation path cannot be run directly on a partial raw file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "trials.csv"
            fields = (
                "scenario_class", "num_agents", "seed", "method_id",
                "fault_schedule_fingerprint", "success", "timeout",
                "verified_correct", "attempt_count", "rollback_scope_objects",
                "wasted_rule_commands", "partial_state_exposure_ms",
            )
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "scenario_class": "command_rejection", "num_agents": 4,
                        "seed": 0, "method_id": method,
                        "fault_schedule_fingerprint": "f" * 64,
                        "success": True, "timeout": False,
                        "verified_correct": True, "attempt_count": 1,
                        "rollback_scope_objects": "[]", "wasted_rule_commands": 0,
                        "partial_state_exposure_ms": 0,
                    })
            with self.assertRaisesRegex(ValueError, "2250"):
                aggregate_experiment("exp1_transactional", raw, root / "metrics.csv")

    def test_paired_differences_do_not_collapse_repeated_scenario_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "trials.csv"
            fields = (
                "scenario_fingerprint", "seed", "num_agents", "method_id",
                "success", "paired_analysis_eligible", "verified_formation_latency_s",
                "route_install_latency_s",
            )
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for seed in (0, 1):
                    for method, latency in (("proposed", 0.010), ("cspf", 0.012)):
                        writer.writerow({
                            "scenario_fingerprint": "shared-topology-fingerprint",
                            "seed": seed,
                            "num_agents": 4,
                            "method_id": method,
                            "success": True,
                            "paired_analysis_eligible": True,
                            "verified_formation_latency_s": latency,
                            "route_install_latency_s": latency,
                        })
            aggregate_experiment("exp1", raw, root / "metrics.csv")
            with (root / "paired_differences.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            latency = next(
                row for row in rows
                if row["method_id"] == "cspf"
                and row["metric"] == "conditional_verified_latency_ms"
            )
            self.assertEqual(latency["paired_trials"], "2")
            self.assertAlmostEqual(float(latency["paired_difference"]), 2.0)

    def test_exp1_audit_rejects_rowwise_instead_of_paired_attrition(self) -> None:
        rows = []
        for method in ("proposed", "cspf", "global_sfc_embedding"):
            rows.append({
                "scenario_fingerprint": "a" * 64,
                "seed": "0",
                "method_id": method,
                "failure_stage": "PREPARATION" if method == "proposed" else "",
                "infrastructure_valid": "false" if method == "proposed" else "true",
                "paired_analysis_eligible": "false" if method == "proposed" else "true",
                "paired_exclusion_reason": (
                    "paired_preparation_failure" if method == "proposed" else ""
                ),
            })
        errors = exp1_paired_eligibility_errors(rows)
        self.assertIn("paired eligibility is inconsistent", errors[0])

        for row in rows:
            row["paired_analysis_eligible"] = "false"
            row["paired_exclusion_reason"] = "paired_preparation_failure"
        self.assertEqual(exp1_paired_eligibility_errors(rows), [])

        for row in rows:
            row["failure_stage"] = (
                "DATA_PLANE_VERIFICATION"
                if row["method_id"] == "global_sfc_embedding"
                else ""
            )
            row["infrastructure_valid"] = "true"
            row["paired_analysis_eligible"] = "true"
            row["paired_exclusion_reason"] = ""
        self.assertEqual(exp1_paired_eligibility_errors(rows), [])

    def test_exp1_audit_requires_exact_method_set_for_each_seed(self) -> None:
        rows = []
        for seed, methods in (
            ("0", ("proposed", "cspf", "global_sfc_embedding")),
            ("1", ("proposed", "cspf", "cspf")),
        ):
            for method in methods:
                rows.append({
                    "scenario_fingerprint": "shared-fingerprint",
                    "seed": seed,
                    "method_id": method,
                    "failure_stage": "",
                    "infrastructure_valid": "true",
                    "paired_analysis_eligible": "true",
                    "paired_exclusion_reason": "",
                })
        errors = exp1_paired_eligibility_errors(rows)
        self.assertTrue(any("canonical method set" in error for error in errors))

    def test_exp1_canonical_grid_rejects_an_entire_missing_triplet(self) -> None:
        rows = [
            {"num_agents": "4", "seed": "0", "method_id": method}
            for method in ("proposed", "cspf", "global_sfc_embedding")
        ]
        errors = exp1_canonical_grid_errors(rows)
        self.assertTrue(any("750 rows" in error for error in errors))

    def test_exp1_canonical_grid_rejects_fractional_task_size(self) -> None:
        rows = [
            {"num_agents": str(size), "seed": str(seed), "method_id": method}
            for size in (4, 8, 12, 16, 20)
            for seed in range(50)
            for method in ("proposed", "cspf", "global_sfc_embedding")
        ]
        self.assertEqual(exp1_canonical_grid_errors(rows), [])
        rows[0]["num_agents"] = "4.5"
        self.assertTrue(exp1_canonical_grid_errors(rows))

    def test_exp1_normalizer_default_rejects_an_incomplete_formal_grid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runs.csv"
            target = root / "trials.csv"
            config_path = Path("configs/exp1_netns_verified_formation_v3.yaml")
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "num_agents",
                "method_id", "failure_reason", "configuration_sha256",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "result_mode": "real_linux_netns_veth_tc_data_plane",
                        "scenario_fingerprint": "one-pair",
                        "seed": 0,
                        "num_agents": 4,
                        "method_id": method,
                        "failure_reason": "",
                        "configuration_sha256": _configuration_sha256(config_path),
                    })
            with self.assertRaisesRegex(ValueError, "750 rows"):
                normalize(source, target, config_path)

    def test_exp1_paired_attrition_retains_raw_and_keeps_data_plane_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runs.csv"
            normalized = root / "trials.csv"
            metrics = root / "metrics.csv"
            config_path = Path("configs/exp1_netns_verified_formation_v3.yaml")
            configuration_hash = _configuration_sha256(config_path)
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "num_agents",
                "method_id", "success", "failure_stage", "failure_reason",
                "configuration_sha256", "verified_formation_latency_s",
                "route_install_latency_s", "control_messages", "rules_installed",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for fingerprint, seed, failing_method, failure_stage in (
                    ("a" * 64, 0, "proposed", "PREPARATION"),
                    ("b" * 64, 1, "global_sfc_embedding", "DATA_PLANE_VERIFICATION"),
                ):
                    for method in ("proposed", "cspf", "global_sfc_embedding"):
                        failed = method == failing_method
                        writer.writerow({
                            "result_mode": "real_linux_netns_veth_tc_data_plane",
                            "scenario_fingerprint": fingerprint,
                            "seed": seed,
                            "num_agents": 4,
                            "method_id": method,
                            "success": str(not failed),
                            "failure_stage": failure_stage if failed else "",
                            "failure_reason": "failed" if failed else "",
                            "configuration_sha256": configuration_hash,
                            "verified_formation_latency_s": (
                                "" if failure_stage == "PREPARATION" and failed
                                else {"proposed": "0.010", "cspf": "0.012", "global_sfc_embedding": "0.014"}[method]
                            ),
                            "route_install_latency_s": {
                                "proposed": "0.001", "cspf": "0.002",
                                "global_sfc_embedding": "0.003",
                            }[method],
                            "control_messages": 1,
                            "rules_installed": 2,
                        })

            output = normalize(
                source,
                normalized,
                config_path,
                require_complete_grid=False,
            )
            self.assertEqual(len(output), 6)
            preparation_group = [
                row for row in output if row["scenario_fingerprint"] == "a" * 64
            ]
            self.assertEqual(
                {row["paired_analysis_eligible"] for row in preparation_group},
                {"false"},
            )
            self.assertEqual(
                [row["method_id"] for row in preparation_group if row["infrastructure_valid"] == "false"],
                ["proposed"],
            )
            data_plane_group = [
                row for row in output if row["scenario_fingerprint"] == "b" * 64
            ]
            self.assertEqual(
                {row["paired_analysis_eligible"] for row in data_plane_group},
                {"true"},
            )

            aggregated = aggregate_experiment("exp1", normalized, metrics)
            success = {
                row["method_id"]: (row["numerator"], row["denominator"])
                for row in aggregated
                if row["metric"] == "success_rate"
            }
            self.assertEqual(success["proposed"], (1, 1))
            self.assertEqual(success["cspf"], (1, 1))
            self.assertEqual(success["global_sfc_embedding"], (0, 1))
            retention = [
                row for row in aggregated
                if row["metric"] == "paired_scenario_retention_rate"
            ]
            self.assertEqual(
                {(row["numerator"], row["denominator"]) for row in retention},
                {(1, 2)},
            )
            preparation = [
                row for row in aggregated
                if row["metric"] == "preparation_failure_rate"
            ]
            self.assertEqual(
                {(row["numerator"], row["denominator"]) for row in preparation},
                {(1, 2)},
            )
            with metrics.with_name("paired_differences.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                differences = list(csv.DictReader(handle))
            latency_differences = {
                (row["method_id"], row["metric"]): float(row["paired_difference"])
                for row in differences
                if row["metric"] in {
                    "conditional_verified_latency_ms", "route_install_latency_ms"
                }
            }
            self.assertEqual(
                latency_differences[("cspf", "conditional_verified_latency_ms")],
                2.0,
            )
            self.assertEqual(
                latency_differences[("cspf", "route_install_latency_ms")],
                1.0,
            )
            self.assertEqual(
                latency_differences[("global_sfc_embedding", "route_install_latency_ms")],
                2.0,
            )

    def test_exp1_plot_outputs_total_and_route_latency_not_success_curve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = root / "aggregated" / "exp1" / "metrics.csv"
            metrics.parent.mkdir(parents=True)
            fields = (
                "protocol_id", "experiment", "series", "method_id", "x_name",
                "x_value", "metric", "estimate", "ci_low", "ci_high",
                "numerator", "denominator", "interval", "raw_path", "raw_sha256",
            )
            with metrics.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf"):
                    for metric, estimate in (
                        ("success_rate", 1.0),
                        ("conditional_verified_latency_ms", 5.0),
                        ("route_install_latency_ms", 2.0),
                    ):
                        writer.writerow({
                            "protocol_id": "wcnc_final_v3", "experiment": "exp1",
                            "series": "", "method_id": method, "x_name": "num_agents",
                            "x_value": 4, "metric": metric, "estimate": estimate,
                            "ci_low": estimate, "ci_high": estimate, "numerator": 1,
                            "denominator": 1, "interval": "test", "raw_path": "raw.csv",
                            "raw_sha256": hashlib.sha256(b"raw").hexdigest(),
                        })
            environment = dict(os.environ)
            environment["MPLCONFIGDIR"] = str(root / "mpl")
            subprocess.run(
                (
                    sys.executable, "scripts/plot_wcnc_final_v3.py", "--root", str(root),
                    "--experiment", "exp1",
                ),
                check=True,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertTrue((root / "figures" / "exp1_conditional_verified_latency_ms.pdf").is_file())
            self.assertTrue((root / "figures" / "exp1_route_install_latency_ms.pdf").is_file())
            self.assertFalse((root / "figures" / "exp1_success_rate.pdf").exists())
            route_svg = (
                root / "figures" / "exp1_route_install_latency_ms.svg"
            ).read_text(encoding="utf-8")
            self.assertIn("CSPF-based Formation", route_svg)
            self.assertNotIn("CSPF Recovery", route_svg)

    def test_transactional_plot_reads_only_aggregate_csv_and_is_repeatable(self) -> None:
        """Changing or removing raw inputs cannot affect transactional figures."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = root / "aggregated" / "exp1_transactional" / "metrics.csv"
            metrics.parent.mkdir(parents=True)
            fields = (
                "protocol_id", "experiment", "series", "method_id", "x_name",
                "x_value", "metric", "estimate", "ci_low", "ci_high",
                "numerator", "denominator", "interval", "raw_path", "raw_sha256",
            )
            with metrics.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for scenario_index, scenario in enumerate((
                    "stale_version", "prepare_ack_timeout", "command_rejection",
                )):
                    for method_index, method in enumerate((
                        "proposed", "cspf", "global_sfc_embedding",
                    )):
                        for size in (4, 8, 12, 16, 20):
                            for metric_index, metric in enumerate((
                                "method_owned_formation_latency_ms",
                                "time_to_correct_formation_ms",
                                "rollback_scope_objects",
                                "wasted_rule_commands",
                            )):
                                estimate = float(
                                    scenario_index + method_index + metric_index + size
                                )
                                writer.writerow({
                                    "protocol_id": "wcnc_final_v3",
                                    "experiment": "exp1_transactional",
                                    "series": scenario,
                                    "method_id": method, "x_name": "num_agents",
                                    "x_value": size, "metric": metric,
                                    "estimate": estimate, "ci_low": estimate - 0.25,
                                    "ci_high": estimate + 0.25, "numerator": 50,
                                    "denominator": 50,
                                    "interval": "fixture", "raw_path": "not-read.csv",
                                    "raw_sha256": "0" * 64,
                                })
            # An invalid raw tree proves the plotter consumes only metrics.csv.
            raw = root / "raw" / "exp1_transactional" / "trials.csv"
            raw.parent.mkdir(parents=True)
            raw.write_text("this is not CSV\n", encoding="utf-8")
            environment = dict(os.environ)
            environment["MPLCONFIGDIR"] = str(root / "mpl")
            command = (
                sys.executable, "scripts/plot_wcnc_final_v3.py", "--root", str(root),
                "--experiment", "exp1_transactional",
            )
            subprocess.run(command, check=True, env=environment, text=True, capture_output=True)
            figures = root / "figures"
            names = (
                "method_owned_formation_latency_ms", "time_to_correct_formation_ms",
                "rollback_scope_objects", "wasted_rule_commands",
            )
            first_hashes = {
                f"{name}.{suffix}": hashlib.sha256(
                    (figures / f"exp1_transactional_{name}.{suffix}").read_bytes()
                ).hexdigest()
                for name in names for suffix in ("pdf", "png", "svg")
            }
            self.assertFalse((figures / "exp1_transactional_success_rate.pdf").exists())
            subprocess.run(command, check=True, env=environment, text=True, capture_output=True)
            second_hashes = {
                f"{name}.{suffix}": hashlib.sha256(
                    (figures / f"exp1_transactional_{name}.{suffix}").read_bytes()
                ).hexdigest()
                for name in names for suffix in ("pdf", "png", "svg")
            }
            self.assertEqual(first_hashes, second_hashes)

    def test_transactional_audit_rejects_paired_fingerprint_drift_and_missing_attempts(self) -> None:
        """A formal arm needs paired fault/verifier fingerprints and causal evidence."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, attempts = self._write_task4_shaped_transactional_artifacts(root)
            raw = root / "raw" / "exp1_transactional" / "trials.csv"
            raw.parent.mkdir(parents=True)
            normalize_transactional(runs, raw, attempts_path=attempts)
            with raw.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fields = list(rows[0])
            rows[0]["fault_schedule_fingerprint"] = "d" * 64
            rows[0]["verifier_fingerprint"] = "e" * 64
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerows(rows)
            report = audit(root, {
                "raw_artifact_hashes": {
                    str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (root / "raw").glob("**/*") if path.is_file()
                },
            })
            self.assertIn("paired fault schedule drift", report["errors"])
            self.assertIn("paired verifier fingerprint drift", report["errors"])
            self.assertIn("missing transactional attempt provenance", report["errors"])

    def test_manifest_keeps_nominal_and_transactional_execution_arms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_manifest(Path(directory), repo=Path("."))
        self.assertEqual(set(manifest["exp1_arms"]), {"nominal", "exp1_transactional_v1"})

    def test_exp1_baselines_have_executable_deterministic_planners(self) -> None:
        edges = (FormationEdge("e1", 0, 1, 1.0), FormationEdge("e2", 1, 2, 1.0))
        for method in ("proposed", "cspf", "global_sfc_embedding"):
            first = _plan_canonical_formation(method, edges, 2)
            self.assertEqual(first, _plan_canonical_formation(method, edges, 2))
            self.assertEqual(first["planned_edge_count"], 2)

    def test_exp4_has_only_applicable_methods_and_no_zero_encoded_na(self) -> None:
        self.assertEqual(set(EXPERIMENT_FAILURE_METHODS["exp4"]["link_failure"]), {"proposed", "cspf", "full_rebuild"})
        self.assertNotIn("cspf", EXPERIMENT_FAILURE_METHODS["exp4"]["agent_failure"])
        self.assertNotIn("cspf", EXPERIMENT_FAILURE_METHODS["exp4"]["capacity_degradation"])
        headroom = generate_paper_failure_snapshot(
            "capacity_degradation", 1.1, 0, 0, capacity_ratio=1.1
        )
        self.assertAlmostEqual(headroom.post_fault_capacity_ratio, 1.1)

    def test_raw_to_aggregate_is_repeatable_and_retains_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = root / "trials.csv"
            fields = ["seed", "method_id", "gamma", "scenario_fingerprint", "success", "timeout", "failure_reason", "ground_truth_resolvable", "qos_satisfied", "pre_verification_correct_decision", "safe_rejection", "rollback_triggered", "verification_rescued", "unsafe_proposal_before_verification", "coordination_latency_ms"]
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for method in ("proposed", "sanet_dw", "weighted_sum", "independent"):
                    writer.writerow({"seed": 0, "method_id": method, "gamma": 1.0, "scenario_fingerprint": "same", "success": method != "independent", "timeout": False, "failure_reason": "failed" if method == "independent" else "", "ground_truth_resolvable": True, "qos_satisfied": method != "independent", "pre_verification_correct_decision": method != "independent", "safe_rejection": False, "rollback_triggered": method == "independent", "verification_rescued": method == "independent", "unsafe_proposal_before_verification": method == "independent", "coordination_latency_ms": 1})
            one = root / "one.csv"; two = root / "two.csv"
            aggregate_experiment("exp2", raw, one); aggregate_experiment("exp2", raw, two)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            with one.open(encoding="utf-8", newline="") as handle:
                aggregated = list(csv.DictReader(handle))
            failed = [row for row in aggregated if row["method_id"] == "independent" and row["metric"] == "success_rate"]
            self.assertEqual(failed[0]["denominator"], "1")
            self.assertEqual(failed[0]["estimate"], "0.0")

    def test_formal_plot_loader_refuses_demo(self) -> None:
        with self.assertRaisesRegex(ValueError, "demo"):
            load(Path("figures/out/aggregated_demo.csv"))

    def test_exp1_normalizer_rejects_non_measured_or_unpaired_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runs.csv"; target = Path(directory) / "out.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("result_mode", "scenario_fingerprint", "seed", "method_id", "failure_reason")); writer.writeheader()
                writer.writerow({"result_mode": "deterministic_cost_model", "scenario_fingerprint": "x", "seed": 0, "method_id": "proposed", "failure_reason": ""})
            with self.assertRaisesRegex(ValueError, "measured"):
                normalize(source, target)

    def test_exp1_normalizer_rejects_rows_from_a_different_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runs.csv"; target = Path(directory) / "out.csv"
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "method_id",
                "failure_reason", "configuration_sha256",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "result_mode": "real_linux_netns_veth_tc_data_plane",
                        "scenario_fingerprint": "paired", "seed": 0,
                        "method_id": method, "failure_reason": "",
                        "configuration_sha256": "0" * 64,
                    })
            with self.assertRaisesRegex(ValueError, "configuration hash"):
                normalize(source, target)

    def test_final_audit_rejects_exp1_rows_from_a_different_configuration(self) -> None:
        methods = {
            "exp1": ("proposed", "cspf", "global_sfc_embedding"),
            "exp2": ("proposed", "sanet_dw", "weighted_sum", "independent"),
            "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
            "exp4": ("proposed", "cspf", "full_rebuild"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for exp, method_set in methods.items():
                path = root / "raw" / exp / "trials.csv"; path.parent.mkdir(parents=True)
                fields = (
                    "scenario_fingerprint", "method_id", "success", "failure_reason",
                    "failure_type", "configuration_sha256",
                )
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                    for method in method_set:
                        writer.writerow({
                            "scenario_fingerprint": "paired", "method_id": method,
                            "success": True, "failure_reason": "",
                            "failure_type": "link_failure",
                            "configuration_sha256": "0" * 64 if exp == "exp1" else "",
                        })
            hashes = {
                str(path.relative_to(root)): __import__("hashlib").sha256(path.read_bytes()).hexdigest()
                for path in (root / "raw").glob("**/*") if path.is_file()
            }
            report = audit(root, {"raw_artifact_hashes": hashes})
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("Exp1 configuration hash drift", report["errors"])

    def test_manifest_hash_detects_raw_data_drift(self) -> None:
        methods = {
            "exp1": ("proposed", "cspf", "global_sfc_embedding"),
            "exp2": ("proposed", "sanet_dw", "weighted_sum", "independent"),
            "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
            "exp4": ("proposed", "cspf", "full_rebuild"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for exp, method_set in methods.items():
                path = root / "raw" / exp / "trials.csv"; path.parent.mkdir(parents=True)
                fields = ("scenario_fingerprint", "method_id", "success", "failure_reason", "failure_type")
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                    for method in method_set:
                        writer.writerow({"scenario_fingerprint": "paired", "method_id": method, "success": True, "failure_reason": "", "failure_type": "link_failure"})
            hashes = {str(path.relative_to(root)): __import__("hashlib").sha256(path.read_bytes()).hexdigest() for path in (root / "raw").glob("**/*") if path.is_file()}
            manifest = {
                "git_commit": "0" * 40,
                "raw_artifact_hashes": hashes,
                "source_hashes": {"scripts/aggregate_wcnc_final_v3.py": "0" * 64},
                "config_hashes": {"configs/wcnc_final_v3.yaml": "0" * 64},
            }
            target = root / "raw" / "exp2" / "trials.csv"
            target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            report = audit(root, manifest)
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("raw artifact drift", report["errors"])
            self.assertIn("scoped source drift", report["errors"])
            self.assertIn("config drift", report["errors"])
            self.assertIn("git commit drift", report["errors"])


if __name__ == "__main__": unittest.main()
