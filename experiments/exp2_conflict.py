from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path

from experiments.paper_protocol import (
    EXPERIMENT_METHODS,
    METHODS,
    RunMode,
    mode_spec,
)
from src.controller.paper_cross_layer_coordination import (
    PaperCoordinationOutcome,
    coordinate_paper,
)
from src.metrics.paper import PaperTrial, write_paper_trials
from src.simulation.paper_conflicts import (
    PaperConflictSnapshot,
    generate_conflict_snapshot,
)


CONFLICT_DENSITY_POINTS = (0, 10, 20, 30, 40, 50, 60)


async def run_exp2(
    mode: str | RunMode,
    output_root: Path,
    *,
    seeds: tuple[int, ...] | None = None,
    event_ids: tuple[int, ...] | None = None,
    conflict_densities: tuple[int, ...] = CONFLICT_DENSITY_POINTS,
) -> list[PaperTrial]:
    """Run the paired conflict-density grid after exact oracle labeling."""

    selected_mode = RunMode(mode)
    specification = mode_spec(selected_mode)
    selected_seeds = (
        tuple(range(specification.topology_seeds))
        if seeds is None
        else tuple(sorted(set(seeds)))
    )
    selected_events = (
        tuple(range(specification.rate_events_per_seed))
        if event_ids is None
        else tuple(sorted(set(event_ids)))
    )
    selected_densities = tuple(sorted(set(int(item) for item in conflict_densities)))
    if not selected_seeds or not selected_events or not selected_densities:
        raise ValueError("Exp.2 requires non-empty seeds, events, and density points")
    if any(item not in CONFLICT_DENSITY_POINTS for item in selected_densities):
        raise ValueError("unsupported Exp.2 conflict density")

    rows: list[PaperTrial] = []
    for density in selected_densities:
        for seed in selected_seeds:
            for event_id in selected_events:
                # Generation and oracle labeling occur once before any method
                # selection; oracle work is absent from method latency.
                snapshot = generate_conflict_snapshot(density, seed, event_id)
                trial_id = (
                    f"exp2:conflict_density:{density}:seed:{seed:04d}:"
                    f"event:{event_id:03d}"
                )
                for method_id in EXPERIMENT_METHODS["exp2"]:
                    outcome = coordinate_paper(method_id, snapshot)
                    rows.append(
                        _paper_trial(selected_mode, snapshot, outcome, trial_id)
                    )
    rows.sort(
        key=lambda row: (
            row.conflict_density or 0.0,
            row.seed,
            row.event_id,
            row.method_id,
        )
    )
    write_paper_trials(
        output_root / "raw" / selected_mode.value / "exp2" / "trials.csv",
        rows,
    )
    return rows


def _paper_trial(
    mode: RunMode,
    snapshot: PaperConflictSnapshot,
    outcome: PaperCoordinationOutcome,
    trial_id: str,
) -> PaperTrial:
    method = METHODS[outcome.method_id]
    type_counts = Counter(snapshot.conflict_types.values())
    conflict_type = (
        "none"
        if not type_counts
        else ";".join(
            f"{kind.value}:{type_counts[kind]}" for kind in sorted(type_counts, key=lambda item: item.value)
        )
    )
    return PaperTrial(
        experiment="exp2",
        mode=mode.value,
        trial_id=trial_id,
        seed=snapshot.seed,
        event_id=snapshot.event_id,
        method_id=method.method_id,
        method_label=method.label,
        method_source=method.source,
        adapted=method.adapted,
        topology_fingerprint=snapshot.topology_fingerprint,
        scenario_fingerprint=snapshot.scenario_fingerprint,
        qos_fingerprint=snapshot.qos_fingerprint,
        event_fingerprint=snapshot.event_fingerprint,
        series="conflict_density",
        result_mode="shared_action_space_transactionally_verified_simulation",
        event_occurred_at=0.0,
        stable_verify_finished_at=(
            outcome.resolution_latency_ms / 1000.0 if outcome.success else None
        ),
        task_size=len(snapshot.task.app_agents),
        num_dag_edges=len(snapshot.task.biz_edges),
        num_gateways=len(snapshot.catalog.gateways),
        conflict_density=snapshot.conflict_density,
        conflict_type=conflict_type,
        ground_truth_feasible=snapshot.oracle.feasible,
        resolution_latency_ms=outcome.resolution_latency_ms,
        success=outcome.success,
        qos_satisfied=outcome.qos_satisfied,
        safe_rejection=outcome.safe_rejection,
        failure_reason=outcome.failure_reason or None,
        control_messages=outcome.control_messages,
        rollback_count=outcome.rollback_count,
        stale_state_detected=outcome.stale_state_detected,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run final paper Exp.2")
    parser.add_argument("--mode", choices=("pilot", "paper"), default="pilot")
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = asyncio.run(run_exp2(args.mode, args.output_root))
    print(f"wrote {len(rows)} Exp.2 {args.mode} trials")


if __name__ == "__main__":
    main()
