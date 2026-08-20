from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.run_pilot import run_pilot


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
