from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from experiments.exp2_cross_layer_robustness import run_case
from experiments.exp3_business_elasticity import run_exp3
from experiments.exp4_failure import run_exp4
from experiments.paper_protocol import PROTOCOL_ID, load_and_validate_wcnc_v3_config
from src.core.models import to_jsonable
from src.simulation.demand_capacity_ratio_v3 import HeterogeneousDemandCapacityGenerator
from src.simulation.demand_capacity_truth import evaluate_truth


ROOT = Path("results/paper/wcnc_final_v3")
EXP2_ENGINE = {
    "proposed": "proposed",
    "sanet_dw": "sanet_dw",
    "weighted_sum": "weighted_sum",
    "independent": "independent",
}


def fingerprint(value: object) -> str:
    encoded = json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def run_exp2_canonical(
    output_root: Path, seeds: Iterable[int], gamma_grid: Iterable[float],
    *, max_combinations: int, timeout_ms: float,
) -> list[dict[str, object]]:
    raw_dir = output_root / "raw" / "exp2"
    raw_dir.mkdir(parents=True, exist_ok=True)
    generator = HeterogeneousDemandCapacityGenerator()
    rows: list[dict[str, object]] = []
    scenarios: dict[str, dict[str, object]] = {}
    run_index = 0
    for gamma in gamma_grid:
        for seed in seeds:
            snapshot = generator.generate(float(gamma), int(seed))
            observation_fingerprint = fingerprint(snapshot.observed_state)
            oracle_fingerprint = fingerprint(snapshot.true_state)
            scenarios[snapshot.fingerprint] = {
                "protocol_id": PROTOCOL_ID,
                "seed": seed,
                "gamma": gamma,
                "scenario_fingerprint": snapshot.fingerprint,
                "environment_fingerprint": snapshot.metadata["environment_fingerprint"],
                "observation_fingerprint": observation_fingerprint,
                "oracle_fingerprint": oracle_fingerprint,
                "conflict_class": snapshot.conflict_class,
                "epsilon_by_flow": snapshot.metadata["epsilon_by_flow"],
            }
            for method_id, engine in EXP2_ENGINE.items():
                run_index += 1
                try:
                    metric, _events, decision = await run_case(
                        snapshot, engine, run_index=run_index,
                        coordination_timeout_ms=timeout_ms,
                        max_combinations=max_combinations if method_id == "proposed" else None,
                        post_evaluator=evaluate_truth,
                    )
                    row = {
                        **asdict(metric),
                        "protocol_id": PROTOCOL_ID,
                        "execution_mode_detail": metric.result_mode,
                        "result_mode": "transactional_simulation",
                        "method_id": method_id,
                        "gamma": gamma,
                        "environment_fingerprint": snapshot.metadata["environment_fingerprint"],
                        "observation_fingerprint": observation_fingerprint,
                        "oracle_fingerprint": oracle_fingerprint,
                        "pre_verification_correct_decision": decision["pre_verification_correct_decision"],
                        "pre_verification_feasible": decision["pre_verification_feasible"],
                        "unsafe_proposal_before_verification": decision["unsafe_proposal_before_verification"],
                        "verification_rescued": decision["verification_rescued"],
                        "search_timeout": bool(metric.timeout or decision.get("coordination_details", {}).get("search_timed_out")),
                    }
                except Exception as error:
                    row = {
                        "protocol_id": PROTOCOL_ID, "seed": seed, "gamma": gamma,
                        "method_id": method_id, "scenario_fingerprint": snapshot.fingerprint,
                        "environment_fingerprint": snapshot.metadata["environment_fingerprint"],
                        "observation_fingerprint": observation_fingerprint,
                        "oracle_fingerprint": oracle_fingerprint,
                        "result_mode": "transactional_simulation", "success": False,
                        "timeout": isinstance(error, TimeoutError),
                        "failure_reason": f"{type(error).__name__}:{error}",
                    }
                rows.append(row)
    _write_csv(raw_dir / "trials.csv", rows)
    _write_jsonl(raw_dir / "scenarios.jsonl", scenarios.values())
    return rows


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def normalize_paper_trials(source: Path, target: Path) -> None:
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["protocol_id"] = PROTOCOL_ID
        row["execution_mode_detail"] = row.get("result_mode", "")
        row["result_mode"] = "transactional_simulation"
        row["failure_reason"] = row.get("failure_reason", "")
        row["timeout"] = str("timeout" in row["failure_reason"].lower()).lower()
    _write_csv(target, rows)


def parse_seeds(value: str) -> tuple[int, ...]:
    start, end = (int(part) for part in value.split(":", 1))
    return tuple(range(start, end + 1))


async def main_async(args: argparse.Namespace) -> None:
    config = load_and_validate_wcnc_v3_config(args.config)
    root = Path(args.output_root)
    seeds = parse_seeds(args.seeds)
    experiments = ("exp2", "exp3", "exp4") if args.experiment == "all" else (args.experiment,)
    if "exp1" in experiments:
        raise RuntimeError("Exp1 canonical formal data must be produced by the measured-netns runner")
    if "exp2" in experiments:
        await run_exp2_canonical(root, seeds, config["exp2"]["gamma"],
            max_combinations=int(config["exp2"]["max_combinations"]),
            timeout_ms=float(config["exp2"]["coordination_timeout_ms"]))
    mode = "pilot" if seeds and seeds[0] >= 9000 else "paper"
    if "exp3" in experiments:
        await run_exp3(mode, root, seeds=seeds, buckets=tuple(config["exp3"]["affected_dependency_scope_percent"]))
        source = root / "raw" / mode / "exp3" / "trials.csv"
        target = root / "raw" / "exp3" / "trials.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        normalize_paper_trials(source, target)
    if "exp4" in experiments:
        await run_exp4(mode, root, seeds=seeds, event_ids=(0,),
            link_ratios=tuple(config["exp4"]["link_affected_flow_ratio"]),
            agent_ratios=tuple(config["exp4"]["agent_dependency_closure_ratio"]),
            capacity_ratios=tuple(config["exp4"]["capacity_ratio"]))
        source = root / "raw" / mode / "exp4" / "trials.csv"
        target = root / "raw" / "exp4" / "trials.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        normalize_paper_trials(source, target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/wcnc_final_v3.yaml")
    parser.add_argument("--output-root", default=str(ROOT))
    parser.add_argument("--experiment", choices=("all", "exp1", "exp2", "exp3", "exp4"), default="all")
    parser.add_argument("--seeds", default="9000:9019")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
