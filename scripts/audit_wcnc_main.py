from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

import yaml

os.environ.setdefault("MPLCONFIGDIR", "/tmp/6g-agent-subnet-wcnc-matplotlib")

try:
    from scripts.paper_style import METHOD_STYLES
    from scripts.plot_wcnc_main_figures import (
        build_fig1_data,
        build_fig2_data,
        build_fig3_data,
        build_fig4_data,
    )
except ModuleNotFoundError:  # direct execution from the scripts directory
    from paper_style import METHOD_STYLES
    from plot_wcnc_main_figures import (
        build_fig1_data,
        build_fig2_data,
        build_fig3_data,
        build_fig4_data,
    )


EXPECTED_FIGURES = {
    "Fig1_Verified_Formation.pdf",
    "Fig1_Verified_Formation.png",
    "Fig2_Cross_Layer_Conflict.pdf",
    "Fig2_Cross_Layer_Conflict.png",
    "Fig3_Business_Reconfiguration.pdf",
    "Fig3_Business_Reconfiguration.png",
    "Fig4_Failure_Recovery.pdf",
    "Fig4_Failure_Recovery.png",
}
EXPECTED_PANEL_DATA = {
    "fig1.csv",
    "fig2.csv",
    "fig3a.csv",
    "fig3b.csv",
    "fig4a.csv",
    "fig4b.csv",
}
EXP2_METHODS = {
    "ours",
    "alc",
    "layer_wise_independent",
    "no_global_verification",
}
EXP4_METHODS = {"proposed", "full_rebuild", "cspf"}
EXP3_EXPECTED_HASHES = {
    "runs.csv": "a9bf881864b45b30bd6d0e78c77f5ad4cad6601943a00e648e71059a820fb4b2",
    "events.jsonl": "a8e9622fd276412331740ea91ec64d71f81f8279e921e079125905d48996e105",
    "probe_samples.csv": "95e22270b1244a28356e63a478688bc5692250dfc7d9d3b69cd7eefa886540d4",
    "scenario_snapshots.json": "640c9ca7764c8790a18d7910501b32ee38ee9b243575ab448efbe30af532a1b5",
}
EXPECTED_STYLE = {
    "ours": ("Ours", "#1F4E79", "-", "o", ""),
    "full_reconfiguration": (
        "Full Reconfiguration",
        "#C55A11",
        "--",
        "s",
        r"\\",
    ),
    "alc": ("Adjacent-Layer Coordination (ALC)", "#548235", "-.", "^", ".."),
    "layer_wise_independent": (
        "Layer-wise Independent",
        "#595959",
        ":",
        "D",
        "xx",
    ),
    "no_global_verification": (
        "w/o Global Verification",
        "#8064A2",
        (0, (3.0, 1.5)),
        "v",
        "--",
    ),
    "cspf": ("CSPF", "#31859C", "-", "P", "//"),
}


def audit(
    *,
    exp1_dir: Path,
    exp2_dir: Path,
    exp3_dir: Path,
    exp4_dir: Path,
    figure_dir: Path,
) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    exp1_raw_path = exp1_dir / "raw" / "runs.csv"
    exp2_raw_path = exp2_dir / "raw" / "runs.csv"
    exp3_raw_path = exp3_dir / "raw" / "runs.csv"
    exp4_raw_path = exp4_dir / "raw" / "runs.csv"
    exp1 = _read_csv(exp1_raw_path)
    exp2 = _read_csv(exp2_raw_path)
    exp3 = _read_csv(exp3_raw_path)
    exp4 = _read_csv(exp4_raw_path)

    _audit_exp1(checks, exp1_dir, exp1)
    _audit_exp2(checks, exp2_dir, exp2)
    _audit_exp3(checks, exp3_dir, exp3)
    _audit_exp4(checks, exp4_dir, exp4)
    _audit_plotting_data(
        checks,
        figure_dir,
        exp1_raw_path,
        exp2_raw_path,
        exp3_raw_path,
        exp4_raw_path,
    )
    _audit_figure_files(checks, figure_dir)
    _audit_style_and_names(checks, figure_dir)
    _audit_report(checks, figure_dir / "experiment_report.md")
    _audit_line_endings(checks)

    source_files = {
        "exp1_runs": exp1_raw_path,
        "exp1_events": exp1_dir / "raw" / "events.jsonl",
        "exp2_runs": exp2_raw_path,
        "exp2_scenarios": exp2_dir / "raw" / "scenarios.jsonl",
        "exp3_runs": exp3_raw_path,
        "exp4_runs": exp4_raw_path,
        "exp4_proposals": exp4_dir / "raw" / "proposals.jsonl",
    }
    return {
        "pass": all(bool(item["passed"]) for item in checks),
        "checks": checks,
        "source_sha256": {
            label: _sha256(path) for label, path in source_files.items()
        },
        "result_scope": {
            "fig1": "real_linux_netns_veth_tc_data_plane",
            "fig2": "paired_demand_to_capacity_control_plane_simulation",
            "fig3": "unchanged_existing_experiment_results",
            "fig4": "paired_transactional_failure_recovery_simulation",
        },
        "main_paper_structure": {
            "figures": 4,
            "panels": 6,
            "fig1_panels": 1,
            "fig2_panels": 1,
            "fig3_panels": 2,
            "fig4_panels": 2,
        },
    }


