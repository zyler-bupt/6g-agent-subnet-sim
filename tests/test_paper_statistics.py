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
                        "formation_latency_ms": 10.0 + seed + event_id,
                        "success": True,
                    }
                )
                rows.append(
                    {
                        "experiment": "exp1",
                        "mode": "pilot",
                        "series": "state_churn",
                        "seed": seed,
                        "event_id": event_id,
                        "method_id": "proposed",
                        "method_label": "Proposed",
                        "task_size": 24,
                        "state_churn_probability": 0.2,
                        "formation_latency_ms": 20.0,
                        "success": seed > 0,
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.csv"
            summary = aggregate_experiment(rows, output, bootstrap_iterations=300)

            self.assertTrue(output.exists())
            self.assertEqual(len(summary), 2)
            latency = next(row for row in summary if row.metric == "formation_latency_ms")
            success = next(row for row in summary if row.metric == "success_rate_percent")
            self.assertEqual(latency.x_name, "task_size")
            self.assertEqual(latency.x_value, 8.0)
            self.assertEqual(latency.sample_count, 6)
            self.assertEqual(latency.cluster_count, 3)
            self.assertEqual(success.x_name, "state_churn_probability_percent")
            self.assertEqual(success.x_value, 20.0)
            self.assertAlmostEqual(success.mean, 200.0 / 3.0)
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
                        "series": "affected_scope",
                        "seed": seed,
                        "event_id": event_id,
                        "method_id": "local_only",
                        "method_label": "Local-Only",
                        "affected_scope_ratio": 0.21,
                        "affected_scope_bucket_percent": 20,
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
        self.assertEqual(success.x_name, "affected_scope_ratio_percent")
        self.assertEqual(success.x_value, 20.0)


if __name__ == "__main__":
    unittest.main()
