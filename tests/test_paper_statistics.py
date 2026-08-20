from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from scripts.aggregate_results import aggregate_experiment
from scripts.paper_statistics import cluster_bootstrap_interval


class ClusterBootstrapTests(unittest.TestCase):
    def test_cluster_bootstrap_resamples_complete_topology_seed_clusters(self) -> None:
        rows = [
            {"seed": 0, "value": 0.0},
            {"seed": 0, "value": 100.0},
            {"seed": 1, "value": 50.0},
            {"seed": 1, "value": 50.0},
        ]

        interval = cluster_bootstrap_interval(
            rows,
            value="value",
            cluster="seed",
            statistic=np.mean,
            iterations=1000,
            seed=9,
        )

        self.assertEqual(interval.cluster_count, 2)
        self.assertEqual(interval.sample_count, 4)
        self.assertAlmostEqual(interval.estimate, 50.0)
        self.assertAlmostEqual(interval.lower, 50.0)
        self.assertAlmostEqual(interval.upper, 50.0)

    def test_cluster_bootstrap_is_deterministic_for_a_registered_seed(self) -> None:
        rows = [
            {"seed": seed, "value": float(seed + event)}
            for seed in range(5)
            for event in range(2)
        ]
        first = cluster_bootstrap_interval(
            rows,
            "value",
            "seed",
            np.mean,
            iterations=500,
            seed=17,
        )
        second = cluster_bootstrap_interval(
            rows,
            "value",
            "seed",
            np.mean,
            iterations=500,
            seed=17,
        )

        self.assertEqual(first, second)
        self.assertLessEqual(first.lower, first.estimate)
        self.assertGreaterEqual(first.upper, first.estimate)


class PaperAggregationTests(unittest.TestCase):
    def test_exp1_aggregation_writes_long_form_latency_and_rate_rows(self) -> None:
        rows = []
        for seed in range(3):
            for event_id in range(2):
                rows.append(
                    {
                        "experiment": "exp1",
                        "mode": "pilot",
                        "series": "task_size",
                        "seed": seed,
                        "event_id": event_id,
                        "method_id": "proposed",
                        "method_label": "Proposed",
                        "task_size": 8,
                        "state_churn_probability": "",
                        "controller_processing_latency_ms": 3.0 + seed,
                        "formation_latency_ms": 10.0 + seed + event_id,
                        "success": True,
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.csv"
            summary = aggregate_experiment(rows, output, bootstrap_iterations=300)

            self.assertTrue(output.exists())
            self.assertEqual(len(summary), 3)
            latency = next(row for row in summary if row.metric == "formation_latency_ms")
            controller = next(
                row
                for row in summary
                if row.metric == "controller_processing_latency_ms"
            )
            success = next(row for row in summary if row.metric == "success_rate_percent")
            self.assertEqual(latency.x_name, "task_size")
            self.assertEqual(latency.x_value, 8.0)
            self.assertEqual(latency.sample_count, 6)
            self.assertEqual(latency.cluster_count, 3)
            self.assertEqual(controller.sample_count, 6)
            self.assertEqual(success.x_name, "task_size")
            self.assertEqual(success.x_value, 8.0)
            self.assertAlmostEqual(success.mean, 100.0)
            self.assertGreaterEqual(latency.p95, latency.p50)
            self.assertLessEqual(latency.ci_lower, latency.mean)
            self.assertGreaterEqual(latency.ci_upper, latency.mean)

    def test_exp3_aggregation_keeps_latency_conditional_and_rates_unconditional(self) -> None:
        rows = []
        for seed in range(3):
            for event_id in range(2):
                success = not (seed == 0 and event_id == 0)
                rows.append(
                    {
                        "experiment": "exp3",
                        "mode": "pilot",
                        "series": "affected_agents",
                        "seed": seed,
                        "event_id": event_id,
                        "method_id": "local_only",
                        "method_label": "Local-Only",
                        "affected_scope_ratio": 0.21,
                        "affected_scope_bucket_percent": 20,
                        "affected_agent_count": 7,
                        "reconfiguration_latency_ms": 8.0 if success else "",
                        "rule_change_ratio": 0.10,
                        "gateway_change_ratio": 0.08,
                        "unaffected_disturbance_ratio": 0.05,
                        "success": success,
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            summary = aggregate_experiment(
                rows,
                Path(directory) / "summary.csv",
                bootstrap_iterations=300,
            )

        self.assertEqual(
            {row.metric for row in summary},
            {
                "reconfiguration_latency_ms",
                "rule_change_ratio_percent",
                "gateway_change_ratio_percent",
                "unaffected_disturbance_ratio_percent",
                "success_rate_percent",
            },
        )
        latency = next(
            row for row in summary if row.metric == "reconfiguration_latency_ms"
        )
        success = next(row for row in summary if row.metric == "success_rate_percent")
        self.assertEqual(latency.sample_count, 5)
        self.assertEqual(success.sample_count, 6)
        self.assertAlmostEqual(success.mean, 500.0 / 6.0)
        self.assertEqual(success.x_name, "affected_agent_count")
        self.assertEqual(success.x_value, 7.0)

    def test_exp4_aggregation_supports_failure_bars_and_capacity_stress(self) -> None:
        rows = []
        for seed in range(3):
            rows.extend(
                (
                    {
                        "experiment": "exp4",
                        "mode": "pilot",
                        "series": "failure_type",
                        "seed": seed,
                        "event_id": 0,
                        "method_id": "cspf",
                        "method_label": "CSPF",
                        "failure_type": "link_failure",
                        "failure_severity": 1.0,
                        "recovery_latency_ms": 4.0 + seed,
                        "rule_change_ratio": 0.08,
                        "gateway_change_ratio": 0.20,
                        "unaffected_disturbance_ratio": 0.0,
                        "modification_scope_ratio": 0.12,
                        "success": True,
                        "qos_satisfied": True,
                    },
                    {
                        "experiment": "exp4",
                        "mode": "pilot",
                        "series": "capacity_stress",
                        "seed": seed,
                        "event_id": 0,
                        "method_id": "cspf",
                        "method_label": "CSPF",
                        "failure_type": "capacity_degradation",
                        "failure_severity": 0.4,
                        "recovery_latency_ms": "",
                        "rule_change_ratio": 0.0,
                        "gateway_change_ratio": 0.0,
                        "unaffected_disturbance_ratio": 0.0,
                        "modification_scope_ratio": "",
                        "success": seed == 2,
                        "qos_satisfied": seed == 2,
                    },
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            summary = aggregate_experiment(
                rows,
                Path(directory) / "summary.csv",
                bootstrap_iterations=300,
            )

        link_latency = next(
            row
            for row in summary
            if row.series == "failure_type"
            and row.metric == "recovery_latency_ms"
        )
        stress_success = next(
            row
            for row in summary
            if row.series == "capacity_stress"
            and row.metric == "success_rate_percent"
        )
        modification_scope = next(
            row
            for row in summary
            if row.series == "failure_type"
            and row.metric == "modification_scope_ratio_percent"
        )
        self.assertEqual(link_latency.x_name, "failure_type_index")
        self.assertEqual(link_latency.x_value, 1.0)
        self.assertEqual(link_latency.sample_count, 3)
        self.assertAlmostEqual(modification_scope.mean, 12.0)
        self.assertEqual(stress_success.x_name, "capacity_reduction_percent")
        self.assertEqual(stress_success.x_value, 40.0)
        self.assertAlmostEqual(stress_success.mean, 100.0 / 3.0)


if __name__ == "__main__":
    unittest.main()
