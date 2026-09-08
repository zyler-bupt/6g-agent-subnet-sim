from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Callable, Iterable

from src.core.cross_layer import LayerProposal


class ConflictType(str, Enum):
    APPLICATION_CAPACITY = "APPLICATION_CAPACITY"
    TRANSPORT_NETWORK = "TRANSPORT_NETWORK"
    NETWORK_PHYSICAL = "NETWORK_PHYSICAL"
    SHARED_RESOURCE = "SHARED_RESOURCE"
    WRITE_SET = "WRITE_SET"
    QOS_INFEASIBLE = "QOS_INFEASIBLE"
    STALE_STATE = "STALE_STATE"


@dataclass(frozen=True)
class ConflictRecord:
    conflict_id: str
    conflict_type: str
    involved_proposals: tuple[str, ...]
    involved_layers: frozenset[str]
    affected_edges: frozenset[str]
    detected_at: float
    resolved: bool = False
    resolution: str | None = None
    details: dict[str, object] = field(default_factory=dict)


def detect_write_set_conflicts(
    proposals: Iterable[LayerProposal],
    *,
    clock: Callable[[], float] = perf_counter,
) -> tuple[ConflictRecord, ...]:
    selected = tuple(proposals)
    records: list[ConflictRecord] = []
    for left_index, left in enumerate(selected):
        for right in selected[left_index + 1 :]:
            overlap = left.write_set & right.write_set
            if not overlap or _writes_compatible(left, right, overlap):
                continue
            records.append(
                ConflictRecord(
                    conflict_id=f"write:{left.proposal_id}:{right.proposal_id}",
                    conflict_type=ConflictType.WRITE_SET.value,
                    involved_proposals=(left.proposal_id, right.proposal_id),
                    involved_layers=frozenset({left.layer, right.layer}),
                    affected_edges=left.affected_edges | right.affected_edges,
                    detected_at=clock(),
                    details={"overlap": sorted(overlap)},
                )
            )
    return tuple(records)


def conflicts_from_violations(
    proposals: Iterable[LayerProposal],
    violations: Iterable[str],
    *,
    clock: Callable[[], float] = perf_counter,
) -> tuple[ConflictRecord, ...]:
    selected = tuple(proposals)
    proposal_ids = tuple(proposal.proposal_id for proposal in selected)
    layers = frozenset(proposal.layer for proposal in selected)
    edges = frozenset(
        edge_id for proposal in selected for edge_id in proposal.affected_edges
    )
    records = []
    for index, violation in enumerate(dict.fromkeys(violations), start=1):
        conflict_type = _violation_type(violation)
        records.append(
            ConflictRecord(
                conflict_id=f"constraint:{index}:{conflict_type.value}",
                conflict_type=conflict_type.value,
                involved_proposals=proposal_ids,
                involved_layers=layers,
                affected_edges=edges,
                detected_at=clock(),
                details={"violation": violation},
            )
        )
    return tuple(records)


def _writes_compatible(
    left: LayerProposal,
    right: LayerProposal,
    overlap: frozenset[str],
) -> bool:
    if left.is_keep or right.is_keep:
        return True
    left_values = left.parameters.get("write_values", {})
    right_values = right.parameters.get("write_values", {})
    return all(
        key in left_values
        and key in right_values
        and left_values[key] == right_values[key]
        for key in overlap
    )


def _violation_type(violation: str) -> ConflictType:
    if violation.startswith("application_transport_rate"):
        return ConflictType.APPLICATION_CAPACITY
    if violation.startswith("application_capacity"):
        return ConflictType.APPLICATION_CAPACITY
    if violation.startswith("transport_network_rate"):
        return ConflictType.TRANSPORT_NETWORK
    if violation.startswith("network_physical_capacity"):
        return ConflictType.NETWORK_PHYSICAL
    if violation.startswith("network_physical_access"):
        return ConflictType.NETWORK_PHYSICAL
    if violation.startswith("shared_resource"):
        return ConflictType.SHARED_RESOURCE
    if violation.startswith("stale_state"):
        return ConflictType.STALE_STATE
    return ConflictType.QOS_INFEASIBLE
