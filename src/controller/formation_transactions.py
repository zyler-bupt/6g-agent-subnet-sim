"""Deterministic protocol primitives for transactional Exp1 formation."""

from __future__ import annotations

import random
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
    for edge in edges:
        predecessors_by_target.setdefault(edge.target_index, []).append(edge)

    ancestors = {logical_edge_id}
    pending = [selected.source_index]
    while pending:
        target_index = pending.pop()
        for predecessor in predecessors_by_target.get(target_index, ()):
            if predecessor.edge_id not in ancestors:
                ancestors.add(predecessor.edge_id)
                pending.append(predecessor.source_index)

    return tuple(sorted(ancestors | set(task_descendant_closure(logical_edge_id, edges))))
