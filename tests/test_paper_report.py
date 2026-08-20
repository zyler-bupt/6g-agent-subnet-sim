from __future__ import annotations

import unittest
from pathlib import Path

from scripts.write_experiment_report import build_experiment_report


ROOT = Path(__file__).resolve().parents[1]


class PaperExperimentReportTests(unittest.TestCase):
    def test_report_contains_all_required_adaptations_and_final_answers(self) -> None:
        report = build_experiment_report(ROOT / "results")

        for text in (
            "SRD",
            "Controller Processing Latency",
            "End-to-End Formation Latency",
            "SANet-DW*",
            "NetRen*",
            "NetKeeper*",
            "Number of Affected Agents",
            "Modification Scope",
            "baseline implementation anomaly",
            "全部 100% / 全失败",
            "stress range",
            "raw CSV",
            "final PDF figures",
            "* denotes an adaptation",
            "results/paper_figures_final/",
        ):
            self.assertIn(text, report)
        for experiment in ("Exp.1", "Exp.2", "Exp.3", "Exp.4"):
            self.assertIn(f"## {experiment}", report)


if __name__ == "__main__":
    unittest.main()
