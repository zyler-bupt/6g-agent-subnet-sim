from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.run_paper import validate_all_pilots, validate_pilot_manifest
from scripts.run_pilot import run_pilot


ROOT = Path(__file__).resolve().parents[1]


class PaperCliEntryPointTests(unittest.TestCase):
    def test_paper_scripts_run_directly_from_the_repository_root(self) -> None:
        for relative_path in (
            "scripts/aggregate_results.py",
            "scripts/sanity_check_results.py",
            "scripts/plot_final_paper_figures.py",
            "scripts/run_pilot.py",
            "scripts/run_paper.py",
        ):
            with self.subTest(script=relative_path):
                completed = subprocess.run(
                    ["python3", relative_path, "--help"],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


class PaperGateTests(unittest.TestCase):
    def test_paper_runner_refuses_missing_pilots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "pilot gate failed"):
                validate_all_pilots(
                    Path(directory),
                    config_path=ROOT / "configs" / "paper_experiments.yaml",
                )

    def test_manifest_requires_exact_method_set_and_config_hash(self) -> None:
        manifest = {
            "experiment": "exp1",
            "mode": "pilot",
            "config_sha256": "current-config",
            "methods": ["proposed"],
            "error_count": 0,
            "raw_sha256": "raw",
            "aggregate_sha256": "aggregate",
            "sanity_sha256": "sanity",
        }

        with self.assertRaisesRegex(RuntimeError, "method set"):
            validate_pilot_manifest(
                manifest,
                "exp1",
                current_config_hash="current-config",
            )
        manifest["methods"] = list(EXPERIMENT_METHODS["exp1"])
        with self.assertRaisesRegex(RuntimeError, "config hash"):
            validate_pilot_manifest(
                manifest,
                "exp1",
                current_config_hash="different-config",
            )

    def test_manifest_rejects_reduced_pilot_runtime_grid(self) -> None:
        manifest = {
            "experiment": "exp1",
            "mode": "pilot",
            "config_sha256": "current-config",
            "methods": list(EXPERIMENT_METHODS["exp1"]),
            "error_count": 0,
            "raw_sha256": "raw",
            "aggregate_sha256": "aggregate",
            "sanity_sha256": "sanity",
            "runtime_parameters": {
                "experiment": "exp1",
                "mode": "pilot",
                "configured_mode_spec": {
                    "topology_seeds": 5,
                    "events_per_seed": 2,
                    "rate_events_per_seed": 2,
                },
                "topology_seeds": [0],
                "event_ids": [0],
                "methods": sorted(EXPERIMENT_METHODS["exp1"]),
                "series": ["state_churn", "task_size"],
                "task_sizes": [8],
                "background_churn_probability": 0.02,
                "state_churn_percent": [0.0],
            },
        }

        with self.assertRaisesRegex(RuntimeError, "runtime grid"):
            validate_pilot_manifest(
                manifest,
                "exp1",
                current_config_hash="current-config",
            )


class PilotRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exp2_pilot_orchestrates_conditional_metrics_and_two_panel_figure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await run_pilot(
                ("exp2",),
                output_root=root,
                seeds=(0,),
                exp2_event_ids=(0,),
                exp2_conflict_densities=(0, 40),
                bootstrap_iterations=100,
            )

            self.assertEqual(result.experiments, ("exp2",))
            self.assertEqual(result.error_count, 0)
            expected = (
                root / "raw" / "pilot" / "exp2" / "trials.csv",
                root / "aggregated" / "pilot" / "exp2" / "summary.csv",
                root / "aggregated" / "pilot" / "exp2" / "sanity.json",
                root / "aggregated" / "pilot" / "exp2" / "PILOT_SUMMARY.md",
                root / "paper_figures_final" / "Fig2_CrossLayer.pdf",
                root / "paper_figures_final" / "Fig2_CrossLayer.png",
                root / "paper_figures_final" / "Fig2_CrossLayer.csv",
            )
            self.assertTrue(all(path.exists() for path in expected))
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Feasible Solution Rate", report)
            self.assertIn("Safe Rejection", report)

    async def test_exp1_pilot_orchestrates_raw_aggregate_sanity_plot_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await run_pilot(
                ("exp1",),
                output_root=root,
                seeds=(0,),
                exp1_task_sizes=(8,),
                exp1_churn_points=(0,),
                bootstrap_iterations=100,
            )

            self.assertEqual(result.experiments, ("exp1",))
            self.assertEqual(result.error_count, 0)
            expected = (
                root / "raw" / "pilot" / "exp1" / "trials.csv",
                root / "aggregated" / "pilot" / "exp1" / "summary.csv",
                root / "aggregated" / "pilot" / "exp1" / "sanity.json",
                root / "aggregated" / "pilot" / "exp1" / "PILOT_SUMMARY.md",
                root / "paper_figures_final" / "Fig1_Formation.pdf",
                root / "paper_figures_final" / "Fig1_Formation.png",
                root / "paper_figures_final" / "Fig1_Formation.csv",
            )
            self.assertTrue(all(path.exists() for path in expected))
            manifest = (
                root / "aggregated" / "pilot" / "exp1" / "pilot_manifest.json"
            ).read_text(encoding="utf-8")
            self.assertIn('"runtime_parameters"', manifest)
            self.assertIn('"background_churn_probability": 0.02', manifest)
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Pilot only", report)
            self.assertIn("P95 Formation Latency", report)
            self.assertIn("Method Ranking", report)

    async def test_exp3_pilot_orchestrates_tradeoff_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await run_pilot(
                ("exp3",),
                output_root=root,
                seeds=(0,),
                exp3_buckets=(10,),
                bootstrap_iterations=100,
            )

            self.assertEqual(result.experiments, ("exp3",))
            self.assertEqual(result.error_count, 0)
            expected = (
                root / "raw" / "pilot" / "exp3" / "trials.csv",
                root / "aggregated" / "pilot" / "exp3" / "summary.csv",
                root / "aggregated" / "pilot" / "exp3" / "sanity.json",
                root / "aggregated" / "pilot" / "exp3" / "PILOT_SUMMARY.md",
                root / "paper_figures_final" / "Fig3_Elasticity.pdf",
                root / "paper_figures_final" / "Fig3_Elasticity.png",
                root / "paper_figures_final" / "Fig3_Elasticity.csv",
            )
            self.assertTrue(all(path.exists() for path in expected))
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Pilot only", report)
            self.assertIn("Rule Change Ratio", report)
            self.assertIn("Success Rate", report)

    async def test_exp4_pilot_orchestrates_two_panel_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = await run_pilot(
                ("exp4",),
                output_root=root,
                seeds=(0,),
                exp4_event_ids=(0,),
                exp4_capacity_reductions=(10,),
                bootstrap_iterations=100,
            )

            self.assertEqual(result.experiments, ("exp4",))
            self.assertEqual(result.error_count, 0)
            expected = (
                root / "raw" / "pilot" / "exp4" / "trials.csv",
                root / "aggregated" / "pilot" / "exp4" / "summary.csv",
                root / "aggregated" / "pilot" / "exp4" / "sanity.json",
                root / "aggregated" / "pilot" / "exp4" / "PILOT_SUMMARY.md",
                root / "paper_figures_final" / "Fig4_Recovery.pdf",
                root / "paper_figures_final" / "Fig4_Recovery.png",
                root / "paper_figures_final" / "Fig4_Recovery.csv",
            )
            self.assertTrue(all(path.exists() for path in expected))
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Pilot only", report)
            self.assertIn("Failure Type", report)
            self.assertIn("Capacity Stress", report)


if __name__ == "__main__":
    unittest.main()
