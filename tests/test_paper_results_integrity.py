from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.aggregate_results import aggregate_experiment


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
FIGURE_STEMS = (
    "Fig1_Formation",
    "Fig2_CrossLayer",
    "Fig3_Elasticity",
    "Fig4_Recovery",
)
HISTORICAL_METHODS = {
    "exp1": {"proposed", "proposed_without_batch", "cspf", "srd"},
    "exp2": {"proposed", "sanet_dw", "adjacent_layer", "independent"},
    "exp3": {"proposed", "netren", "local_only", "full_rebuild"},
    "exp4": {"proposed", "full_rebuild", "netkeeper", "cspf"},
}


class PaperResultIntegrityTests(unittest.TestCase):
    def test_checked_in_aggregates_reproduce_byte_for_byte_from_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for experiment in ("exp1", "exp2", "exp3", "exp4"):
                with self.subTest(experiment=experiment):
                    reproduced = temporary / experiment / "summary.csv"
                    aggregate_experiment(
                        RESULTS / "raw" / "paper" / experiment / "trials.csv",
                        reproduced,
                        bootstrap_iterations=5000,
                    )
                    expected = RESULTS / "aggregated" / "paper" / experiment / "summary.csv"
                    self.assertEqual(reproduced.read_bytes(), expected.read_bytes())

    def test_every_historical_paper_instance_has_its_archived_method_set(self) -> None:
        expected_rows = {"exp1": 7800, "exp2": 4200, "exp3": 3000, "exp4": 4800}
        for experiment, row_count in expected_rows.items():
            with self.subTest(experiment=experiment):
                raw = RESULTS / "raw" / "paper" / experiment / "trials.csv"
                with raw.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), row_count)
                self.assertEqual(len({int(row["seed"]) for row in rows}), 30)
                paired = {}
                for row in rows:
                    paired.setdefault(row["trial_id"], set()).add(row["method_id"])
                self.assertTrue(
                    all(
                        methods == HISTORICAL_METHODS[experiment]
                        for methods in paired.values()
                    )
                )

    def test_four_composite_figures_are_vector_pdfs_with_eight_total_panels(self) -> None:
        expected_panels = {
            "Fig1_Formation": 2,
            "Fig2_CrossLayer": 2,
            "Fig3_Elasticity": 2,
            "Fig4_Recovery": 2,
        }
        self.assertEqual(sum(expected_panels.values()), 8)
        for stem in FIGURE_STEMS:
            with self.subTest(figure=stem):
                pdf = RESULTS / "paper_figures_final" / f"{stem}.pdf"
                png = RESULTS / "paper_figures_final" / f"{stem}.png"
                source = RESULTS / "paper_figures_final" / f"{stem}.csv"
                payload = pdf.read_bytes()
                self.assertTrue(payload.startswith(b"%PDF"))
                self.assertNotIn(b"/Subtype /Image", payload)
                self.assertGreater(png.stat().st_size, 10_000)
                self.assertGreater(source.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
