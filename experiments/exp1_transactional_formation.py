"""Measured, fault-injected transactional arm for Exp1 initial formation."""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

import yaml

import experiments.exp1_netns_verified_formation as nominal
from experiments.paper_protocol import PROTOCOL_ID, stable_fingerprint
from src.controller.formation_transactions import (
    CommandResult,
    FaultClass,
    FormationFaultSchedule,
    FormationTransaction,
    FormationTransactionEngine,
    StageResult,
    TransactionAttempt,
    build_fault_schedule,
    build_method_transactions,
    build_retry_transactions,
)


ARM_ID = "exp1_transactional_v1"
METHODS = ("proposed", "cspf", "global_sfc_embedding")
SCENARIO_CLASSES = (
    "stale_version",
    "prepare_ack_timeout",
    "command_rejection",
)
TASK_SIZES = (4, 8, 12, 16, 20)
INHERITED_CONFIG = "configs/exp1_netns_verified_formation_v3.yaml"
DEFAULT_OUTPUT_DIR = Path("results/paper/wcnc_final_v3/raw/exp1_transactional")


@dataclass(frozen=True)
class TransactionalScheduledRun:
    run_sequence: int
    seed: int
    num_agents: int
    method_id: str
    scenario_class: str


@dataclass(frozen=True)
class TransactionalFormationRun:
    protocol_id: str
    arm_id: str
    phase: str
    run_id: str
    run_sequence: int
    scenario_class: str
    seed: int
    num_agents: int
    num_gateways: int
    num_business_edges: int
    method_id: str
    fault_schedule_fingerprint: str
    logical_fault_target: str
    observation_version_fingerprint: str
    verifier_fingerprint: str
    common_infrastructure_fingerprint: str
    common_infrastructure_provenance: str
    configuration_sha256: str
    attempt_count: int
    prepare_attempts: int
    commit_attempts: int
    rollback_count: int
    rollback_scope_objects: str
    wasted_rule_commands: int
    partial_state_exposure_ms: float
    planning_latency_ms: float
    common_infrastructure_latency_ms: float
    prepare_latency_ms: float
    commit_latency_ms: float
    rollback_replan_latency_ms: float
    method_owned_formation_latency_ms: float | None
    final_verification_latency_ms: float
    time_to_correct_formation_ms: float | None
    verified_correct: bool
    infrastructure_cleanup_success: bool
    cleanup_failure_reason: str
    leaked_state_fingerprint: str
    success: bool
    timeout: bool
    failure_stage: str
    failure_reason: str


def load_transactional_config(path: str | Path) -> dict[str, Any]:
    """Load a frozen transactional config and resolve its nominal inheritance."""
    config_path = Path(path)
    source = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise RuntimeError("transactional Exp1 config must be a mapping")
    inherit_from = source.get("verification", {}).get("inherit_from")
    if inherit_from != INHERITED_CONFIG:
        raise RuntimeError(
            f"transactional Exp1 verification must inherit from {INHERITED_CONFIG}"
        )
    inherited_path = Path(str(inherit_from))
    if not inherited_path.is_absolute():
        inherited_path = Path(__file__).resolve().parents[1] / inherited_path
    inherited = yaml.safe_load(inherited_path.read_text(encoding="utf-8"))
    if not isinstance(inherited, dict):
        raise RuntimeError("inherited nominal Exp1 config must be a mapping")

    effective = deepcopy(inherited)
    for key, value in source.items():
        if key != "verification":
            effective[key] = deepcopy(value)
    effective["verification"] = deepcopy(inherited["verification"])
    effective["verification"]["inherit_from"] = str(inherit_from)
    _validate_transactional_config(effective)
    return effective


def _validate_transactional_config(config: Mapping[str, object]) -> None:
    experiment = config.get("experiment")
    simulation = config.get("simulation")
    transaction = config.get("transaction")
    verification = config.get("verification")
    if not all(isinstance(item, Mapping) for item in (experiment, simulation, transaction, verification)):
        raise RuntimeError("transactional Exp1 config sections must be mappings")
    assert isinstance(experiment, Mapping)
    assert isinstance(simulation, Mapping)
    assert isinstance(transaction, Mapping)
    assert isinstance(verification, Mapping)
    phase = experiment.get("phase")
    expected_seeds = "0:49" if phase == "formal" else "9000:9019" if phase == "pilot" else None
    requirements = (
        (experiment.get("protocol_id") == PROTOCOL_ID, "protocol_id"),
        (experiment.get("arm_id") == ARM_ID, "arm_id"),
        (experiment.get("frozen") is True, "frozen"),
        (tuple(config.get("methods", ())) == METHODS, "methods"),
        (tuple(config.get("scenario_classes", ())) == SCENARIO_CLASSES, "scenario_classes"),
        (tuple(simulation.get("task_sizes", ())) == TASK_SIZES, "task_sizes"),
        (simulation.get("seeds") == expected_seeds, "seeds"),
        (int(simulation.get("timeout_s", 0)) == 45, "timeout_s"),
        (int(transaction.get("max_attempts", 0)) == 2, "max_attempts"),
        (int(transaction.get("prepare_ack_timeout_ms", 0)) == 200, "prepare_ack_timeout_ms"),
        (verification.get("inherit_from") == INHERITED_CONFIG, "verification.inherit_from"),
    )
    failed = [name for accepted, name in requirements if not accepted]
    if failed:
        raise RuntimeError("transactional Exp1 frozen protocol drift: " + ",".join(failed))