def _audit_exp1(
    checks: list[dict[str, object]],
    exp1_dir: Path,
    rows: list[dict[str, str]],
) -> None:
    counts = Counter(int(row["num_agents"]) for row in rows)
    seed_errors = {
        size: sorted(
            int(row["seed"]) for row in rows if int(row["num_agents"]) == size
        )
        for size in (4, 8, 12, 16, 20)
        if {
            int(row["seed"]) for row in rows if int(row["num_agents"]) == size
        }
        != set(range(30))
    }
    _record(
        checks,
        "Fig. 1 retains exactly 30 predeclared seeds at each task size",
        counts == Counter({4: 30, 8: 30, 12: 30, 16: 30, 20: 30})
        and not seed_errors,
        {"counts": dict(counts), "seed_errors": seed_errors},
    )

    config = _load_yaml(Path("configs/exp1_netns_verified_formation.yaml"))
    config_hash = _configuration_sha256(config)
    events = _read_jsonl(exp1_dir / "raw" / "events.jsonl")
    events_by_run: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        events_by_run[str(event["run_id"])].append(event)

    timestamp_fields = (
        "task_received_at",
        "mapping_finished_at",
        "traffic_control_started_at",
        "traffic_control_finished_at",
        "route_install_started_at",
        "route_install_finished_at",
        "activation_finished_at",
        "verification_started_at",
        "ping_verify_started_at",
        "ping_verify_finished_at",
        "iperf_verify_started_at",
        "data_plane_verified_at",
    )
    run_errors: list[str] = []
    command_errors: list[str] = []
    tc_errors: list[str] = []
    retry_errors: list[str] = []
    infrastructure_failures: list[str] = []
    background_runs = 0
    retry_runs = 0
    for row in rows:
        run_id = row["run_id"]
        run_events = events_by_run[run_id]
        timestamps = [float(row[field]) for field in timestamp_fields]
        if (
            row["result_mode"] != "real_linux_netns_veth_tc_data_plane"
            or row["configuration_sha256"] != config_hash
            or not all(left <= right for left, right in zip(timestamps, timestamps[1:]))
            or not math.isclose(
                float(row["verified_formation_latency_s"]),
                float(row["data_plane_verified_at"])
                - float(row["task_received_at"]),
                abs_tol=1e-9,
            )
            or not math.isclose(
                float(row["verification_latency_s"]),
                float(row["data_plane_verified_at"])
                - float(row["verification_started_at"]),
                abs_tol=1e-9,
            )
        ):
            run_errors.append(run_id)
        if not _bool(row["success"]) and not row["failure_reason"].startswith(
            "data_plane_verification_failed:"
        ):
            infrastructure_failures.append(run_id + ":" + row["failure_reason"])

        route_events = [
            event for event in run_events if event["stage"] == "ROUTE_INSTALL_STARTED"
        ]
        ping_events = [
            event for event in run_events if event["stage"] == "PING_COMMAND_RESULT"
        ]
        iperf_events = [
            event for event in run_events if event["stage"] == "IPERF3_COMMAND_RESULT"
        ]
        if len(route_events) != 1:
            command_errors.append(run_id + ":route_event_count")
        else:
            commands = route_events[0]["details"].get("commands", [])
            command_tuples = [tuple(item.get("command", ())) for item in commands]
            if (
                int(route_events[0]["details"].get("route_commands", 0))
                != len(command_tuples)
                or not any(command[:2] == ("ip", "route") for command in command_tuples)
                or not any(command[:2] == ("ip", "rule") for command in command_tuples)
            ):
                command_errors.append(run_id + ":route_rule_commands")
        expected_edges = int(row["num_business_edges"])
        if (
            len(ping_events) < expected_edges
            or len(iperf_events) < int(row["iperf_flows_passed"])
        ):
            command_errors.append(run_id + ":probe_event_count")
        for event in ping_events:
            command = list(event["details"].get("command", ()))
            if (
                not command
                or command[0] != "ping"
                or "-c" not in command
                or command[command.index("-c") + 1] != "3"
            ):
                command_errors.append(run_id + ":ping_command")
                break
        for event in iperf_events:
            details = event["details"]
            command = list(details.get("command", ()))
            if (
                not command
                or command[0] != "iperf3"
                or "-c" not in command
                or "-J" not in command
                or "-t" not in command
                or not math.isclose(
                    float(command[command.index("-t") + 1]),
                    1.0,
                    abs_tol=1e-9,
                )
                or "throughput_mbps" not in details
                or "retransmissions" not in details
                or "reported_duration_s" not in details
                or "returncode" not in details
            ):
                command_errors.append(run_id + ":iperf3_command_or_metrics")
                break

        tc_events = [
            event
            for event in run_events
            if event["stage"] == "TRAFFIC_CONTROL_FINISHED"
        ]
        if len(tc_events) != 1:
            tc_errors.append(run_id + ":tc_event_count")
        else:
            profiles = tc_events[0]["details"].get("profiles", [])
            if len(profiles) != int(row["tc_profile_count"]):
                tc_errors.append(run_id + ":tc_profile_count")
            for profile in profiles:
                if not (
                    5.0 <= float(profile["base_delay_ms"]) <= 20.0
                    and 1.0 <= float(profile["jitter_ms"]) <= 5.0
                    and 0.0 <= float(profile["packet_loss_percent"]) <= 1.0
                    and 40.0 <= float(profile["bandwidth_mbps"]) <= 100.0
                    and 100 <= int(profile["queue_limit_packets"]) <= 300
                ):
                    tc_errors.append(run_id + ":tc_profile_bounds")
                    break

        backoffs = [
            event
            for event in run_events
            if event["stage"] == "VERIFICATION_RETRY_BACKOFF"
        ]
        attempt_two_ping = sum(
            int(event["details"].get("attempt", 0)) == 2 for event in ping_events
        )
        attempt_two_iperf = sum(
            int(event["details"].get("attempt", 0)) == 2 for event in iperf_events
        )
        if int(row["retry_count"]) > 0:
            retry_runs += 1
        if (
            len(backoffs) != int(row["retry_count"])
            or attempt_two_ping != int(row["ping_retried_edges"])
            or attempt_two_iperf != int(row["iperf_retried_flows"])
            or any(
                int(event["details"]["configured_backoff_ms"]) != 200
                or float(event["details"]["observed_backoff_ms"]) < 190.0
                for event in backoffs
            )
        ):
            retry_errors.append(run_id)
        if _bool(row["background_traffic_enabled"]):
            background_runs += 1
            activation = next(
                event
                for event in run_events
                if event["stage"] == "ACTIVATION_FINISHED"
            )
            background = activation["details"]["background_traffic"]
            if not (
                0.10 <= float(background["utilization"]) <= 0.35
                and background["server_command"]
                and background["client_command"]
                and "iperf3" in background["client_command"]
                and "-u" in background["client_command"]
            ):
                tc_errors.append(run_id + ":background_traffic")

    _record(
        checks,
        "Fig. 1 is real wall-clock Linux data-plane measurement",
        not run_errors and not infrastructure_failures,
        {
            "invalid_timing_or_mode": run_errors,
            "infrastructure_failures": infrastructure_failures,
            "successful_runs": sum(_bool(row["success"]) for row in rows),
            "formation_failures": sum(not _bool(row["success"]) for row in rows),
        },
    )
    _record(
        checks,
        "Fig. 1 logs actual route/rule, ping, and iPerf3 commands and metrics",
        not command_errors,
        {"errors": command_errors},
    )
    _record(
        checks,
        "Fig. 1 applies seed-derived real HTB/netem and background traffic",
        not tc_errors and background_runs > 0,
        {"errors": tc_errors, "background_traffic_runs": background_runs},
    )
    _record(
        checks,
        "Fig. 1 retries are observed probes separated by real fixed backoff",
        not retry_errors,
        {"errors": retry_errors, "runs_with_retry": retry_runs},
    )


