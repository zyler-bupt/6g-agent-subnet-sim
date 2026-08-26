from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import yaml


EXP1_FORMAL_TASK_SIZES = (4, 8, 12, 16, 20)
EXP1_FORMAL_SEEDS = tuple(range(50))
EXP1_FORMAL_METHODS = ("proposed", "cspf", "global_sfc_embedding")


def exp1_canonical_grid_errors(rows: list[dict[str, str]]) -> list[str]:
    expected = {
        (num_agents, seed, method)
        for num_agents in EXP1_FORMAL_TASK_SIZES
        for seed in EXP1_FORMAL_SEEDS
        for method in EXP1_FORMAL_METHODS
    }
    observed: list[tuple[int, int, str]] = []
    try:
        observed = [
            (
                int(str(row.get("num_agents", "")).strip()),
                int(row.get("seed", "")),
                row.get("method_id", ""),
            )
            for row in rows
        ]
    except (TypeError, ValueError):
        return ["Exp1 canonical grid has an invalid task size or seed"]
    errors: list[str] = []
    if len(observed) != len(expected):
        errors.append(
            f"Exp1 canonical grid must contain exactly {len(expected)} rows; "
            f"got {len(observed)}"
        )
    observed_set = set(observed)
    if observed_set != expected:
        errors.append(
            "Exp1 canonical grid does not match 5 task sizes x 50 seeds x 3 methods"
        )
    if len(observed) != len(observed_set):
        errors.append("Exp1 canonical grid contains duplicate method trials")
    return errors


def _configuration_sha256(config_path: Path) -> str:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize(
    source: Path,
    target: Path,
    config_path: Path = Path("configs/exp1_netns_verified_formation_v3.yaml"),
    *,
    require_complete_grid: bool = True,
) -> list[dict[str, str]]:
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("Exp1 canonical input is empty")
    required = {"proposed", "cspf", "global_sfc_embedding"}
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row.get("result_mode") != "real_linux_netns_veth_tc_data_plane":
            raise ValueError("Exp1 canonical input must be measured Linux netns data")
        row["protocol_id"] = "wcnc_final_v3"
        row["execution_mode_detail"] = row["result_mode"]
        row["result_mode"] = "measured_netns"
        row["failure_reason"] = row.get("failure_reason", "")
        row["timeout"] = str("timeout" in row["failure_reason"].lower()).lower()
        grouped.setdefault((row["scenario_fingerprint"], row["seed"]), set()).add(row["method_id"])
    expected_configuration_hash = _configuration_sha256(config_path)
    observed_configuration_hashes = {
        row.get("configuration_sha256", "") for row in rows
    }
    if observed_configuration_hashes != {expected_configuration_hash}:
        raise ValueError(
            "Exp1 configuration hash does not match the canonical v3 config"
        )
    if any(methods != required for methods in grouped.values()):
        raise ValueError("Exp1 paired scenario is missing a canonical method")
    if require_complete_grid:
        grid_errors = exp1_canonical_grid_errors(rows)
        if grid_errors:
            raise ValueError("; ".join(grid_errors))
    preparation_failed_groups = {
        (row["scenario_fingerprint"], row["seed"])
        for row in rows
        if row.get("failure_stage") == "PREPARATION"
    }
    for row in rows:
        group = (row["scenario_fingerprint"], row["seed"])
        row["infrastructure_valid"] = str(
            row.get("failure_stage") != "PREPARATION"
        ).lower()
        row["paired_analysis_eligible"] = str(
            group not in preparation_failed_groups
        ).lower()
        row["paired_exclusion_reason"] = (
            "paired_preparation_failure"
            if group in preparation_failed_groups
            else ""
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", required=True); parser.add_argument("--target", default="results/paper/wcnc_final_v3/raw/exp1/trials.csv"); parser.add_argument("--config", default="configs/exp1_netns_verified_formation_v3.yaml")
    args = parser.parse_args(); normalize(Path(args.source), Path(args.target), Path(args.config))


if __name__ == "__main__": main()