def build_transactional_schedule(
    config: Mapping[str, object],
    *,
    seeds: Sequence[int] | None = None,
    task_sizes: Sequence[int] | None = None,
    methods: Sequence[str] | None = None,
    scenario_classes: Sequence[str] | None = None,
) -> tuple[TransactionalScheduledRun, ...]:
    simulation = config["simulation"]
    if not isinstance(simulation, Mapping):
        raise ValueError("simulation must be a mapping")
    selected_seeds = tuple(int(value) for value in (
        seeds if seeds is not None else _parse_seeds(str(simulation["seeds"]))
    ))
    selected_sizes = tuple(int(value) for value in (
        task_sizes if task_sizes is not None else simulation["task_sizes"]  # type: ignore[arg-type]
    ))
    selected_methods = tuple(str(value) for value in (
        methods if methods is not None else config["methods"]  # type: ignore[arg-type]
    ))
    selected_scenarios = tuple(str(value) for value in (
        scenario_classes if scenario_classes is not None else config["scenario_classes"]  # type: ignore[arg-type]
    ))
    if not selected_seeds or not selected_sizes or not selected_methods or not selected_scenarios:
        raise ValueError("transactional invocation grid dimensions must not be empty")
    if not set(selected_methods).issubset(METHODS):
        raise ValueError("unsupported transactional Exp1 method")
    if not set(selected_scenarios).issubset(SCENARIO_CLASSES):
        raise ValueError("unsupported transactional Exp1 scenario class")
    return tuple(
        TransactionalScheduledRun(
            run_sequence=index + 1,
            seed=seed,
            num_agents=num_agents,
            method_id=method_id,
            scenario_class=scenario_class,
        )
        for index, (seed, num_agents, scenario_class, method_id) in enumerate(
            (seed, num_agents, scenario_class, method_id)
            for num_agents in selected_sizes
            for seed in selected_seeds
            for scenario_class in selected_scenarios
            for method_id in selected_methods
        )
    )


class _FaultInjectingExecutor:
    """The sole post-plan boundary allowed to observe the fault schedule."""

    def __init__(
        self,
        topology: object,
        schedule: FormationFaultSchedule,
        observed_versions: Mapping[str, int],
    ) -> None:
        self._topology = topology
        self._schedule = schedule
        self._versions = dict(observed_versions)
        self._target_attempts = 0
        self._injected_transactions: set[str] = set()
        self.fault_events: list[dict[str, object]] = []

    @property
    def versions(self) -> Mapping[str, int]:
        return dict(self._versions)

    def stage(self, transaction: FormationTransaction, ack_timeout_ms: int) -> StageResult:
        if transaction.scope_kind == "shared" or any(
            getattr(command, "namespace", "").startswith("gateway-")
            or getattr(command, "namespace", "") == "outer"
            for command in transaction.commands
        ):
            return StageResult(
                accepted=False,
                reason="policy backend accepts only method-owned agent route candidates",
            )
        is_target = transaction.logical_edge_id == self._schedule.logical_edge_id
        if is_target:
            self._target_attempts += 1
        if is_target and self._target_attempts == self._schedule.reject_attempt:
            gateway = f"gateway-{self._schedule.gateway_index}"
            mechanism = "executor_rejection"
            if self._schedule.fault_class is FaultClass.STALE_VERSION:
                self._versions[gateway] = self._versions.get(gateway, 0) + 1
                reason = f"stale observed version for {gateway}"
                mechanism = "observed_version_mismatch"
            elif self._schedule.fault_class is FaultClass.PREPARE_ACK_TIMEOUT:
                threading.Event().wait(ack_timeout_ms / 1000.0)
                reason = f"prepare ACK deadline exceeded after {ack_timeout_ms} ms"
                mechanism = "threading.Event.wait"
            else:
                reason = f"injected command rejection for {transaction.logical_edge_id}"
            self._injected_transactions.add(transaction.transaction_id)
            self.fault_events.append(
                {
                    "timestamp": time.perf_counter(),
                    "stage": "FAULT_INJECTED",
                    "details": {
                        "fault_class": self._schedule.fault_class.value,
                        "fault_attempt": self._target_attempts,
                        "logical_edge_id": self._schedule.logical_edge_id,
                        "gateway_index": self._schedule.gateway_index,
                        "mechanism": mechanism,
                        "reason": reason,
                    },
                }
            )
            return StageResult(
                accepted=False,
                commands_attempted=1 if transaction.commands else 0,
                reason=reason,
                affected_objects=transaction.affected_objects,
                readback_before=(),
                readback_after=(),
            )
        for target, expected in transaction.expected_versions:
            if self._versions.get(target, 0) != expected:
                return StageResult(
                    accepted=False,
                    reason=f"expected version mismatch for {target}",
                    affected_objects=transaction.affected_objects,
                    readback_before=(),
                    readback_after=(),
                )
        return self._topology.stage_transaction_commands(
            transaction.transaction_id,
            transaction.commands,
            ack_timeout_ms=ack_timeout_ms,
        )

    def activate(self, transaction_id: str) -> CommandResult:
        return self._topology.activate_transaction(transaction_id)

    def flush(self, transaction_id: str) -> CommandResult:
        if transaction_id in self._injected_transactions:
            self._injected_transactions.remove(transaction_id)
            return CommandResult(accepted=True, readback_before=(), readback_after=())
        return self._topology.abort_transaction(transaction_id)

    def readback(self, transaction_id: str) -> tuple[dict[str, object], ...]:
        if transaction_id in self._injected_transactions:
            return ()
        return self._topology.read_transaction_state(transaction_id)


