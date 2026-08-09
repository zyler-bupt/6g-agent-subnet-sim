from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product
from time import perf_counter
from typing import Callable

from src.controller.conflicts import ConflictRecord, conflicts_from_violations
from src.controller.feasibility import (
    CrossLayerFeasibilityResult,
    combination_objective,
    evaluate_cross_layer_combination,
)
from src.controller.ground_truth import proposal_groups
from src.core.cross_layer import CrossLayerTaskState, LayerProposal


METHOD_ALIASES = {
    "proposed": "proposed",
    "proposed_four_layer": "proposed",
    "independent": "independent",
    "independent_layer": "independent",
    "adjacent": "adjacent",
    "adjacent_layer": "adjacent",
    "no_verification": "no_verification",
    "without_cross_layer_verification": "no_verification",
    "without_application": "without_application",
    "without_transport": "without_transport",
    "without_network": "without_network",
    "without_physical": "without_physical",
}


@dataclass(frozen=True)
class CoordinationResult:
    method: str
    selected_proposals: tuple[LayerProposal, ...]
    rejected_proposals: dict[str, str]
    conflicts: tuple[ConflictRecord, ...]
    conflict_detected: bool
    conflict_resolved: bool
    global_check_performed: bool
    pairwise_checks: int
    candidate_combinations: int
    rejected_combinations: int
    selected_feasibility: CrossLayerFeasibilityResult | None
    coordination_latency_ms: float
    feasibility_latency_ms: float
    details: dict[str, object] = field(default_factory=dict)


