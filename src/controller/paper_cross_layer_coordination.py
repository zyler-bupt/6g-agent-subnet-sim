from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from src.controller import feasibility as feasibility_model
from src.controller.feasibility import (
    combination_objective,
    evaluate_cross_layer_combination,
    project_cross_layer_state,
)
from src.core.cross_layer import CrossLayerTaskState, LayerProposal
from src.simulation.paper_conflicts import PaperConflictSnapshot


_LAYERS = ("application", "transport", "network", "physical")


@dataclass(frozen=True)
class PaperCoordinationOutcome:
    method_id: str
    policy: str
    selected_proposals: tuple[LayerProposal, ...]
    proposal_fingerprint: str
    layer_weights: dict[str, float]
    hard_arbitration_performed: bool
    candidate_combinations: int
    success: bool
    qos_satisfied: bool
    safe_rejection: bool
    resolution_latency_ms: float
    failure_reason: str
    rollback_count: int
    control_messages: int
    stale_state_detected: bool


def coordinate_paper(
    method_id: str,
    snapshot: PaperConflictSnapshot,
) -> PaperCoordinationOutcome:
    """Coordinate one immutable shared action pool, then run one verifier.

    SANet-DW*, Adjacent-Layer, and Independent never call the hard-feasibility
    evaluator while selecting.  Their chosen combinations are nevertheless
    passed to the same final verifier used by Proposed.
    """

    groups = _layer_groups(snapshot.proposals)
    combinations = tuple(product(*(groups[layer] for layer in _LAYERS)))
    weights: dict[str, float] = {}
    hard_arbitration = False
    evaluated_count = 0
    if method_id == "proposed":
        hard_arbitration = True
        feasible = []
        for raw in combinations:
            combination = tuple(raw)
            result = evaluate_cross_layer_combination(snapshot.state, combination)
            evaluated_count += 1
            if result.feasible:
                feasible.append(
                    (
                        combination_objective(result, combination),
                        tuple(item.proposal_id for item in combination),
                        combination,
                    )
                )
        selected = (
            min(feasible, key=lambda item: (item[0], item[1]))[2]
            if feasible
            else ()
        )
        policy = "explicit_global_hard_feasibility_arbitration"
    elif method_id == "sanet_dw":
        weights = dynamic_layer_weights(snapshot.state)
        selected = min(
            combinations,
            key=lambda combination: (
                _sanet_score(snapshot.state, tuple(combination), weights),
                tuple(item.proposal_id for item in combination),
            ),
        )
        evaluated_count = len(combinations)
        policy = "sanet_dynamic_weight_global_objective"
    elif method_id == "adjacent_layer":
        selected, evaluated_count = _adjacent_selection(snapshot.state, groups)
        policy = "adjacent_pairs_a_t_t_n_n_p"
    elif method_id == "independent":
        selected = tuple(
            min(
                groups[layer],
                key=lambda proposal: (
                    _independent_score(snapshot.state, proposal),
                    proposal.proposal_id,
                ),
            )
            for layer in _LAYERS
        )
        evaluated_count = sum(len(items) for items in groups.values())
        policy = "independent_layer_local_objectives"
    else:
        raise ValueError(f"unsupported paper Exp.2 method: {method_id}")

    if not selected:
        verification = None
        success = False
        qos_satisfied = False
        safe_rejection = hard_arbitration
        failure_reason = "no feasible action combination"
        rollback_count = 0
    else:
        # This final transaction verifier is common to all methods.  It is not
        # SANet-DW*'s selection-time hard-feasibility arbitration.
        verification = feasibility_model.evaluate_cross_layer_combination(
            snapshot.state,
            selected,
        )
        success = verification.feasible
        qos_satisfied = verification.feasible and all(
            metric.qos_satisfied for metric in verification.edge_metrics.values()
        )
        # The common transaction verifier runs before commit.  Rejecting an
        # infeasible selected combination (and rolling back its staged state)
        # is therefore a safe rejection even when the selection policy itself
        # did not perform Proposed's explicit hard-feasibility arbitration.
        safe_rejection = not verification.feasible
        failure_reason = (
            "" if success else ";".join(verification.violations[:6])
        )
        rollback_count = 0 if success else 1

    changed_actions = sum(not item.is_keep for item in selected)
    conflicted_edges = len(snapshot.conflicted_edge_ids)
    selection_unit_ms = {
        "proposed": 0.018,
        "sanet_dw": 0.011,
        "adjacent_layer": 0.010,
        "independent": 0.006,
    }[method_id]
    collection_ms = 0.035 * max(1, conflicted_edges)
    selection_ms = selection_unit_ms * max(1, evaluated_count)
    common_verification_ms = 0.85 + 0.025 * len(snapshot.task.biz_edges)
    transaction_ms = 0.12 * changed_actions
    rollback_ms = 0.65 * rollback_count
    resolution_latency_ms = (
        collection_ms
        + selection_ms
        + common_verification_ms
        + transaction_ms
        + rollback_ms
    )
    return PaperCoordinationOutcome(
        method_id=method_id,
        policy=policy,
        selected_proposals=tuple(selected),
        proposal_fingerprint=snapshot.proposal_fingerprint,
        layer_weights=weights,
        hard_arbitration_performed=hard_arbitration,
        candidate_combinations=evaluated_count,
        success=success,
        qos_satisfied=qos_satisfied,
        safe_rejection=safe_rejection,
        resolution_latency_ms=resolution_latency_ms,
        failure_reason=failure_reason,
        rollback_count=rollback_count,
        control_messages=(4 + changed_actions + (1 if rollback_count else 0)),
        stale_state_detected=False,
    )