def run_transactional_trial(
    topology: object,
    config: Mapping[str, object],
    *,
    method_id: str,
    scenario_class: str,
    seed: int,
    num_agents: int,
    run_sequence: int = 1,
) -> tuple[TransactionalFormationRun, list[dict[str, object]]]:
    """Run one measured trial while retaining all exceptions as terminal data."""
    _validate_transactional_config(config)
    task = config["task"]
    traffic_control = config["traffic_control"]
    verification = config["verification"]
    transaction_config = config["transaction"]
    simulation = config["simulation"]
    experiment = config["experiment"]
    assert isinstance(task, Mapping)
    assert isinstance(traffic_control, Mapping)
    assert isinstance(verification, Mapping)
    assert isinstance(transaction_config, Mapping)
    assert isinstance(simulation, Mapping)
    assert isinstance(experiment, Mapping)
    if method_id not in METHODS:
        raise ValueError(f"unsupported transactional Exp1 method: {method_id}")
    FaultClass(scenario_class)

    run_id = (
        f"agents={num_agents}:seed={seed}:scenario={scenario_class}:method={method_id}"
    )
    events: list[dict[str, object]] = []
    engine: FormationTransactionEngine | None = None
    executor: _FaultInjectingExecutor | None = None
    background: object | None = None
    edges: tuple[nominal.FormationEdge, ...] = ()
    fault_schedule: FormationFaultSchedule | None = None
    common_fingerprint = ""
    common_provenance = ""
    observation_fingerprint = ""
    verifier_fingerprint = ""
    configuration_sha256 = stable_fingerprint(config)
    attempt_count = 1
    rollback_scope: set[str] = set()
    wasted_rule_commands = 0
    rollback_count = 0
    planning_latency = 0.0
    replan_planning_latency = 0.0
    common_latency = 0.0
    prepare_latency = 0.0
    commit_latency = 0.0
    rollback_replan_latency = 0.0
    final_verification_latency = 0.0
    partial_state_exposure = 0.0
    first_commit_at: float | None = None
    method_commit_finished: float | None = None
    verified_correct_at: float | None = None
    verified_correct = False
    cleanup_failures: list[dict[str, object]] = []
    task_received = time.perf_counter()
    deadline = task_received + float(simulation["timeout_s"])
    current_stage = "PREPARATION"
    failure_reason = ""
    success = False
    timed_out = False
    committed_ids: list[str] = []
    committed_keys: set[tuple[object, ...]] = set()

    try:
        topology.reset_task_routes()
        profiles = topology.configure_traffic_control(traffic_control, seed=seed)
        starter = getattr(topology, "start_background_traffic", None)
        if callable(starter):
            background = starter(
                traffic_control["background_traffic"], profiles, seed=seed  # type: ignore[index]
            )
        task_received = time.perf_counter()
        deadline = task_received + float(simulation["timeout_s"])
        events.append(_event(run_id, task_received, "TASK_RECEIVED", {}))
        edges = nominal.generate_dag(
            num_agents,
            float(task["edge_ratio"]),
            seed,
            float(task["required_throughput_mbps"]),
        )
        observed_versions = {
            f"gateway-{index}": 0 for index in range(int(topology.num_gateways))
        }
        observation_fingerprint = stable_fingerprint(observed_versions)

        current_stage = "PLAN"
        plan_started = time.perf_counter()
        try:
            plan = topology.plan_task_routes(
                method_id,
                edges,
                profiles=profiles,
                max_path_delay_ms=float(verification["ping"]["max_average_rtt_ms"]),  # type: ignore[index]
            )
        finally:
            plan_finished = time.perf_counter()
            planning_latency += plan_finished - plan_started
        events.append(
            _event(
                run_id,
                plan_finished,
                "PLAN_FINISHED",
                {"method_id": method_id, "fault_schedule_visible": False},
            )
        )

        # The schedule is deliberately constructed only after the planner has
        # returned.  Only the common executor below receives this object.
        fault_schedule = build_fault_schedule(
            scenario_class, num_agents, seed, edges
        )
        executor = _FaultInjectingExecutor(topology, fault_schedule, observed_versions)
        engine = FormationTransactionEngine(
            executor,
            ack_timeout_ms=int(transaction_config["prepare_ack_timeout_ms"]),
        )

        current_stage = "COMMON_INFRASTRUCTURE"
        common_commands, common_provenance = _common_infrastructure(plan)
        canonical_common = tuple(topology.common_infrastructure_commands())
        if common_commands != canonical_common:
            raise RuntimeError("method plan common infrastructure differs from canonical topology")
        common_fingerprint = stable_fingerprint(
            [_command_payload(command) for command in canonical_common]
        )
        common_started = time.perf_counter()
        try:
            topology.install_common_infrastructure(canonical_common)
        finally:
            common_finished = time.perf_counter()
            common_latency += common_finished - common_started
        events.append(
            _event(
                run_id,
                common_finished,
                "COMMON_INFRASTRUCTURE_INSTALLED",
                {
                    "fingerprint": common_fingerprint,
                    "provenance": common_provenance,
                    "command_count": len(canonical_common),
                    "method_owned": False,
                },
            )
        )

        current_stage = "PREPARE"
        initial_waves = _method_owned_waves(
            build_method_transactions(method_id, plan, edges)
        )
        expected_keys = {
            _transaction_key(transaction)
            for wave in initial_waves
            for transaction in wave
        }
        pending = list(initial_waves)
        retry_used = False
        while pending:
            _ensure_before(deadline)
            raw_wave = pending.pop(0)
            wave = tuple(
                _with_versions(transaction, executor.versions, fault_schedule)
                for transaction in raw_wave
                if _transaction_key(transaction) not in committed_keys
            )
            if not wave:
                continue
            prepare_started = time.perf_counter()
            try:
                prepared = _parallel_apply(engine.prepare, wave)
            finally:
                prepare_finished = time.perf_counter()
                prepare_latency += prepare_finished - prepare_started
            rejected = [
                (transaction, result)
                for transaction, result in zip(wave, prepared)
                if not result.accepted
            ]
            accepted = [
                (transaction, result)
                for transaction, result in zip(wave, prepared)
                if result.accepted
            ]
            if rejected:
                recovery_started = time.perf_counter()
                wasted_rule_commands += sum(
                    result.commands_attempted for _transaction, result in rejected
                )
                abort_targets = [transaction for transaction, _result in rejected]
                if method_id == "proposed":
                    abort_targets.extend(
                        transaction for transaction, _result in accepted
                    )
                abort_failures = _abort_transactions(engine, abort_targets)
                cleanup_failures.extend(abort_failures)
                if abort_failures:
                    current_stage = "ABORT_OR_ROLLBACK"
                    raise RuntimeError(_cleanup_failure_reason(abort_failures))
                replay: tuple[FormationTransaction, ...] = ()
                if method_id == "proposed":
                    replay_items = []
                    for transaction, result in accepted:
                        wasted_rule_commands += result.commands_attempted
                        replay_items.append(
                            replace(
                                transaction,
                                transaction_id=transaction.transaction_id + ":replay",
                                attempt_index=1,
                            )
                        )
                    replay = tuple(replay_items)
                    recovery_finished = time.perf_counter()
                    rollback_replan_latency += recovery_finished - recovery_started
                else:
                    recovery_finished = time.perf_counter()
                    rollback_replan_latency += recovery_finished - recovery_started
                    current_stage = "COMMIT"
                    commit_started = time.perf_counter()
                    try:
                        commits = _parallel_apply(
                            lambda transaction: engine.commit(transaction.transaction_id),
                            tuple(transaction for transaction, _result in accepted),
                        )
                    finally:
                        commit_finished = time.perf_counter()
                        commit_latency += commit_finished - commit_started
                    commit_failures: list[TransactionAttempt] = []
                    for (transaction, _prepared), committed in zip(accepted, commits):
                        if committed.accepted:
                            committed_ids.append(transaction.transaction_id)
                            committed_keys.add(_transaction_key(transaction))
                            first_commit_at = first_commit_at or commit_finished
                        else:
                            commit_failures.append(committed)
                    if commit_failures:
                        raise RuntimeError(commit_failures[0].reason or "commit rejected")
                if retry_used or int(transaction_config["max_attempts"]) < 2:
                    raise RuntimeError(rejected[0][1].reason or "prepare rejected")
                retry_used = True
                attempt_count = 2
                failed_object = (
                    rejected[0][0].logical_edge_id
                    or rejected[0][0].chain_id
                    or rejected[0][0].affected_objects[0]
                )
                replan_started = time.perf_counter()
                try:
                    retry_plan = topology.plan_task_routes(
                        method_id,
                        edges,
                        profiles=profiles,
                        max_path_delay_ms=float(verification["ping"]["max_average_rtt_ms"]),  # type: ignore[index]
                    )
                finally:
                    replan_finished = time.perf_counter()
                    replan_duration = replan_finished - replan_started
                    planning_latency += replan_duration
                    replan_planning_latency += replan_duration
                    rollback_replan_latency += replan_duration
                retry_waves = _method_owned_waves(
                    build_retry_transactions(method_id, failed_object, retry_plan, edges)
                )
                scoped = {
                    item
                    for retry_wave in retry_waves
                    for transaction in retry_wave
                    for item in transaction.affected_objects
                }
                rollback_scope.update(scoped)
                if replay:
                    retry_waves = (
                        tuple(retry_waves[0]) + replay,
                        *retry_waves[1:],
                    )
                pending = list(retry_waves) + pending
                continue

            current_stage = "COMMIT"
            commit_started = time.perf_counter()
            try:
                commits = _parallel_apply(
                    lambda transaction: engine.commit(transaction.transaction_id), wave
                )
            finally:
                commit_finished = time.perf_counter()
                commit_latency += commit_finished - commit_started
            commit_failures = []
            for transaction, committed in zip(wave, commits):
                if committed.accepted:
                    committed_ids.append(transaction.transaction_id)
                    committed_keys.add(_transaction_key(transaction))
                    first_commit_at = first_commit_at or commit_finished
                else:
                    commit_failures.append(committed)
            if commit_failures:
                raise RuntimeError(commit_failures[0].reason or "commit rejected")

        if committed_keys != expected_keys:
            missing = sorted(str(item) for item in expected_keys - committed_keys)
            raise RuntimeError("incomplete commit; missing transaction scopes: " + ",".join(missing))
        method_commit_finished = time.perf_counter()
        if first_commit_at is not None:
            partial_state_exposure += max(0.0, method_commit_finished - first_commit_at)
            first_commit_at = None
        _ensure_before(deadline)

        current_stage = "FINAL_VERIFY"
        verifier_fingerprint = stable_fingerprint(
            {
                "verification": verification,
                "edges": [asdict(edge) for edge in edges],
            }
        )
        verify_started = time.perf_counter()
        try:
            verified, verifier_reason = _verify_final(topology, edges, verification)
        finally:
            verify_finished = time.perf_counter()
            final_verification_latency += verify_finished - verify_started
        if not verified:
            raise RuntimeError(verifier_reason)
        _ensure_before(deadline)
        success = True
        verified_correct = True
        verified_correct_at = verify_finished
        current_stage = "VERIFIED_CORRECT"
        events.append(_event(run_id, verify_finished, "VERIFIED_CORRECT", {}))
    except Exception as error:
        ended = time.perf_counter()
        timed_out = isinstance(error, (TimeoutError,)) or ended >= deadline
        failure_reason = f"{type(error).__name__}: {error}"
        events.append(
            _event(
                run_id,
                ended,
                "TRIAL_FAILED",
                {"failure_stage": current_stage, "reason": failure_reason},
            )
        )
        if engine is not None:
            for transaction_id in reversed(committed_ids):
                try:
                    rolled_back = engine.rollback(transaction_id)
                except RuntimeError as cleanup_error:
                    cleanup_failures.append(
                        _cleanup_exception_failure(
                            "rollback", transaction_id, cleanup_error
                        )
                    )
                    continue
                if rolled_back.accepted:
                    rollback_count += 1
                    rollback_scope.update(rolled_back.affected_objects)
                else:
                    cleanup_failures.append(_cleanup_attempt_failure(rolled_back))
        if first_commit_at is not None:
            partial_state_exposure += max(0.0, time.perf_counter() - first_commit_at)
            first_commit_at = None
    finally:
        stopper = getattr(topology, "stop_background_traffic", None)
        if background is not None and callable(stopper):
            try:
                stopper(background)
            except Exception as error:
                cleanup_failures.append(
                    _cleanup_exception_failure("background_cleanup", "background", error)
                )
                events.append(
                    _event(
                        run_id,
                        time.perf_counter(),
                        "BACKGROUND_CLEANUP_FAILED",
                        {"reason": f"{type(error).__name__}: {error}"},
                    )
                )
                if success:
                    success = False
                    current_stage = "BACKGROUND_CLEANUP"
                    failure_reason = f"{type(error).__name__}: {error}"

    ended = time.perf_counter()
    if executor is not None:
        events.extend(
            _event(run_id, float(item["timestamp"]), str(item["stage"]), item["details"])
            for item in executor.fault_events
        )
    if engine is not None:
        events.extend(_attempt_event(run_id, attempt) for attempt in engine.attempts)
    events.sort(key=lambda item: (float(item["timestamp"]), str(item["stage"])))
    prepare_attempts = sum(
        attempt.operation == "prepare" for attempt in (engine.attempts if engine else ())
    )
    commit_attempts = sum(
        attempt.operation == "commit" for attempt in (engine.attempts if engine else ())
    )
    if fault_schedule is None and edges:
        fault_schedule = build_fault_schedule(scenario_class, num_agents, seed, edges)
    fault_fingerprint = (
        stable_fingerprint(fault_schedule.fingerprint_payload()) if fault_schedule else ""
    )
    logical_target = (
        f"{fault_schedule.logical_edge_id}@gateway-{fault_schedule.gateway_index}"
        if fault_schedule else ""
    )
    cleanup_failure_reason = _cleanup_failure_reason(cleanup_failures)
    leaked_state_fingerprint = (
        stable_fingerprint(cleanup_failures) if cleanup_failures else ""
    )
    row = TransactionalFormationRun(
        protocol_id=str(experiment["protocol_id"]),
        arm_id=str(experiment["arm_id"]),
        phase=str(experiment["phase"]),
        run_id=run_id,
        run_sequence=run_sequence,
        scenario_class=scenario_class,
        seed=seed,
        num_agents=num_agents,
        num_gateways=int(getattr(topology, "num_gateways", 0)),
        num_business_edges=len(edges),
        method_id=method_id,
        fault_schedule_fingerprint=fault_fingerprint,
        logical_fault_target=logical_target,
        observation_version_fingerprint=observation_fingerprint,
        verifier_fingerprint=verifier_fingerprint,
        common_infrastructure_fingerprint=common_fingerprint,
        common_infrastructure_provenance=common_provenance,
        configuration_sha256=configuration_sha256,
        attempt_count=attempt_count,
        prepare_attempts=int(prepare_attempts),
        commit_attempts=int(commit_attempts),
        rollback_count=rollback_count,
        rollback_scope_objects=json.dumps(sorted(rollback_scope), separators=(",", ":")),
        wasted_rule_commands=wasted_rule_commands,
        partial_state_exposure_ms=partial_state_exposure * 1000.0,
        planning_latency_ms=planning_latency * 1000.0,
        common_infrastructure_latency_ms=common_latency * 1000.0,
        prepare_latency_ms=prepare_latency * 1000.0,
        commit_latency_ms=commit_latency * 1000.0,
        rollback_replan_latency_ms=rollback_replan_latency * 1000.0,
        method_owned_formation_latency_ms=(
            (
                planning_latency
                + prepare_latency
                + commit_latency
                + rollback_replan_latency
                - replan_planning_latency
            ) * 1000.0
            if method_commit_finished is not None else None
        ),
        final_verification_latency_ms=final_verification_latency * 1000.0,
        time_to_correct_formation_ms=(
            (verified_correct_at - task_received) * 1000.0
            if verified_correct_at is not None else None
        ),
        verified_correct=verified_correct,
        infrastructure_cleanup_success=not cleanup_failures,
        cleanup_failure_reason=cleanup_failure_reason,
        leaked_state_fingerprint=leaked_state_fingerprint,
        success=success,
        timeout=timed_out,
        failure_stage="" if success else current_stage,
        failure_reason="" if success else failure_reason,
    )
    events.append(
        _event(
            run_id,
            ended,
            "TERMINAL_ROW_READY",
            {"success": row.success, "timeout": row.timeout},
        )
    )
    events.sort(key=lambda item: (float(item["timestamp"]), str(item["stage"])))
    return row, events