class CrossLayerCoordinator:
    """Select candidates without executing them or mutating stable state."""

    def __init__(self, *, clock: Callable[[], float] = perf_counter) -> None:
        self.clock = clock

    def coordinate(
        self,
        method: str,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
    ) -> CoordinationResult:
        normalized = METHOD_ALIASES.get(method)
        if normalized is None:
            raise ValueError(f"unknown cross-layer method: {method}")
        started = self.clock()
        missing = _missing_proposal_layers(proposals)
        if missing and normalized.startswith("proposed"):
            result = _missing_layer_rejection(normalized, proposals, missing)
            elapsed = (self.clock() - started) * 1000.0
            return replace(result, coordination_latency_ms=max(0.0, elapsed))
        if normalized == "proposed":
            result = self._proposed(state, proposals, normalized)
        elif normalized == "independent":
            result = self._independent(state, proposals, normalized)
        elif normalized == "adjacent":
            result = self._adjacent(state, proposals, normalized)
        elif normalized == "no_verification":
            result = self._no_verification(state, proposals, normalized)
        else:
            excluded = normalized.removeprefix("without_")
            filtered = tuple(
                proposal
                for proposal in proposals
                if proposal.layer != excluded or proposal.is_keep
            )
            result = self._proposed(state, filtered, normalized)
            result = replace(
                result,
                details={**result.details, "excluded_layer": excluded},
            )
        elapsed = (self.clock() - started) * 1000.0
        return replace(result, coordination_latency_ms=max(0.0, elapsed))

    def _proposed(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals)
        combinations = tuple(product(*(groups[key] for key in sorted(groups))))
        local_best = _local_best(groups)
        feasibility_started = self.clock()
        local_result = evaluate_cross_layer_combination(state, local_best)
        detected = not local_result.feasible
        conflicts = (
            conflicts_from_violations(local_best, local_result.violations)
            if detected
            else ()
        )
        feasible: list[
            tuple[
                tuple[float, float, float, float],
                tuple[str, ...],
                tuple[LayerProposal, ...],
                CrossLayerFeasibilityResult,
            ]
        ] = []
        rejected_combinations = 0
        reasons_by_proposal: dict[str, set[str]] = {}
        for raw_combination in combinations:
            combination = tuple(raw_combination)
            result = evaluate_cross_layer_combination(state, combination)
            ids = tuple(item.proposal_id for item in combination)
            if result.feasible:
                feasible.append(
                    (_proposed_policy_objective(result, combination), ids, combination, result)
                )
            else:
                rejected_combinations += 1
                reason = ";".join(result.violations)
                for proposal in combination:
                    reasons_by_proposal.setdefault(proposal.proposal_id, set()).add(reason)
        feasibility_ms = (self.clock() - feasibility_started) * 1000.0
        selected: tuple[LayerProposal, ...] = ()
        selected_result: CrossLayerFeasibilityResult | None = None
        if feasible:
            # The policy tie-break deliberately differs from the Ground Truth
            # oracle.  The oracle minimizes its scientific reference objective
            # and then ascending proposal ids; the Controller prioritizes the
            # task policy tuple and uses descending canonical ids only for
            # otherwise complete ties.
            best_objective = min(item[0] for item in feasible)
            tied = [item for item in feasible if item[0] == best_objective]
            _objective, _ids, selected, selected_result = max(
                tied,
                key=lambda item: item[1],
            )
        selected_ids = {proposal.proposal_id for proposal in selected}
        rejected = _rejected_map(
            proposals,
            selected_ids,
            reasons_by_proposal,
            default="feasible_but_not_minimum_cost",
        )
        resolved = bool(detected and selected_result is not None and selected_result.feasible)
        if resolved:
            conflicts = tuple(
                replace(
                    record,
                    resolved=True,
                    resolution="selected_minimum_cost_feasible_combination",
                )
                for record in conflicts
            )
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals=rejected,
            conflicts=conflicts,
            conflict_detected=detected,
            conflict_resolved=resolved,
            global_check_performed=True,
            pairwise_checks=0,
            candidate_combinations=len(combinations),
            rejected_combinations=rejected_combinations,
            selected_feasibility=selected_result,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=max(0.0, feasibility_ms),
            details={
                "selection_policy": "exact_feasible_search_then_task_policy_lexicographic",
                "search_type": "exact_feasible-combination search",
                "feasible_combinations": len(feasible),
                "objective": "service_margin_then_change_scope_then_overhead",
                "tie_break": "descending_canonical_proposal_ids",
            },
        )

    def _independent(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals, require_all_layers=False)
        selected = _local_best(groups)
        selected_ids = {proposal.proposal_id for proposal in selected}
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals={
                proposal.proposal_id: "lower_layer_local_utility"
                for proposal in proposals
                if proposal.proposal_id not in selected_ids
            },
            conflicts=(),
            conflict_detected=False,
            conflict_resolved=False,
            global_check_performed=False,
            pairwise_checks=0,
            candidate_combinations=len(selected),
            rejected_combinations=0,
            selected_feasibility=None,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            details={"selection_policy": "independent_layer_maximum_utility"},
        )

    def _adjacent(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals, require_all_layers=False)
        selected_by_group = {
            key: max(items, key=lambda item: (item.utility, item.proposal_id))
            for key, items in groups.items()
        }
        detected_records: list[ConflictRecord] = []
        rejected_combo_count = 0
        pairwise_checks = 0
        feasibility_started = self.clock()
        for edge_id in sorted({key[0] for key in groups}):
            for left_layer, right_layer in (
                ("application", "transport"),
                ("transport", "network"),
                ("network", "physical"),
            ):
                left_key = (edge_id, left_layer)
                right_key = (edge_id, right_layer)
                pairwise_checks += 1
                if left_key not in selected_by_group or right_key not in selected_by_group:
                    continue
                current_pair = (
                    selected_by_group[left_key],
                    selected_by_group[right_key],
                )
                current_result = _adjacent_pair_result(
                    state,
                    current_pair,
                    left_layer,
                    right_layer,
                )
                if current_result.feasible:
                    continue
                detected_records.extend(
                    conflicts_from_violations(current_pair, current_result.violations)
                )
                alternatives = []
                for left, right in product(groups[left_key], groups[right_key]):
                    result = _adjacent_pair_result(
                        state,
                        (left, right),
                        left_layer,
                        right_layer,
                    )
                    if result.feasible:
                        alternatives.append(
                            (
                                combination_objective(result, (left, right)),
                                (left.proposal_id, right.proposal_id),
                                left,
                                right,
                            )
                        )
                    else:
                        rejected_combo_count += 1
                if alternatives:
                    _score, _ids, left, right = min(
                        alternatives,
                        key=lambda item: (item[0], item[1]),
                    )
                    selected_by_group[left_key] = left
                    selected_by_group[right_key] = right
        feasibility_ms = (self.clock() - feasibility_started) * 1000.0
        selected = tuple(selected_by_group[key] for key in sorted(selected_by_group))
        selected_ids = {proposal.proposal_id for proposal in selected}
        detected = bool(detected_records)
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals={
                proposal.proposal_id: "not_selected_by_adjacent_pair_coordination"
                for proposal in proposals
                if proposal.proposal_id not in selected_ids
            },
            conflicts=tuple(detected_records),
            conflict_detected=detected,
            # No global verification means pairwise success is not a proven
            # task-level resolution.
            conflict_resolved=False,
            global_check_performed=False,
            pairwise_checks=pairwise_checks,
            candidate_combinations=pairwise_checks,
            rejected_combinations=rejected_combo_count,
            selected_feasibility=None,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=max(0.0, feasibility_ms),
            details={"selection_policy": "three_adjacent_pair_checks_only"},
        )

    def _no_verification(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals, require_all_layers=False)
        if not groups:
            return _empty_rejection(method, "no_layer_observations")
        combinations = tuple(product(*(groups[key] for key in sorted(groups))))
        # Joint ranking uses only Agent-declared gain/cost.  It deliberately
        # does not call the feasibility model before execution.
        ranked = sorted(
            combinations,
            key=lambda combination: (
                -sum(item.utility for item in combination)
                + 0.5 * _declared_interaction_pairs(combination),
                sum(0 if item.is_keep else 1 for item in combination),
                tuple(item.proposal_id for item in combination),
            ),
        )
        selected = tuple(ranked[0])
        selected_ids = {proposal.proposal_id for proposal in selected}
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals={
                proposal.proposal_id: "lower_declared_joint_score"
                for proposal in proposals
                if proposal.proposal_id not in selected_ids
            },
            conflicts=(),
            conflict_detected=False,
            conflict_resolved=False,
            global_check_performed=False,
            pairwise_checks=0,
            candidate_combinations=len(combinations),
            rejected_combinations=0,
            selected_feasibility=None,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            details={
                "selection_policy": "declared_joint_score_without_feasibility",
                "final_verification_skipped": True,
            },
        )


