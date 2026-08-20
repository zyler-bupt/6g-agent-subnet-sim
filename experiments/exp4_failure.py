from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import (
    EXPERIMENT_METHODS,
    METHODS,
    RunMode,
    mode_spec,
)
from src.controller.paper_failure_recovery import (
    PaperFailureOutcome,
    run_paper_failure_method,
)
from src.metrics.paper import PaperTrial, write_paper_trials
from src.simulation.paper_failure_scenarios import (
    PaperFailureSnapshot,
    generate_paper_failure_snapshot,
)


MAIN_FAILURE_POINTS = (
    ("agent_failure", 1.0),
    ("link_failure", 1.0),
    ("capacity_degradation", 0.30),
)
CAPACITY_REDUCTION_POINTS = (10, 20, 30, 40, 50)


async def run_exp4(
    mode: str | RunMode,
    output_root: Path,
    *,
    seeds: tuple[int, ...] | None = None,
    event_ids: tuple[int, ...] | None = None,
    capacity_reductions: tuple[int, ...] = CAPACITY_REDUCTION_POINTS,
) -> list[PaperTrial]:
    """Run the final paired failure-type and capacity-stress Exp.4 grids."""

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
    selected_reductions = tuple(sorted(set(int(value) for value in capacity_reductions)))
    if not selected_seeds or not selected_events or not selected_reductions:
        raise ValueError("Exp.4 requires non-empty seeds, events, and stress points")
    if any(value not in CAPACITY_REDUCTION_POINTS for value in selected_reductions):
        raise ValueError("unsupported Exp.4 capacity reduction")

    rows: list[PaperTrial] = []
    for failure_type, severity in MAIN_FAILURE_POINTS:
        for seed in selected_seeds:
            for event_id in selected_events:
                snapshot = generate_paper_failure_snapshot(
                    failure_type,
                    severity,
                    seed,
                    event_id,
                )
                trial_id = (
                    f"exp4:failure_type:{failure_type}:seed:{seed:04d}:"
                    f"event:{event_id:03d}"
                )
                rows.extend(
                    await _run_paired_methods(
                        selected_mode,
                        snapshot,
                        trial_id,
                        series="failure_type",
                    )
                )

    for reduction_percent in selected_reductions:
        for seed in selected_seeds:
            for event_id in selected_events:
                snapshot = generate_paper_failure_snapshot(
                    "capacity_degradation",
                    reduction_percent / 100.0,
                    seed,
                    event_id,
                )
                trial_id = (
                    f"exp4:capacity_stress:{reduction_percent}:seed:{seed:04d}:"
                    f"event:{event_id:03d}"
                )
                rows.extend(
                    await _run_paired_methods(
                        selected_mode,
                        snapshot,
                        trial_id,
                        series="capacity_stress",
                    )
                )
    rows.sort(
        key=lambda row: (
            row.series or "",
            row.failure_type or "",
            row.failure_severity or 0.0,
            row.seed,
            row.event_id,
            row.method_id,
        )
    )
    write_paper_trials(
        output_root / "raw" / selected_mode.value / "exp4" / "trials.csv",
        rows,
    )
    return rows


async def _run_paired_methods(
    mode: RunMode,
    snapshot: PaperFailureSnapshot,
    trial_id: str,
    *,
    series: str,
) -> list[PaperTrial]:
    rows = []
    for method_id in EXPERIMENT_METHODS["exp4"]:
        outcome = await run_paper_failure_method(snapshot, method_id)
        rows.append(_paper_trial(mode, snapshot, outcome, trial_id, series))
    return rows


def _paper_trial(
    mode: RunMode,
    snapshot: PaperFailureSnapshot,
    outcome: PaperFailureOutcome,
    trial_id: str,
    series: str,
) -> PaperTrial:
    method = METHODS[outcome.method_id]
    return PaperTrial(
        experiment="exp4",
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
        series=series,
        result_mode="transactionally_verified_failure_recovery_simulation",
        event_occurred_at=outcome.event_occurred_at,
        stable_verify_finished_at=outcome.stable_verify_finished_at,
        task_size=len(snapshot.task.app_agents),
        num_dag_edges=len(snapshot.task.biz_edges),
        num_gateways=len(snapshot.catalog.gateways),
        failure_type=snapshot.failure_type,
        failure_severity=snapshot.failure_severity,
        recovery_latency_ms=outcome.recovery_latency_ms,
        success=outcome.success,
        qos_satisfied=outcome.qos_satisfied,
        safe_rejection=(
            not outcome.success
            and outcome.rollback_count == 0
            and outcome.changed_rules == 0
        ),
        failure_reason=outcome.failure_reason or None,
        total_rules=outcome.total_rules,
        changed_rules=outcome.changed_rules,
        rule_change_ratio=outcome.rule_change_ratio,
        total_gateways=outcome.total_gateways,
        changed_gateways=outcome.changed_gateways,
        gateway_change_ratio=outcome.gateway_change_ratio,
        total_flows=outcome.total_flows,
        unaffected_flows=outcome.unaffected_flows,
        disturbed_unaffected_flows=outcome.disturbed_unaffected_flows,
        unaffected_disturbance_ratio=outcome.unaffected_disturbance_ratio,
        control_messages=outcome.control_messages,
        control_bytes=outcome.control_bytes,
        rollback_count=outcome.rollback_count,
        stale_state_detected=outcome.stale_state_detected,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run final paper Exp.4")
    parser.add_argument("--mode", choices=("pilot", "paper"), default="pilot")
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    return parser


def main() -> None:
    args = _parser().parse_args()
    rows = asyncio.run(run_exp4(args.mode, args.output_root))
    print(f"wrote {len(rows)} Exp.4 {args.mode} trials")


if __name__ == "__main__":
    main()
