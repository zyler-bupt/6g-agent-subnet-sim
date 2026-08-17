from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import Counter
from itertools import product
from pathlib import Path
from typing import Sequence

from experiments.exp2_demand_capacity_ratio import (
    compact_scenario_row,
    sha256_json,
    validate_execution_source_manifest,
    validate_formal_pilot_binding,
)
from src.simulation.conflict_scenario_generator import ConflictScenarioConfig
from src.simulation.demand_capacity_ratio import DemandCapacityRatioGenerator


METHODS = (
    "ours",
    "alc",
    "layer_wise_independent",
    "no_global_verification",
)
FORMAL_RATIOS = (0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3)
CLASSES = ("NO_CONFLICT", "RESOLVABLE_CONFLICT", "UNRESOLVABLE_CONFLICT")
AGREEMENT_FIELDS = (
    "conflict_class",
    "environment_fingerprint",
    "scenario_fingerprint",
    "configuration_sha256",
    "execution_manifest_sha256",
    "oracle_input_sha256",
    "oracle_evaluation_sha256",
)


def aggregate(
    input_path: Path,
    output_dir: Path,
    expected_seeds: int = 100,
    *,
    expected_ratios: Sequence[float] = FORMAL_RATIOS,
    expected_seed_values: Sequence[int] | None = None,
    expected_methods: Sequence[str] = METHODS,
    scenarios_path: Path | None = None,
) -> dict[str, object]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    configuration_path = input_path.parent / "configuration.json"
    try:
        configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        configuration = {}
    generator_version = configuration.get("experiment", {}).get(
        "generator_version"
    )
    strict_v4 = generator_version == "wcnc-final-gamma-v4"
    strict_v5 = generator_version == "wcnc-final-gamma-v5"
    strict_registered = strict_v4 or strict_v5
    ratios = tuple(float(value) for value in expected_ratios)
    seeds = tuple(
        range(expected_seeds)
        if expected_seed_values is None
        else (int(value) for value in expected_seed_values)
    )
    methods = tuple(expected_methods)
    expected_keys = set(product(ratios, seeds, methods))
    observed_keys = [
        (float(row["demand_to_capacity_ratio"]), int(row["seed"]), row["method"])
        for row in rows
    ]
    observed_counts = Counter(observed_keys)
    missing_cartesian = sorted(expected_keys - set(observed_counts))
    unexpected_cartesian = sorted(set(observed_counts) - expected_keys)
    duplicate_cartesian = sorted(
        (*key, count) for key, count in observed_counts.items() if count != 1
    )

    groups: dict[tuple[float, str], list[dict[str, str]]] = {}
    paired: dict[tuple[float, int], list[dict[str, str]]] = {}
    environment_by_seed: dict[int, set[str]] = {}
    formula_errors: list[str] = []
    flow_audit_errors: list[str] = []
    oracle_count_errors: list[str] = []
    hash_presence_errors: list[str] = []
    shared_trunk_errors: list[str] = []
    runner_errors: list[str] = []
    method_orders: Counter[tuple[str, int]] = Counter()
    for row in rows:
        gamma = float(row["demand_to_capacity_ratio"])
        seed = int(row["seed"])
        groups.setdefault((gamma, row["method"]), []).append(row)
        paired.setdefault((gamma, seed), []).append(row)
        environment_by_seed.setdefault(seed, set()).add(
            row.get("environment_fingerprint", "")
        )
        try:
            method_order = int(row.get("method_order", ""))
            if method_order not in range(len(methods)):
                raise ValueError
            method_orders[(row["method"], method_order)] += 1
        except (TypeError, ValueError):
            if strict_registered:
                runner_errors.append(row.get("run_id", "") + ":invalid_method_order")
        if _bool(row.get("timeout", False)) or str(
            row.get("failure_reason", "")
        ).startswith("runner_error:"):
            runner_errors.append(row.get("run_id", "") + ":runner_failure")
        for field in AGREEMENT_FIELDS[1:]:
            value = row.get(field, "")
            if not _is_sha256(value):
                hash_presence_errors.append(row.get("run_id", "") + f":{field}")
        capacities_raw = row.get("flow_capacities_json", "")
        if not capacities_raw:
            flow_audit_errors.append(row.get("run_id", "") + ":missing_flow_capacities")
            continue
        capacities = json.loads(capacities_raw)
        requested = json.loads(row.get("flow_r_req_mbps_json", "{}"))
        bottlenecks = json.loads(row.get("flow_bottlenecks_json", "{}"))
        if (
            len(capacities) != 3
            or set(capacities) != set(requested)
            or set(capacities) != set(bottlenecks)
            or set(bottlenecks.values()) != {"transport", "network", "physical"}
        ):
            flow_audit_errors.append(row.get("run_id", "") + ":flow_schema")
        for flow_id, values in capacities.items():
            layer_capacities = {
                "transport": float(values["transport_admissible_mbps"]),
                "network": float(values["network_available_mbps"]),
                "physical": float(values["physical_available_mbps"]),
            }
            c_eff = min(layer_capacities.values())
            if bottlenecks.get(flow_id) != min(
                layer_capacities, key=layer_capacities.get
            ):
                flow_audit_errors.append(
                    row.get("run_id", "") + f":{flow_id}:bottleneck_label"
                )
            if not math.isclose(
                c_eff, float(values["effective_mbps"]), abs_tol=1e-9
            ):
                formula_errors.append(row.get("run_id", "") + f":{flow_id}:c_eff")
            if not math.isclose(
                float(requested[flow_id]) / c_eff, gamma, abs_tol=1e-9
            ):
                formula_errors.append(row.get("run_id", "") + f":{flow_id}:gamma")
        if strict_registered:
            try:
                utilization = float(row["shared_trunk_utilization"])
                shared_capacity = float(row["shared_trunk_capacity_mbps"])
                expected_capacity = sum(
                    float(values["effective_mbps"])
                    for values in capacities.values()
                ) / utilization
                if not 0.75 <= utilization <= 0.90 or not math.isclose(
                    shared_capacity, expected_capacity, rel_tol=0.0, abs_tol=1e-9
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                shared_trunk_errors.append(row.get("run_id", ""))
        if int(row.get("ground_truth_candidate_combinations", 0)) != 4096:
            oracle_count_errors.append(row.get("run_id", ""))

    pairing_errors: list[dict[str, object]] = []
    for key, group in sorted(paired.items()):
        disagreement = {
            field: sorted({row.get(field, "") for row in group})
            for field in AGREEMENT_FIELDS
            if len({row.get(field, "") for row in group}) != 1
            or not next(iter({row.get(field, "") for row in group}), "")
        }
        if (
            len(group) != len(methods)
            or {row["method"] for row in group} != set(methods)
            or disagreement
        ):
            pairing_errors.append(
                {"gamma": key[0], "seed": key[1], "disagreement": disagreement}
            )

    count_errors = [
        {"gamma": gamma, "method": method, "samples": len(group)}
        for (gamma, method), group in sorted(groups.items())
        if len(group) != len(seeds)
    ]
    environment_errors = sorted(
        seed
        for seed, fingerprints in environment_by_seed.items()
        if len(fingerprints) != 1 or "" in fingerprints
    )
    global_agreement_errors = {
        field: sorted({row.get(field, "") for row in rows})
        for field in ("configuration_sha256", "execution_manifest_sha256")
        if len({row.get(field, "") for row in rows}) != 1
        or not next(iter({row.get(field, "") for row in rows}), "")
    }
    method_order_errors: list[dict[str, object]] = []
    if strict_registered:
        for method in methods:
            counts_by_order = [
                method_orders[(method, order)] for order in range(len(methods))
            ]
            if max(counts_by_order, default=0) - min(counts_by_order, default=0) > 1:
                method_order_errors.append(
                    {"method": method, "counts_by_order": counts_by_order}
                )

    resolved_scenarios_path = scenarios_path or input_path.with_name(
        "scenarios.jsonl"
    )
    scenario_audit_errors = _audit_scenarios(
        resolved_scenarios_path,
        paired,
        ratios,
        seeds,
    )
    provenance_file_errors = _audit_provenance_files(
        input_path.parent,
        rows,
        ratios=ratios,
        seeds=seeds,
        methods=methods,
    )
    decision_event_errors = (
        audit_run_decision_event_links(
            rows,
            input_path.with_name("decisions.jsonl"),
            input_path.with_name("events.jsonl"),
        )
        if strict_v5
        else []
    )
    oracle_replay_errors = (
        replay_v5_oracle(resolved_scenarios_path, configuration)
        if strict_v5
        else []
    )
    source_manifest_errors = (
        audit_v5_source_manifest(input_path.parent)
        if strict_v5
        else []
    )
    empty_input = not rows
    truth_state_error = any(
        not _bool(row.get("ground_truth_uses_true_state", False)) for row in rows
    )
    passed = not any(
        (
            empty_input,
            missing_cartesian,
            unexpected_cartesian,
            duplicate_cartesian,
            pairing_errors,
            count_errors,
            environment_errors,
            global_agreement_errors,
            formula_errors,
            flow_audit_errors,
            oracle_count_errors,
            hash_presence_errors,
            shared_trunk_errors,
            runner_errors,
            method_order_errors,
            scenario_audit_errors,
            provenance_file_errors,
            decision_event_errors,
            oracle_replay_errors,
            source_manifest_errors,
            truth_state_error,
        )
    )

    summary: list[dict[str, object]] = []
    for (gamma, method), group in sorted(groups.items()):
        satisfaction = [float(_bool(row["qos_satisfied"])) for row in group]
        summary.append(
            {
                "demand_to_capacity_ratio": gamma,
                "method": method,
                "samples": len(group),
                **_stats(satisfaction, "task_satisfaction_rate"),
            }
        )
    counts: Counter[tuple[float, str]] = Counter()
    for (gamma, _seed), group in paired.items():
        if group:
            counts[(gamma, group[0].get("conflict_class", ""))] += 1
    class_rows = [
        {
            "demand_to_capacity_ratio": gamma,
            "conflict_class": conflict_class,
            "samples": counts[(gamma, conflict_class)],
        }
        for gamma in ratios
        for conflict_class in CLASSES
    ]
    paired_comparisons = []
    if not any(
        (
            missing_cartesian,
            unexpected_cartesian,
            duplicate_cartesian,
            pairing_errors,
        )
    ):
        paired_comparisons = _paired_comparison_rows(
            paired, ratios, seeds, methods
        )
    integrity: dict[str, object] = {
        "pass": passed,
        "empty_input": empty_input,
        "methods": list(methods),
        "expected_ratios": list(ratios),
        "expected_seed_values": list(seeds),
        "raw_runs": len(rows),
        "expected_raw_runs": len(expected_keys),
        "paired_gamma_seed_groups": len(paired),
        "missing_cartesian_rows": [list(key) for key in missing_cartesian],
        "unexpected_cartesian_rows": [list(key) for key in unexpected_cartesian],
        "duplicate_cartesian_rows": [list(item) for item in duplicate_cartesian],
        "pairing_errors": pairing_errors,
        "count_errors": count_errors,
        "environment_invariance_errors": environment_errors,
        "global_agreement_errors": global_agreement_errors,
        "gamma_formula_errors": formula_errors,
        "flow_audit_errors": flow_audit_errors,
        "oracle_count_errors": oracle_count_errors,
        "hash_presence_errors": hash_presence_errors,
        "shared_trunk_errors": shared_trunk_errors,
        "runner_errors": runner_errors,
        "method_order_errors": method_order_errors,
        "scenario_audit_errors": scenario_audit_errors,
        "provenance_file_errors": provenance_file_errors,
        "decision_event_errors": decision_event_errors,
        "oracle_replay_errors": oracle_replay_errors,
        "source_manifest_errors": source_manifest_errors,
        "ground_truth_uses_true_state": not truth_state_error and bool(rows),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "summary.csv", summary)
    _write(output_dir / "ground_truth_class_counts.csv", class_rows)
    _write(output_dir / "paired_comparisons.csv", paired_comparisons)
    _write(
        output_dir / "conditional_feasible_summary.csv",
        conditional_feasible_summary(rows),
    )
    (output_dir / "experiment_integrity_report.json").write_text(
        json.dumps(integrity, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return integrity


def _audit_scenarios(
    path: Path,
    paired: dict[tuple[float, int], list[dict[str, str]]],
    ratios: tuple[float, ...],
    seeds: tuple[int, ...],
) -> list[str]:
    if not path.is_file():
        return ["missing_scenarios_jsonl"]
    scenarios = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_key: dict[tuple[float, int], list[dict[str, object]]] = {}
    for scenario in scenarios:
        key = (float(scenario["pressure"]), int(scenario["seed"]))
        by_key.setdefault(key, []).append(scenario)
    errors: list[str] = []
    expected = set(product(ratios, seeds))
    for key in sorted(expected | set(by_key)):
        values = by_key.get(key, [])
        if len(values) != 1:
            errors.append(f"scenario_count:{key[0]}:{key[1]}:{len(values)}")
            continue
        scenario = values[0]
        oracle_input = scenario.get("oracle_input")
        input_sha = str(scenario.get("oracle_input_sha256", ""))
        if oracle_input is None or input_sha != _sha256_json(oracle_input):
            errors.append(f"oracle_input_sha256:{key[0]}:{key[1]}")
        if not _is_sha256(str(scenario.get("oracle_evaluation_sha256", ""))):
            errors.append(f"oracle_evaluation_sha256:{key[0]}:{key[1]}")
        run_group = paired.get(key, [])
        if not run_group:
            continue
        for field in (
            "scenario_fingerprint",
            "configuration_sha256",
            "execution_manifest_sha256",
            "oracle_input_sha256",
            "oracle_evaluation_sha256",
        ):
            if str(scenario.get(field, "")) != run_group[0].get(field, ""):
                errors.append(f"scenario_run_mismatch:{key[0]}:{key[1]}:{field}")
    return errors


def _audit_provenance_files(
    raw_dir: Path,
    rows: list[dict[str, str]],
    *,
    ratios: tuple[float, ...],
    seeds: tuple[int, ...],
    methods: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    loaded: dict[str, object] = {}
    for filename, field in (
        ("configuration.json", "configuration_sha256"),
        ("execution_manifest.json", "execution_manifest_sha256"),
    ):
        path = raw_dir / filename
        if not path.is_file():
            errors.append(f"missing_{filename}")
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            errors.append(f"invalid_{filename}")
            continue
        actual = _sha256_json(value)
        loaded[filename] = value
        if any(row.get(field, "") != actual for row in rows):
            errors.append(f"{filename}_sha256_mismatch")
    configuration = loaded.get("configuration.json")
    manifest = loaded.get("execution_manifest.json")
    if isinstance(configuration, dict) and isinstance(manifest, dict):
        expected_manifest_fields = {
            "phase": configuration.get("experiment", {}).get("phase"),
            "generator_version": configuration.get("experiment", {}).get(
                "generator_version"
            ),
            "configuration_sha256": _sha256_json(configuration),
            "methods": list(methods),
            "seeds": list(seeds),
            "ratios": list(ratios),
            "output_dir": configuration.get("experiment", {}).get("output_dir"),
        }
        for field, expected in expected_manifest_fields.items():
            if manifest.get(field) != expected:
                errors.append(f"execution_manifest_field:{field}")
        experiment = configuration.get("experiment", {})
        if experiment.get("generator_version") == "wcnc-final-gamma-v5":
            try:
                validate_formal_pilot_binding(configuration)
            except ValueError as error:
                errors.append(f"formal_pilot_binding:{error}")
        if experiment.get("generator_version") in {
            "wcnc-final-gamma-v4",
            "wcnc-final-gamma-v5",
        }:
            selection_path = Path(str(experiment.get("pilot_selection_path", "")))
            pilot_config_path = Path(str(experiment.get("pilot_config_path", "")))
            generator_path = Path(str(experiment.get("generator_source_path", "")))
            for label, path, expected_sha in (
                (
                    "pilot_selection",
                    selection_path,
                    experiment.get("pilot_selection_sha256"),
                ),
                (
                    "pilot_config",
                    pilot_config_path,
                    experiment.get("pilot_config_sha256"),
                ),
                (
                    "generator_source",
                    generator_path,
                    experiment.get("generator_source_sha256"),
                ),
            ):
                if not path.is_file() or _sha256_file(path) != expected_sha:
                    errors.append(f"{label}_sha256_mismatch")
            if selection_path.is_file():
                try:
                    selection = json.loads(selection_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    errors.append("invalid_pilot_selection")
                else:
                    if selection.get("retained_ratios") != list(ratios):
                        errors.append("pilot_selection_ratio_mismatch")
                    if selection.get("selection_inputs") != "ground_truth_class_only":
                        errors.append("pilot_selection_used_non_ground_truth_inputs")
                    if selection.get("execution_manifest", {}).get("methods") != []:
                        errors.append("pilot_executed_comparison_methods")
    return errors


def _sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def wilson_interval(successes: int, samples: int) -> tuple[float, float]:
    if samples <= 0:
        return (0.0, 0.0)
    proportion = successes / samples
    z = 1.96
    denominator = 1.0 + z * z / samples
    center = (proportion + z * z / (2.0 * samples)) / denominator
    half_width = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / samples
            + z * z / (4.0 * samples * samples)
        )
        / denominator
    )
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def exact_mcnemar_pvalue(ours_only: int, baseline_only: int) -> float:
    discordant = ours_only + baseline_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, value)
        for value in range(min(ours_only, baseline_only) + 1)
    )
    return min(1.0, 2.0 * tail / (2**discordant))


def audit_run_decision_event_links(
    rows: Sequence[dict[str, object]],
    decisions_path: Path,
    events_path: Path,
) -> list[str]:
    errors: list[str] = []
    decisions = _read_jsonl(decisions_path, errors, "decisions")
    events = _read_jsonl(events_path, errors, "events")
    runs_by_id = {str(row.get("run_id", "")): row for row in rows}
    decisions_by_id: dict[str, list[dict[str, object]]] = {}
    for decision in decisions:
        decisions_by_id.setdefault(str(decision.get("run_id", "")), []).append(
            decision
        )
    events_by_id: dict[str, list[dict[str, object]]] = {}
    for event in events:
        events_by_id.setdefault(
            str(event.get("experiment_run_id", "")), []
        ).append(event)

    for run_id, row in runs_by_id.items():
        linked_decisions = decisions_by_id.get(run_id, [])
        if len(linked_decisions) != 1:
            errors.append(f"{run_id}:decision_count:{len(linked_decisions)}")
            continue
        decision = linked_decisions[0]
        for field in ("method", "scenario_fingerprint"):
            if str(decision.get(field, "")) != str(row.get(field, "")):
                errors.append(f"{run_id}:decision_{field}")
        for field in (
            "qos_satisfied",
            "transaction_attempted",
            "rollback_triggered",
            "rollback_success",
            "safe_rejection",
        ):
            if _bool(decision.get(field, False)) != _bool(row.get(field, False)):
                errors.append(f"{run_id}:decision_{field}")
        linked_events = events_by_id.get(run_id, [])
        if not linked_events:
            errors.append(f"{run_id}:missing_events")
        for event in linked_events:
            if str(event.get("method", "")) != str(row.get("method", "")):
                errors.append(f"{run_id}:event_method")
            if str(event.get("scenario_fingerprint", "")) != str(
                row.get("scenario_fingerprint", "")
            ):
                errors.append(f"{run_id}:event_scenario_fingerprint")
    for run_id in sorted(set(decisions_by_id) - set(runs_by_id)):
        errors.append(f"{run_id}:unexpected_decision")
    for run_id in sorted(set(events_by_id) - set(runs_by_id)):
        errors.append(f"{run_id}:unexpected_events")
    return errors


def audit_v5_source_manifest(
    raw_dir: Path,
    *,
    require_clean: bool = True,
) -> list[str]:
    errors: list[str] = []
    source_path = raw_dir / "execution_source_manifest.json"
    execution_path = raw_dir / "execution_manifest.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["missing_or_invalid_execution_source_manifest"]
    try:
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["missing_or_invalid_execution_manifest"]
    if execution.get("source_manifest_sha256") != sha256_json(source):
        errors.append("source_manifest_sha256_mismatch")
    try:
        validate_execution_source_manifest(
            source,
            require_clean=require_clean,
        )
    except ValueError as error:
        errors.append(f"source_manifest_validation:{error}")
    return errors


def replay_v5_oracle(
    scenarios_path: Path,
    config: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    scenarios = _read_jsonl(scenarios_path, errors, "scenarios")
    task = dict(config.get("task", {}))
    scenario_config = ConflictScenarioConfig(
        num_agents=int(task.get("num_agents", 10)),
        edge_ratio=float(task.get("edge_ratio", 1.5)),
        num_gateways=int(task.get("num_gateways", 4)),
        cross_gateway_edge_ratio=float(task.get("cross_gateway_edge_ratio", 0.5)),
        multi_hop=True,
    )
    generator = DemandCapacityRatioGenerator()
    for row in scenarios:
        seed = int(row.get("seed", -1))
        gamma = float(row.get("pressure", -1.0))
        identity = f"gamma={gamma:g}:seed={seed}"
        try:
            expected = compact_scenario_row(
                generator.generate(gamma, seed, scenario_config)
            )
        except Exception as error:
            errors.append(f"{identity}:replay_error:{type(error).__name__}")
            continue
        for field in (
            "scenario_fingerprint",
            "conflict_class",
            "oracle_input_sha256",
            "oracle_evaluation_sha256",
        ):
            if row.get(field) != expected[field]:
                errors.append(f"{identity}:{field}")
        recorded_truth = dict(row.get("ground_truth", {}))
        expected_truth = dict(expected["ground_truth"])
        for field in (
            "candidate_combinations",
            "feasible_combinations",
            "ground_truth_conflict",
            "ground_truth_resolvable",
            "best_feasible_combination",
        ):
            if recorded_truth.get(field) != expected_truth[field]:
                errors.append(f"{identity}:{field}")
    return errors


def conditional_feasible_summary(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    groups: dict[tuple[float, str], list[dict[str, object]]] = {}
    for row in rows:
        if int(row.get("ground_truth_feasible_combinations", 0)) <= 0:
            continue
        key = (
            float(row["demand_to_capacity_ratio"]),
            str(row["method"]),
        )
        groups.setdefault(key, []).append(row)
    summary: list[dict[str, object]] = []
    for (gamma, method), group in sorted(groups.items()):
        successes = sum(_bool(row.get("qos_satisfied", False)) for row in group)
        values = [float(_bool(row.get("qos_satisfied", False))) for row in group]
        summary.append(
            {
                "demand_to_capacity_ratio": gamma,
                "method": method,
                "oracle_feasible_samples": len(group),
                "successful_samples": successes,
                **_stats(values, "conditional_task_satisfaction_rate"),
            }
        )
    return summary


def _read_jsonl(
    path: Path,
    errors: list[str],
    label: str,
) -> list[dict[str, object]]:
    if not path.is_file():
        errors.append(f"missing_{label}_jsonl")
        return []
    values: list[dict[str, object]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                values.append(json.loads(line))
    except (OSError, json.JSONDecodeError, TypeError):
        errors.append(f"invalid_{label}_jsonl")
        return []
    return values


def _paired_comparison_rows(
    paired: dict[tuple[float, int], list[dict[str, str]]],
    ratios: tuple[float, ...],
    seeds: tuple[int, ...],
    methods: tuple[str, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for gamma in ratios:
        gamma_rows: list[dict[str, object]] = []
        by_seed = {
            seed: {row["method"]: _bool(row["qos_satisfied"]) for row in paired[(gamma, seed)]}
            for seed in seeds
        }
        for baseline in (method for method in methods if method != "ours"):
            differences = [
                float(by_seed[seed]["ours"]) - float(by_seed[seed][baseline])
                for seed in seeds
            ]
            ours_only = sum(
                by_seed[seed]["ours"] and not by_seed[seed][baseline]
                for seed in seeds
            )
            baseline_only = sum(
                by_seed[seed][baseline] and not by_seed[seed]["ours"]
                for seed in seeds
            )
            lower, upper = _paired_bootstrap_interval(
                differences,
                seed=f"exp2:{gamma:g}:ours:{baseline}",
            )
            gamma_rows.append(
                {
                    "demand_to_capacity_ratio": gamma,
                    "reference_method": "ours",
                    "baseline_method": baseline,
                    "paired_samples": len(differences),
                    "satisfaction_difference": statistics.fmean(differences),
                    "difference_lower95": lower,
                    "difference_upper95": upper,
                    "bootstrap_resamples": 10000,
                    "ours_only_successes": ours_only,
                    "baseline_only_successes": baseline_only,
                    "mcnemar_exact_p": exact_mcnemar_pvalue(
                        ours_only, baseline_only
                    ),
                }
            )
        ordered = sorted(
            enumerate(gamma_rows), key=lambda item: item[1]["mcnemar_exact_p"]
        )
        running = 0.0
        for rank, (index, row) in enumerate(ordered):
            adjusted = min(
                1.0,
                (len(ordered) - rank) * float(row["mcnemar_exact_p"]),
            )
            running = max(running, adjusted)
            gamma_rows[index]["mcnemar_holm_p"] = running
        rows.extend(gamma_rows)
    return rows


def _paired_bootstrap_interval(
    differences: list[float],
    *,
    seed: str,
    resamples: int = 10000,
) -> tuple[float, float]:
    if not differences:
        return (0.0, 0.0)
    rng = random.Random(seed)
    samples = len(differences)
    estimates = sorted(
        sum(differences[rng.randrange(samples)] for _ in range(samples)) / samples
        for _ in range(resamples)
    )
    return (
        _percentile(estimates, 0.025),
        _percentile(estimates, 0.975),
    )


def _percentile(sorted_values: list[float], probability: float) -> float:
    position = probability * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _stats(values: list[float], prefix: str) -> dict[str, object]:
    mean = statistics.fmean(values) if values else 0.0
    lower, upper = wilson_interval(sum(value >= 0.5 for value in values), len(values))
    return {
        f"{prefix}_mean": mean,
        f"{prefix}_ci95": max(mean - lower, upper - mean),
        f"{prefix}_lower95": lower,
        f"{prefix}_upper95": upper,
        f"{prefix}_ci_method": "wilson_score_95",
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _parse_int_values(value: str) -> tuple[int, ...]:
    if ":" in value:
        start, finish = (int(item) for item in value.split(":", 1))
        return tuple(range(start, finish + 1))
    return tuple(int(item) for item in value.split(",") if item.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate WCNC demand-to-capacity results"
    )
    parser.add_argument(
        "--input", default="results/exp2_demand_capacity_v5/raw/runs.csv"
    )
    parser.add_argument(
        "--output-dir", default="results/exp2_demand_capacity_v5/processed"
    )
    parser.add_argument(
        "--expected-ratios",
        default=",".join(str(value) for value in FORMAL_RATIOS),
    )
    parser.add_argument("--expected-seed-values", default="0:99")
    args = parser.parse_args()
    report = aggregate(
        Path(args.input),
        Path(args.output_dir),
        expected_ratios=tuple(
            float(value) for value in args.expected_ratios.split(",")
        ),
        expected_seed_values=_parse_int_values(args.expected_seed_values),
    )
    print("PASS" if report["pass"] else "FAIL", json.dumps(report, sort_keys=True))
    if not report["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