def _local_best(
    groups: dict[tuple[str, str], tuple[LayerProposal, ...]],
) -> tuple[LayerProposal, ...]:
    return tuple(
        max(groups[key], key=lambda item: (item.utility, item.proposal_id))
        for key in sorted(groups)
    )


def _proposed_policy_objective(
    feasibility: CrossLayerFeasibilityResult,
    proposals: tuple[LayerProposal, ...],
) -> tuple[float, float, float, float]:
    """Controller policy objective, intentionally separate from the oracle.

    Feasibility is a hard filter.  Among feasible combinations the Controller
    first maximizes the minimum normalized bottleneck margin, then minimizes
    changed actions and declared execution cost, and finally maximizes declared
    benefit.  GroundTruthSolver continues to use ``combination_objective``.
    """

    margins = []
    for metric in feasibility.edge_metrics.values():
        bottleneck = min(
            metric.transport_rate_mbps,
            metric.network_bandwidth_mbps,
            metric.physical_capacity_mbps,
        )
        margins.append(
            (bottleneck - metric.application_rate_mbps)
            / max(metric.application_rate_mbps, 1e-9)
        )
    minimum_margin = min(margins, default=0.0)
    changed = float(sum(not proposal.is_keep for proposal in proposals))
    overhead = sum(proposal.expected_cost for proposal in proposals)
    benefit = sum(
        proposal.expected_qos_gain * proposal.confidence
        for proposal in proposals
    )
    return (-minimum_margin, changed, overhead, -benefit)


