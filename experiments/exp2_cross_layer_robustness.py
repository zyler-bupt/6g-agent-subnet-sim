from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import signal
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import yaml

from src.controller.authorized_actions import AuthorizedActionExecutor
from src.controller.cross_layer_coordinator import (
    CoordinationResult,
    CrossLayerCoordinator,
)
from src.controller.feasibility import evaluate_cross_layer_combination
from src.core.events import EventInjector
from src.core.models import to_jsonable
from src.metrics.exp2_robustness import (
    RobustnessRunMetrics,
    write_jsonl,
    write_robustness_csv,
)
from src.simulation.conflict_robustness import (
    UNRESOLVABLE_CASES,
    UNRESOLVABLE_CONFLICT,
    ConflictRobustnessGenerator,
    RobustnessScenarioSnapshot,
)
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig


METHODS = ("proposed", "independent", "adjacent", "no_verification")


async def run_case(
    snapshot: RobustnessScenarioSnapshot,
    method: str,
    *,
    run_index: int,
    coordination_timeout_ms: float,
    execute_transaction: bool = True,
) -> tuple[RobustnessRunMetrics, list[dict[str, object]], dict[str, object]]:
    if method not in METHODS:
        raise ValueError(f"unsupported robustness method: {method}")
    controller, verifier, _provider = snapshot.instantiate()
    stable, formation = await controller.build_task_subnet(
        snapshot.base.base.task,
        verifier=verifier,
        run_id=run_index,
        seed=snapshot.seed,
    )
    if not formation.networking_success:
        raise RuntimeError(
            "initial transactional build failed: " + formation.failure_reason
        )
    stable_before = stable.version
    started = perf_counter()
    coordination, peak_memory_mb, timed_out = _coordinate_measured(
        method,
        snapshot,
        coordination_timeout_ms,
    )
    transaction = None
    post = None
    event_rows: list[dict[str, object]] = []
    decision_rejected = timed_out or not coordination.selected_proposals
    transaction_attempted = execute_transaction and not decision_rejected
    failure_reason = ""
    if timed_out:
        failure_reason = "coordination_timeout"
    elif decision_rejected:
        failure_reason = _rejection_reason(snapshot, coordination)
    elif execute_transaction:
        event = EventInjector(
            prefix=f"exp2-robust-{snapshot.experiment}-{method}-{snapshot.seed}"
        ).cross_layer_conflict(
            stable.task.task_id,
            scenario=snapshot.scenario,
            pressure=snapshot.pressure,
            scenario_fingerprint=snapshot.fingerprint,
        )
        execution = await AuthorizedActionExecutor(controller, verifier).execute(
            stable,
            snapshot.true_state,
            coordination,
            event,
            run_id=run_index,
            seed=snapshot.seed,
            received_at=perf_counter(),
        )
        transaction = execution.transaction
        post = execution.post_execution_feasibility
        failure_reason = transaction.failure_reason
        event_rows = [
            {
                **to_jsonable(record),
                "experiment_run_id": _run_id(snapshot, method),
                "experiment": snapshot.experiment,
                "scenario": snapshot.scenario,
                "method": method,
                "scenario_fingerprint": snapshot.fingerprint,
            }
            for record in transaction.event_log
        ]
    else:
        post = evaluate_cross_layer_combination(
            snapshot.true_state,
            coordination.selected_proposals,
        )

    if post is None:
        post = evaluate_cross_layer_combination(snapshot.true_state)
    finished = perf_counter()
    rollback_triggered = bool(transaction and transaction.rollback_triggered)
    rollback_success = bool(transaction and transaction.rollback_success)
    transaction_success = bool(transaction and transaction.success)
    stable_after = transaction.state.version if transaction is not None else stable.version
    no_partial_commit = _no_partial_commit(
        controller,
        stable.task.task_id,
        stable.involved_gateways,
        stable_before,
        stable_after,
        transaction_success,
    )
    unresolvable = snapshot.conflict_class == UNRESOLVABLE_CONFLICT
    safe_rejection = decision_rejected or (unresolvable and rollback_success)
    unsafe_execution = unresolvable and transaction_success
    qos_satisfied = bool(
        (transaction_success and post.feasible)
        or (not transaction_attempted and not decision_rejected and post.feasible)
    )
    stale_detected = any(
        item.startswith("stale_state") for item in post.violations
    ) or (
        snapshot.metadata.get("stale_ms", 0) > 0
        and coordination.conflict_detected
    )
    stale_rejected = bool(
        snapshot.metadata.get("stale_ms", 0) > 0 and decision_rejected
    )
    raw_combinations = _raw_combination_count(snapshot.proposals)
    evaluated = _evaluated_count(method, snapshot.proposals, coordination)
    feasible_count = int(coordination.details.get("feasible_combinations", 0))
    # Proposed and no-verification materialize the global Cartesian product.
    # Independent and adjacent never construct a global combination: their
    # entire global space is reported as structurally pruned, while the next
    # field separately records layer-local or adjacent-pair evaluations.
    pruned = raw_combinations if method in {"independent", "adjacent"} else 0
    selected_ids = tuple(
        proposal.proposal_id for proposal in coordination.selected_proposals
    )
    rejected_ids = tuple(sorted(coordination.rejected_proposals))
    proposal_version = int(snapshot.metadata.get("proposal_generated_version", 1))
    execution_version = int(
        snapshot.metadata.get(
            "execution_version",
            snapshot.true_state.metadata.get("stable_version", 1),
        )
    )
    read_version = int(snapshot.metadata.get("read_set_version", proposal_version))
    metric = RobustnessRunMetrics(
        run_id=_run_id(snapshot, method),
        seed=snapshot.seed,
        experiment=snapshot.experiment,
        scenario=snapshot.scenario,
        method=method,
        conflict_pressure=snapshot.pressure,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_control_plane_simulation",
        conflict_class=snapshot.conflict_class,
        ground_truth_conflict=snapshot.ground_truth.ground_truth_conflict,
        ground_truth_resolvable=snapshot.ground_truth.ground_truth_resolvable,
        ground_truth_feasible_combinations=len(
            snapshot.ground_truth.feasible_combinations
        ),
        ground_truth_uses_true_state=True,
        noise_ratio=float(snapshot.metadata.get("noise_ratio", 0.0)),
        stale_ms=int(snapshot.metadata.get("stale_ms", 0)),
        missing_layer=str(snapshot.metadata.get("missing_layer", "")),
        proposals_per_layer=int(
            snapshot.metadata.get("proposals_per_layer", 0)
        ),
        num_proposals=len(snapshot.proposals),
        num_layers_observed=len({proposal.layer for proposal in snapshot.proposals}),
        num_raw_combinations=raw_combinations,
        num_pruned_combinations=pruned,
        num_evaluated_combinations=evaluated,
        num_feasible_combinations=feasible_count,
        combination_evaluation_mode={
            "proposed": "global_exact_feasibility",
            "no_verification": "global_declared_score_without_feasibility",
            "independent": "layer_local_candidates",
            "adjacent": "adjacent_pair_candidates",
        }[method],
        selected_proposal_ids=";".join(selected_ids),
        selected_action_set=";".join(
            f"{proposal.layer}:{proposal.action}"
            for proposal in coordination.selected_proposals
        ),
        rejected_proposal_ids=";".join(rejected_ids),
        pre_execution_feasibility_checked=coordination.global_check_performed,
        post_execution_verification_result=(
            "NOT_EXECUTED"
            if not transaction_attempted
            else ("PASS" if transaction_success else "FAIL")
        ),
        conflict_detected=coordination.conflict_detected,
        conflict_resolved=(snapshot.ground_truth.ground_truth_resolvable and qos_satisfied),
        qos_satisfied=qos_satisfied,
        infeasible_configuration=bool(transaction_attempted and not post.feasible),
        decision_rejected=decision_rejected,
        safe_rejection=safe_rejection,
        unsafe_execution=unsafe_execution,
        transaction_attempted=transaction_attempted,
        rollback_triggered=rollback_triggered,
        rollback_success=rollback_success,
        no_partial_commit=no_partial_commit,
        stable_version_before=stable_before,
        stable_version_after=stable_after,
        proposal_generated_version=proposal_version,
        execution_version=execution_version,
        read_set_version=read_version,
        stale_state_detected=stale_detected,
        stale_proposal_rejected=stale_rejected,
        proposal_regenerated=bool(
            snapshot.metadata.get("proposal_regenerated", False)
        ),
        coordination_latency_ms=coordination.coordination_latency_ms,
        feasibility_latency_ms=coordination.feasibility_latency_ms,
        transaction_latency_ms=(
            execution.transaction_latency_ms
            if transaction_attempted
            else 0.0
        ),
        total_latency_ms=max(0.0, (finished - started) * 1000.0),
        peak_memory_mb=peak_memory_mb,
        timeout=timed_out,
        control_messages=transaction.control_messages if transaction else 0,
        control_bytes=transaction.control_bytes if transaction else 0,
        success=transaction_success or (
            not execute_transaction and not timed_out and post.feasible
        ),
        failure_reason=failure_reason,
    )
    decision = {
        **asdict(metric),
        "coordination_details": to_jsonable(coordination.details),
        "ground_truth_best_combination": list(
            snapshot.ground_truth.best_feasible_combination
        ),
        "post_execution_violations": list(post.violations),
    }
    if not event_rows:
        event_rows.append(
            {
                "experiment_run_id": metric.run_id,
                "timestamp": finished,
                "component": "CrossLayerCoordinator",
                "event_stage": (
                    "SAFE_REJECTED" if decision_rejected else "COORDINATION_ONLY"
                ),
                "details": {
                    "failure_reason": failure_reason,
                    "selected_proposal_ids": list(selected_ids),
                },
            }
        )
    return metric, event_rows, decision


