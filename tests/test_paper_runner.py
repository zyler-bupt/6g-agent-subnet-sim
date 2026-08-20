from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.run_pilot import run_pilot


ROOT = Path(__file__).resolve().parents[1]


class PaperCliEntryPointTests(unittest.TestCase):
    def test_paper_scripts_run_directly_from_the_repository_root(self) -> None:
        for relative_path in (
            "scripts/aggregate_results.py",
            "scripts/sanity_check_results.py",
            "scripts/plot_final_paper_figures.py",
            "scripts/run_pilot.py",
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
                root / "paper_figures" / "Fig2_Cross_Layer_Coordination.pdf",
                root / "paper_figures" / "Fig2_Cross_Layer_Coordination.png",
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
                root / "paper_figures" / "Fig1_Formation.pdf",
                root / "paper_figures" / "Fig1_Formation.png",
            )
            self.assertTrue(all(path.exists() for path in expected))
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
                root / "paper_figures" / "Fig3_Business_Elasticity.pdf",
                root / "paper_figures" / "Fig3_Business_Elasticity.png",
            )
            self.assertTrue(all(path.exists() for path in expected))
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Pilot only", report)
            self.assertIn("Rule Change Ratio", report)
            self.assertIn("Success Rate", report)

    async def test_exp4_pilot_orchestrates_three_panel_outputs(self) -> None:
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
                root / "paper_figures" / "Fig4_Failure_Recovery.pdf",
                root / "paper_figures" / "Fig4_Failure_Recovery.png",
            )
            self.assertTrue(all(path.exists() for path in expected))
            report = expected[3].read_text(encoding="utf-8")
            self.assertIn("Pilot only", report)
            self.assertIn("Failure Type", report)
            self.assertIn("Capacity Stress", report)


if __name__ == "__main__":
    unittest.main()
