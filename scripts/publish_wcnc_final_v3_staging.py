#!/usr/bin/env python3
"""Validate staged Exp2--Exp4 grids and publish them without overwrites."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from experiments.paper_protocol import PROTOCOL_ID, load_and_validate_wcnc_v3_config


def _number(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric grid value: {value!r}") from error


def _point(row: dict[str, str], experiment: str) -> float:
    if experiment == "exp2":
        return _number(row.get("gamma"))
    if experiment == "exp3":
        return _number(row.get("affected_scope_bucket_percent"))
    failure_type = row.get("failure_type", "")
    field = {
        "link_failure": "affected_flow_ratio",
        "agent_failure": "dependency_closure_ratio",
        "capacity_degradation": "post_fault_capacity_ratio",
    }.get(failure_type)
    return _number(row.get(field, row.get("failure_severity")))


def _expected_grid(config: dict[str, object], experiment: str, seeds: tuple[int, ...]) -> dict[tuple[str, float, int, int], set[str]]:
    expected: dict[tuple[str, float, int, int], set[str]] = {}
    if experiment == "exp2":
        for point in config["exp2"]["gamma"]:
            for seed in seeds:
                expected[("", float(point), seed, 0)] = set(config["exp2"]["methods"])
    elif experiment == "exp3":
        for point in config["exp3"]["affected_dependency_scope_percent"]:
            for seed in seeds:
                for event_id in range(5):
                    expected[("", float(point), seed, event_id)] = set(config["exp3"]["methods"])
    elif experiment == "exp4":
        specs = (
            ("link_failure", "link_affected_flow_ratio"),
            ("agent_failure", "agent_dependency_closure_ratio"),
            ("capacity_degradation", "capacity_ratio"),
        )
        for failure_type, field in specs:
            for point in config["exp4"][field]:
                for seed in seeds:
                    expected[(failure_type, float(point), seed, 0)] = set(
                        config["exp4"]["methods_by_failure"][failure_type]
                    )
    else:
        raise ValueError(f"unsupported staged experiment: {experiment}")
    return expected


def validate_staged_experiment(
    raw_dir: Path,
    experiment: str,
    *,
    seeds: Iterable[int],
    config_path: Path = Path("configs/wcnc_final_v3.yaml"),
) -> None:
    """Fail unless ``raw_dir`` is the exact frozen, paired invocation grid."""
    trials = raw_dir / "trials.csv"
    commit_path = raw_dir / "execution_commit.txt"
    if not trials.is_file() or not commit_path.is_file():
        raise ValueError("staged raw lacks trials.csv or execution_commit.txt")
    commit = commit_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("staged execution commit is not a 40-hex commit")
    with trials.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    expected = _expected_grid(
        load_and_validate_wcnc_v3_config(config_path), experiment,
        tuple(int(seed) for seed in seeds),
    )
    observed: dict[tuple[str, float, int, int], list[str]] = defaultdict(list)
    fingerprints: dict[tuple[str, float, int, int], set[str]] = defaultdict(set)
    for row in rows:
        required = ("protocol_id", "seed", "method_id", "scenario_fingerprint", "result_mode", "success", "timeout", "failure_reason")
        if any(name not in row for name in required):
            raise ValueError("staged row schema is incomplete")
        if row["protocol_id"] != PROTOCOL_ID or row["result_mode"] != "transactional_simulation":
            raise ValueError("staged row protocol/result mode drift")
        key = (
            row.get("failure_type", "") if experiment == "exp4" else "",
            _point(row, experiment), int(row["seed"]), int(row.get("event_id") or 0),
        )
        observed[key].append(row["method_id"])
        fingerprints[key].add(row["scenario_fingerprint"])
    if set(observed) != set(expected):
        raise ValueError("staged invocation grid is incomplete or has unexpected rows")
    for key, methods in observed.items():
        if len(methods) != len(set(methods)):
            raise ValueError(f"duplicate method row in staged grid: {key}")
        if set(methods) != expected[key]:
            raise ValueError(f"staged method grid drift: {key}")
        if len(fingerprints[key]) != 1:
            raise ValueError(f"methods do not share one scenario fingerprint: {key}")


def publish_immutable_tree(source: Path, target: Path) -> None:
    """Preflight an entire tree, then copy only missing files."""
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"empty publication source: {source}")
    for path in files:
        destination = target / path.relative_to(source)
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise ValueError(f"refusing to overwrite immutable canonical artifact: {destination}")
    for path in files:
        destination = target / path.relative_to(source)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--experiment", choices=("exp2", "exp3", "exp4"), required=True)
    parser.add_argument("--seeds", default="0:99")
    parser.add_argument("--config", type=Path, default=Path("configs/wcnc_final_v3.yaml"))
    args = parser.parse_args()
    start, end = (int(value) for value in args.seeds.split(":", 1))
    validate_staged_experiment(
        args.source, args.experiment, seeds=range(start, end + 1), config_path=args.config,
    )
    publish_immutable_tree(args.source, args.target)


if __name__ == "__main__":
    main()
