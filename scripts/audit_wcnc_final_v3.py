from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import (
    EXPERIMENT_FAILURE_METHODS, EXPERIMENT_METHODS, METHODS, PROTOCOL_ID,
    load_and_validate_wcnc_v3_config,
)
from scripts.normalize_wcnc_final_v3_exp1 import exp1_canonical_grid_errors


SCOPED_SOURCES = (
    "experiments/paper_protocol.py", "experiments/run_wcnc_final_v3.py",
    "experiments/exp1_netns_verified_formation.py", "experiments/exp2_cross_layer_robustness.py",
    "experiments/exp3_business_elasticity.py", "experiments/exp4_failure.py",
    "src/controller/cross_layer_coordinator.py", "src/controller/business_reconfiguration.py",
    "src/controller/paper_failure_recovery.py", "src/simulation/demand_capacity_ratio.py",
    "src/simulation/demand_capacity_ratio_v3.py",
    "src/simulation/paper_failure_scenarios.py",
    "scripts/aggregate_wcnc_final_v3.py", "scripts/plot_wcnc_final_v3.py",
    "scripts/normalize_wcnc_final_v3_exp1.py",
    "scripts/run_wcnc_final_v3_remote.sh",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configuration_sha(path: Path) -> str:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truth(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def exp1_paired_eligibility_errors(rows: list[dict[str, str]]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("scenario_fingerprint", ""), row.get("seed", ""))].append(row)
    errors: list[str] = []
    for (fingerprint, seed), items in grouped.items():
        expected_methods = {"proposed", "cspf", "global_sfc_embedding"}
        observed_methods = [row.get("method_id", "") for row in items]
        if len(observed_methods) != len(expected_methods) or set(observed_methods) != expected_methods:
            errors.append(
                f"Exp1 canonical method set is incomplete or duplicated for "
                f"{fingerprint}:{seed}"
            )
        preparation_failed = any(
            row.get("failure_stage") == "PREPARATION" for row in items
        )
        expected_eligible = not preparation_failed
        expected_reason = "paired_preparation_failure" if preparation_failed else ""
        for row in items:
            infrastructure_valid = row.get("failure_stage") != "PREPARATION"
            if _truth(row.get("infrastructure_valid")) != infrastructure_valid:
                errors.append(
                    f"Exp1 infrastructure validity is inconsistent for "
                    f"{fingerprint}:{seed}:{row.get('method_id', '')}"
                )
            if (
                _truth(row.get("paired_analysis_eligible")) != expected_eligible
                or row.get("paired_exclusion_reason", "") != expected_reason
            ):
                errors.append(
                    f"Exp1 paired eligibility is inconsistent for "
                    f"{fingerprint}:{seed}:{row.get('method_id', '')}"
                )
    return errors


def build_manifest(root: Path, repo: Path = Path(".")) -> dict[str, object]:
    config_path = repo / "configs/wcnc_final_v3.yaml"; config = load_and_validate_wcnc_v3_config(config_path)
    raw_hashes = {str(path.relative_to(root)): sha(path) for path in sorted((root / "raw").glob("**/*")) if path.is_file()}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()
    return {
        "protocol_id": PROTOCOL_ID, "git_commit": commit,
        "source_hashes": {name: sha(repo / name) for name in SCOPED_SOURCES},
        "config_hashes": {
            str(path): sha(path)
            for path in (
                config_path,
                repo / "configs/exp1_netns_verified_formation_v3.yaml",
                repo / "configs/exp1_netns_verified_formation_pilot_v3.yaml",
                repo / "configs/wcnc_final_v3_raw_schema.json",
            )
        },
        "baseline_method_set": {"exp1": list(EXPERIMENT_METHODS["exp1"]), "exp2": list(EXPERIMENT_METHODS["exp2"]), "exp3": list(EXPERIMENT_METHODS["exp3"]), "exp4": EXPERIMENT_FAILURE_METHODS["exp4"]},
        "baseline_display_labels": {key: value.label for key, value in METHODS.items()},
        "adapted_references": {key: {"adapted": value.adapted, "reference": value.reference, "note": value.why_included} for key, value in METHODS.items() if value.adapted},
        "seed_ranges": config["formal"], "environment": {"python": platform.python_version(), "platform": platform.platform(),
            "dependencies": {name: importlib.metadata.version(name) for name in ("numpy", "matplotlib", "PyYAML")}},
        "state_schema_versions": {"true_state": "exp2-true-v1", "observed_state": "exp2-observed-v1"},
        "result_modes": {exp: config[exp]["result_mode"] for exp in ("exp1", "exp2", "exp3", "exp4")},
        "raw_artifact_hashes": raw_hashes,
        "aggregation_rule": config["statistics"],
        "plotting_script_hash": sha(repo / "scripts/plot_wcnc_final_v3.py"),
    }


def audit(root: Path, manifest: dict[str, object]) -> dict[str, object]:
    checks: dict[str, object] = {}; errors: list[str] = []
    if "git_commit" in manifest:
        current_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        commit_match = current_commit == manifest["git_commit"]
        checks["git_commit_matches"] = commit_match
        if not commit_match:
            errors.append("git commit drift")
    current_raw = {str(path.relative_to(root)): sha(path) for path in sorted((root / "raw").glob("**/*")) if path.is_file()}
    checks["raw_hashes_match"] = current_raw == manifest["raw_artifact_hashes"]
    if not checks["raw_hashes_match"]: errors.append("raw artifact drift")
    if "source_hashes" in manifest:
        source_match = all(Path(name).is_file() and sha(Path(name)) == expected for name, expected in manifest["source_hashes"].items())
        checks["source_hashes_match"] = source_match
        if not source_match: errors.append("scoped source drift")
    if "config_hashes" in manifest:
        config_match = all(Path(name).is_file() and sha(Path(name)) == expected for name, expected in manifest["config_hashes"].items())
        checks["config_hashes_match"] = config_match
        if not config_match: errors.append("config drift")
    if "plotting_script_hash" in manifest:
        plot_match = sha(Path("scripts/plot_wcnc_final_v3.py")) == manifest["plotting_script_hash"]
        checks["plotting_script_hash_matches"] = plot_match
        if not plot_match: errors.append("plotting source drift")
    for exp in ("exp1", "exp2", "exp3", "exp4"):
        path = root / "raw" / exp / "trials.csv"
        if not path.exists(): errors.append(f"missing {path}"); continue
        with path.open(encoding="utf-8", newline="") as handle: rows = list(csv.DictReader(handle))
        grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for row in rows:
            key = (
                row.get("scenario_fingerprint", row.get("trial_id", "")),
                row.get("seed", ""),
                row.get("failure_type", "") if exp == "exp4" else "",
            )
            grouped[key].append(row.get("method_id") or row.get("method") or "")
        valid = True
        for (_, _, failure_type), methods in grouped.items():
            expected = set(
                EXPERIMENT_FAILURE_METHODS["exp4"][failure_type]
                if exp == "exp4"
                else EXPERIMENT_METHODS[exp]
            )
            if len(methods) != len(expected) or set(methods) != expected:
                valid = False
                break
        checks[f"{exp}_paired_method_set"] = valid
        if not valid: errors.append(f"{exp} method/fingerprint pairing drift")
        checks[f"{exp}_failure_rows_retained"] = all("success" in row and "failure_reason" in row for row in rows)
        if any("demo" in value.lower() or "synthetic" in value.lower() for row in rows for value in row.values()):
            errors.append(f"{exp} contains demo/synthetic provenance")
        if exp == "exp1":
            grid_errors = exp1_canonical_grid_errors(rows)
            grid_complete = not grid_errors
            checks["exp1_canonical_grid_complete"] = grid_complete
            errors.extend(grid_errors)
            expected_configuration_hash = configuration_sha(
                Path("configs/exp1_netns_verified_formation_v3.yaml")
            )
            observed_configuration_hashes = {
                row.get("configuration_sha256", "") for row in rows
            }
            configuration_match = observed_configuration_hashes == {
                expected_configuration_hash
            }
            checks["exp1_configuration_hash_matches"] = configuration_match
            if not configuration_match:
                errors.append("Exp1 configuration hash drift")
            eligibility_errors = exp1_paired_eligibility_errors(rows)
            eligibility_match = not eligibility_errors
            checks["exp1_paired_analysis_eligibility_consistent"] = eligibility_match
            errors.extend(eligibility_errors)
    for exp in ("exp1", "exp2", "exp3", "exp4"):
        aggregated = root / "aggregated" / exp / "metrics.csv"
        if aggregated.exists():
            with aggregated.open(encoding="utf-8", newline="") as handle: rows = list(csv.DictReader(handle))
            checks[f"{exp}_all_points_have_raw_provenance"] = all(row.get("raw_path") and row.get("raw_sha256") for row in rows)
    status = "PASS" if not errors and all(value is True for value in checks.values()) else "FAIL"
    return {"protocol_id": PROTOCOL_ID, "status": status, "checks": checks, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="results/paper/wcnc_final_v3"); parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args(); root = Path(args.root)
    if args.write_manifest:
        manifest = build_manifest(root); (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    report = audit(root, manifest); (root / "integrity_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report["status"] != "PASS": raise SystemExit(json.dumps(report["errors"]))


if __name__ == "__main__": main()
