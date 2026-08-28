from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.paper_protocol import (
    EXPERIMENT_FAILURE_METHODS, EXPERIMENT_METHODS, METHODS, PROTOCOL_ID,
    load_and_validate_wcnc_v3_config, stable_fingerprint,
)
from experiments.exp1_transactional_formation import load_transactional_config
from scripts.normalize_wcnc_final_v3_exp1 import exp1_canonical_grid_errors
from scripts.normalize_wcnc_final_v3_exp1_transactional import (
    _validate_attempt_events,
    _validate_canonical_rows as validate_transactional_canonical_rows,
)
from scripts.aggregate_wcnc_final_v3 import aggregate_experiment


SCOPED_SOURCES = (
    "experiments/paper_protocol.py", "experiments/run_wcnc_final_v3.py",
    "experiments/exp1_netns_verified_formation.py", "experiments/exp2_cross_layer_robustness.py",
    "experiments/exp1_transactional_formation.py",
    "experiments/exp3_business_elasticity.py", "experiments/exp4_failure.py",
    "src/controller/cross_layer_coordinator.py", "src/controller/business_reconfiguration.py",
    "src/controller/paper_failure_recovery.py", "src/simulation/demand_capacity_ratio.py",
    "src/simulation/demand_capacity_ratio_v3.py",
    "src/simulation/paper_failure_scenarios.py",
    "scripts/aggregate_wcnc_final_v3.py", "scripts/plot_wcnc_final_v3.py",
    "scripts/normalize_wcnc_final_v3_exp1.py",
    "scripts/normalize_wcnc_final_v3_exp1_transactional.py",
    "scripts/audit_wcnc_final_v3.py",
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
    nominal_raw = root / "raw" / "exp1" / "trials.csv"
    transactional_raw = root / "raw" / "exp1_transactional" / "trials.csv"
    aggregate_hashes = {
        str(path.relative_to(root)): sha(path)
        for path in sorted((root / "aggregated").glob("**/*"))
        if path.is_file()
    } if (root / "aggregated").exists() else {}
    figure_hashes = {
        str(path.relative_to(root)): sha(path)
        for path in sorted((root / "figures").glob("**/*"))
        if path.is_file()
    } if (root / "figures").exists() else {}
    exp1_arms = {
        "nominal": {
            "raw_path": "raw/exp1/trials.csv",
            "trials_sha256": sha(nominal_raw) if nominal_raw.exists() else None,
            "config_sha256": sha(repo / "configs/exp1_netns_verified_formation_v3.yaml"),
            "schema_sha256": sha(repo / "configs/wcnc_final_v3_raw_schema.json"),
            "source_hashes": {
                name: sha(repo / name)
                for name in (
                    "experiments/exp1_netns_verified_formation.py",
                    "scripts/normalize_wcnc_final_v3_exp1.py",
                )
            },
            # Missing execution evidence stays explicit rather than inferred.
            "execution_commit_path": "raw/exp1/execution_commit.txt",
            "execution_commit": _execution_commit(root / "raw" / "exp1"),
        },
        "exp1_transactional_v1": {
            "raw_path": "raw/exp1_transactional/trials.csv",
            "trials_sha256": sha(transactional_raw) if transactional_raw.exists() else None,
            "config_sha256": sha(repo / "configs/exp1_transactional_formation_v1.yaml"),
            "schema_sha256": sha(repo / "configs/wcnc_final_v3_raw_schema.json"),
            "source_hashes": {
                name: sha(repo / name)
                for name in (
                    "experiments/exp1_transactional_formation.py",
                    "scripts/normalize_wcnc_final_v3_exp1_transactional.py",
                )
            },
            "execution_commit_path": "raw/exp1_transactional/execution_commit.txt",
            "execution_commit": _execution_commit(root / "raw" / "exp1_transactional"),
        },
    }
    return {
        "protocol_id": PROTOCOL_ID, "git_commit": commit,
        "source_hashes": {name: sha(repo / name) for name in SCOPED_SOURCES},
        "config_hashes": {
            str(path): sha(path)
            for path in (
                config_path,
                repo / "configs/exp1_netns_verified_formation_v3.yaml",
                repo / "configs/exp1_netns_verified_formation_pilot_v3.yaml",
                repo / "configs/exp1_transactional_formation_v1.yaml",
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
        "aggregate_hashes": aggregate_hashes,
        "figure_hashes": figure_hashes,
        "exp1_arms": exp1_arms,
        "aggregation_rule": config["statistics"],
        "plotting_script_hash": sha(repo / "scripts/plot_wcnc_final_v3.py"),
    }


def _execution_commit(arm_root: Path) -> str | None:
    path = arm_root / "execution_commit.txt"
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                decoded = json.loads(line)
                if not isinstance(decoded, dict):
                    raise ValueError("transactional attempt provenance has a non-object event")
                events.append(decoded)
    return events


def _transactional_audit(root: Path, errors: list[str], checks: dict[str, object]) -> None:
    raw_path = root / "raw" / "exp1_transactional" / "trials.csv"
    if not raw_path.exists():
        checks["exp1_transactional_raw_present"] = False
        errors.append("missing transactional raw arm")
        return
    rows = _read_csv(raw_path)
    normalization_manifest: dict[str, object] | None = None
    normalization_manifest_path = raw_path.with_name("normalization_manifest.json")
    try:
        loaded_manifest = json.loads(normalization_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_manifest, dict):
            raise TypeError("normalization manifest is not an object")
        normalization_manifest = loaded_manifest
        transactional_config = load_transactional_config(
            Path("configs/exp1_transactional_formation_v1.yaml")
        )
        canonical_metadata_ok = (
            normalization_manifest.get("trials_sha256") == sha(raw_path)
            and normalization_manifest.get("row_count") == 2250
            and normalization_manifest.get("configuration_sha256") == stable_fingerprint(transactional_config)
            and normalization_manifest.get("raw_schema_file_sha256") == sha(Path("configs/wcnc_final_v3_raw_schema.json"))
            and all(row.get("configuration_sha256") == stable_fingerprint(transactional_config) for row in rows)
        )
    except (OSError, TypeError, json.JSONDecodeError):
        canonical_metadata_ok = False
    checks["exp1_transactional_canonical_metadata"] = canonical_metadata_ok
    if not canonical_metadata_ok:
        errors.append("transactional canonical metadata drift")
    required_methods = {"proposed", "cspf", "global_sfc_embedding"}
    expected = {
        (scenario, size, seed, method)
        for scenario in ("stale_version", "prepare_ack_timeout", "command_rejection")
        for size in (4, 8, 12, 16, 20)
        for seed in range(50)
        for method in required_methods
    }
    try:
        observed = [
            (row["scenario_class"], int(row["num_agents"]), int(row["seed"]), row["method_id"])
            for row in rows
        ]
    except (KeyError, ValueError):
        observed = []
    grid_ok = len(observed) == 2250 and set(observed) == expected and len(set(observed)) == len(observed)
    checks["exp1_transactional_exact_2250_grid"] = grid_ok
    if not grid_ok:
        errors.append("transactional exact 2250 grid drift")
    trial_ids = [row.get("trial_id", "") for row in rows]
    run_ids = [row.get("run_id", "") for row in rows]
    identifiers_ok = all(trial_ids) and len(trial_ids) == len(set(trial_ids)) and all(run_ids) and len(run_ids) == len(set(run_ids))
    checks["exp1_transactional_unique_trial_and_run_ids"] = identifiers_ok
    if not identifiers_ok:
        errors.append("transactional trial or run identifier drift")
    pairs: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        pairs[(row.get("scenario_class", ""), row.get("num_agents", ""), row.get("seed", ""))].append(row)
    fault_ok = verifier_ok = True
    for paired_rows in pairs.values():
        methods = {row.get("method_id", "") for row in paired_rows}
        faults = {row.get("fault_schedule_fingerprint", "") for row in paired_rows}
        verifiers = {row.get("verifier_fingerprint", "") for row in paired_rows}
        if methods != required_methods or len(faults) != 1 or "" in faults:
            fault_ok = False
        if methods != required_methods or len(verifiers) != 1 or "" in verifiers:
            verifier_ok = False
    checks["exp1_transactional_paired_fault_fingerprints"] = fault_ok
    checks["exp1_transactional_paired_verifier_fingerprints"] = verifier_ok
    if not fault_ok:
        errors.append("paired fault schedule drift")
    if not verifier_ok:
        errors.append("paired verifier fingerprint drift")
    attempts_path = raw_path.with_name("attempts.jsonl")
    if not attempts_path.exists():
        checks["exp1_transactional_attempt_provenance"] = False
        errors.append("missing transactional attempt provenance")
    else:
        try:
            events = _read_events(attempts_path)
            event_ids = [(str(event.get("run_id", "")), event.get("event_sequence")) for event in events]
            if not all(run_id and isinstance(sequence, int) for run_id, sequence in event_ids):
                raise ValueError("attempt events lack unique identifiers")
            if len(event_ids) != len(set(event_ids)):
                raise ValueError("attempt events duplicate identifiers")
            validate_transactional_canonical_rows(rows)
            _validate_attempt_events(rows, events)
            if normalization_manifest is None:
                raise ValueError("normalization manifest is unavailable")
            if normalization_manifest.get("source_attempts_sha256") != sha(attempts_path):
                raise ValueError("attempt provenance hash disagrees with normalizer")
            checks["exp1_transactional_attempt_provenance"] = True
        except (ValueError, TypeError, json.JSONDecodeError):
            checks["exp1_transactional_attempt_provenance"] = False
            errors.append("transactional attempt provenance drift")
    aggregate = root / "aggregated" / "exp1_transactional" / "metrics.csv"
    paired = aggregate.with_name("paired_differences.csv")
    if not aggregate.exists():
        checks["exp1_transactional_aggregate_provenance"] = False
        errors.append("missing transactional aggregate provenance")
    else:
        raw_sha = sha(raw_path)
        aggregate_ok = False
        try:
            with tempfile.TemporaryDirectory() as directory:
                expected = Path(directory) / "metrics.csv"
                aggregate_experiment("exp1_transactional", raw_path, expected)
                expected_paired = expected.with_name("paired_differences.csv")
                aggregate_ok = (
                    aggregate.read_bytes() == expected.read_bytes()
                    and paired.is_file()
                    and paired.read_bytes() == expected_paired.read_bytes()
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            aggregate_ok = False
        checks["exp1_transactional_aggregate_provenance"] = aggregate_ok
        if not aggregate_ok:
            errors.append("transactional raw to aggregate provenance drift")
    figure_manifest = root / "figures" / "figure_manifest.json"
    if not figure_manifest.exists():
        checks["exp1_transactional_figure_provenance"] = False
        errors.append("missing transactional figure provenance")
    else:
        try:
            figure_data = json.loads(figure_manifest.read_text(encoding="utf-8"))
            metrics_sha = sha(aggregate) if aggregate.exists() else None
            aggregate_rows = _read_csv(aggregate) if aggregate.exists() else []
            rowset_sha = hashlib.sha256(
                "\n".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":"))
                    for row in sorted(aggregate_rows, key=lambda row: json.dumps(row, sort_keys=True))
                ).encode("utf-8")
            ).hexdigest()
            expected_figures = {
                f"exp1_transactional_{metric}.{suffix}"
                for metric in (
                    "method_owned_formation_latency_ms", "time_to_correct_formation_ms",
                    "rollback_scope_objects", "wasted_rule_commands",
                )
                for suffix in ("pdf", "png", "svg")
            }
            actual_figures = {
                path.name for path in (root / "figures").glob("exp1_transactional_*")
                if path.suffix in {".pdf", ".png", ".svg"}
            }
            figure_hashes = figure_data.get("figure_hashes")
            figures_ok = (
                figure_data.get("experiment") == "exp1_transactional"
                and figure_data.get("input_metrics_path") == "aggregated/exp1_transactional/metrics.csv"
                and figure_data.get("input_metrics_sha256") == metrics_sha
                and figure_data.get("input_metrics_row_count") == len(aggregate_rows)
                and figure_data.get("input_metrics_rowset_sha256") == rowset_sha
                and figure_data.get("plotting_script_sha256") == sha(Path("scripts/plot_wcnc_final_v3.py"))
                and isinstance(figure_hashes, dict)
                and set(figure_hashes) == expected_figures
                and actual_figures == expected_figures
                and not any((root / "figures" / f"exp1_transactional_success_rate.{suffix}").exists() for suffix in ("pdf", "png", "svg"))
                and all(
                    (root / "figures" / name).is_file()
                    and sha(root / "figures" / name) == digest
                    for name, digest in figure_hashes.items()
                )
            )
        except (OSError, TypeError, json.JSONDecodeError):
            figures_ok = False
        checks["exp1_transactional_figure_provenance"] = figures_ok
        if not figures_ok:
            errors.append("transactional aggregate to figure provenance drift")


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
    for artifact_kind, directory in (("aggregate", "aggregated"), ("figure", "figures")):
        expected_hashes = manifest.get(f"{artifact_kind}_hashes")
        if expected_hashes is None:
            continue
        current_hashes = {
            str(path.relative_to(root)): sha(path)
            for path in sorted((root / directory).glob("**/*"))
            if path.is_file()
        } if (root / directory).exists() else {}
        matches = current_hashes == expected_hashes
        checks[f"{artifact_kind}_hashes_match"] = matches
        if not matches:
            errors.append(f"{artifact_kind} artifact drift")
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
    arms = manifest.get("exp1_arms")
    if arms is not None:
        expected_arms = {"nominal", "exp1_transactional_v1"}
        arms_ok = isinstance(arms, dict) and set(arms) == expected_arms
        arm_contracts = {
            "nominal": {
                "raw_path": "raw/exp1/trials.csv",
                "config_path": "configs/exp1_netns_verified_formation_v3.yaml",
                "sources": {
                    "experiments/exp1_netns_verified_formation.py",
                    "scripts/normalize_wcnc_final_v3_exp1.py",
                },
            },
            "exp1_transactional_v1": {
                "raw_path": "raw/exp1_transactional/trials.csv",
                "config_path": "configs/exp1_transactional_formation_v1.yaml",
                "sources": {
                    "experiments/exp1_transactional_formation.py",
                    "scripts/normalize_wcnc_final_v3_exp1_transactional.py",
                },
            },
        }
        if arms_ok and isinstance(arms, dict):
            arm_paths = {entry.get("raw_path") for entry in arms.values() if isinstance(entry, dict)}
            arm_contract_ok = arm_paths == {contract["raw_path"] for contract in arm_contracts.values()}
            for arm_name, contract in arm_contracts.items():
                entry = arms[arm_name]
                arm_contract_ok = arm_contract_ok and isinstance(entry, dict) and (
                    entry.get("raw_path") == contract["raw_path"]
                    and entry.get("config_sha256") == sha(Path(contract["config_path"]))
                    and entry.get("schema_sha256") == sha(Path("configs/wcnc_final_v3_raw_schema.json"))
                    and isinstance(entry.get("source_hashes"), dict)
                    and set(entry["source_hashes"]) == contract["sources"]
                    and all(sha(Path(name)) == digest for name, digest in entry["source_hashes"].items())
                )
            arms_ok = arm_contract_ok
        checks["exp1_arm_manifest_complete"] = arms_ok
        if not arms_ok:
            errors.append("Exp1 arm manifest drift")
        if isinstance(arms, dict) and set(arms) == expected_arms:
            for arm_name, entry in arms.items():
                if not isinstance(entry, dict):
                    errors.append("Exp1 arm manifest drift")
                    continue
                raw_path = root / str(entry.get("raw_path", ""))
                current_hash = sha(raw_path) if raw_path.is_file() else None
                arm_hash_ok = current_hash == entry.get("trials_sha256")
                checks[f"{arm_name}_raw_hash_immutable"] = arm_hash_ok
                if not arm_hash_ok:
                    errors.append(f"{arm_name} raw hash drift")
                commit_path = root / str(entry.get("execution_commit_path", ""))
                current_commit = (
                    commit_path.read_text(encoding="utf-8").strip()
                    if commit_path.is_file() else None
                )
                commit_ok = current_commit == entry.get("execution_commit")
                commit_ok = commit_ok and isinstance(current_commit, str) and bool(
                    __import__("re").fullmatch(r"[0-9a-f]{40}", current_commit)
                )
                if commit_ok:
                    resolved = subprocess.run(
                        ["git", "cat-file", "-e", f"{current_commit}^{{commit}}"],
                        text=True, capture_output=True,
                    )
                    commit_ok = resolved.returncode == 0
                checks[f"{arm_name}_execution_commit_immutable"] = commit_ok
                if not commit_ok:
                    errors.append(f"{arm_name} execution commit drift")
                if raw_path.is_file() and not current_commit:
                    errors.append(f"missing {arm_name} execution commit evidence")
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
    _transactional_audit(root, errors, checks)
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