def run_transactional_experiment(
    config: Mapping[str, object],
    output_dir: Path,
    *,
    seeds: Sequence[int] | None = None,
    task_sizes: Sequence[int] | None = None,
    methods: Sequence[str] | None = None,
    scenario_classes: Sequence[str] | None = None,
    require_complete_grid: bool = False,
    topology_factory: Callable[[int, int], object] = nominal.ProcessNetnsTopology,
) -> list[TransactionalFormationRun]:
    """Execute and incrementally persist the exact invocation Cartesian grid."""
    schedule = build_transactional_schedule(
        config,
        seeds=seeds,
        task_sizes=task_sizes,
        methods=methods,
        scenario_classes=scenario_classes,
    )
    frozen_schedule = build_transactional_schedule(config)
    raw_dir = output_dir / "raw"
    if raw_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing raw output: {raw_dir}")
    raw_dir.mkdir(parents=True)
    runs_path = raw_dir / "runs.csv"
    attempts_path = raw_dir / "attempts.jsonl"
    scope_path = raw_dir / "measurement_scope.json"
    invocation_grid = _grid_payload(schedule)
    scope = {
        "protocol_id": config["experiment"]["protocol_id"],  # type: ignore[index]
        "arm_id": config["experiment"]["arm_id"],  # type: ignore[index]
        "phase": config["experiment"]["phase"],  # type: ignore[index]
        "configuration_sha256": stable_fingerprint(config),
        "frozen_config_grid": _grid_payload(frozen_schedule),
        "invocation_grid": invocation_grid,
        "invocation_grid_sha256": stable_fingerprint(invocation_grid),
        "grid_source": "cli_overrides" if any(
            value is not None for value in (seeds, task_sizes, methods, scenario_classes)
        ) else "frozen_config",
        "duration_clock": "time.perf_counter",
        "fault_visibility": "common executor boundary after planning",
        "common_infrastructure": "canonical topology commands installed outside method transactions",
        "attempts_written_before_terminal_rows": True,
        "completed_rows": 0,
    }
    scope_path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with runs_path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=[field.name for field in fields(TransactionalFormationRun)]).writeheader()
    attempts_path.write_text("", encoding="utf-8")

    rows: list[TransactionalFormationRun] = []
    num_gateways = int(config["task"]["num_gateways"])  # type: ignore[index]
    for scheduled in schedule:
        row: TransactionalFormationRun | None = None
        events: list[dict[str, object]] = []
        try:
            with topology_factory(scheduled.num_agents, num_gateways) as topology:  # type: ignore[attr-defined]
                row, events = run_transactional_trial(
                    topology,
                    config,
                    method_id=scheduled.method_id,
                    scenario_class=scheduled.scenario_class,
                    seed=scheduled.seed,
                    num_agents=scheduled.num_agents,
                    run_sequence=scheduled.run_sequence,
                )
        except Exception as error:
            if row is None:
                row, events = _construction_failure(config, scheduled, num_gateways, error)
            else:
                row = _append_teardown_failure(row, error)
                events.append(
                    _event(
                        row.run_id,
                        time.perf_counter(),
                        "TOPOLOGY_TEARDOWN_FAILED",
                        {"reason": f"{type(error).__name__}: {error}"},
                    )
                )
        events = [event for event in events if event["stage"] != "TERMINAL_ROW_READY"]
        events.append(
            _event(
                row.run_id,
                time.perf_counter(),
                "TERMINAL_ROW_READY",
                {"success": row.success, "timeout": row.timeout},
            )
        )
        events.sort(key=lambda item: (float(item["timestamp"]), str(item["stage"])))
        _append_jsonl(attempts_path, events)
        _append_csv(runs_path, row)
        rows.append(row)

    if require_complete_grid:
        _validate_complete_grid(rows, schedule)
    scope["completed_rows"] = len(rows)
    scope["complete_grid_validated"] = bool(require_complete_grid)
    scope_path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rows


