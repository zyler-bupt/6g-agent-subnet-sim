from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.paper_style import METHOD_STYLES
from scripts.plot_final_paper_figures import plot_exp1


class PaperStyleTests(unittest.TestCase):
    def test_method_registry_has_canonical_cross_figure_entries(self) -> None:
        self.assertEqual(METHOD_STYLES["proposed"].label, "Proposed")
        self.assertEqual(METHOD_STYLES["cspf"].marker, "s")
        self.assertEqual(METHOD_STYLES["sanet_dw"].label, "SANet-DW*")
        self.assertEqual(METHOD_STYLES["netren"].label, "NetRen*")
        self.assertEqual(METHOD_STYLES["netkeeper"].label, "NetKeeper*")
        self.assertNotEqual(
            METHOD_STYLES["proposed"].linestyle,
            METHOD_STYLES["proposed_without_batch"].linestyle,
        )


class Exp1PlotTests(unittest.TestCase):
    def test_exp1_plot_writes_one_two_panel_vector_pdf_and_png(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.csv"
            rows = []
            for method_index, method_id in enumerate(EXPERIMENT_METHODS["exp1"]):
                for task_size in (8, 16, 24, 32):
                    rows.append(
                        _summary_row(
                            method_id,
                            "task_size",
                            "task_size",
                            task_size,
                            "formation_latency_ms",
                            8.0 + method_index * 3.0 + task_size,
                        )
                    )
                for churn in (0, 10, 20, 30):
                    rows.append(
                        _summary_row(
                            method_id,
                            "state_churn",
                            "state_churn_probability_percent",
                            churn,
                            "success_rate_percent",
                            100.0 - method_index * churn * 0.5,
                        )
                    )
            with summary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            outputs = plot_exp1(summary, root / "figures")

            pdf = root / "figures" / "Fig1_Formation.pdf"
            png = root / "figures" / "Fig1_Formation.png"
            self.assertEqual(outputs, (pdf, png))
            self.assertTrue(pdf.exists())
            self.assertTrue(png.exists())
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
            self.assertGreater(pdf.stat().st_size, 2000)


def _summary_row(
    method_id: str,
    series: str,
    x_name: str,
    x_value: float,
    metric: str,
    estimate: float,
) -> dict[str, object]:
    return {
        "experiment": "exp1",
        "mode": "pilot",
        "series": series,
        "method_id": method_id,
        "method_label": METHOD_STYLES.get(method_id, METHOD_STYLES["proposed"]).label,
        "x_name": x_name,
        "x_value": x_value,
        "metric": metric,
        "mean": estimate,
        "p50": estimate,
        "p95": estimate + 1.0,
        "ci_lower": max(0.0, estimate - 2.0),
        "ci_upper": min(100.0, estimate + 2.0)
        if metric == "success_rate_percent"
        else estimate + 2.0,
        "sample_count": 10,
        "cluster_count": 5,
        "bootstrap_iterations": 100,
    }


if __name__ == "__main__":
    unittest.main()
