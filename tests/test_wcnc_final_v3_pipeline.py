from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from experiments.exp1_netns_verified_formation import FormationEdge, _plan_canonical_formation
from experiments.paper_protocol import EXPERIMENT_FAILURE_METHODS
from src.simulation.paper_failure_scenarios import generate_paper_failure_snapshot
from scripts.aggregate_wcnc_final_v3 import aggregate_experiment
from scripts.normalize_wcnc_final_v3_exp1 import normalize
from scripts.plot_wcnc_final_v3 import load
from scripts.audit_wcnc_final_v3 import audit


class WcncFinalV3PipelineTests(unittest.TestCase):
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
