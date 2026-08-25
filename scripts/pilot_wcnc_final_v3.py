from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import load_and_validate_wcnc_v3_config
from src.simulation.demand_capacity_ratio_v3 import HeterogeneousDemandCapacityGenerator
from src.simulation.paper_failure_scenarios import generate_paper_failure_snapshot


def parse_seeds(value: str) -> tuple[int, ...]:
    start, end = (int(part) for part in value.split(":", 1)); return tuple(range(start, end + 1))


def run(output: Path) -> dict[str, object]:
    config = load_and_validate_wcnc_v3_config(); seeds = parse_seeds(config["pilot"]["seeds"])
    generator = HeterogeneousDemandCapacityGenerator(); distributions = {}; durations = {}
    for gamma in config["exp2"]["gamma"]:
        started = time.perf_counter(); labels = [generator.generate(float(gamma), seed).conflict_class for seed in seeds]
        distributions[str(gamma)] = dict(sorted(Counter(labels).items()))
        durations[str(gamma)] = time.perf_counter() - started
    required_netns = ("ip", "tc", "unshare", "nsenter", "ping", "iperf3")
    exp4_actual = {}
    for failure_type, config_key in (("link_failure", "link_affected_flow_ratio"), ("agent_failure", "agent_dependency_closure_ratio"), ("capacity_degradation", "capacity_ratio")):
        for requested in config["exp4"][config_key]:
            snapshots = [generate_paper_failure_snapshot(
                failure_type, float(requested), seed, 0,
                capacity_ratio=(float(requested) if failure_type == "capacity_degradation" else None),
            ) for seed in seeds]
            values = [
                snapshot.affected_flow_ratio if failure_type == "link_failure"
                else snapshot.dependency_closure_ratio if failure_type == "agent_failure"
                else snapshot.post_fault_capacity_ratio
                for snapshot in snapshots
            ]
            exp4_actual[f"{failure_type}:{requested}"] = {
                "min": min(values), "mean": sum(values) / len(values), "max": max(values)
            }
    report = {
        "protocol_id": "wcnc_final_v3", "pilot_seed_range": config["pilot"]["seeds"],
        "selection_inputs": ["ground_truth_class", "runtime", "exceptions"],
        "method_outcomes_inspected": False, "gamma_grid_frozen_before_pilot": config["exp2"]["gamma"],
        "exp2_ground_truth_distribution": distributions, "exp2_generation_seconds": durations,
        "exp3_scope_grid": config["exp3"]["affected_dependency_scope_percent"],
        "exp4_severity_grids": {key: value for key, value in config["exp4"].items() if isinstance(value, list)},
        "exp4_actual_ground_truth_ranges": exp4_actual,
        "exp1_netns_dependencies": {name: bool(shutil.which(name)) for name in required_netns},
    }
    output.parent.mkdir(parents=True, exist_ok=True); output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = ["# WCNC final v3 pilot ground-truth report", "", f"Pilot seeds: `{config['pilot']['seeds']}`.", "", "Only oracle classes, runtime, and exceptions were inspected; no method outcomes were used to select points.", "", "| gamma | class counts | generation seconds |", "|---:|---|---:|"]
    for gamma in config["exp2"]["gamma"]:
        key = str(gamma); markdown.append(f"| {gamma:g} | `{json.dumps(distributions[key], sort_keys=True)}` | {durations[key]:.3f} |")
    output.with_suffix(".md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--output", default="results/paper/wcnc_final_v3/pilot/ground_truth_distribution.json")
    print(json.dumps(run(Path(parser.parse_args().output))["exp2_ground_truth_distribution"], indent=2, sort_keys=True))


if __name__ == "__main__": main()
