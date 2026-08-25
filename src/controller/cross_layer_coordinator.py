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
    project_cross_layer_state,
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
    "weighted_sum": "weighted_sum",
    "weighted_sum_multi_objective": "weighted_sum",
    "sanet_dw": "sanet_dw",
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
        *,
        max_combinations: int | None = None,
        coordination_timeout_ms: float | None = None,
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
            result = self._proposed(
                state,
                proposals,
                normalized,
                max_combinations=max_combinations,
                coordination_timeout_ms=coordination_timeout_ms,
            )
        elif normalized == "independent":
            result = self._independent(state, proposals, normalized)
        elif normalized == "adjacent":
            result = self._adjacent(state, proposals, normalized)
        elif normalized == "no_verification":
            result = self._no_verification(state, proposals, normalized)
        elif normalized == "weighted_sum":
            result = self._weighted_sum(state, proposals, normalized)
        elif normalized == "sanet_dw":
            result = self._sanet_dynamic_weight(state, proposals, normalized)
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
        *,
        max_combinations: int | None = None,
        coordination_timeout_ms: float | None = None,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals)
        group_values = tuple(groups[key] for key in sorted(groups))
        total_combinations = 1
        for values in group_values:
            total_combinations *= len(values)
        budget = total_combinations if max_combinations is None else max(0, int(max_combinations))
        deadline = (
            None
            if coordination_timeout_ms is None
            else self.clock() + max(0.0, coordination_timeout_ms) / 1000.0
        )
        common_objective = _uses_common_objective(state)
        local_best = (
            _local_objective_best(state, groups)
            if common_objective
            else _local_best(groups)
        )
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
        evaluated_combinations = 0
        search_timed_out = False
        for raw_combination in product(*group_values):
            if evaluated_combinations >= budget:
                break
            if deadline is not None and self.clock() >= deadline:
                search_timed_out = True
                break
            evaluated_combinations += 1
            combination = tuple(raw_combination)
            result = evaluate_cross_layer_combination(state, combination)
            ids = tuple(item.proposal_id for item in combination)
            if result.feasible:
                feasible.append(
                    (
                        _declared_joint_objective(state, combination)
                        if common_objective
                        else _proposed_policy_objective(result, combination),
                        ids,
                        combination,
                        result,
                    )
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
            best_objective = min(item[0] for item in feasible)
            tied = [item for item in feasible if item[0] == best_objective]
            tie_selector = min if common_objective else max
            _objective, _ids, selected, selected_result = tie_selector(
                tied, key=lambda item: item[1]
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
            candidate_combinations=evaluated_combinations,
            rejected_combinations=rejected_combinations,
            selected_feasibility=selected_result,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=max(0.0, feasibility_ms),
            details={
                "selection_policy": (
                    "global_hard_feasibility_common_objective"
                    if common_objective
                    else "exact_feasible_search_then_task_policy_lexicographic"
                ),
                "search_type": "exact_feasible-combination search",
                "feasible_combinations": len(feasible),
                "objective": (
                    "normalized_deficit_plus_physical_action_cost"
                    if common_objective
                    else "service_margin_then_change_scope_then_overhead"
                ),
                "tie_break": (
                    "ascending_canonical_proposal_ids"
                    if common_objective
                    else "descending_canonical_proposal_ids"
                ),
                "total_combinations": total_combinations,
                "search_budget_exhausted": evaluated_combinations < total_combinations,
                "search_timed_out": search_timed_out,
            },
        )

    def _independent(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals, require_all_layers=False)
        common_objective = _uses_common_objective(state)
        selected = (
            _local_objective_best(state, groups)
            if common_objective
            else _local_best(groups)
        )
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
            details={
                "selection_policy": (
                    "independent_layer_local_objective"
                    if common_objective
                    else "independent_layer_maximum_utility"
                )
            },
        )

    def _adjacent(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        groups = proposal_groups(proposals, require_all_layers=False)
        common_objective = _uses_common_objective(state)
        if common_objective:
            selected_by_group = {
                key: min(
                    items,
                    key=lambda item: (
                        _local_layer_objective(state, item),
                        item.proposal_id,
                    ),
                )
                for key, items in groups.items()
            }
        else:
            selected_by_group = {
                key: max(items, key=lambda item: (item.utility, item.proposal_id))
                for key, items in groups.items()
            }
        detected_records: list[ConflictRecord] = []
        rejected_combo_count = 0
        pairwise_checks = 0
        feasibility_started = self.clock()
        for edge_id in sorted({key[0] for key in groups}):
            for pair_index, (left_layer, right_layer) in enumerate(
                (
                    ("application", "transport"),
                    ("transport", "network"),
                    ("network", "physical"),
                )
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
                candidate_pairs = (
                    product(groups[left_key], groups[right_key])
                    if pair_index == 0
                    else (
                        (selected_by_group[left_key], right)
                        for right in groups[right_key]
                    )
                )
                for left, right in candidate_pairs:
                    result = _adjacent_pair_result(
                        state,
                        (left, right),
                        left_layer,
                        right_layer,
                    )
                    if result.feasible:
                        alternatives.append(
                            (
                                _adjacent_common_objective((left, right))
                                if common_objective
                                else combination_objective(result, (left, right)),
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
            details={
                "selection_policy": (
                    "adjacent_pair_common_objective"
                    if common_objective
                    else "three_adjacent_pair_checks_only"
                )
            },
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
        # Joint ranking combines the four layers' declared rates, latency,
        # reliability and utility as a *soft* end-to-end score.  It never
        # calls the hard feasibility model.  This keeps the ablation distinct
        # from layer-wise independent maximization while still allowing an
        # unsafe combination to reach the common transaction verifier.
        ranked = sorted(
            combinations,
            key=lambda combination: (
                _declared_joint_objective(state, tuple(combination)),
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
                "selection_policy": (
                    "global_common_objective_without_hard_verification"
                    if _uses_common_objective(state)
                    else "declared_joint_score_without_feasibility"
                ),
                "final_verification_skipped": True,
            },
        )

    def _weighted_sum(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        """Classic weighted-sum multi-objective coordination (soft optimization).

        Each layer picks the proposal that maximizes a weighted sum of its
        objectives.  Like independent optimization it performs *no* task-level
        hard-feasibility check, so at high conflict density it can still emit
        infeasible joint combinations (lower Safe Rejection Rate than Proposed).
        A small bonus for "keep" (feasibility-preserving) actions makes it
        slightly more constraint-aware than pure independent maximization,
        which is why it sits between Independent and SANet/Proposed on the curve.
        """
        groups = proposal_groups(proposals, require_all_layers=False)
        if not groups:
            return _empty_rejection(method, "no_layer_observations")
        layer_weight = {"application": 1.0, "transport": 1.0, "network": 1.0, "physical": 1.0}
        pressure = _observed_pressure(state)

        def _score(proposal: LayerProposal) -> float:
            if _uses_common_objective(state):
                base = -_local_layer_objective(state, proposal)[0]
            else:
                base = proposal.utility * layer_weight.get(proposal.layer, 1.0)
            status_quo = 2.0 * max(0.0, 1.0 - pressure) if proposal.is_keep else 0.0
            return base + status_quo

        selected = tuple(
            max(items, key=lambda item: (_score(item), item.proposal_id))
            for items in groups.values()
        )
        selected_ids = {proposal.proposal_id for proposal in selected}
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals={
                proposal.proposal_id: "lower_weighted_utility"
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
            details={"selection_policy": "weighted_sum_multi_objective"},
        )

    def _sanet_dynamic_weight(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
        method: str,
    ) -> CoordinationResult:
        """SANet-inspired observed-state dynamic-weight soft coordination.

        This adapted baseline deliberately ranks declared actions only.  It
        never invokes the hard feasibility evaluator used by Proposed.
        """
        groups = proposal_groups(proposals, require_all_layers=False)
        if not groups:
            return _empty_rejection(method, "no_layer_observations")
        pressure = _observed_pressure(state)
        weights = {
            "application": 1.0 + max(0.0, pressure - 1.0),
            "transport": 1.0 + sum(item.retransmission_rate for item in state.transport.values()),
            "network": 1.0 + sum(item.utilization for item in state.network.values()) / max(len(state.network), 1),
            "physical": 1.0 + sum(item.resource_utilization for item in state.physical.values()) / max(len(state.physical), 1),
        }

        def score(item: LayerProposal) -> tuple[float, str]:
            benefit = item.expected_qos_gain * item.confidence
            cost = float(item.parameters.get("normalized_action_cost", item.expected_cost))
            stress = pressure - 1.0
            state_adaptation = (
                -4.0 * stress
                if item.is_keep
                else 4.0 * stress * max(0.25, benefit)
            )
            return (
                weights.get(item.layer, 1.0) * benefit - cost + state_adaptation,
                item.proposal_id,
            )

        selected = tuple(max(items, key=score) for _, items in sorted(groups.items()))
        selected_ids = {item.proposal_id for item in selected}
        return CoordinationResult(
            method=method,
            selected_proposals=selected,
            rejected_proposals={
                item.proposal_id: "lower_sanet_dynamic_weight_score"
                for item in proposals if item.proposal_id not in selected_ids
            },
            conflicts=(), conflict_detected=False, conflict_resolved=False,
            global_check_performed=False, pairwise_checks=0,
            candidate_combinations=len(selected), rejected_combinations=0,
            selected_feasibility=None, coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            details={
                "selection_policy": "sanet_inspired_dynamic_weight_soft_objective",
                "weights": weights,
                "adapted": True,
            },
        )


def _local_best(
    groups: dict[tuple[str, str], tuple[LayerProposal, ...]],
) -> tuple[LayerProposal, ...]:
    return tuple(
        max(groups[key], key=lambda item: (item.utility, item.proposal_id))
        for key in sorted(groups)
    )


def _uses_common_objective(state: CrossLayerTaskState) -> bool:
    return state.metadata.get("common_objective_version") == (
        "normalized-deficit-cost-v1"
    )


def _observed_pressure(state: CrossLayerTaskState) -> float:
    demands = [item.required_rate_mbps for item in state.application.values()]
    capacities = [
        min(
            state.transport[key].admissible_capacity_mbps
            if state.transport[key].admissible_capacity_mbps is not None
            else state.transport[key].send_rate_mbps,
            state.network[key].available_bandwidth_mbps,
            state.physical[key].available_capacity_mbps,
        )
        for key in state.application
    ]
    return sum(demands) / max(sum(capacities), 1e-9)


def _local_objective_best(
    state: CrossLayerTaskState,
    groups: dict[tuple[str, str], tuple[LayerProposal, ...]],
) -> tuple[LayerProposal, ...]:
    return tuple(
        min(
            groups[key],
            key=lambda item: (_local_layer_objective(state, item), item.proposal_id),
        )
        for key in sorted(groups)
    )


def _local_layer_objective(
    state: CrossLayerTaskState,
    proposal: LayerProposal,
) -> tuple[float, float, float, float]:
    edge_id = next(iter(proposal.affected_edges))
    parameters = proposal.parameters
    required = max(float(proposal.required_bandwidth_mbps), 1e-9)
    deficit = 0.0
    if proposal.layer == "transport":
        send_rate = float(parameters.get("transport_rate_mbps", required))
        capacity_value = parameters.get(
            "transport_admissible_capacity_mbps",
            state.transport[edge_id].admissible_capacity_mbps,
        )
        capacity = send_rate if capacity_value is None else float(capacity_value)
        deficit = max(0.0, required - min(send_rate, capacity)) / required
    elif proposal.layer == "network":
        capacity = float(
            parameters.get(
                "network_bandwidth_mbps",
                state.network[edge_id].available_bandwidth_mbps,
            )
        )
        deficit = max(0.0, required - capacity) / required
    elif proposal.layer == "physical":
        capacity = float(
            parameters.get(
                "physical_capacity_mbps",
                state.physical[edge_id].available_capacity_mbps,
            )
        )
        deficit = max(0.0, required - capacity) / required
    action_cost = _action_cost((proposal,))
    changed = float(not proposal.is_keep)
    return (4.0 * deficit + action_cost, deficit, action_cost, changed)


def _adjacent_common_objective(
    pair: tuple[LayerProposal, LayerProposal],
) -> tuple[float, float, float, float]:
    action_cost = _action_cost(pair)
    changed = float(sum(not proposal.is_keep for proposal in pair))
    quality_loss = sum(
        float(proposal.parameters.get("quality_loss_fraction", 0.0))
        for proposal in pair
    )
    reserved = sum(
        float(proposal.parameters.get("reserved_increment_fraction", 0.0))
        for proposal in pair
    )
    return (action_cost, changed, quality_loss, reserved)


def _action_cost(proposals: tuple[LayerProposal, ...]) -> float:
    return sum(
        float(
            proposal.parameters.get(
                "normalized_action_cost", proposal.expected_cost
            )
        )
        for proposal in proposals
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


def _declared_joint_objective(
    state: CrossLayerTaskState,
    combination: tuple[LayerProposal, ...],
) -> tuple[float, float, float, float]:
    """Score declared cross-layer effects without enforcing hard bounds.

    This is intentionally not a feasibility check: no combination is removed
    and no boolean hard constraint is evaluated.  It is a continuous, fallible
    prediction based on the values declared by the four layer Agents.  The
    stable-state verifier remains the first component that can reject the
    selected configuration for a hard violation.
    """

    if _uses_common_objective(state):
        return _normalized_common_objective(state, combination)

    by_edge_layer = {
        (edge_id, proposal.layer): proposal
        for proposal in combination
        for edge_id in proposal.affected_edges
    }
    service_penalty = 0.0
    resource_penalty = 0.0
    for edge_id, constraint in state.constraints.items():
        application = state.application[edge_id]
        transport = state.transport[edge_id]
        network = state.network[edge_id]
        physical = state.physical[edge_id]
        app_proposal = by_edge_layer.get((edge_id, "application"))
        transport_proposal = by_edge_layer.get((edge_id, "transport"))
        network_proposal = by_edge_layer.get((edge_id, "network"))
        physical_proposal = by_edge_layer.get((edge_id, "physical"))
        application_rate = float(
            (app_proposal.parameters if app_proposal else {}).get(
                "application_rate_mbps", application.required_rate_mbps
            )
        )
        transport_rate = float(
            (transport_proposal.parameters if transport_proposal else {}).get(
                "transport_rate_mbps", transport.send_rate_mbps
            )
        )
        transport_capacity_value = (
            (transport_proposal.parameters if transport_proposal else {}).get(
                "transport_admissible_capacity_mbps",
                transport.admissible_capacity_mbps,
            )
        )
        transport_capacity = (
            transport_rate
            if transport_capacity_value is None
            else float(transport_capacity_value)
        )
        network_bandwidth = float(
            (network_proposal.parameters if network_proposal else {}).get(
                "network_bandwidth_mbps", network.available_bandwidth_mbps
            )
        )
        physical_capacity = float(
            (physical_proposal.parameters if physical_proposal else {}).get(
                "physical_capacity_mbps", physical.available_capacity_mbps
            )
        )
        scale = max(application_rate, constraint.desired_rate_mbps, 1e-9)
        bottleneck = min(
            transport_rate,
            transport_capacity,
            network_bandwidth,
            physical_capacity,
        )
        service_penalty += max(0.0, application_rate - bottleneck) / scale
        # Declared service above any independently admissible capacity is a
        # soft resource risk. Capacities are not ordered relative to each other.
        resource_penalty += max(0.0, transport_rate - transport_capacity) / scale
        resource_penalty += max(0.0, transport_rate - network_bandwidth) / scale
        resource_penalty += max(0.0, transport_rate - physical_capacity) / scale

        network_parameters = network_proposal.parameters if network_proposal else {}
        physical_parameters = physical_proposal.parameters if physical_proposal else {}
        transport_parameters = transport_proposal.parameters if transport_proposal else {}
        predicted_latency = (
            float(transport_parameters.get("transport_rtt_ms", transport.rtt_ms))
            + float(network_parameters.get("network_latency_ms", network.latency_ms))
            + float(physical_parameters.get("access_latency_ms", physical.access_latency_ms))
        )
        service_penalty += max(
            0.0,
            predicted_latency - constraint.max_latency_ms,
        ) / max(constraint.max_latency_ms, 1e-9)
        reliability = (
            float(transport_parameters.get("transport_reliability", transport.reliability))
            * float(network_parameters.get("network_reliability", network.reliability))
            * float(physical_parameters.get("physical_reliability", physical.reliability))
        )
        service_penalty += max(
            0.0,
            constraint.min_reliability - reliability,
        ) / max(constraint.min_reliability, 1e-9)

    shared_demand = {
        resource_id: float(
            state.metadata.get("shared_resource_background_demand_mbps", {}).get(
                resource_id, 0.0
            )
        )
        for resource_id in state.shared_resource_capacity_mbps
    }
    for edge_id, constraint in state.constraints.items():
        proposal = by_edge_layer.get((edge_id, "application"))
        rate = float(
            (proposal.parameters if proposal else {}).get(
                "application_rate_mbps",
                state.application[edge_id].required_rate_mbps,
            )
        )
        shared_demand[constraint.shared_resource_id] = (
            shared_demand.get(constraint.shared_resource_id, 0.0) + rate
        )
    for resource_id, demand in shared_demand.items():
        capacity = state.shared_resource_capacity_mbps.get(resource_id, 0.0)
        resource_penalty += max(0.0, demand - capacity) / max(capacity, 1e-9)

    declared_utility = sum(proposal.utility for proposal in combination)
    declared_cost = sum(proposal.expected_cost for proposal in combination)
    changed = float(sum(not proposal.is_keep for proposal in combination))
    interaction_risk = float(_declared_interaction_pairs(combination))
    return (
        20.0 * service_penalty
        + 8.0 * resource_penalty
        + 0.50 * interaction_risk
        - declared_utility,
        changed,
        declared_cost,
        -declared_utility,
    )


def _normalized_common_objective(
    state: CrossLayerTaskState,
    combination: tuple[LayerProposal, ...],
) -> tuple[float, float, float, float]:
    """Dimensionless soft objective shared by all v3 selection scopes.

    The global-verification ablation may trade a small predicted deficit for a
    lower physical action cost, but it never receives method-specific proposal
    values. Ours applies the same ranking only after the hard feasibility
    filter. Local and adjacent baselines use the same deficit/cost components
    restricted to the information they are allowed to observe.
    """

    projected = project_cross_layer_state(state, combination)
    penalty = 0.0
    shared_demand = {
        resource_id: float(
            projected.metadata.get(
                "shared_resource_background_demand_mbps", {}
            ).get(resource_id, 0.0)
        )
        for resource_id in projected.shared_resource_capacity_mbps
    }
    route_requirements = projected.metadata.get("route_access_requirements", {})
    for edge_id, constraint in projected.constraints.items():
        application = projected.application[edge_id]
        transport = projected.transport[edge_id]
        network = projected.network[edge_id]
        physical = projected.physical[edge_id]
        transport_capacity = (
            transport.send_rate_mbps
            if transport.admissible_capacity_mbps is None
            else transport.admissible_capacity_mbps
        )
        scale = max(application.required_rate_mbps, 1e-9)
        supported = min(
            transport.send_rate_mbps,
            transport_capacity,
            network.available_bandwidth_mbps,
            physical.available_capacity_mbps,
        )
        penalty += max(0.0, application.required_rate_mbps - supported) / scale
        penalty += max(
            0.0,
            constraint.desired_rate_mbps - application.required_rate_mbps,
        ) / max(constraint.desired_rate_mbps, 1e-9)

        required_access = route_requirements.get(edge_id, {}).get(
            network.selected_route
        )
        if required_access is not None and physical.access_id != required_access:
            penalty += 1.0
        if not network.reachable or not physical.online:
            penalty += 1.0

        shared_demand[constraint.shared_resource_id] = (
            shared_demand.get(constraint.shared_resource_id, 0.0)
            + application.required_rate_mbps
        )

    for resource_id, demand in shared_demand.items():
        capacity = projected.shared_resource_capacity_mbps.get(resource_id, 0.0)
        penalty += max(0.0, demand - capacity) / max(capacity, 1e-9)

    action_cost = _action_cost(combination)
    changed = float(sum(not proposal.is_keep for proposal in combination))
    return (4.0 * penalty + action_cost, penalty, action_cost, changed)


def _adjacent_pair_result(
    state: CrossLayerTaskState,
    pair: tuple[LayerProposal, LayerProposal],
    left_layer: str,
    right_layer: str,
) -> CrossLayerFeasibilityResult:
    result = evaluate_cross_layer_combination(state, pair)
    edge_ids = set(pair[0].affected_edges) & set(pair[1].affected_edges)
    if len(edge_ids) != 1:
        raise ValueError("adjacent pair must affect exactly one common edge")
    edge_id = next(iter(edge_ids))
    prefixes = {
        ("application", "transport"): (
            "application_transport_rate",
            "application_transport_capacity",
            "transport_admissible_capacity",
            "application_quality_qos",
            "write_set",
        ),
        ("transport", "network"): (
            "application_network_capacity",
            "transport_network_rate",
            "write_set",
        ),
        ("network", "physical"): (
            "application_physical_capacity",
            "transport_physical_capacity",
            "network_physical_capacity",
            "network_physical_access",
            "unreachable",
            "latency_qos",
            "reliability_qos",
            "loss_qos",
            "write_set",
        ),
    }[(left_layer, right_layer)]
    local_violations = tuple(
        violation
        for violation in result.violations
        if violation.startswith("write_set:")
        or (violation.endswith(f":{edge_id}") and violation.startswith(prefixes))
    )
    return replace(
        result,
        feasible=not local_violations,
        violations=local_violations,
    )
