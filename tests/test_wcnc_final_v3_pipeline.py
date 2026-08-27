from __future__ import annotations

import csv
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.exp1_netns_verified_formation import FormationEdge, _plan_canonical_formation
from experiments.paper_protocol import EXPERIMENT_FAILURE_METHODS
from src.simulation.paper_failure_scenarios import generate_paper_failure_snapshot
from scripts.aggregate_wcnc_final_v3 import aggregate_experiment
from scripts.normalize_wcnc_final_v3_exp1 import _configuration_sha256, normalize
from scripts.normalize_wcnc_final_v3_exp1_transactional import normalize_transactional
from scripts.plot_wcnc_final_v3 import load
from scripts.audit_wcnc_final_v3 import (
    audit,
    exp1_canonical_grid_errors,
    exp1_paired_eligibility_errors,
)


class WcncFinalV3PipelineTests(unittest.TestCase):
    def test_transactional_normalizer_requires_exact_2250_grid(self) -> None:
        """A formal transactional artifact cannot be normalized from a partial run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete_runs = root / "runs.csv"
            attempts = root / "attempts.jsonl"
            target = root / "trials.csv"
            fields = (
                "protocol_id", "arm_id", "phase", "run_id", "scenario_class",
                "seed", "num_agents", "method_id", "fault_schedule_fingerprint",
                "configuration_sha256", "result_mode", "success", "timeout",
                "failure_reason",
            )
            with incomplete_runs.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "protocol_id": "wcnc_final_v3",
                        "arm_id": "exp1_transactional_v1",
                        "phase": "formal",
                        "run_id": f"run-{method}",
                        "scenario_class": "command_rejection",
                        "seed": 0,
                        "num_agents": 4,
                        "method_id": method,
                        "fault_schedule_fingerprint": "f" * 64,
                        "configuration_sha256": "not-reached",
                        "result_mode": "real_linux_netns_transactional_formation",
                        "success": True,
                        "timeout": False,
                        "failure_reason": "",
                    })
            attempts.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "2250"):
                normalize_transactional(
                    incomplete_runs,
                    target,
                    Path("configs/exp1_transactional_formation_v1.yaml"),
                )

    def test_transactional_formal_aggregation_rejects_partial_raw(self) -> None:
        """The paper aggregation path cannot be run directly on a partial raw file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "trials.csv"
            fields = (
                "scenario_class", "num_agents", "seed", "method_id",
                "fault_schedule_fingerprint", "success", "timeout",
                "verified_correct", "attempt_count", "rollback_scope_objects",
                "wasted_rule_commands", "partial_state_exposure_ms",
            )
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "scenario_class": "command_rejection", "num_agents": 4,
                        "seed": 0, "method_id": method,
                        "fault_schedule_fingerprint": "f" * 64,
                        "success": True, "timeout": False,
                        "verified_correct": True, "attempt_count": 1,
                        "rollback_scope_objects": "[]", "wasted_rule_commands": 0,
                        "partial_state_exposure_ms": 0,
                    })
            with self.assertRaisesRegex(ValueError, "2250"):
                aggregate_experiment("exp1_transactional", raw, root / "metrics.csv")

    def test_paired_differences_do_not_collapse_repeated_scenario_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "trials.csv"
            fields = (
                "scenario_fingerprint", "seed", "num_agents", "method_id",
                "success", "paired_analysis_eligible", "verified_formation_latency_s",
                "route_install_latency_s",
            )
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for seed in (0, 1):
                    for method, latency in (("proposed", 0.010), ("cspf", 0.012)):
                        writer.writerow({
                            "scenario_fingerprint": "shared-topology-fingerprint",
                            "seed": seed,
                            "num_agents": 4,
                            "method_id": method,
                            "success": True,
                            "paired_analysis_eligible": True,
                            "verified_formation_latency_s": latency,
                            "route_install_latency_s": latency,
                        })
            aggregate_experiment("exp1", raw, root / "metrics.csv")
            with (root / "paired_differences.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            latency = next(
                row for row in rows
                if row["method_id"] == "cspf"
                and row["metric"] == "conditional_verified_latency_ms"
            )
            self.assertEqual(latency["paired_trials"], "2")
            self.assertAlmostEqual(float(latency["paired_difference"]), 2.0)

    def test_exp1_audit_rejects_rowwise_instead_of_paired_attrition(self) -> None:
        rows = []
        for method in ("proposed", "cspf", "global_sfc_embedding"):
            rows.append({
                "scenario_fingerprint": "a" * 64,
                "seed": "0",
                "method_id": method,
                "failure_stage": "PREPARATION" if method == "proposed" else "",
                "infrastructure_valid": "false" if method == "proposed" else "true",
                "paired_analysis_eligible": "false" if method == "proposed" else "true",
                "paired_exclusion_reason": (
                    "paired_preparation_failure" if method == "proposed" else ""
                ),
            })
        errors = exp1_paired_eligibility_errors(rows)
        self.assertIn("paired eligibility is inconsistent", errors[0])

        for row in rows:
            row["paired_analysis_eligible"] = "false"
            row["paired_exclusion_reason"] = "paired_preparation_failure"
        self.assertEqual(exp1_paired_eligibility_errors(rows), [])

        for row in rows:
            row["failure_stage"] = (
                "DATA_PLANE_VERIFICATION"
                if row["method_id"] == "global_sfc_embedding"
                else ""
            )
            row["infrastructure_valid"] = "true"
            row["paired_analysis_eligible"] = "true"
            row["paired_exclusion_reason"] = ""
        self.assertEqual(exp1_paired_eligibility_errors(rows), [])

    def test_exp1_audit_requires_exact_method_set_for_each_seed(self) -> None:
        rows = []
        for seed, methods in (
            ("0", ("proposed", "cspf", "global_sfc_embedding")),
            ("1", ("proposed", "cspf", "cspf")),
        ):
            for method in methods:
                rows.append({
                    "scenario_fingerprint": "shared-fingerprint",
                    "seed": seed,
                    "method_id": method,
                    "failure_stage": "",
                    "infrastructure_valid": "true",
                    "paired_analysis_eligible": "true",
                    "paired_exclusion_reason": "",
                })
        errors = exp1_paired_eligibility_errors(rows)
        self.assertTrue(any("canonical method set" in error for error in errors))

    def test_exp1_canonical_grid_rejects_an_entire_missing_triplet(self) -> None:
        rows = [
            {"num_agents": "4", "seed": "0", "method_id": method}
            for method in ("proposed", "cspf", "global_sfc_embedding")
        ]
        errors = exp1_canonical_grid_errors(rows)
        self.assertTrue(any("750 rows" in error for error in errors))

    def test_exp1_canonical_grid_rejects_fractional_task_size(self) -> None:
        rows = [
            {"num_agents": str(size), "seed": str(seed), "method_id": method}
            for size in (4, 8, 12, 16, 20)
            for seed in range(50)
            for method in ("proposed", "cspf", "global_sfc_embedding")
        ]
        self.assertEqual(exp1_canonical_grid_errors(rows), [])
        rows[0]["num_agents"] = "4.5"
        self.assertTrue(exp1_canonical_grid_errors(rows))

    def test_exp1_normalizer_default_rejects_an_incomplete_formal_grid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runs.csv"
            target = root / "trials.csv"
            config_path = Path("configs/exp1_netns_verified_formation_v3.yaml")
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "num_agents",
                "method_id", "failure_reason", "configuration_sha256",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "result_mode": "real_linux_netns_veth_tc_data_plane",
                        "scenario_fingerprint": "one-pair",
                        "seed": 0,
                        "num_agents": 4,
                        "method_id": method,
                        "failure_reason": "",
                        "configuration_sha256": _configuration_sha256(config_path),
                    })
            with self.assertRaisesRegex(ValueError, "750 rows"):
                normalize(source, target, config_path)

    def test_exp1_paired_attrition_retains_raw_and_keeps_data_plane_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runs.csv"
            normalized = root / "trials.csv"
            metrics = root / "metrics.csv"
            config_path = Path("configs/exp1_netns_verified_formation_v3.yaml")
            configuration_hash = _configuration_sha256(config_path)
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "num_agents",
                "method_id", "success", "failure_stage", "failure_reason",
                "configuration_sha256", "verified_formation_latency_s",
                "route_install_latency_s", "control_messages", "rules_installed",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for fingerprint, seed, failing_method, failure_stage in (
                    ("a" * 64, 0, "proposed", "PREPARATION"),
                    ("b" * 64, 1, "global_sfc_embedding", "DATA_PLANE_VERIFICATION"),
                ):
                    for method in ("proposed", "cspf", "global_sfc_embedding"):
                        failed = method == failing_method
                        writer.writerow({
                            "result_mode": "real_linux_netns_veth_tc_data_plane",
                            "scenario_fingerprint": fingerprint,
                            "seed": seed,
                            "num_agents": 4,
                            "method_id": method,
                            "success": str(not failed),
                            "failure_stage": failure_stage if failed else "",
                            "failure_reason": "failed" if failed else "",
                            "configuration_sha256": configuration_hash,
                            "verified_formation_latency_s": (
                                "" if failure_stage == "PREPARATION" and failed
                                else {"proposed": "0.010", "cspf": "0.012", "global_sfc_embedding": "0.014"}[method]
                            ),
                            "route_install_latency_s": {
                                "proposed": "0.001", "cspf": "0.002",
                                "global_sfc_embedding": "0.003",
                            }[method],
                            "control_messages": 1,
                            "rules_installed": 2,
                        })

            output = normalize(
                source,
                normalized,
                config_path,
                require_complete_grid=False,
            )
            self.assertEqual(len(output), 6)
            preparation_group = [
                row for row in output if row["scenario_fingerprint"] == "a" * 64
            ]
            self.assertEqual(
                {row["paired_analysis_eligible"] for row in preparation_group},
                {"false"},
            )
            self.assertEqual(
                [row["method_id"] for row in preparation_group if row["infrastructure_valid"] == "false"],
                ["proposed"],
            )
            data_plane_group = [
                row for row in output if row["scenario_fingerprint"] == "b" * 64
            ]
            self.assertEqual(
                {row["paired_analysis_eligible"] for row in data_plane_group},
                {"true"},
            )

            aggregated = aggregate_experiment("exp1", normalized, metrics)
            success = {
                row["method_id"]: (row["numerator"], row["denominator"])
                for row in aggregated
                if row["metric"] == "success_rate"
            }
            self.assertEqual(success["proposed"], (1, 1))
            self.assertEqual(success["cspf"], (1, 1))
            self.assertEqual(success["global_sfc_embedding"], (0, 1))
            retention = [
                row for row in aggregated
                if row["metric"] == "paired_scenario_retention_rate"
            ]
            self.assertEqual(
                {(row["numerator"], row["denominator"]) for row in retention},
                {(1, 2)},
            )
            preparation = [
                row for row in aggregated
                if row["metric"] == "preparation_failure_rate"
            ]
            self.assertEqual(
                {(row["numerator"], row["denominator"]) for row in preparation},
                {(1, 2)},
            )
            with metrics.with_name("paired_differences.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                differences = list(csv.DictReader(handle))
            latency_differences = {
                (row["method_id"], row["metric"]): float(row["paired_difference"])
                for row in differences
                if row["metric"] in {
                    "conditional_verified_latency_ms", "route_install_latency_ms"
                }
            }
            self.assertEqual(
                latency_differences[("cspf", "conditional_verified_latency_ms")],
                2.0,
            )
            self.assertEqual(
                latency_differences[("cspf", "route_install_latency_ms")],
                1.0,
            )
            self.assertEqual(
                latency_differences[("global_sfc_embedding", "route_install_latency_ms")],
                2.0,
            )

    def test_exp1_plot_outputs_total_and_route_latency_not_success_curve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = root / "aggregated" / "exp1" / "metrics.csv"
            metrics.parent.mkdir(parents=True)
            fields = (
                "protocol_id", "experiment", "series", "method_id", "x_name",
                "x_value", "metric", "estimate", "ci_low", "ci_high",
                "numerator", "denominator", "interval", "raw_path", "raw_sha256",
            )
            with metrics.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for method in ("proposed", "cspf"):
                    for metric, estimate in (
                        ("success_rate", 1.0),
                        ("conditional_verified_latency_ms", 5.0),
                        ("route_install_latency_ms", 2.0),
                    ):
                        writer.writerow({
                            "protocol_id": "wcnc_final_v3", "experiment": "exp1",
                            "series": "", "method_id": method, "x_name": "num_agents",
                            "x_value": 4, "metric": metric, "estimate": estimate,
                            "ci_low": estimate, "ci_high": estimate, "numerator": 1,
                            "denominator": 1, "interval": "test", "raw_path": "raw.csv",
                            "raw_sha256": hashlib.sha256(b"raw").hexdigest(),
                        })
            environment = dict(os.environ)
            environment["MPLCONFIGDIR"] = str(root / "mpl")
            subprocess.run(
                (
                    sys.executable, "scripts/plot_wcnc_final_v3.py", "--root", str(root),
                    "--experiment", "exp1",
                ),
                check=True,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertTrue((root / "figures" / "exp1_conditional_verified_latency_ms.pdf").is_file())
            self.assertTrue((root / "figures" / "exp1_route_install_latency_ms.pdf").is_file())
            self.assertFalse((root / "figures" / "exp1_success_rate.pdf").exists())
            route_svg = (
                root / "figures" / "exp1_route_install_latency_ms.svg"
            ).read_text(encoding="utf-8")
            self.assertIn("CSPF-based Formation", route_svg)
            self.assertNotIn("CSPF Recovery", route_svg)

    def test_exp1_baselines_have_executable_deterministic_planners(self) -> None:
        edges = (FormationEdge("e1", 0, 1, 1.0), FormationEdge("e2", 1, 2, 1.0))
        for method in ("proposed", "cspf", "global_sfc_embedding"):
            first = _plan_canonical_formation(method, edges, 2)
            self.assertEqual(first, _plan_canonical_formation(method, edges, 2))
            self.assertEqual(first["planned_edge_count"], 2)

    def test_exp4_has_only_applicable_methods_and_no_zero_encoded_na(self) -> None:
        self.assertEqual(set(EXPERIMENT_FAILURE_METHODS["exp4"]["link_failure"]), {"proposed", "cspf", "full_rebuild"})
        self.assertNotIn("cspf", EXPERIMENT_FAILURE_METHODS["exp4"]["agent_failure"])
        self.assertNotIn("cspf", EXPERIMENT_FAILURE_METHODS["exp4"]["capacity_degradation"])
        headroom = generate_paper_failure_snapshot(
            "capacity_degradation", 1.1, 0, 0, capacity_ratio=1.1
        )
        self.assertAlmostEqual(headroom.post_fault_capacity_ratio, 1.1)

    def test_raw_to_aggregate_is_repeatable_and_retains_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = root / "trials.csv"
            fields = ["seed", "method_id", "gamma", "scenario_fingerprint", "success", "timeout", "failure_reason", "ground_truth_resolvable", "qos_satisfied", "pre_verification_correct_decision", "safe_rejection", "rollback_triggered", "verification_rescued", "unsafe_proposal_before_verification", "coordination_latency_ms"]
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for method in ("proposed", "sanet_dw", "weighted_sum", "independent"):
                    writer.writerow({"seed": 0, "method_id": method, "gamma": 1.0, "scenario_fingerprint": "same", "success": method != "independent", "timeout": False, "failure_reason": "failed" if method == "independent" else "", "ground_truth_resolvable": True, "qos_satisfied": method != "independent", "pre_verification_correct_decision": method != "independent", "safe_rejection": False, "rollback_triggered": method == "independent", "verification_rescued": method == "independent", "unsafe_proposal_before_verification": method == "independent", "coordination_latency_ms": 1})
            one = root / "one.csv"; two = root / "two.csv"
            aggregate_experiment("exp2", raw, one); aggregate_experiment("exp2", raw, two)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            with one.open(encoding="utf-8", newline="") as handle:
                aggregated = list(csv.DictReader(handle))
            failed = [row for row in aggregated if row["method_id"] == "independent" and row["metric"] == "success_rate"]
            self.assertEqual(failed[0]["denominator"], "1")
            self.assertEqual(failed[0]["estimate"], "0.0")

    def test_formal_plot_loader_refuses_demo(self) -> None:
        with self.assertRaisesRegex(ValueError, "demo"):
            load(Path("figures/out/aggregated_demo.csv"))

    def test_exp1_normalizer_rejects_non_measured_or_unpaired_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runs.csv"; target = Path(directory) / "out.csv"
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("result_mode", "scenario_fingerprint", "seed", "method_id", "failure_reason")); writer.writeheader()
                writer.writerow({"result_mode": "deterministic_cost_model", "scenario_fingerprint": "x", "seed": 0, "method_id": "proposed", "failure_reason": ""})
            with self.assertRaisesRegex(ValueError, "measured"):
                normalize(source, target)

    def test_exp1_normalizer_rejects_rows_from_a_different_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "runs.csv"; target = Path(directory) / "out.csv"
            fields = (
                "result_mode", "scenario_fingerprint", "seed", "method_id",
                "failure_reason", "configuration_sha256",
            )
            with source.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for method in ("proposed", "cspf", "global_sfc_embedding"):
                    writer.writerow({
                        "result_mode": "real_linux_netns_veth_tc_data_plane",
                        "scenario_fingerprint": "paired", "seed": 0,
                        "method_id": method, "failure_reason": "",
                        "configuration_sha256": "0" * 64,
                    })
            with self.assertRaisesRegex(ValueError, "configuration hash"):
                normalize(source, target)

    def test_final_audit_rejects_exp1_rows_from_a_different_configuration(self) -> None:
        methods = {
            "exp1": ("proposed", "cspf", "global_sfc_embedding"),
            "exp2": ("proposed", "sanet_dw", "weighted_sum", "independent"),
            "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
            "exp4": ("proposed", "cspf", "full_rebuild"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for exp, method_set in methods.items():
                path = root / "raw" / exp / "trials.csv"; path.parent.mkdir(parents=True)
                fields = (
                    "scenario_fingerprint", "method_id", "success", "failure_reason",
                    "failure_type", "configuration_sha256",
                )
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                    for method in method_set:
                        writer.writerow({
                            "scenario_fingerprint": "paired", "method_id": method,
                            "success": True, "failure_reason": "",
                            "failure_type": "link_failure",
                            "configuration_sha256": "0" * 64 if exp == "exp1" else "",
                        })
            hashes = {
                str(path.relative_to(root)): __import__("hashlib").sha256(path.read_bytes()).hexdigest()
                for path in (root / "raw").glob("**/*") if path.is_file()
            }
            report = audit(root, {"raw_artifact_hashes": hashes})
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("Exp1 configuration hash drift", report["errors"])

    def test_manifest_hash_detects_raw_data_drift(self) -> None:
        methods = {
            "exp1": ("proposed", "cspf", "global_sfc_embedding"),
            "exp2": ("proposed", "sanet_dw", "weighted_sum", "independent"),
            "exp3": ("proposed", "netren", "local_only", "full_rebuild"),
            "exp4": ("proposed", "cspf", "full_rebuild"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for exp, method_set in methods.items():
                path = root / "raw" / exp / "trials.csv"; path.parent.mkdir(parents=True)
                fields = ("scenario_fingerprint", "method_id", "success", "failure_reason", "failure_type")
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                    for method in method_set:
                        writer.writerow({"scenario_fingerprint": "paired", "method_id": method, "success": True, "failure_reason": "", "failure_type": "link_failure"})
            hashes = {str(path.relative_to(root)): __import__("hashlib").sha256(path.read_bytes()).hexdigest() for path in (root / "raw").glob("**/*") if path.is_file()}
            manifest = {
                "git_commit": "0" * 40,
                "raw_artifact_hashes": hashes,
                "source_hashes": {"scripts/aggregate_wcnc_final_v3.py": "0" * 64},
                "config_hashes": {"configs/wcnc_final_v3.yaml": "0" * 64},
            }
            target = root / "raw" / "exp2" / "trials.csv"
            target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            report = audit(root, manifest)
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("raw artifact drift", report["errors"])
            self.assertIn("scoped source drift", report["errors"])
            self.assertIn("config drift", report["errors"])
            self.assertIn("git commit drift", report["errors"])


if __name__ == "__main__": unittest.main()