def _audit_exp2(
    checks: list[dict[str, object]],
    exp2_dir: Path,
    rows: list[dict[str, str]],
) -> None:
    expected_ratios = {0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3}
    groups: dict[tuple[float, int], list[dict[str, str]]] = defaultdict(list)
    per_point = Counter()
    formula_errors: list[str] = []
    for row in rows:
        gamma = float(row["demand_to_capacity_ratio"])
        seed = int(row["seed"])
        groups[(gamma, seed)].append(row)
        per_point[(gamma, row["method"])] += 1
        c_eff = min(
            float(row["c_t_mbps"]),
            float(row["c_n_mbps"]),
            float(row["c_p_mbps"]),
        )
        if (
            not math.isclose(c_eff, float(row["c_eff_mbps"]), abs_tol=1e-9)
            or not math.isclose(
                float(row["r_req_mbps"]) / c_eff,
                gamma,
                abs_tol=1e-9,
            )
        ):
            formula_errors.append(row["run_id"])
    pair_errors = []
    for key, group in groups.items():
        if (
            {row["method"] for row in group} != EXP2_METHODS
            or len({row["scenario_fingerprint"] for row in group}) != 1
            or len({row["environment_fingerprint"] for row in group}) != 1
            or len({row["conflict_class"] for row in group}) != 1
        ):
            pair_errors.append(key)
    environment_by_seed: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        environment_by_seed[int(row["seed"])].add(
            row["environment_fingerprint"]
        )
    environment_errors = [
        seed
        for seed, fingerprints in environment_by_seed.items()
        if len(fingerprints) != 1
    ]
    config = _load_yaml(Path("configs/exp2_demand_capacity_ratio.yaml"))
    expected_hash = _configuration_sha256(config)
    count_ok = (
        len(rows) == 2800
        and set(per_point.values()) == {100}
        and set(gamma for gamma, _method in per_point) == expected_ratios
        and {int(row["seed"]) for row in rows} == set(range(100))
    )
    _record(
        checks,
        "Fig. 2 uses the frozen seven-point gamma grid with 100 all-seed scenarios",
        count_ok
        and {row["configuration_sha256"] for row in rows} == {expected_hash},
        {
            "runs": len(rows),
            "point_counts": {
                f"{gamma:g}:{method}": count
                for (gamma, method), count in sorted(per_point.items())
            },
        },
    )
    _record(
        checks,
        "Fig. 2 gamma formula and nuisance-distribution invariance hold",
        not formula_errors and not environment_errors,
        {
            "formula_errors": formula_errors,
            "environment_invariance_errors": environment_errors,
        },
    )
    _record(
        checks,
        "Fig. 2 methods are same-scenario paired and Ground Truth uses true state",
        not pair_errors
        and all(_bool(row["ground_truth_uses_true_state"]) for row in rows),
        {"pairing_errors": pair_errors},
    )

    unique = {key: group[0] for key, group in groups.items()}
    classes = {row["conflict_class"] for row in unique.values()}
    highest_rows = [
        row for (gamma, _seed), row in unique.items() if gamma == 1.3
    ]
    unresolvable_highest = sum(
        row["conflict_class"] == "UNRESOLVABLE_CONFLICT"
        for row in highest_rows
    )
    ours_unresolvable_errors = [
        row["run_id"]
        for row in rows
        if row["method"] == "ours"
        and row["conflict_class"] == "UNRESOLVABLE_CONFLICT"
        and (
            _bool(row["qos_satisfied"])
            or not _bool(row["safe_rejection"])
            or _bool(row["unsafe_execution"])
        )
    ]
    _record(
        checks,
        "Fig. 2 preserves all Ground Truth classes and safe rejection semantics",
        classes
        == {
            "NO_CONFLICT",
            "RESOLVABLE_CONFLICT",
            "UNRESOLVABLE_CONFLICT",
        }
        and 0 < unresolvable_highest < len(highest_rows)
        and not ours_unresolvable_errors,
        {
            "classes": sorted(classes),
            "gamma_1.3_unresolvable": unresolvable_highest,
            "gamma_1.3_scenarios": len(highest_rows),
            "ours_unresolvable_errors": ours_unresolvable_errors,
        },
    )

    pilot_config = config["experiment"]["pilot_source"]
    pilot_path = Path(pilot_config) / "raw" / "runs.csv"
    pilot_ok = False
    pilot_details: dict[str, object] = {"path": str(pilot_path)}
    if pilot_path.exists():
        pilot = _read_csv(pilot_path)
        pilot_unique = {
            (float(row["demand_to_capacity_ratio"]), int(row["seed"])): row
            for row in pilot
        }
        pilot_highest = max(gamma for gamma, _seed in pilot_unique)
        pilot_rows = [
            row
            for (gamma, _seed), row in pilot_unique.items()
            if gamma == pilot_highest
        ]
        pilot_unresolvable = sum(
            row["conflict_class"] == "UNRESOLVABLE_CONFLICT"
            for row in pilot_rows
        )
        pilot_ok = (
            pilot_highest > 1.3
            and pilot_unresolvable == len(pilot_rows)
            and max(float(row["demand_to_capacity_ratio"]) for row in rows)
            == 1.3
        )
        pilot_details.update(
            highest_gamma=pilot_highest,
            scenarios=len(pilot_rows),
            unresolvable=pilot_unresolvable,
        )
    _record(
        checks,
        "Fig. 2 excludes only the pre-run pilot's fully infeasible region",
        pilot_ok,
        pilot_details,
    )