def _missing_proposal_layers(
    proposals: tuple[LayerProposal, ...],
) -> dict[str, tuple[str, ...]]:
    required = {"application", "transport", "network", "physical"}
    present: dict[str, set[str]] = {}
    for proposal in proposals:
        for edge_id in proposal.affected_edges:
            present.setdefault(edge_id, set()).add(proposal.layer)
    return {
        edge_id: tuple(sorted(required - layers))
        for edge_id, layers in present.items()
        if layers != required
    }


def _missing_layer_rejection(
    method: str,
    proposals: tuple[LayerProposal, ...],
    missing: dict[str, tuple[str, ...]],
) -> CoordinationResult:
    return CoordinationResult(
        method=method,
        selected_proposals=(),
        rejected_proposals={
            proposal.proposal_id: "missing_layer_observation"
            for proposal in proposals
        },
        conflicts=(),
        conflict_detected=True,
        conflict_resolved=False,
        global_check_performed=True,
        pairwise_checks=0,
        candidate_combinations=0,
        rejected_combinations=0,
        selected_feasibility=None,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        details={
            "selection_policy": "safe_rejection_on_missing_layer_observation",
            "safe_rejection": True,
            "missing_layers": missing,
        },
    )


def _empty_rejection(method: str, reason: str) -> CoordinationResult:
    return CoordinationResult(
        method=method,
        selected_proposals=(),
        rejected_proposals={},
        conflicts=(),
        conflict_detected=False,
        conflict_resolved=False,
        global_check_performed=False,
        pairwise_checks=0,
        candidate_combinations=0,
        rejected_combinations=0,
        selected_feasibility=None,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        details={"selection_policy": "empty_rejection", "reason": reason},
    )


def _rejected_map(
    proposals: tuple[LayerProposal, ...],
    selected_ids: set[str],
    reasons: dict[str, set[str]],
    *,
    default: str,
) -> dict[str, str]:
    output = {}
    for proposal in proposals:
        if proposal.proposal_id in selected_ids:
            continue
        proposal_reasons = sorted(reasons.get(proposal.proposal_id, ()))
        output[proposal.proposal_id] = (
            proposal_reasons[0] if proposal_reasons else default
        )
    return output


def _declared_interaction_pairs(
    combination: tuple[LayerProposal, ...],
) -> int:
    changed = tuple(item for item in combination if not item.is_keep)
    return sum(
        bool(left.affected_edges & right.affected_edges)
        for index, left in enumerate(changed)
        for right in changed[index + 1 :]
    )


def _adjacent_pair_result(
    state: CrossLayerTaskState,
    pair: tuple[LayerProposal, LayerProposal],
    left_layer: str,
    right_layer: str,
) -> CrossLayerFeasibilityResult:
    result = evaluate_cross_layer_combination(state, pair)
    prefixes = {
        ("application", "transport"): (
            "application_transport_rate",
            "application_quality_qos",
            "write_set",
        ),
        ("transport", "network"): (
            "transport_network_rate",
            "latency_qos",
            "write_set",
        ),
        ("network", "physical"): (
            "network_physical_capacity",
            "network_physical_access",
            "unreachable",
            "reliability_qos",
            "loss_qos",
            "write_set",
        ),
    }[(left_layer, right_layer)]
    local_violations = tuple(
        violation
        for violation in result.violations
        if violation.startswith(prefixes)
    )
    return replace(
        result,
        feasible=not local_violations,
        violations=local_violations,
    )
