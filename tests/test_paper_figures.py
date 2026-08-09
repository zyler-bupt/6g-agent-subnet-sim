from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from statistics import fmean

from experiments.exp1_initial_formation import run_one
from scripts.aggregate_exp1 import aggregate_exp1
from scripts.plot_paper_figures import (
    build_fig1_data,
    build_fig2_data,
    build_fig3_data,
    build_fig4_data,
)
from src.metrics.exp1 import write_exp1_metrics_csv
from src.simulation.scenario_generator import ElasticScenarioGenerator, ScenarioConfig


ROOT = Path(__file__).resolve().parents[1]


class InitialFormationPaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_plane_and_verified_formation_definitions(self) -> None:
        snapshot = ElasticScenarioGenerator().generate(
            ScenarioConfig(num_agents=10, num_gateways=4),
            seed=7,
        )
        row = await run_one(snapshot, run_index=1, verification_config={})
        self.assertTrue(row.success)
        self.assertEqual(row.version, 1)
        self.assertGreaterEqual(row.formation_latency_ms, row.control_plane_latency_ms)
        self.assertAlmostEqual(
            row.control_plane_latency_ms,
            (row.t_stage_started - row.t_task_received) * 1000.0,
            places=5,
        )
        breakdown = (
            row.mapping_latency_ms
            + row.cross_layer_coordination_latency_ms
            + row.compilation_and_planning_latency_ms
            + row.installation_latency_ms
            + row.activation_latency_ms
            + row.verification_latency_ms
        )
        self.assertAlmostEqual(breakdown, row.formation_latency_ms, places=5)
        self.assertFalse(row.real_ping_verification)
        self.assertFalse(row.real_iperf3_verification)

    async def test_exp1_aggregate_preserves_samples_and_ci(self) -> None:
        generator = ElasticScenarioGenerator()
        rows = [
            await run_one(
                generator.generate(ScenarioConfig(num_agents=10, num_gateways=4), seed=seed),
                run_index=seed + 1,
                verification_config={},
            )
            for seed in (0, 1)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_exp1_metrics_csv(root / "runs.csv", rows)
            summary = aggregate_exp1(root / "runs.csv", root / "processed")
            self.assertEqual(len(summary), 1)
            self.assertEqual(summary[0]["runs"], 2)
            self.assertEqual(summary[0]["successful_runs"], 2)
            self.assertGreaterEqual(float(summary[0]["formation_latency_ms_ci95"]), 0.0)


class PaperDataSelectionTests(unittest.TestCase):
    def test_figure_one_exposes_both_latency_definitions(self) -> None:
        panel_a, panel_b = build_fig1_data(ROOT / "results/exp1/raw/runs.csv")
        self.assertEqual({row["series"] for row in panel_a}, {"t_form", "t_ctrl"})
        self.assertTrue(all(not row["contains_ping"] for row in panel_a))
        self.assertEqual(
            {row["component"] for row in panel_b},
            {
                "Mapping",
                "Cross-Layer Coordination",
                "Compilation",
                "Installation",
                "Activation",
                "Verification",
            },
        )

    def test_proposal_scale_reports_exact_combination_growth(self) -> None:
        *_conflicts, scale = build_fig2_data(
            ROOT / "results/exp2/raw/runs.csv",
            ROOT / "results/exp2_robustness/raw/runs.csv",
        )
        proposed = [row for row in scale if row["method"] == "proposed"]
        self.assertEqual(
            [row["num_raw_combinations"] for row in proposed],
            [1, 16, 81, 256, 625],
        )

    def test_local_only_outcomes_are_computed_from_verifier_results(self) -> None:
        _latency, _rules, outcomes = build_fig3_data(ROOT / "results/exp3/raw/runs.csv")
        for ratio in {row["removed_agents_percent"] for row in outcomes}:
            total = sum(row["percent"] for row in outcomes if row["removed_agents_percent"] == ratio)
            self.assertAlmostEqual(total, 100.0)

    def test_repair_stack_excludes_detection_and_closes_to_repair_time(self) -> None:
        _success, _rules, breakdown = build_fig4_data(ROOT / "results/exp4/raw/runs.csv")
        self.assertTrue(all(row["detection_interval_excluded"] for row in breakdown))
        with (ROOT / "results/exp4/raw/runs.csv").open(newline="", encoding="utf-8") as handle:
            raw = list(csv.DictReader(handle))
        agent_proposed = [
            row for row in raw
            if row["fault_type"] == "AGENT_FAILURE"
            and row["method"] == "proposed"
            and row["success"] == "True"
        ]
        measured = fmean(float(row["repair_latency_ms"]) for row in agent_proposed)
        plotted = sum(
            float(row["mean_ms"])
            for row in breakdown
            if row["paper_fault_family"] == "Agent Failure" and row["method"] == "proposed"
        )
        self.assertAlmostEqual(plotted, measured, places=6)


if __name__ == "__main__":
    unittest.main()
