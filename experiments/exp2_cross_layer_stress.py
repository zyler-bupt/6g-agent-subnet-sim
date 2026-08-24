from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

import yaml

from experiments.exp2_cross_layer_robustness import run_case
from src.metrics.exp2_robustness import RobustnessRunMetrics, write_jsonl, write_robustness_csv
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig
from src.simulation.conflict_stress import CrossLayerStressGenerator


METHOD_TO_ENGINE = {
    "ours": "proposed",
    "alc": "adjacent",
    "layer_wise_independent": "independent",
    "no_global_verification": "no_verification",
}


async def run_experiment(
    config: dict[str, Any],
    *,
    methods: Sequence[str],
    seeds: Sequence[int],
    output_dir: Path,
) -> list[RobustnessRunMetrics]:
    if output_dir.resolve() in {
        Path("results/exp2").resolve(),
        Path("results/exp2_robustness").resolve(),
    }:
        raise ValueError("stress experiment must not overwrite existing Exp2 results")
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    task = config.get("task", {})
    scenario_config = ConflictScenarioConfig(
        num_agents=int(task.get("num_agents", 10)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", 4)),
        cross_gateway_edge_ratio=float(task.get("cross_gateway_edge_ratio", 0.5)),
        multi_hop=True,
    )
    timeout = float(config.get("simulation", {}).get("coordination_timeout_ms", 1000))
    intensities = tuple(float(value) for value in config["stress"]["intensities"])
    generator = CrossLayerStressGenerator()
    rows: list[RobustnessRunMetrics] = []
    events: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    scenarios: list[dict[str, object]] = []
    run_index = 0
    for intensity in intensities:
        for seed in seeds:
            snapshot = generator.generate(intensity, seed, scenario_config)
            scenarios.append(snapshot.to_dict())
            for method in methods:
                if method not in METHOD_TO_ENGINE:
                    raise ValueError(f"unknown stress method: {method}")
                run_index += 1
                engine_method = METHOD_TO_ENGINE[method]
                try:
                    metric, run_events, decision = await run_case(
                        snapshot,
                        engine_method,
                        run_index=run_index,
                        coordination_timeout_ms=timeout,
                    )
                    run_id = f"rho={intensity:g}:seed={seed}:method={method}"
                    metric = replace(metric, run_id=run_id, method=method)
                    for event in run_events:
                        event["experiment_run_id"] = run_id
                        event["method"] = method
                    decision["run_id"] = run_id
                    decision["method"] = method
                except Exception as error:
                    metric = _failed_metric(snapshot, method, error)
                    run_events = []
                    decision = {**asdict(metric), "runner_error": repr(error)}
                rows.append(metric)
                events.extend(run_events)
                decisions.append(decision)
    write_robustness_csv(raw_dir / "runs.csv", rows)
    write_jsonl(raw_dir / "events.jsonl", events)
    write_jsonl(raw_dir / "decisions.jsonl", decisions)
    unique = {row["scenario_fingerprint"]: row for row in scenarios}
    write_jsonl(raw_dir / "scenarios.jsonl", unique.values())
    _write_class_counts(raw_dir / "ground_truth_class_counts.csv", rows)
    return rows


def _failed_metric(snapshot, method: str, error: Exception) -> RobustnessRunMetrics:
    values: dict[str, object] = {field: 0 for field in RobustnessRunMetrics.__dataclass_fields__}
    values.update(
        run_id=f"rho={snapshot.pressure:g}:seed={snapshot.seed}:method={method}",
        seed=snapshot.seed,
        experiment="cross_layer_stress",
        scenario="cross_layer_stress",
        method=method,
        conflict_pressure=snapshot.pressure,
        scenario_fingerprint=snapshot.fingerprint,
        result_mode="in_memory_transactional_control_plane_stress_simulation",
        conflict_class=snapshot.conflict_class,
        ground_truth_conflict=snapshot.ground_truth.ground_truth_conflict,
        ground_truth_resolvable=snapshot.ground_truth.ground_truth_resolvable,
        ground_truth_uses_true_state=True,
        success=False,
        qos_satisfied=False,
        failure_reason=f"runner_error:{type(error).__name__}:{error}",
    )
    return RobustnessRunMetrics(**values)


def _write_class_counts(path: Path, rows: Sequence[RobustnessRunMetrics]) -> None:
    unique = {
        (row.conflict_pressure, row.seed, row.scenario_fingerprint): row
        for row in rows
    }
    counts: dict[tuple[float, str], int] = {}
    for row in unique.values():
        key = (row.conflict_pressure, row.conflict_class)
        counts[key] = counts.get(key, 0) + 1
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("conflict_intensity", "conflict_class", "samples"), lineterminator="\n")
        writer.writeheader()
        for (intensity, conflict_class), samples in sorted(counts.items()):
            writer.writerow({"conflict_intensity": intensity, "conflict_class": conflict_class, "samples": samples})


def _parse_seeds(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        return tuple(range(start, finish + 1))
    return tuple(int(item) for item in value.split(",") if item.strip())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WCNC cross-layer conflict intensity stress test")
    parser.add_argument("--config", default="configs/exp2_cross_layer_stress.yaml")
    parser.add_argument("--methods", default=",".join(METHOD_TO_ENGINE))
    parser.add_argument("--seeds", default="0:99")
    parser.add_argument("--output-dir", default="results/exp2_stress")
    return parser


def main() -> None:
    args = _parser().parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    methods = tuple(value for value in args.methods.split(",") if value)
    rows = asyncio.run(
        run_experiment(
            config,
            methods=methods,
            seeds=_parse_seeds(args.seeds),
            output_dir=Path(args.output_dir),
        )
    )
    print(f"Exp2 stress completed: {len(rows)} runs; failures={sum(not row.success and not row.safe_rejection for row in rows)}")


if __name__ == "__main__":
    main()
