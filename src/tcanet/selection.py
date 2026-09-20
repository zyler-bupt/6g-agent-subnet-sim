"""Two-stage lexicographic selection over feasible joint decisions.

Paper Sec. III-B: TCANet first retains the decisions with minimum
soft-target violation

    U*_m = argmin_{u in F_m} V_m(u)                       (Eq. 6)

with ``V_m(u)`` the mean normalized violation over soft targets (Eq. 4),
then selects the one with minimum modification scope

    u*_m in argmin_{u in U*_m} R_m(u)                     (Eq. 7)

where ``R_m(u) = |Delta Pi(u)| + |Delta FT(u)| + |Delta Phi(u)|`` counts
changed path records, forwarding entries and supporting-agent bindings
(Eq. 5).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.tcanet.binding import BindingTable, SupportBinding
from src.tcanet.candidates import CandidateAction
from src.tcanet.feasibility import FeasibilityResult, Projection
from src.tcanet.spec import SoftTarget, TaskSpecification
from src.tcanet.subnet import (
    ForwardingEntry,
    PathRecord,
    SubnetState,
    compile_forwarding,
)

_EPS = 1e-9


def soft_violation(
    soft_targets: tuple[SoftTarget, ...],
    projection: Projection,
) -> float:
    """``V_m(u)`` — mean normalized soft-target violation (Eq. 4)."""
    if not soft_targets:
        return 0.0
    metrics = _soft_metrics(projection)
    total = 0.0
    for target in soft_targets:
        value = metrics.get(target.metric, 0.0)
        total += target.violation(value)
    return total / len(soft_targets)


def _soft_metrics(projection: Projection) -> dict[str, float]:
    return {
        "mean_delay_ms": projection.mean_delay_ms,
        "total_effective_demand_mbps": projection.total_effective_demand_mbps,
        "max_resource_utilization": projection.max_resource_utilization,
        "min_delivered_mbps": projection.min_delivered_mbps,
    }


def modification_scope(
    subnet: SubnetState,
    actions: tuple[CandidateAction, ...],
    task: TaskSpecification,
) -> int:
    """``R_m(u) = |Delta Pi| + |Delta FT| + |Delta Phi|`` (Eq. 5)."""
    new_paths = dict(subnet.paths)
    binding_overrides: dict[str, SupportBinding] = {}
    for action in actions:
        if action.is_no_change:
            continue
        if action.action == "REROUTE":
            new_paths[action.target] = PathRecord(
                dep_id=action.target,
                gateway_path=tuple(
                    str(node) for node in action.parameters["gateway_path"]
                ),
            )
        elif action.action == "REBIND_SUPPORT":
            base = binding_overrides.get(
                action.target, subnet.bindings.binding(action.target)
            )
            role = str(action.parameters["role"])
            agent_id = str(action.parameters["agent_id"])
            binding_overrides[action.target] = SupportBinding(
                dep_id=action.target,
                t_agent_id=agent_id if role == "t" else base.t_agent_id,
                n_agent_id=agent_id if role == "n" else base.n_agent_id,
                p_agent_id=agent_id if role == "p" else base.p_agent_id,
            )

    changed_paths = {
        dep_id
        for dep_id in set(new_paths) | set(subnet.paths)
        if _path_signature(new_paths.get(dep_id))
        != _path_signature(subnet.paths.get(dep_id))
    }
    candidate_table = compile_forwarding(task.dag, new_paths)
    changed_forwarding = _diff_forwarding(subnet.forwarding, candidate_table)

    new_bindings = BindingTable(
        records={**subnet.bindings.records, **binding_overrides}
    )
    changed_bindings = subnet.bindings.diff(new_bindings)
    return len(changed_paths) + len(changed_forwarding) + len(changed_bindings)


def _path_signature(record: PathRecord | None) -> tuple[str, ...]:
    return record.gateway_path if record is not None else ()


def _diff_forwarding(
    current: dict[str, ForwardingEntry],
    candidate: dict[str, ForwardingEntry],
) -> set[str]:
    changed: set[str] = set()
    for rule_id in set(current) | set(candidate):
        if current.get(rule_id) != candidate.get(rule_id):
            changed.add(rule_id)
    return changed


@dataclass(frozen=True)
class CandidateEvaluation:
    actions: tuple[CandidateAction, ...]
    feasibility: FeasibilityResult
    soft_violation: float
    modification_scope: int

    @property
    def label(self) -> str:
        return " + ".join(
            action.action_id for action in self.actions if not action.is_no_change
        ) or "no-change"


@dataclass(frozen=True)
class SelectionTrace:
    """Stage-by-stage record of the lexicographic selection (for the demo)."""

    evaluations: tuple[CandidateEvaluation, ...]
    stage1_min_v: float
    stage1_survivors: tuple[CandidateEvaluation, ...]
    stage2_min_r: int
    selected: CandidateEvaluation | None
    tiebreak_note: str = ""


def two_stage_select(
    evaluations: tuple[CandidateEvaluation, ...],
) -> SelectionTrace:
    """Lexicographic ``(V, R)`` selection over feasible decisions (Eq. 6-7)."""
    feasible = tuple(item for item in evaluations if item.feasibility.feasible)
    if not feasible:
        return SelectionTrace(
            evaluations=evaluations,
            stage1_min_v=float("inf"),
            stage1_survivors=(),
            stage2_min_r=0,
            selected=None,
        )
    min_v = min(item.soft_violation for item in feasible)
    survivors = tuple(item for item in feasible if item.soft_violation <= min_v + _EPS)
    min_r = min(item.modification_scope for item in survivors)
    best = tuple(item for item in survivors if item.modification_scope == min_r)
    # Deterministic tiebreak: fewest non-no-change actions first (minimal
    # intervention), then the sorted action ids.
    best = tuple(
        sorted(
            best,
            key=lambda item: (
                sum(1 for action in item.actions if not action.is_no_change),
                tuple(action.action_id for action in item.actions),
            ),
        )
    )
    selected = best[0]
    note = ""
    if len(best) > 1:
        note = (
            f"{len(best)} decisions tie on (V, R); fewest changed layers, "
            "then stable id order decides"
        )
    return SelectionTrace(
        evaluations=evaluations,
        stage1_min_v=min_v,
        stage1_survivors=survivors,
        stage2_min_r=min_r,
        selected=selected,
        tiebreak_note=note,
    )