def _common_infrastructure(plan: object) -> tuple[tuple[object, ...], str]:
    selected: list[object] = []
    labels: list[str] = []
    seen: set[tuple[object, ...]] = set()
    for batch in tuple(getattr(plan, "batches", ())):
        commands = tuple(getattr(batch, "commands", ()))
        owners = tuple(getattr(batch, "command_owners", ()))
        if len(commands) != len(owners):
            raise ValueError(f"batch {getattr(batch, 'label', '')!r} lacks exact ownership")
        for command, owner in zip(commands, owners):
            if owner != "shared":
                continue
            identity = (
                getattr(command, "namespace", None),
                getattr(command, "pid", None),
                tuple(getattr(command, "argv", ())),
            )
            if identity not in seen:
                seen.add(identity)
                selected.append(command)
                labels.append(str(getattr(batch, "label", "")))
    if not selected:
        raise ValueError("method plan is missing common infrastructure provenance")
    provenance = json.dumps(
        {"source": "plan.command_owners=shared", "batch_labels": sorted(set(labels))},
        sort_keys=True,
        separators=(",", ":"),
    )
    return tuple(selected), provenance


def _method_owned_waves(
    waves: Sequence[Sequence[FormationTransaction]],
) -> tuple[tuple[FormationTransaction, ...], ...]:
    filtered_waves: list[tuple[FormationTransaction, ...]] = []
    for wave in waves:
        filtered_transactions: list[FormationTransaction] = []
        for transaction in wave:
            if transaction.scope_kind == "shared":
                continue
            route_candidates = [
                command for command in transaction.commands
                if _is_agent_route_candidate(command)
            ]
            if not route_candidates:
                raise ValueError(
                    "method-owned transaction has no agent route candidates"
                )
            for command in transaction.commands:
                if _is_agent_route_candidate(command):
                    continue
                activation = _sfc_activation_rule(command)
                if transaction.scope_kind == "chain" and activation is not None:
                    if _activation_matches_staged_route(activation, route_candidates):
                        # The policy-table backend derives this same
                        # namespace/table/destination activation from the
                        # corresponding staged route.
                        continue
                    raise ValueError(
                        "SFC activation rule does not correspond to a staged route"
                    )
                raise ValueError(
                    "unsupported method-owned command for transactional policy backend"
                )
            filtered_transactions.append(
                replace(transaction, commands=tuple(route_candidates))
            )
        if filtered_transactions:
            filtered_waves.append(tuple(filtered_transactions))
    return tuple(filtered_waves)