def dynamic_layer_weights(state: CrossLayerTaskState) -> dict[str, float]:
    current = _layer_losses(state, state)
    raw = {layer: 0.05 + current[layer] for layer in _LAYERS}
    total = sum(raw.values())
    return {layer: raw[layer] / total for layer in _LAYERS}


def _layer_groups(
    proposals: tuple[LayerProposal, ...],
) -> dict[str, tuple[LayerProposal, ...]]:
    groups = {
        layer: tuple(
            sorted(
                (item for item in proposals if item.layer == layer),
                key=lambda item: item.proposal_id,
            )
        )
        for layer in _LAYERS
    }
    if any(len(items) != 3 for items in groups.values()):
        raise ValueError("paper Exp.2 requires three shared actions per layer")
    return groups


def _sanet_score(
    state: CrossLayerTaskState,
    combination: tuple[LayerProposal, ...],
    weights: dict[str, float],
) -> float:
    projected = project_cross_layer_state(state, combination)
    losses = _layer_losses(state, projected)
    weighted_objective = sum(weights[layer] * losses[layer] for layer in _LAYERS)
    action_cost = sum(item.expected_cost for item in combination)
    return weighted_objective + 0.001 * action_cost


def _independent_score(
    state: CrossLayerTaskState,
    proposal: LayerProposal,
) -> float:
    projected = project_cross_layer_state(state, (proposal,))
    local = _local_layer_loss(projected, proposal.layer)
    return local + 0.50 * proposal.expected_cost


def _adjacent_selection(
    state: CrossLayerTaskState,
    groups: dict[str, tuple[LayerProposal, ...]],
) -> tuple[tuple[LayerProposal, ...], int]:
    selected = {
        layer: min(
            groups[layer],
            key=lambda proposal: (
                _independent_score(state, proposal),
                proposal.proposal_id,
            ),
        )
        for layer in _LAYERS
    }
    checks = 0
    for pair_index, (left, right) in enumerate(
        (("application", "transport"), ("transport", "network"), ("network", "physical"))
    ):
        candidates = (
            product(groups[left], groups[right])
            if pair_index == 0
            else ((selected[left], candidate) for candidate in groups[right])
        )
        ranked = []
        for left_action, right_action in candidates:
            checks += 1
            projected = project_cross_layer_state(
                state,
                (left_action, right_action),
            )
            ranked.append(
                (
                    _pair_loss(projected, left, right),
                    (left_action.proposal_id, right_action.proposal_id),
                    left_action,
                    right_action,
                )
            )
        _score, _ids, left_action, right_action = min(ranked)
        selected[left] = left_action
        selected[right] = right_action
    return tuple(selected[layer] for layer in _LAYERS), checks


