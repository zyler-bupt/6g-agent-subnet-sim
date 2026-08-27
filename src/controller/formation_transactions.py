"""Deterministic protocol primitives for transactional Exp1 formation."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class FormationEdgeLike(Protocol):
    """The endpoint information required to scope a formation transaction."""

    edge_id: str
    source_index: int
    target_index: int


class FaultClass(str, Enum):
    STALE_VERSION = "stale_version"
    PREPARE_ACK_TIMEOUT = "prepare_ack_timeout"
    COMMAND_REJECTION = "command_rejection"


class TransactionPhase(str, Enum):
    NEW = "new"
    PREPARED = "prepared"
    REJECTED = "rejected"
    COMMITTED = "committed"
    ABORTED = "aborted"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class FormationTransaction:
    """Pure deployment payload consumed by the common transaction engine."""

    transaction_id: str
    attempt_index: int
    expected_versions: tuple[tuple[str, int], ...]
    commands: tuple[object, ...]
    affected_objects: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_versions", tuple(self.expected_versions))
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "affected_objects", tuple(self.affected_objects))
        if not self.transaction_id:
            raise ValueError("transaction_id must not be empty")
        if self.attempt_index < 0:
            raise ValueError("attempt_index must be nonnegative")
        seen_targets: set[str] = set()
        for target, version in self.expected_versions:
            if not target:
                raise ValueError("expected version target must not be empty")
            if target in seen_targets:
                raise ValueError(f"duplicate expected version target: {target}")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                raise ValueError("expected version must be a nonnegative integer")
            seen_targets.add(target)
        if any(not object_id for object_id in self.affected_objects):
            raise ValueError("affected objects must not contain empty identifiers")


@dataclass(frozen=True)
class CommandResult:
    """Executor result for activation or cleanup commands."""

    accepted: bool
    commands_attempted: int = 0
    reason: str = ""
    affected_objects: tuple[str, ...] = ()
    readback_before: tuple[dict[str, object], ...] | None = None
    readback_after: tuple[dict[str, object], ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "affected_objects", tuple(self.affected_objects))
        if self.readback_before is not None:
            object.__setattr__(self, "readback_before", tuple(self.readback_before))
        if self.readback_after is not None:
            object.__setattr__(self, "readback_after", tuple(self.readback_after))
        if self.commands_attempted < 0:
            raise ValueError("commands_attempted must be nonnegative")


@dataclass(frozen=True)
class StageResult(CommandResult):
    """Executor result for an inactive-table preparation attempt."""


class FormationTransactionExecutor(Protocol):
    """Boundary implemented by the topology-specific transaction backend."""

    def stage(
        self, transaction: FormationTransaction, ack_timeout_ms: int
    ) -> StageResult: ...

    def activate(self, transaction_id: str) -> CommandResult: ...

    def flush(self, transaction_id: str) -> CommandResult: ...

    def readback(self, transaction_id: str) -> tuple[dict[str, object], ...]: ...


class FormationTransactionEngine:
    """Fail-closed prepare/commit/abort state machine shared by Exp1 methods."""

    def __init__(
        self, executor: FormationTransactionExecutor, *, ack_timeout_ms: int
    ) -> None:
        if ack_timeout_ms <= 0:
            raise ValueError("ack_timeout_ms must be positive")
        self.executor = executor
        self.ack_timeout_ms = ack_timeout_ms
        self._states: dict[str, TransactionPhase] = {}
        self._transactions: dict[str, FormationTransaction] = {}
        self.attempts: list[TransactionAttempt] = []

    def prepare(self, transaction: FormationTransaction) -> TransactionAttempt:
        self._require_new(transaction.transaction_id)
        self._transactions[transaction.transaction_id] = transaction
        started_ns = time.monotonic_ns()
        try:
            result = self.executor.stage(transaction, self.ack_timeout_ms)
        except Exception as error:
            result = StageResult(accepted=False, reason=_exception_reason(error))
        phase = TransactionPhase.PREPARED if result.accepted else TransactionPhase.REJECTED
        attempt = self._record(
            transaction,
            "prepare",
            phase,
            TransactionPhase.REJECTED,
            result,
            result.readback_before if result.readback_before is not None else (),
            started_ns,
        )
        self._states[transaction.transaction_id] = attempt.phase
        return attempt

    def commit(self, transaction_id: str) -> TransactionAttempt:
        transaction = self._require_phase(transaction_id, TransactionPhase.PREPARED)
        before = self._readback_before_operation(transaction_id)
        started_ns = time.monotonic_ns()
        try:
            result = self.executor.activate(transaction_id)
        except Exception as error:
            result = CommandResult(accepted=False, reason=_exception_reason(error))
        phase = TransactionPhase.COMMITTED if result.accepted else TransactionPhase.REJECTED
        attempt = self._record(
            transaction,
            "commit",
            phase,
            TransactionPhase.REJECTED,
            result,
            result.readback_before if result.readback_before is not None else before,
            started_ns,
        )
        self._states[transaction_id] = attempt.phase
        return attempt

    def abort(self, transaction_id: str) -> TransactionAttempt:
        transaction = self._require_abortable(transaction_id)
        previous_phase = self._states[transaction_id]
        before = self._readback_before_operation(transaction_id)
        started_ns = time.monotonic_ns()
        try:
            result = self.executor.flush(transaction_id)
        except Exception as error:
            result = CommandResult(accepted=False, reason=_exception_reason(error))
        phase = TransactionPhase.ABORTED if result.accepted else previous_phase
        attempt = self._record(
            transaction,
            "abort",
            phase,
            previous_phase,
            result,
            result.readback_before if result.readback_before is not None else before,
            started_ns,
        )
        self._states[transaction_id] = attempt.phase
        return attempt

    def rollback(self, transaction_id: str) -> TransactionAttempt:
        transaction = self._require_phase(transaction_id, TransactionPhase.COMMITTED)
        before = self._readback_before_operation(transaction_id)
        started_ns = time.monotonic_ns()
        try:
            result = self.executor.flush(transaction_id)
        except Exception as error:
            result = CommandResult(accepted=False, reason=_exception_reason(error))
        phase = TransactionPhase.ROLLED_BACK if result.accepted else TransactionPhase.COMMITTED
        attempt = self._record(
            transaction,
            "rollback",
            phase,
            TransactionPhase.COMMITTED,
            result,
            result.readback_before if result.readback_before is not None else before,
            started_ns,
        )
        self._states[transaction_id] = attempt.phase
        return attempt

    def _require_new(self, transaction_id: str) -> None:
        if transaction_id in self._states:
            raise RuntimeError(f"transaction {transaction_id} already exists")

    def _require_phase(
        self, transaction_id: str, expected_phase: TransactionPhase
    ) -> FormationTransaction:
        if self._states.get(transaction_id) is not expected_phase:
            expected = "prepared" if expected_phase is TransactionPhase.PREPARED else expected_phase.value
            raise RuntimeError(f"transaction {transaction_id} is not {expected}")
        return self._transactions[transaction_id]

    def _require_abortable(self, transaction_id: str) -> FormationTransaction:
        phase = self._states.get(transaction_id)
        if phase not in {TransactionPhase.PREPARED, TransactionPhase.REJECTED}:
            raise RuntimeError(f"transaction {transaction_id} is not abortable")
        return self._transactions[transaction_id]

    def _record(
        self,
        transaction: FormationTransaction,
        operation: str,
        phase: TransactionPhase,
        failure_phase: TransactionPhase,
        result: CommandResult,
        before: tuple[dict[str, object], ...],
        started_ns: int,
    ) -> TransactionAttempt:
        effective_phase = phase
        accepted = result.accepted
        reason = result.reason
        after = result.readback_after
        after_fingerprint = ""
        if after is None:
            try:
                after = self.executor.readback(transaction.transaction_id)
            except Exception as error:
                accepted = False
                effective_phase = failure_phase
                reason = _combined_reason(reason, f"post-operation readback failed: {_exception_reason(error)}")
                after_fingerprint = _error_fingerprint(error)
        ended_ns = time.monotonic_ns()
        attempt = TransactionAttempt(
            transaction_id=transaction.transaction_id,
            attempt_index=transaction.attempt_index,
            operation=operation,
            phase=effective_phase,
            accepted=accepted,
            started_ns=started_ns,
            ended_ns=ended_ns,
            affected_objects=result.affected_objects or transaction.affected_objects,
            commands_attempted=result.commands_attempted,
            reason=reason,
            readback_before_fingerprint=_readback_fingerprint(before),
            readback_after_fingerprint=after_fingerprint or _readback_fingerprint(after or ()),
        )
        self.attempts.append(attempt)
        return attempt

    def _readback_before_operation(
        self, transaction_id: str
    ) -> tuple[dict[str, object], ...]:
        try:
            return self.executor.readback(transaction_id)
        except Exception:
            return ()


def _readback_fingerprint(readback: tuple[dict[str, object], ...]) -> str:
    payload = json.dumps(readback, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error_fingerprint(error: Exception) -> str:
    payload = {"error_type": type(error).__name__, "message": str(error)}
    return _readback_fingerprint((payload,))


def _exception_reason(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _combined_reason(existing: str, extra: str) -> str:
    return f"{existing}; {extra}" if existing else extra


@dataclass(frozen=True)
class FormationFaultSchedule:
    fault_class: FaultClass
    num_agents: int
    seed: int
    logical_edge_id: str
    gateway_index: int
    reject_attempt: int = 1

    def fingerprint_payload(self) -> dict[str, int | str]:
        """Return stable, method-independent data for schedule fingerprinting."""
        return {
            "fault_class": self.fault_class.value,
            "num_agents": self.num_agents,
            "seed": self.seed,
            "logical_edge_id": self.logical_edge_id,
            "gateway_index": self.gateway_index,
            "reject_attempt": self.reject_attempt,
        }


@dataclass(frozen=True)
class TransactionAttempt:
    transaction_id: str
    attempt_index: int
    operation: str
    phase: TransactionPhase
    accepted: bool
    started_ns: int
    ended_ns: int
    affected_objects: tuple[str, ...] = ()
    commands_attempted: int = 0
    reason: str = ""
    readback_before_fingerprint: str = ""
    readback_after_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "affected_objects", tuple(self.affected_objects))
        if self.attempt_index < 0:
            raise ValueError("attempt_index must be nonnegative")
        if self.started_ns < 0:
            raise ValueError("started_ns must be nonnegative")
        if self.ended_ns < 0:
            raise ValueError("ended_ns must be nonnegative")
        if self.ended_ns < self.started_ns:
            raise ValueError("ended_ns must not precede started_ns")
        if self.commands_attempted < 0:
            raise ValueError("commands_attempted must be nonnegative")


def build_fault_schedule(
    fault_class: str,
    num_agents: int,
    seed: int,
    edges: Sequence[FormationEdgeLike],
) -> FormationFaultSchedule:
    """Select the paired deployment fault without reference to method."""
    ordered = sorted(edges, key=lambda edge: edge.edge_id)
    if not ordered:
        raise ValueError("fault schedule requires at least one formation edge")
    rng = random.Random(f"exp1-transactional-v1:{fault_class}:{num_agents}:{seed}")
    edge = ordered[rng.randrange(len(ordered))]
    gateway_index = rng.randrange(4)
    return FormationFaultSchedule(
        FaultClass(fault_class), num_agents, seed, edge.edge_id, gateway_index
    )


def task_descendant_closure(
    logical_edge_id: str, edges: Sequence[FormationEdgeLike]
) -> frozenset[str]:
    """Return an edge and all Task-DAG edges reachable from its target."""
    edge_by_id = {edge.edge_id: edge for edge in edges}
    if logical_edge_id not in edge_by_id:
        raise ValueError(f"unknown logical edge: {logical_edge_id}")

    children_by_source: dict[int, list[FormationEdgeLike]] = {}
    for edge in edges:
        children_by_source.setdefault(edge.source_index, []).append(edge)

    closure = {logical_edge_id}
    pending = [edge_by_id[logical_edge_id].target_index]
    while pending:
        source_index = pending.pop()
        for child in children_by_source.get(source_index, ()):
            if child.edge_id not in closure:
                closure.add(child.edge_id)
                pending.append(child.target_index)
    return frozenset(closure)


def retry_scope_for_method(
    method_id: str, logical_edge_id: str, edges: Sequence[FormationEdgeLike]
) -> frozenset[str]:
    """Return the retry unit owned by each Exp1 formation method."""
    if method_id == "proposed":
        return task_descendant_closure(logical_edge_id, edges)
    if method_id == "cspf":
        if not any(edge.edge_id == logical_edge_id for edge in edges):
            raise ValueError(f"unknown logical edge: {logical_edge_id}")
        return frozenset({logical_edge_id})
    if method_id == "global_sfc_embedding":
        chain = _global_chain_for_edge(logical_edge_id, edges)
        return frozenset({"chain:" + ",".join(chain)})
    raise ValueError(f"unsupported formation method: {method_id}")


def _global_chain_for_edge(
    logical_edge_id: str, edges: Sequence[FormationEdgeLike]
) -> tuple[str, ...]:
    edge_by_id = {edge.edge_id: edge for edge in edges}
    if logical_edge_id not in edge_by_id:
        raise ValueError(f"unknown logical edge: {logical_edge_id}")

    selected = edge_by_id[logical_edge_id]
    predecessors_by_target: dict[int, list[FormationEdgeLike]] = {}
    successors_by_source: dict[int, list[FormationEdgeLike]] = {}
    for edge in edges:
        predecessors_by_target.setdefault(edge.target_index, []).append(edge)
        successors_by_source.setdefault(edge.source_index, []).append(edge)

    selected_ids = {logical_edge_id}
    upstream: list[FormationEdgeLike] = []
    target_index = selected.source_index
    while candidates := _unselected_edges(
        predecessors_by_target.get(target_index, ()), selected_ids
    ):
        predecessor = candidates[0]
        upstream.append(predecessor)
        selected_ids.add(predecessor.edge_id)
        target_index = predecessor.source_index

    downstream: list[FormationEdgeLike] = []
    source_index = selected.target_index
    while candidates := _unselected_edges(
        successors_by_source.get(source_index, ()), selected_ids
    ):
        successor = candidates[0]
        downstream.append(successor)
        selected_ids.add(successor.edge_id)
        source_index = successor.target_index

    return tuple(
        edge.edge_id for edge in reversed(upstream)
    ) + (logical_edge_id,) + tuple(edge.edge_id for edge in downstream)


def _unselected_edges(
    edges: Sequence[FormationEdgeLike], selected_ids: set[str]
) -> list[FormationEdgeLike]:
    return sorted(
        (edge for edge in edges if edge.edge_id not in selected_ids),
        key=lambda edge: edge.edge_id,
    )