def _is_agent_route_candidate(command: object) -> bool:
    return (
        getattr(command, "namespace", "").startswith("agent-")
        and tuple(getattr(command, "argv", ()))[:3] == ("ip", "route", "replace")
    )


def _sfc_activation_rule(
    command: object,
) -> tuple[str, int, ipaddress.IPv4Network | ipaddress.IPv6Network] | None:
    argv = tuple(getattr(command, "argv", ()))
    namespace = str(getattr(command, "namespace", ""))
    if not namespace.startswith("agent-"):
        return None
    if len(argv) != 9 or argv[:4] != ("ip", "rule", "add", "priority"):
        return None
    if argv[5] != "to" or argv[7] != "lookup":
        return None
    try:
        priority = int(argv[4])
        network = ipaddress.ip_network(argv[6], strict=False)
        table = int(argv[8])
    except ValueError:
        return None
    if priority <= 0 or table <= 0 or network.prefixlen != network.max_prefixlen:
        return None
    return namespace, table, network


def _activation_matches_staged_route(
    activation: tuple[str, int, ipaddress.IPv4Network | ipaddress.IPv6Network],
    routes: Sequence[object],
) -> bool:
    namespace, table, destination = activation
    for route in routes:
        if getattr(route, "namespace", "") != namespace:
            continue
        route_table_and_destination = _route_table_and_destination(route)
        if route_table_and_destination == (table, destination):
            return True
    return False


def _route_table_and_destination(
    command: object,
) -> tuple[int, ipaddress.IPv4Network | ipaddress.IPv6Network] | None:
    argv = tuple(getattr(command, "argv", ()))
    if argv[:3] != ("ip", "route", "replace"):
        return None
    try:
        table_index = argv.index("table", 3)
        table = int(argv[table_index + 1])
        destination = argv[3] if table_index != 3 else argv[5]
        network = ipaddress.ip_network(destination, strict=False)
    except (IndexError, ValueError):
        return None
    return table, network


def _with_versions(
    transaction: FormationTransaction,
    versions: Mapping[str, int],
    schedule: FormationFaultSchedule,
) -> FormationTransaction:
    expected = ()
    if transaction.logical_edge_id == schedule.logical_edge_id:
        gateway = f"gateway-{schedule.gateway_index}"
        expected = ((gateway, int(versions.get(gateway, 0))),)
    return replace(transaction, expected_versions=expected)


def _transaction_key(transaction: FormationTransaction) -> tuple[object, ...]:
    return (
        transaction.scope_kind,
        transaction.logical_edge_id,
        transaction.chain_id,
        transaction.hop_index,
        tuple(
            (
                getattr(command, "namespace", ""),
                getattr(command, "pid", None),
                tuple(getattr(command, "argv", ())),
            )
            for command in transaction.commands
        ),
    )


def _parallel_apply(function: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
    if len(items) <= 1:
        return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(32, len(items))) as pool:
        futures = [pool.submit(function, item) for item in items]
        return [future.result() for future in futures]