def _layer_losses(
    original: CrossLayerTaskState,
    projected: CrossLayerTaskState,
) -> dict[str, float]:
    edges = tuple(original.metadata.get("paper_conflicted_edge_ids", ()))
    if not edges:
        return {layer: 0.0 for layer in _LAYERS}
    totals = {layer: 0.0 for layer in _LAYERS}
    for edge_id in edges:
        constraint = projected.constraints[edge_id]
        application = projected.application[edge_id]
        transport = projected.transport[edge_id]
        network = projected.network[edge_id]
        physical = projected.physical[edge_id]
        scale = max(application.required_rate_mbps, constraint.desired_rate_mbps, 1e-9)
        transport_capacity = (
            transport.send_rate_mbps
            if transport.admissible_capacity_mbps is None
            else transport.admissible_capacity_mbps
        )
        totals["application"] += max(
            0.0, constraint.desired_rate_mbps - application.required_rate_mbps
        ) / max(constraint.desired_rate_mbps, 1e-9)
        totals["application"] += max(
            0.0,
            application.required_rate_mbps
            - projected.shared_resource_capacity_mbps[constraint.shared_resource_id],
        ) / scale
        totals["transport"] += max(
            0.0,
            application.required_rate_mbps
            - min(transport.send_rate_mbps, transport_capacity),
        ) / scale
        totals["transport"] += max(
            0.0, transport.send_rate_mbps - transport_capacity
        ) / scale
        totals["network"] += max(
            0.0, transport.send_rate_mbps - network.available_bandwidth_mbps
        ) / scale
        totals["network"] += max(0.0, network.utilization - 0.70)
        totals["physical"] += max(
            0.0, network.available_bandwidth_mbps - physical.available_capacity_mbps
        ) / max(network.available_bandwidth_mbps, 1e-9)
        totals["physical"] += max(
            0.0, application.required_rate_mbps - physical.available_capacity_mbps
        ) / scale
        totals["physical"] += max(0.0, 0.80 - physical.signal_quality)
    return {layer: value / len(edges) for layer, value in totals.items()}


def _local_layer_loss(state: CrossLayerTaskState, layer: str) -> float:
    edges = tuple(state.metadata.get("paper_conflicted_edge_ids", ()))
    if not edges:
        return 0.0
    edge_losses = []
    for edge_id in edges:
        if layer == "application":
            application = state.application[edge_id]
            desired = state.constraints[edge_id].desired_rate_mbps
            loss = max(0.0, desired - application.required_rate_mbps) / max(desired, 1e-9)
        elif layer == "transport":
            transport = state.transport[edge_id]
            capacity = transport.admissible_capacity_mbps or transport.send_rate_mbps
            loss = max(0.0, transport.send_rate_mbps - capacity) / max(capacity, 1e-9)
        elif layer == "network":
            network = state.network[edge_id]
            loss = 2.0 * max(0.0, network.utilization - 0.55)
            loss += network.queue_occupancy
        else:
            physical = state.physical[edge_id]
            loss = max(0.0, physical.resource_utilization - 0.50)
            loss += 2.0 * max(0.0, 0.85 - physical.signal_quality)
        edge_losses.append(loss)
    # Independent optimization is local across layers, not permissive across
    # flows: each layer protects its worst local flow without seeing another
    # layer's state or performing cross-layer arbitration.
    return max(edge_losses)


def _pair_loss(state: CrossLayerTaskState, left: str, right: str) -> float:
    edges = tuple(state.metadata.get("paper_conflicted_edge_ids", ()))
    if not edges:
        return 0.0
    total = 0.0
    for edge_id in edges:
        application = state.application[edge_id]
        transport = state.transport[edge_id]
        network = state.network[edge_id]
        physical = state.physical[edge_id]
        if (left, right) == ("application", "transport"):
            capacity = transport.admissible_capacity_mbps or transport.send_rate_mbps
            total += max(
                0.0,
                application.required_rate_mbps - min(transport.send_rate_mbps, capacity),
            ) / max(application.required_rate_mbps, 1e-9)
            total += max(
                0.0, transport.send_rate_mbps - capacity
            ) / max(transport.send_rate_mbps, 1e-9)
        elif (left, right) == ("transport", "network"):
            total += max(
                0.0, transport.send_rate_mbps - network.available_bandwidth_mbps
            ) / max(transport.send_rate_mbps, 1e-9)
            total += max(0.0, network.utilization - 0.70)
        else:
            total += max(
                0.0, network.available_bandwidth_mbps - physical.available_capacity_mbps
            ) / max(network.available_bandwidth_mbps, 1e-9)
    return total / len(edges)
