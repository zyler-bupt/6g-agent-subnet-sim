from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import (
    EXPERIMENT_FAILURE_METHODS,
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


LINK_AFFECTED_FLOW_RATIOS = (0.03, 0.06, 0.10, 0.15, 0.25)
AGENT_DEPENDENCY_CLOSURE_RATIOS = (0.1, 0.2, 0.3, 0.4, 0.5)
CAPACITY_RATIOS = (1.1, 1.0, 0.9, 0.75, 0.6)


async def run_exp4(
    mode: str | RunMode,
    output_root: Path,
    *,
    seeds: tuple[int, ...] | None = None,
    event_ids: tuple[int, ...] | None = None,
    link_ratios: tuple[float, ...] = LINK_AFFECTED_FLOW_RATIOS,
    agent_ratios: tuple[float, ...] = AGENT_DEPENDENCY_CLOSURE_RATIOS,
    capacity_ratios: tuple[float, ...] = CAPACITY_RATIOS,
    capacity_reductions: tuple[int, ...] | None = None,
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
    if capacity_reductions is not None:
        rows: list[PaperTrial] = []
        legacy_points = (("agent_failure", 1.0), ("link_failure", 1.0), ("capacity_degradation", 0.30))
        for failure_type, severity in legacy_points:
            for seed in selected_seeds:
                for event_id in selected_events:
                    snapshot = generate_paper_failure_snapshot(failure_type, severity, seed, event_id)
                    rows.extend(await _run_paired_methods(selected_mode, snapshot,
                        f"exp4:failure_type:{failure_type}:seed:{seed:04d}:event:{event_id:03d}", series="failure_type"))
        for reduction in capacity_reductions:
            for seed in selected_seeds:
                for event_id in selected_events:
                    snapshot = generate_paper_failure_snapshot("capacity_degradation", reduction / 100.0, seed, event_id)
                    rows.extend(await _run_paired_methods(selected_mode, snapshot,
                        f"exp4:capacity_stress:{reduction}:seed:{seed:04d}:event:{event_id:03d}", series="capacity_stress"))
        rows.sort(key=lambda row: (row.series or "", row.failure_type or "", row.failure_severity or 0.0, row.seed, row.event_id, row.method_id))
        write_paper_trials(output_root / "raw" / selected_mode.value / "exp4" / "trials.csv", rows)
        return rows
    grids = {
        "link_failure": tuple(float(value) for value in link_ratios),
        "agent_failure": tuple(float(value) for value in agent_ratios),
        "capacity_degradation": tuple(float(value) for value in capacity_ratios),
    }
    if not selected_seeds or not selected_events or any(not values for values in grids.values()):
        raise ValueError("Exp.4 requires non-empty seeds, events, and stress points")

    rows: list[PaperTrial] = []
    for failure_type, values in grids.items():
        for value in values:
            generator_severity = value
            for seed in selected_seeds:
                for event_id in selected_events:
                    snapshot = generate_paper_failure_snapshot(
                        failure_type, generator_severity, seed, event_id,
                        capacity_ratio=(value if failure_type == "capacity_degradation" else None),
                    )
                    trial_id = (
                        f"exp4:{failure_type}:{value:g}:seed:{seed:04d}:"
                        f"event:{event_id:03d}"
                    )
                    rows.extend(
                        await _run_paired_methods(
                            selected_mode,
                            snapshot,
                            trial_id,
                            series={
                                "link_failure": "affected_flow_ratio",
                                "agent_failure": "dependency_closure_ratio",
                                "capacity_degradation": "post_fault_capacity_ratio",
                            }[failure_type],
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
    # Frozen Exp4 protocol: each failure type is compared against its own
    # representative baseline set (never a common list for all failures).
    methods = EXPERIMENT_FAILURE_METHODS["exp4"][snapshot.failure_type]
    for method_id in methods:
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
        method_source=method.reference,
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
        affected_flow_ratio=snapshot.affected_flow_ratio,
        dependency_closure_ratio=snapshot.dependency_closure_ratio,
        post_fault_capacity_ratio=snapshot.post_fault_capacity_ratio,
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
        total_rule_objects=outcome.total_rule_objects,
        changed_rules=outcome.changed_rules,
        rule_change_ratio=outcome.rule_change_ratio,
        total_paths=outcome.total_paths,
        changed_paths=outcome.changed_paths,
        total_agents=outcome.total_agents,
        changed_agents=outcome.changed_agents,
        modification_scope_ratio=outcome.modification_scope_ratio,
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