async def run_experiment(
    config: dict[str, Any],
    seeds: tuple[int, ...],
    output_dir: Path,
) -> list[RobustnessRunMetrics]:
    if output_dir.resolve() == Path("results/exp2").resolve():
        raise ValueError("robustness results must not overwrite results/exp2")
    exp2_before = _artifact_hashes(Path("results/exp2"))
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _write_main_scenario_audit(config, raw_dir)
    generator = ConflictRobustnessGenerator()
    scenario_config = _scenario_config(config)
    default_timeout = float(
        config.get("simulation", {}).get("coordination_timeout_ms", 1000)
    )
    metrics: list[RobustnessRunMetrics] = []
    events: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    scenarios: dict[str, dict[str, object]] = {}
    run_index = 0

    async def execute(
        snapshot: RobustnessScenarioSnapshot,
        methods: Iterable[str],
        *,
        transaction: bool = True,
        timeout_ms: float = default_timeout,
    ) -> None:
        nonlocal run_index
        scenarios.setdefault(snapshot.fingerprint, snapshot.to_dict())
        for method in methods:
            run_index += 1
            try:
                metric, run_events, decision = await run_case(
                    snapshot,
                    str(method),
                    run_index=run_index,
                    coordination_timeout_ms=timeout_ms,
                    execute_transaction=transaction,
                )
            except Exception as error:
                metric = _failed_metric(snapshot, str(method), error)
                run_events = []
                decision = {**asdict(metric), "runner_error": repr(error)}
            metrics.append(metric)
            events.extend(run_events)
            decisions.append(decision)

    unresolved = config.get("unresolvable", {})
    for case in unresolved.get("cases", UNRESOLVABLE_CASES):
        for seed in seeds:
            await execute(
                generator.unresolvable(str(case), seed, scenario_config),
                unresolved.get("methods", METHODS),
            )

    noise = config.get("noise", {})
    for scenario, pressure in noise.get("scenarios", {}).items():
        for ratio in noise.get("ratios", (0.0, 0.05, 0.10, 0.20)):
            for seed in seeds:
                await execute(
                    generator.noisy(
                        str(scenario),
                        float(pressure),
                        float(ratio),
                        seed,
                        scenario_config,
                    ),
                    noise.get("methods", ("proposed", "adjacent")),
                )

    stale = config.get("stale_state", {})
    for stale_ms in stale.get("stale_ms", (0, 20, 50, 100, 200)):
        for seed in seeds:
            await execute(
                generator.stale(int(stale_ms), seed, scenario_config),
                stale.get("methods", METHODS),
            )

    missing = config.get("missing_layer", {})
    for layer in missing.get(
        "layers", ("application", "transport", "network", "physical")
    ):
        for seed in seeds:
            await execute(
                generator.missing_layer(str(layer), seed, scenario_config),
                missing.get("methods", ("proposed", "adjacent")),
            )

    scale = config.get("proposal_scale", {})
    scale_timeout = float(scale.get("coordination_timeout_ms", default_timeout))
    for count in scale.get("proposals_per_layer", (1, 2, 3, 4, 5)):
        for seed in seeds:
            await execute(
                generator.proposal_scale(int(count), seed, scenario_config),
                scale.get("methods", ("proposed", "adjacent")),
                transaction=False,
                timeout_ms=scale_timeout,
            )

    write_robustness_csv(raw_dir / "runs.csv", metrics)
    write_jsonl(raw_dir / "events.jsonl", events)
    write_jsonl(raw_dir / "decisions.jsonl", decisions)
    write_jsonl(raw_dir / "scenarios.jsonl", scenarios.values())
    exp2_after = _artifact_hashes(Path("results/exp2"))
    source_integrity = {
        "status": "PASS" if exp2_before == exp2_after else "FAIL",
        "source_directory": "results/exp2",
        "file_count_before": len(exp2_before),
        "file_count_after": len(exp2_after),
        "hashes_before": exp2_before,
        "hashes_after": exp2_after,
    }
    (output_dir / "exp2_source_integrity.json").write_text(
        json.dumps(source_integrity, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if exp2_before != exp2_after:
        raise RuntimeError("results/exp2 changed during robustness experiment")
    return metrics


def _coordinate_measured(
    method: str,
    snapshot: RobustnessScenarioSnapshot,
    timeout_ms: float,
) -> tuple[CoordinationResult, float, bool]:
    timed_out = False

    def alarm_handler(_signum, _frame):
        raise TimeoutError("coordination timeout")

    previous = signal.getsignal(signal.SIGALRM)
    tracemalloc.start()
    try:
        signal.signal(signal.SIGALRM, alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, timeout_ms / 1000.0))
        result = CrossLayerCoordinator().coordinate(
            method,
            snapshot.observed_state,
            snapshot.proposals,
        )
    except TimeoutError:
        timed_out = True
        result = _timeout_result(method)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return result, peak / (1024.0 * 1024.0), timed_out


def _timeout_result(method: str) -> CoordinationResult:
    return CoordinationResult(
        method=method,
        selected_proposals=(),
        rejected_proposals={},
        conflicts=(),
        conflict_detected=False,
        conflict_resolved=False,
        global_check_performed=(method == "proposed"),
        pairwise_checks=0,
        candidate_combinations=0,
        rejected_combinations=0,
        selected_feasibility=None,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        details={"timeout": True},
    )


def _raw_combination_count(proposals) -> int:
    counts: dict[tuple[str, str], int] = {}
    for proposal in proposals:
        for edge_id in proposal.affected_edges:
            key = (edge_id, proposal.layer)
            counts[key] = counts.get(key, 0) + 1
    product = 1
    for count in counts.values():
        product *= count
    return product if counts else 0


def _evaluated_count(method: str, proposals, result: CoordinationResult) -> int:
    if method in {"proposed", "no_verification"}:
        return result.candidate_combinations
    if method == "independent":
        return len(result.selected_proposals)
    counts: dict[tuple[str, str], int] = {}
    for proposal in proposals:
        for edge_id in proposal.affected_edges:
            counts[(edge_id, proposal.layer)] = (
                counts.get((edge_id, proposal.layer), 0) + 1
            )
    return sum(
        counts.get((edge, left), 0) * counts.get((edge, right), 0)
        for edge in {key[0] for key in counts}
        for left, right in (
            ("application", "transport"),
            ("transport", "network"),
            ("network", "physical"),
        )
    )


def _no_partial_commit(
    controller,
    task_id: str,
    involved_gateways,
    old_version: int,
    state_version: int,
    committed: bool,
) -> bool:
    gateways = tuple(
        controller.gateways[gateway_id]
        for gateway_id in sorted(involved_gateways)
    )
    stable_versions = {gateway.get_stable_version(task_id) for gateway in gateways}
    staged_empty = all(not gateway.get_staged_rules(task_id) for gateway in gateways)
    expected = state_version if committed else old_version
    return stable_versions == {expected} and staged_empty


def _rejection_reason(
    snapshot: RobustnessScenarioSnapshot,
    coordination: CoordinationResult,
) -> str:
    if coordination.details.get("safe_rejection"):
        return "safe_rejection:missing_layer_observation"
    if snapshot.metadata.get("stale_ms", 0) > 0:
        return "safe_rejection:stale_state"
    if snapshot.conflict_class == UNRESOLVABLE_CONFLICT:
        return "safe_rejection:no_feasible_combination"
    return "safe_rejection:no_selected_combination"


def _run_id(snapshot: RobustnessScenarioSnapshot, method: str) -> str:
    factor = (
        snapshot.metadata.get("noise_ratio")
        or snapshot.metadata.get("stale_ms")
        or snapshot.metadata.get("missing_layer")
        or snapshot.metadata.get("proposals_per_layer")
        or "base"
    )
    return (
        f"{snapshot.experiment}:{snapshot.scenario}:factor={factor}:"
        f"seed={snapshot.seed}:method={method}"
    )


def _failed_metric(
    snapshot: RobustnessScenarioSnapshot,
    method: str,
    error: Exception,
) -> RobustnessRunMetrics:
    return RobustnessRunMetrics(
        run_id=_run_id(snapshot, method),
        seed=snapshot.seed,
        experiment=snapshot.experiment,
        scenario=snapshot.scenario,
        method=method,
        conflict_pressure=snapshot.pressure,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_control_plane_simulation",
        conflict_class=snapshot.conflict_class,
        ground_truth_conflict=snapshot.ground_truth.ground_truth_conflict,
        ground_truth_resolvable=snapshot.ground_truth.ground_truth_resolvable,
        ground_truth_feasible_combinations=len(snapshot.ground_truth.feasible_combinations),
        ground_truth_uses_true_state=True,
        noise_ratio=float(snapshot.metadata.get("noise_ratio", 0.0)),
        stale_ms=int(snapshot.metadata.get("stale_ms", 0)),
        missing_layer=str(snapshot.metadata.get("missing_layer", "")),
        proposals_per_layer=int(snapshot.metadata.get("proposals_per_layer", 0)),
        num_proposals=len(snapshot.proposals),
        num_layers_observed=len({proposal.layer for proposal in snapshot.proposals}),
        num_raw_combinations=_raw_combination_count(snapshot.proposals),
        num_pruned_combinations=0,
        num_evaluated_combinations=0,
        num_feasible_combinations=0,
        combination_evaluation_mode="runner_error",
        selected_proposal_ids="",
        selected_action_set="",
        rejected_proposal_ids="",
        pre_execution_feasibility_checked=False,
        post_execution_verification_result="RUNNER_ERROR",
        conflict_detected=False,
        conflict_resolved=False,
        qos_satisfied=False,
        infeasible_configuration=False,
        decision_rejected=False,
        safe_rejection=False,
        unsafe_execution=False,
        transaction_attempted=False,
        rollback_triggered=False,
        rollback_success=False,
        no_partial_commit=False,
        stable_version_before=0,
        stable_version_after=0,
        proposal_generated_version=int(snapshot.metadata.get("proposal_generated_version", 1)),
        execution_version=int(snapshot.metadata.get("execution_version", 1)),
        read_set_version=int(snapshot.metadata.get("read_set_version", 1)),
        stale_state_detected=False,
        stale_proposal_rejected=False,
        proposal_regenerated=False,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        transaction_latency_ms=0.0,
        total_latency_ms=0.0,
        peak_memory_mb=0.0,
        timeout=False,
        control_messages=0,
        control_bytes=0,
        success=False,
        failure_reason=f"runner_error:{type(error).__name__}:{error}",
    )


def _write_main_scenario_audit(config: dict[str, Any], raw_dir: Path) -> None:
    audit = config.get("scenario_audit", {})
    source = Path(audit.get("source_runs", "results/exp2/raw/runs.csv"))
    proposal_source = Path(
        audit.get("source_proposals", "results/exp2/raw/proposals.jsonl")
    )
    actions: dict[tuple[str, str], dict[str, str]] = {}
    if proposal_source.exists():
        with proposal_source.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                proposal = row.get("proposal", {})
                actions.setdefault(
                    (str(row.get("run_id")), str(row.get("method"))),
                    {},
                )[str(proposal.get("proposal_id"))] = str(proposal.get("action"))
    with source.open(encoding="utf-8", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    output = []
    for row in source_rows:
        if ":pressure=" not in row["scenario"]:
            continue
        family, pressure = row["scenario"].split(":pressure=", 1)
        if family not in {
            "application_capacity",
            "transport_network",
            "network_physical",
        }:
            continue
        selected_ids = [value for value in row["selected_proposals"].split(";") if value]
        action_map = actions.get((row["run_id"], row["method"]), {})
        output.append(
            {
                "method": row["method"],
                "scenario": family,
                "conflict_pressure": pressure,
                "seed": row["seed"],
                "ground_truth_conflict": row["ground_truth_conflict"],
                "ground_truth_resolvable": row["ground_truth_resolvable"],
                "conflict_class": (
                    "NO_CONFLICT"
                    if row["ground_truth_conflict"] == "False"
                    else "RESOLVABLE_CONFLICT"
                    if row["ground_truth_resolvable"] == "True"
                    else "UNRESOLVABLE_CONFLICT"
                ),
                "selected_proposals": row["selected_proposals"],
                "selected_action_set": ";".join(
                    action_map.get(proposal_id, "UNKNOWN")
                    for proposal_id in selected_ids
                ),
                "qos_satisfied": row["qos_satisfied"],
                "infeasible_configuration": row["infeasible_configuration"],
                "conflict_detected": row["conflict_detected"],
                "conflict_resolved": row["conflict_resolved"],
                "safe_rejection": False,
                "rollback_triggered": row["rollback_triggered"],
                "coordination_latency_ms": row["coordination_latency_ms"],
                "transaction_latency_ms": row["transaction_latency_ms"],
            }
        )
    path = raw_dir / "main_scenario_audit.csv"
    fields = list(output[0]) if output else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)


def _scenario_config(config: dict[str, Any]) -> ConflictScenarioConfig:
    task = config.get("task", {})
    topology = config.get("topology", {})
    return ConflictScenarioConfig(
        num_agents=int(task.get("num_agents", 10)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", 4)),
        cross_gateway_edge_ratio=float(
            task.get("cross_gateway_edge_ratio", 0.5)
        ),
        multi_hop=bool(topology.get("multi_hop", True)),
    )


def parse_seeds(specification: str) -> tuple[int, ...]:
    if ":" in specification:
        start, end = specification.split(":", 1)
        return tuple(range(int(start), int(end) + 1))
    return tuple(int(value) for value in specification.split(",") if value)


def _artifact_hashes(directory: Path) -> dict[str, str]:
    if not directory.exists():
        raise FileNotFoundError(directory)
    output = {}
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        output[str(path.relative_to(directory))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Experiment 2 robustness and non-triviality audit"
    )
    parser.add_argument(
        "--config",
        default="configs/exp2_cross_layer_robustness.yaml",
    )
    parser.add_argument("--seeds", default="0:29")
    parser.add_argument("--output-dir", default="results/exp2_robustness")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    rows = asyncio.run(
        run_experiment(config, parse_seeds(args.seeds), Path(args.output_dir))
    )
    print(f"Wrote {len(rows)} robustness runs to {args.output_dir}")


if __name__ == "__main__":
    main()