def _audit_exp3(
    checks: list[dict[str, object]],
    exp3_dir: Path,
    rows: list[dict[str, str]],
) -> None:
    observed = {
        name: _sha256(exp3_dir / "raw" / name)
        for name in EXP3_EXPECTED_HASHES
    }
    _record(
        checks,
        "Exp. 3 original raw artifacts remain byte-for-byte unchanged",
        observed == EXP3_EXPECTED_HASHES,
        {
            "expected": EXP3_EXPECTED_HASHES,
            "observed": observed,
        },
    )
    relevant = [
        row
        for row in rows
        if row["method"] in {"proposed", "full_rebuild"}
        and (
            row["scenario"].startswith("agent_count:")
            or row["scenario"].startswith("removal_ratio:")
        )
    ]
    groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in relevant:
        groups[(row["scenario"], int(row["seed"]))].append(row)
    errors = [
        key
        for key, group in groups.items()
        if {row["method"] for row in group} != {"proposed", "full_rebuild"}
        or len({row["scenario_fingerprint"] for row in group}) != 1
        or not all(_bool(row["success"]) for row in group)
    ]
    point_counts = Counter((row["scenario"], row["method"]) for row in relevant)
    _record(
        checks,
        "Fig. 3 uses all unchanged, successful, same-seed paired runs",
        not errors and set(point_counts.values()) == {30},
        {
            "pairing_errors": errors,
            "point_counts": {
                f"{scenario}:{method}": count
                for (scenario, method), count in sorted(point_counts.items())
            },
        },
    )


