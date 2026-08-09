from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from src.controller.feasibility import (
    CrossLayerFeasibilityResult,
    combination_objective,
    evaluate_cross_layer_combination,
)
from src.core.cross_layer import CrossLayerTaskState, LayerProposal


@dataclass(frozen=True)
class EvaluatedCombination:
    proposal_ids: tuple[str, ...]
    feasible: bool
    violations: tuple[str, ...]
    objective: tuple[float, float, float, float]


@dataclass(frozen=True)
class GroundTruthResult:
    ground_truth_conflict: bool
    ground_truth_resolvable: bool
    independent_proposal_ids: tuple[str, ...]
    independent_violations: tuple[str, ...]
    feasible_combinations: tuple[tuple[str, ...], ...]
    best_feasible_combination: tuple[str, ...]
    evaluated_combinations: tuple[EvaluatedCombination, ...]
    local_proposal_feasibility: dict[str, bool]

    @property
    def candidate_combinations(self) -> int:
        return len(self.evaluated_combinations)


class GroundTruthSolver:
    """Exhaustive method-independent oracle for the small proposal pools.

    It accepts no comparison-method parameter.  All experiment methods consume
    the immutable result generated here from the common state/proposal snapshot.
    """

    def solve(
        self,
        state: CrossLayerTaskState,
        proposals: tuple[LayerProposal, ...],
    ) -> GroundTruthResult:
        groups = proposal_groups(proposals)
        combinations = tuple(product(*(groups[key] for key in sorted(groups))))
        if not combinations:
            raise ValueError("ground-truth solver requires proposals")

        evaluated: list[EvaluatedCombination] = []
        feasibility_by_ids: dict[tuple[str, ...], CrossLayerFeasibilityResult] = {}
        proposal_by_id = {proposal.proposal_id: proposal for proposal in proposals}
        for combination in combinations:
            result = evaluate_cross_layer_combination(state, combination)
            ids = tuple(item.proposal_id for item in combination)
            feasibility_by_ids[ids] = result
            evaluated.append(
                EvaluatedCombination(
                    proposal_ids=ids,
                    feasible=result.feasible,
                    violations=result.violations,
                    objective=combination_objective(result, combination),
                )
            )

        independent = tuple(
            max(
                groups[key],
                key=lambda item: (item.utility, item.proposal_id),
            )
            for key in sorted(groups)
        )
        independent_ids = tuple(item.proposal_id for item in independent)
        independent_result = feasibility_by_ids[independent_ids]
        feasible = [item for item in evaluated if item.feasible]
        best_ids: tuple[str, ...] = ()
        if feasible:
            best = min(feasible, key=lambda item: (item.objective, item.proposal_ids))
            best_ids = best.proposal_ids

        local_feasibility = {
            proposal.proposal_id: evaluate_cross_layer_combination(
                state,
                (proposal,),
                check_write_sets=False,
            ).feasible
            for proposal in proposals
        }
        conflict = not independent_result.feasible
        return GroundTruthResult(
            ground_truth_conflict=conflict,
            ground_truth_resolvable=conflict and bool(feasible),
            independent_proposal_ids=independent_ids,
            independent_violations=independent_result.violations,
            feasible_combinations=tuple(item.proposal_ids for item in feasible),
            best_feasible_combination=best_ids,
            evaluated_combinations=tuple(evaluated),
            local_proposal_feasibility=local_feasibility,
        )


def proposal_groups(
    proposals: tuple[LayerProposal, ...],
    *,
    require_all_layers: bool = True,
) -> dict[tuple[str, str], tuple[LayerProposal, ...]]:
    mutable: dict[tuple[str, str], list[LayerProposal]] = {}
    for proposal in proposals:
        for edge_id in proposal.affected_edges:
            mutable.setdefault((edge_id, proposal.layer), []).append(proposal)
    required_layers = {"application", "transport", "network", "physical"}
    edges = {edge_id for edge_id, _layer in mutable}
    for edge_id in edges:
        layers = {layer for candidate_edge, layer in mutable if candidate_edge == edge_id}
        if require_all_layers and layers != required_layers:
            raise ValueError(
                f"edge {edge_id} proposal layers are {sorted(layers)}, "
                f"expected {sorted(required_layers)}"
            )
    return {
        key: tuple(sorted(items, key=lambda item: item.proposal_id))
        for key, items in mutable.items()
    }


def proposals_for_ids(
    proposals: tuple[LayerProposal, ...],
    proposal_ids: tuple[str, ...],
) -> tuple[LayerProposal, ...]:
    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    try:
        return tuple(by_id[proposal_id] for proposal_id in proposal_ids)
    except KeyError as error:
        raise ValueError(f"unknown proposal in combination: {error.args[0]}") from error