def _verify_final(
    topology: object,
    edges: Sequence[nominal.FormationEdge],
    verification: Mapping[str, object],
) -> tuple[bool, str]:
    ping = verification["ping"]
    iperf = verification["iperf3"]
    assert isinstance(ping, Mapping)
    assert isinstance(iperf, Mapping)
    max_attempts = int(verification["max_attempts"])
    backoff_s = int(verification["retry_backoff_ms"]) / 1000.0
    remaining_ping = list(edges)
    for attempt in range(1, max_attempts + 1):
        _count, rows = topology.verify_ping(
            remaining_ping,
            attempt=attempt,
            count=int(ping["count"]),
            timeout_s=float(ping["timeout_s"]),
            interval_s=float(ping["interval_s"]),
            max_packet_loss_percent=float(ping.get("max_packet_loss_percent", 100.0)),
            max_average_rtt_ms=float(ping["max_average_rtt_ms"]),
            parallel=bool(verification.get("parallel", True)),
        )
        passed = {str(row["edge_id"]) for row in rows if bool(row.get("passed"))}
        remaining_ping = [edge for edge in remaining_ping if edge.edge_id not in passed]
        if not remaining_ping or attempt == max_attempts:
            break
        threading.Event().wait(backoff_s)
    if remaining_ping:
        return False, f"ping verification failed for {len(remaining_ping)} edges"

    remaining_iperf = list(edges)
    for attempt in range(1, max_attempts + 1):
        _count, _throughput, rows = topology.verify_iperf3(
            remaining_iperf,
            attempt=attempt,
            duration_s=float(iperf["duration_s"]),
            omit_s=float(iperf.get("omit_s", 0.0)),
            client_timeout_s=float(iperf["client_timeout_s"]),
            base_port=int(iperf["base_port"]),
            parallel=bool(verification.get("parallel", True)),
        )
        passed = {str(row["edge_id"]) for row in rows if bool(row.get("passed"))}
        remaining_iperf = [edge for edge in remaining_iperf if edge.edge_id not in passed]
        if not remaining_iperf or attempt == max_attempts:
            break
        threading.Event().wait(backoff_s)
    if remaining_iperf:
        return False, f"iperf3 verification failed for {len(remaining_iperf)} flows"
    return True, ""


def _attempt_event(run_id: str, attempt: TransactionAttempt) -> dict[str, object]:
    details = asdict(attempt)
    details["phase"] = attempt.phase.value
    return _event(
        run_id,
        attempt.ended_ns / 1_000_000_000.0,
        f"TRANSACTION_{attempt.operation.upper()}",
        details,
    )


def _event(
    run_id: str, timestamp: float, stage: str, details: object
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "timestamp": timestamp,
        "stage": stage,
        "details": details,
    }


def _abort_transactions(
    engine: FormationTransactionEngine,
    transactions: Sequence[FormationTransaction],
) -> list[dict[str, object]]:
    """Attempt every required abort and retain failures as terminal evidence."""
    failures: list[dict[str, object]] = []
    for transaction in transactions:
        try:
            aborted = engine.abort(transaction.transaction_id)
        except RuntimeError as error:
            failures.append(
                _cleanup_exception_failure("abort", transaction.transaction_id, error)
            )
            continue
        if not aborted.accepted:
            failures.append(_cleanup_attempt_failure(aborted))
    return failures


def _cleanup_attempt_failure(attempt: TransactionAttempt) -> dict[str, object]:
    return {
        "operation": attempt.operation,
        "transaction_id": attempt.transaction_id,
        "reason": attempt.reason or f"{attempt.operation} rejected",
        "readback_before_fingerprint": attempt.readback_before_fingerprint,
        "readback_after_fingerprint": attempt.readback_after_fingerprint,
    }


def _cleanup_exception_failure(
    operation: str, transaction_id: str, error: Exception
) -> dict[str, object]:
    return {
        "operation": operation,
        "transaction_id": transaction_id,
        "reason": f"{type(error).__name__}: {error}",
        "readback_before_fingerprint": "",
        "readback_after_fingerprint": "",
    }


def _cleanup_failure_reason(failures: Sequence[Mapping[str, object]]) -> str:
    return "; ".join(
        f"{item['operation']} {item['transaction_id']}: {item['reason']}"
        for item in failures
    )


def _command_payload(command: object) -> dict[str, object]:
    return {
        "namespace": getattr(command, "namespace", ""),
        "argv": list(getattr(command, "argv", ())),
    }


def _ensure_before(deadline: float) -> None:
    if time.perf_counter() >= deadline:
        raise TimeoutError("transactional formation trial deadline exceeded")


def _grid_payload(
    schedule: Sequence[TransactionalScheduledRun],
) -> dict[str, object]:
    return {
        "seeds": sorted({item.seed for item in schedule}),
        "task_sizes": sorted({item.num_agents for item in schedule}),
        "methods": sorted({item.method_id for item in schedule}),
        "scenario_classes": sorted({item.scenario_class for item in schedule}),
        "expected_rows": len(schedule),
        "cartesian_keys": [
            {
                "seed": item.seed,
                "num_agents": item.num_agents,
                "method_id": item.method_id,
                "scenario_class": item.scenario_class,
            }
            for item in schedule
        ],
    }


def _validate_complete_grid(
    rows: Sequence[TransactionalFormationRun],
    schedule: Sequence[TransactionalScheduledRun],
) -> None:
    expected = [
        (item.seed, item.num_agents, item.method_id, item.scenario_class)
        for item in schedule
    ]
    actual = [
        (row.seed, row.num_agents, row.method_id, row.scenario_class) for row in rows
    ]
    if len(actual) != len(set(actual)):
        raise RuntimeError("transactional invocation grid contains duplicate terminal rows")
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise RuntimeError(
            f"transactional invocation grid incomplete: expected={len(expected)} actual={len(actual)}"
        )


def _append_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _append_csv(path: Path, row: TransactionalFormationRun) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(
            handle, fieldnames=[field.name for field in fields(TransactionalFormationRun)]
        ).writerow(asdict(row))
        handle.flush()
        os.fsync(handle.fileno())