def _audit_exp4(
    checks: list[dict[str, object]],
    exp4_dir: Path,
    rows: list[dict[str, str]],
) -> None:
    pairs: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    point_counts = Counter()
    for row in rows:
        pairs[(row["scenario"], int(row["seed"]))].append(row)
        point_counts[(row["scenario"], row["method"])] += 1
    pair_errors = [
        key
        for key, group in pairs.items()
        if {row["method"] for row in group} != EXP4_METHODS
        or len({row["scenario_fingerprint"] for row in group}) != 1
        or len({row["fault_fingerprint"] for row in group}) != 1
        or len({row["fault_effective_at"] for row in group}) != 1
        or len({row["failure_detected_at"] for row in group}) != 1
    ]
    invariant_errors = [
        row["run_id"]
        for row in rows
        if not _bool(row["stable_versions_consistent"])
        or not _bool(row["staged_rules_empty"])
        or int(row["residual_rules"]) != 0
        or (
            _bool(row["recovery_success"])
            and not _bool(row["qos_recovered"])
        )
    ]
    _record(
        checks,
        "Fig. 4 has 30 same-seed paired runs at every failure point",
        len(rows) == 1350
        and len(pairs) == 450
        and set(point_counts.values()) == {30}
        and not pair_errors
        and not invariant_errors,
        {
            "runs": len(rows),
            "paired_groups": len(pairs),
            "pairing_errors": pair_errors,
            "transaction_invariant_errors": invariant_errors,
        },
    )

    physical = [
        row for row in rows if row["fault_type"] == "PHYSICAL_CAPACITY_DROP"
    ]
    physical_errors = [
        row["run_id"]
        for row in physical
        if not _bool(row["fault_was_disruptive"])
        or not _bool(row["requirement_violated_before_recovery"])
        or float(row["pre_recovery_requirement_mbps"]) <= 0.0
        or float(row["post_failure_capacity_mbps"])
        >= float(row["pre_recovery_requirement_mbps"])
        or not math.isclose(
            float(row["pre_recovery_violation_margin_mbps"]),
            float(row["pre_recovery_requirement_mbps"])
            - float(row["post_failure_capacity_mbps"]),
            abs_tol=1e-9,
        )
    ]
    config = _load_yaml(
        Path("configs/exp4_cspf_failure_reconfiguration.yaml")
    )
    physical_ratios = config["fault_sweeps"][
        "physical_post_failure_to_requirement_ratios"
    ]
    _record(
        checks,
        "Every Physical Capacity Failure violates its service requirement before recovery",
        len(physical) == 450
        and not physical_errors
        and physical_ratios == [0.90, 0.75, 0.60, 0.45, 0.30]
        and all(float(value) < 1.0 for value in physical_ratios),
        {
            "physical_method_rows": len(physical),
            "unique_faults": len(
                {(row["scenario"], row["seed"]) for row in physical}
            ),
            "semantic_errors": physical_errors,
            "frozen_ratios": physical_ratios,
        },
    )

    cspf = [row for row in rows if row["method"] == "cspf"]
    cspf_errors = [
        row["run_id"]
        for row in cspf
        if row["selected_layers"] not in {"", "network"}
        or int(row["changed_agents"]) != 0
        or int(row["changed_physical_bindings"]) != 0
        or row["selected_actions"] not in {"", "SWITCH_ROUTE"}
    ]
    proposals = _read_jsonl(exp4_dir / "raw" / "proposals.jsonl")
    cspf_proposals = [
        row for row in proposals if row.get("method") == "cspf"
    ]
    allowed_inputs = {
        "topology",
        "link_up",
        "available_bandwidth",
        "link_delay",
        "te_cost",
        "flow_source",
        "flow_destination",
        "required_bandwidth",
        "maximum_delay",
    }
    proposal_errors = []
    for row in cspf_proposals:
        proposal = row.get("proposal", {})
        parameters = proposal.get("parameters", {})
        if (
            proposal.get("layer") != "network"
            or proposal.get("action") != "SWITCH_ROUTE"
            or set(parameters.get("allowed_inputs", ())) != allowed_inputs
        ):
            proposal_errors.append(row.get("run_id", "unknown"))
    _record(
        checks,
        "CSPF is an implemented constrained-routing baseline with network-only inputs/actions",
        len(cspf) == 450
        and len(cspf_proposals) == 450
        and not cspf_errors
        and not proposal_errors,
        {
            "cspf_rows": len(cspf),
            "cspf_proposals": len(cspf_proposals),
            "network_only_run_errors": cspf_errors,
            "proposal_errors": proposal_errors,
            "allowed_inputs": sorted(allowed_inputs),
        },
    )


