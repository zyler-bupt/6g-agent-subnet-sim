from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from experiments.paper_protocol import EXPERIMENT_METHODS
from scripts.paper_style import METHOD_STYLES
from scripts.plot_final_paper_figures import plot_exp1, plot_exp2, plot_exp3, plot_exp4


class PaperStyleTests(unittest.TestCase):
    def test_method_registry_has_canonical_cross_figure_entries(self) -> None:
        self.assertEqual(METHOD_STYLES["proposed"].label, "Proposed")
        self.assertEqual(METHOD_STYLES["proposed"].color, "#2ca25f")
        self.assertEqual(METHOD_STYLES["proposed"].marker, "D")
        self.assertEqual(METHOD_STYLES["cspf"].marker, "s")
        self.assertEqual(METHOD_STYLES["cspf"].color, "#e74c3c")
        self.assertEqual(METHOD_STYLES["srd"].label, "SRD")
        self.assertEqual(METHOD_STYLES["srd"].color, "#f39c12")
        self.assertEqual(METHOD_STYLES["sanet_dw"].label, "SANet-DW*")
        self.assertEqual(METHOD_STYLES["netren"].label, "NetRen*")
        self.assertEqual(METHOD_STYLES["netkeeper"].label, "NetKeeper*")
        self.assertEqual(METHOD_STYLES["netkeeper"].color, "#f39c12")
        self.assertNotEqual(
            METHOD_STYLES["netkeeper"].color,
            METHOD_STYLES["cspf"].color,
        )
        self.assertNotEqual(
            METHOD_STYLES["proposed"].linestyle,
            METHOD_STYLES["proposed_without_batch"].linestyle,
        )


class Exp1PlotTests(unittest.TestCase):
    def test_exp1_plot_writes_latency_success_pdf_png_and_source_csv(self) -> None:
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
                for task_size in (8, 16, 24, 32):
                    rows.append(
                        _summary_row(
                            method_id,
                            "task_size",
                            "task_size",
                            task_size,
                            "success_rate_percent",
                            100.0 - method_index * task_size * 0.25,
                        )
                    )
            with summary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            outputs = plot_exp1(summary, root / "figures")

            pdf = root / "figures" / "Fig1_Formation.pdf"
            png = root / "figures" / "Fig1_Formation.png"
            source = root / "figures" / "Fig1_Formation.csv"
            self.assertEqual(outputs, (pdf, png, source))
            self.assertTrue(pdf.exists())
            self.assertTrue(png.exists())
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
            self.assertGreater(pdf.stat().st_size, 2000)
            self.assertEqual(_panels(source), {"a", "b"})


class Exp2PlotTests(unittest.TestCase):
    def test_exp2_plot_writes_qos_line_and_conditional_rate_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.csv"
            rows = []
            for method_index, method_id in enumerate(EXPERIMENT_METHODS["exp2"]):
                for density in (0, 10, 20, 30, 40, 50, 60):
                    rows.append(
                        {
                            **_summary_row(
                                method_id,
                                "conflict_density",
                                "conflict_density_percent",
                                density,
                                "qos_satisfaction_rate_percent",
                                max(0.0, 100.0 - method_index * density * 0.25),
                            ),
                            "experiment": "exp2",
                        }
                    )
                for metric, value in (
                    ("feasible_solution_rate_percent", 95.0 - method_index * 12.0),
                    ("safe_rejection_rate_percent", 92.0 - method_index * 10.0),
                ):
                    rows.append(
                        {
                            **_summary_row(
                                method_id,
                                "conflict_density",
                                "conflict_density_percent",
                                50,
                                metric,
                                value,
                            ),
                            "experiment": "exp2",
                        }
                    )
            _write_rows(summary, rows)

            outputs = plot_exp2(summary, root / "figures")

            pdf = root / "figures" / "Fig2_CrossLayer.pdf"
            png = root / "figures" / "Fig2_CrossLayer.png"
            source = root / "figures" / "Fig2_CrossLayer.csv"
            self.assertEqual(outputs, (pdf, png, source))
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(_panels(source), {"a", "b"})


class Exp3PlotTests(unittest.TestCase):
    def test_exp3_plot_writes_latency_and_rule_modification_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.csv"
            rows = []
            for method_index, method_id in enumerate(EXPERIMENT_METHODS["exp3"]):
                for affected_agents in (4, 7, 10, 13, 16):
                    latency = 6.0 + method_index * 4.0 + affected_agents * 0.2
                    rule_ratio = min(105.0, affected_agents * 3 + method_index * 12.0)
                    for metric, estimate in (
                        ("reconfiguration_latency_ms", latency),
                        ("rule_change_ratio_percent", rule_ratio),
                    ):
                        row = {
                                **_summary_row(
                                    method_id,
                                    "affected_agents",
                                    "affected_agent_count",
                                    affected_agents,
                                    metric,
                                    estimate,
                                ),
                                "experiment": "exp3",
                                "mode": "paper",
                            }
                        if affected_agents == 16:
                            row["sample_count"] = 2
                            row["cluster_count"] = 2
                        else:
                            row["cluster_count"] = 30
                        rows.append(row)
            with summary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            outputs = plot_exp3(summary, root / "figures")

            pdf = root / "figures" / "Fig3_Elasticity.pdf"
            png = root / "figures" / "Fig3_Elasticity.png"
            source = root / "figures" / "Fig3_Elasticity.csv"
            self.assertEqual(outputs, (pdf, png, source))
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
            self.assertGreater(pdf.stat().st_size, 2000)
            self.assertEqual(_panels(source), {"a", "b"})
            with source.open(encoding="utf-8", newline="") as handle:
                plotted_x = {float(row["x_value"]) for row in csv.DictReader(handle)}
            self.assertNotIn(16.0, plotted_x)


class Exp4PlotTests(unittest.TestCase):
    def test_exp4_plot_writes_two_panel_latency_and_modification_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = root / "summary.csv"
            rows = []
            for method_index, method_id in enumerate(EXPERIMENT_METHODS["exp4"]):
                for failure_index in range(3):
                    for metric, estimate in (
                        ("recovery_latency_ms", 4.0 + method_index + failure_index),
                        ("modification_scope_ratio_percent", 8.0 + 4.0 * method_index),
                    ):
                        rows.append(
                            {
                                **_summary_row(
                                    method_id,
                                    "failure_type",
                                    "failure_type_index",
                                    failure_index,
                                    metric,
                                    estimate,
                                ),
                                "experiment": "exp4",
                            }
                        )
            with summary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

            outputs = plot_exp4(summary, root / "figures")

            pdf = root / "figures" / "Fig4_Recovery.pdf"
            png = root / "figures" / "Fig4_Recovery.png"
            source = root / "figures" / "Fig4_Recovery.csv"
            self.assertEqual(outputs, (pdf, png, source))
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
            self.assertTrue(png.read_bytes().startswith(b"\x89PNG"))
            self.assertGreater(pdf.stat().st_size, 2000)
            self.assertEqual(_panels(source), {"a", "b"})


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _panels(path: Path) -> set[str]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["panel"] for row in csv.DictReader(handle)}


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
        "sample_count": 30,
        "cluster_count": 5,
        "bootstrap_iterations": 100,
    }


if __name__ == "__main__":
    unittest.main()