def _append_teardown_failure(
    row: TransactionalFormationRun, error: Exception
) -> TransactionalFormationRun:
    failure = _cleanup_exception_failure("topology_teardown", "topology", error)
    cleanup_failures: list[dict[str, object]] = [failure]
    if row.cleanup_failure_reason:
        cleanup_failures.insert(
            0,
            {
                "operation": "prior_cleanup",
                "transaction_id": "trial",
                "reason": row.cleanup_failure_reason,
                "readback_before_fingerprint": "",
                "readback_after_fingerprint": row.leaked_state_fingerprint,
            },
        )
    return replace(
        row,
        success=False,
        infrastructure_cleanup_success=False,
        cleanup_failure_reason=_cleanup_failure_reason(cleanup_failures),
        leaked_state_fingerprint=stable_fingerprint(cleanup_failures),
        failure_stage=row.failure_stage or "TOPOLOGY_TEARDOWN",
        failure_reason=row.failure_reason or failure["reason"],
    )


def _construction_failure(
    config: Mapping[str, object],
    scheduled: TransactionalScheduledRun,
    num_gateways: int,
    error: Exception,
) -> tuple[TransactionalFormationRun, list[dict[str, object]]]:
    now = time.perf_counter()
    run_id = (
        f"agents={scheduled.num_agents}:seed={scheduled.seed}:"
        f"scenario={scheduled.scenario_class}:method={scheduled.method_id}"
    )
    reason = f"{type(error).__name__}: {error}"
    row = TransactionalFormationRun(
        protocol_id=str(config["experiment"]["protocol_id"]),  # type: ignore[index]
        arm_id=str(config["experiment"]["arm_id"]),  # type: ignore[index]
        phase=str(config["experiment"]["phase"]),  # type: ignore[index]
        run_id=run_id,
        run_sequence=scheduled.run_sequence,
        scenario_class=scheduled.scenario_class,
        seed=scheduled.seed,
        num_agents=scheduled.num_agents,
        num_gateways=num_gateways,
        num_business_edges=0,
        method_id=scheduled.method_id,
        fault_schedule_fingerprint="",
        logical_fault_target="",
        observation_version_fingerprint="",
        verifier_fingerprint="",
        common_infrastructure_fingerprint="",
        common_infrastructure_provenance="",
        configuration_sha256=stable_fingerprint(config),
        attempt_count=0,
        prepare_attempts=0,
        commit_attempts=0,
        rollback_count=0,
        rollback_scope_objects="[]",
        wasted_rule_commands=0,
        partial_state_exposure_ms=0.0,
        planning_latency_ms=0.0,
        common_infrastructure_latency_ms=0.0,
        prepare_latency_ms=0.0,
        commit_latency_ms=0.0,
        rollback_replan_latency_ms=0.0,
        method_owned_formation_latency_ms=None,
        final_verification_latency_ms=0.0,
        time_to_correct_formation_ms=None,
        verified_correct=False,
        infrastructure_cleanup_success=False,
        cleanup_failure_reason="",
        leaked_state_fingerprint="",
        success=False,
        timeout=isinstance(error, TimeoutError),
        failure_stage="TOPOLOGY_CONSTRUCTION",
        failure_reason=reason,
    )
    return row, [
        _event(
            run_id, now, "TRIAL_FAILED",
            {"failure_stage": row.failure_stage, "reason": reason},
        ),
        _event(run_id, now, "TERMINAL_ROW_READY", {"success": False, "timeout": row.timeout}),
    ]


def _parse_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, end = (int(item) for item in value.split(":", 1))
        if end < start:
            raise ValueError("seed range end must not precede start")
        return tuple(range(start, end + 1))
    return tuple(int(item) for item in value.split(",") if item.strip())


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _default_output_dir(config: Mapping[str, object]) -> Path:
    output = config.get("output")
    if not isinstance(output, Mapping) or not isinstance(output.get("provenance"), str):
        raise RuntimeError("transactional config is missing output provenance")
    return Path(str(output["provenance"]))


def _validate_cli_invocation(
    config: Mapping[str, object],
    output_dir: Path,
    *,
    has_overrides: bool,
    output_was_explicit: bool,
) -> None:
    experiment = config.get("experiment")
    if not isinstance(experiment, Mapping):
        raise RuntimeError("transactional config is missing experiment metadata")
    if has_overrides:
        if experiment.get("phase") == "formal":
            raise RuntimeError("CLI overrides are forbidden for the formal protocol")
        if (
            not output_was_explicit
            or output_dir.resolve() == DEFAULT_OUTPUT_DIR.resolve()
        ):
            raise RuntimeError(
                "CLI overrides require an explicit noncanonical output directory"
            )
    if (output_dir / "raw").exists():
        raise RuntimeError(
            f"refusing to overwrite existing raw output: {output_dir / 'raw'}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measured transactional Exp1 formation")
    parser.add_argument("--config", default="configs/exp1_transactional_formation_v1.yaml")
    parser.add_argument("--output-dir")
    parser.add_argument("--seeds", default="")
    parser.add_argument("--task-sizes", default="")
    parser.add_argument("--methods", default="")
    parser.add_argument("--scenario-classes", default="")
    parser.add_argument("--require-complete-grid", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_transactional_config(args.config)
    overrides = {
        "seeds": _parse_seeds(args.seeds) if args.seeds else None,
        "task_sizes": tuple(int(item) for item in _parse_csv(args.task_sizes))
        if args.task_sizes else None,
        "methods": _parse_csv(args.methods) if args.methods else None,
        "scenario_classes": _parse_csv(args.scenario_classes)
        if args.scenario_classes else None,
    }
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(config)
    _validate_cli_invocation(
        config,
        output_dir,
        has_overrides=any(value is not None for value in overrides.values()),
        output_was_explicit=args.output_dir is not None,
    )
    if not nominal._inside_user_namespace():
        raise SystemExit(nominal._reexec_in_user_namespace(sys.argv[1:]))
    nominal.validate_isolated_outer_namespace()
    rows = run_transactional_experiment(
        config,
        output_dir,
        require_complete_grid=args.require_complete_grid,
        **overrides,
    )
    successful = sum(row.success for row in rows)
    print(f"Exp1 transactional completed: {successful}/{len(rows)} successful runs")


if __name__ == "__main__":
    main()