def _audit_plotting_data(
    checks: list[dict[str, object]],
    figure_dir: Path,
    exp1_raw_path: Path,
    exp2_raw_path: Path,
    exp3_raw_path: Path,
    exp4_raw_path: Path,
) -> None:
    expected_fig1 = build_fig1_data(exp1_raw_path)
    expected_fig2 = build_fig2_data(exp2_raw_path)
    expected_fig3a, expected_fig3b = build_fig3_data(exp3_raw_path)
    expected_fig4a, expected_fig4b = build_fig4_data(exp4_raw_path)
    expected = {
        "fig1.csv": expected_fig1,
        "fig2.csv": expected_fig2,
        "fig3a.csv": expected_fig3a,
        "fig3b.csv": expected_fig3b,
        "fig4a.csv": expected_fig4a,
        "fig4b.csv": expected_fig4b,
    }
    errors = {
        name: reason
        for name, rows in expected.items()
        if (
            reason := _csv_difference(
                figure_dir / "data" / name,
                rows,
            )
        )
    }
    _record(
        checks,
        "All six plotting CSVs exactly reproduce raw-data aggregation",
        not errors,
        {"errors": errors},
    )


def _audit_figure_files(
    checks: list[dict[str, object]],
    figure_dir: Path,
) -> None:
    figures = {
        path.name
        for path in figure_dir.iterdir()
        if path.name.startswith("Fig") and path.suffix in {".pdf", ".png"}
    }
    panel_data = {
        path.name for path in (figure_dir / "data").glob("*.csv")
    }
    format_errors: list[str] = []
    vector_errors: list[str] = []
    for name in EXPECTED_FIGURES:
        path = figure_dir / name
        if not path.exists() or path.stat().st_size < 1024:
            format_errors.append(name + ":missing_or_empty")
            continue
        header = path.read_bytes()[:8]
        if path.suffix == ".pdf" and not header.startswith(b"%PDF"):
            format_errors.append(name + ":not_pdf")
        if path.suffix == ".pdf":
            payload = path.read_bytes()
            if b"/Subtype /Image" in payload or b"/Type0" not in payload:
                vector_errors.append(name)
        if path.suffix == ".png" and header != b"\x89PNG\r\n\x1a\n":
            format_errors.append(name + ":not_png")
    _record(
        checks,
        "Output contains exactly four PDF/PNG figures and six panel CSVs",
        figures == EXPECTED_FIGURES
        and panel_data == EXPECTED_PANEL_DATA
        and not format_errors
        and not vector_errors,
        {
            "figures": sorted(figures),
            "panel_data": sorted(panel_data),
            "format_errors": format_errors,
            "non_vector_or_unembedded_font_errors": vector_errors,
        },
    )

    dpi_errors: list[str] = []
    try:
        from PIL import Image

        for name in sorted(EXPECTED_FIGURES):
            if not name.endswith(".png"):
                continue
            with Image.open(figure_dir / name) as image:
                dpi = image.info.get("dpi", (0.0, 0.0))
                if min(float(value) for value in dpi) < 299.0:
                    dpi_errors.append(f"{name}:{dpi}")
    except ImportError:
        dpi_errors.append("Pillow_unavailable")
    _record(
        checks,
        "All PNG figures are exported at at least 300 dpi",
        not dpi_errors,
        {"errors": dpi_errors},
    )


