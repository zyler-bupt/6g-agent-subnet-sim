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


if __name__ == "__main__":
    unittest.main()