def _audit_style_and_names(
    checks: list[dict[str, object]],
    figure_dir: Path,
) -> None:
    observed = {
        key: (
            value.label,
            value.color,
            value.linestyle,
            value.marker,
            value.hatch,
        )
        for key, value in METHOD_STYLES.items()
    }
    _record(
        checks,
        "Publication color, line, marker, and hatch mapping is frozen",
        observed == EXPECTED_STYLE,
        {"expected": EXPECTED_STYLE, "observed": observed},
    )
    permitted = {
        "Ours",
        "Full Reconfiguration",
        "Adjacent-Layer Coordination (ALC)",
        "Layer-wise Independent",
        "w/o Global Verification",
        "CSPF",
    }
    label_errors = []
    for path in (figure_dir / "data").glob("*.csv"):
        for row in _read_csv(path):
            if "method" in row and row["method"] not in permitted:
                label_errors.append(f"{path.name}:{row['method']}")
    _record(
        checks,
        "All final plotting CSVs use only frozen publication method names",
        not label_errors,
        {"errors": label_errors, "permitted": sorted(permitted)},
    )


def _audit_report(
    checks: list[dict[str, object]],
    report_path: Path,
) -> None:
    text = report_path.read_text(encoding="utf-8")
    required_phrases = (
        "Actual ping command",
        "Ping retry policy",
        "Actual iPerf3 client command",
        "iPerf3 duration",
        "iPerf3 retry policy",
        "netem randomized ranges",
        "HTB available-bandwidth range",
        "Background traffic",
        "Formation latency is T_form",
        "Demand-to-Capacity Ratio is defined exactly",
        "R_req is the task/application traffic requirement",
        "C_t is the transport-layer",
        "C_n is the bottleneck",
        "C_p is the available physical",
        "NO_CONFLICT",
        "RESOLVABLE_CONFLICT",
        "UNRESOLVABLE_CONFLICT",
        "Highest gamma in the main figure",
        "fully infeasible region",
        "Physical Capacity Failure is defined",
        "Proof before recovery",
        "CSPF implementation",
        "CSPF allowed information",
        "CSPF forbidden actions",
        "Same-seed paired fairness",
        "Raw and plotting data provenance",
        "Raw source SHA-256",
        "Seed policy",
        "Paper captions",
    )
    missing = [phrase for phrase in required_phrases if phrase not in text]
    _record(
        checks,
        "experiment_report.md records every required protocol and provenance item",
        not missing,
        {"missing_phrases": missing},
    )


def _audit_line_endings(checks: list[dict[str, object]]) -> None:
    paths = [
        Path("configs/exp1_netns_verified_formation.yaml"),
        Path("configs/exp1_netns_verified_formation_pilot.yaml"),
        Path("configs/exp2_demand_capacity_ratio.yaml"),
        Path("configs/exp2_demand_capacity_ratio_pilot.yaml"),
        Path("configs/exp4_cspf_failure_reconfiguration.yaml"),
        Path("experiments/exp1_netns_verified_formation.py"),
        Path("experiments/exp2_demand_capacity_ratio.py"),
        Path("scripts/aggregate_exp1_netns.py"),
        Path("scripts/aggregate_exp2_demand_capacity.py"),
        Path("scripts/audit_exp4_cspf.py"),
        Path("scripts/audit_wcnc_main.py"),
        Path("scripts/paper_style.py"),
        Path("scripts/plot_wcnc_main_figures.py"),
        Path("src/controller/cspf.py"),
        Path("src/simulation/demand_capacity_ratio.py"),
    ]
    errors = [
        str(path)
        for path in paths
        if b"\r" in path.read_bytes()
    ]
    _record(
        checks,
        "All new config, script, and source text uses LF line endings",
        not errors,
        {"CR_containing_files": errors},
    )


def _csv_difference(
    path: Path,
    expected: Sequence[dict[str, object]],
) -> str:
    if not path.exists():
        return "missing"
    actual = _read_csv(path)
    if len(actual) != len(expected):
        return f"row_count:{len(actual)}!={len(expected)}"
    for index, (observed, wanted) in enumerate(zip(actual, expected)):
        if list(observed) != list(wanted):
            return f"field_order_at_row_{index}:{list(observed)}!={list(wanted)}"
        for key, expected_value in wanted.items():
            observed_value = observed[key]
            if isinstance(expected_value, (int, float)) and not isinstance(
                expected_value, bool
            ):
                if not math.isclose(
                    float(observed_value),
                    float(expected_value),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                ):
                    return (
                        f"value_at_row_{index}:{key}:"
                        f"{observed_value}!={expected_value}"
                    )
            elif observed_value != str(expected_value):
                return (
                    f"value_at_row_{index}:{key}:"
                    f"{observed_value}!={expected_value}"
                )
    return ""


def _record(
    checks: list[dict[str, object]],
    name: str,
    passed: bool,
    details: object,
) -> None:
    checks.append(
        {
            "check": name,
            "passed": bool(passed),
            "details": details,
        }
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _configuration_sha256(config: dict[str, object]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit final WCNC experiments, plotting data, and figures"
    )
    parser.add_argument("--exp1-dir", default="results/exp1_wcnc_final")
    parser.add_argument("--exp2-dir", default="results/exp2_demand_capacity")
    parser.add_argument("--exp3-dir", default="results/exp3")
    parser.add_argument("--exp4-dir", default="results/exp4_cspf_final")
    parser.add_argument("--figure-dir", default="results/wcnc_main_final")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = audit(
        exp1_dir=Path(args.exp1_dir),
        exp2_dir=Path(args.exp2_dir),
        exp3_dir=Path(args.exp3_dir),
        exp4_dir=Path(args.exp4_dir),
        figure_dir=Path(args.figure_dir),
    )
    output = Path(args.figure_dir) / "integrity_report.json"
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("PASS" if report["pass"] else "FAIL", output)
    if not report["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
