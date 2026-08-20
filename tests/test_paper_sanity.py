from __future__ import annotations

import unittest

from experiments.paper_protocol import EXPERIMENT_METHODS
from src.metrics.paper import PAPER_TRIAL_FIELDS
from scripts.sanity_check_results import check_results


def _row(**overrides):
    values = {field: "" for field in PAPER_TRIAL_FIELDS}
    values.update(
        experiment="exp1",
        mode="pilot",
        trial_id="exp1:task_size:8:seed:0000:event:000",
        seed=0,
        event_id=0,
        method_id="proposed",
        method_label="Proposed",
        method_source="This work",
        adapted=False,
        topology_fingerprint="topology-a",
        scenario_fingerprint="scenario-a",
        qos_fingerprint="qos-a",
        event_fingerprint="event-a",
        series="task_size",
        task_size=8,
        num_dag_edges=12,
        num_gateways=12,
        formation_latency_ms=10.0,
        success=True,
        qos_satisfied=True,
        total_rules=20,
        total_gateways=12,
        total_flows=12,
        control_messages=20,
        rollback_count=0,
        stale_state_detected=False,
    )
    values.update(overrides)
    return values


class PaperSanityTests(unittest.TestCase):
    def test_zero_latency_missing_method_and_fingerprint_mismatch_are_errors(self) -> None:
        rows = []
        methods = EXPERIMENT_METHODS["exp1"][:-1]
        for index, method_id in enumerate(methods):
            rows.append(
                _row(
                    method_id=method_id,
                    method_label=method_id,
                    formation_latency_ms=0.0 if index == 0 else 10.0 + index,
                    topology_fingerprint=("topology-b" if index == 1 else "topology-a"),
                )
            )

        findings = check_results(rows, experiment="exp1")
        error_codes = {item.code for item in findings if item.level == "ERROR"}

        self.assertIn("ZERO_LATENCY", error_codes)
        self.assertIn("INCOMPLETE_METHOD_PAIR", error_codes)
        self.assertIn("MISMATCHED_PAIR_FINGERPRINT", error_codes)

    def test_exp1_warns_when_latency_does_not_grow_with_task_size(self) -> None:
        rows = []
        for task_size, latency in ((8, 20.0), (32, 10.0)):
            for method_id in EXPERIMENT_METHODS["exp1"]:
                rows.append(
                    _row(
                        trial_id=f"exp1:task_size:{task_size}:seed:0000:event:000",
                        method_id=method_id,
                        method_label=method_id,
                        task_size=task_size,
                        formation_latency_ms=latency,
                    )
                )

        findings = check_results(rows, experiment="exp1")
        warning_codes = {item.code for item in findings if item.level == "WARNING"}

        self.assertIn("EXP1_LATENCY_NOT_INCREASING", warning_codes)

    def test_complete_positive_paired_rows_have_no_sanity_errors(self) -> None:
        rows = [
            _row(method_id=method_id, method_label=method_id, formation_latency_ms=10.0 + index)
            for index, method_id in enumerate(EXPERIMENT_METHODS["exp1"])
        ]

        findings = check_results(rows, experiment="exp1")

        self.assertEqual([item for item in findings if item.level == "ERROR"], [])

    def test_non_primary_constant_latency_is_not_a_constant_output_error(self) -> None:
        rows = []
        for churn_probability in (0.1, 0.2):
            for method_id in EXPERIMENT_METHODS["exp1"]:
                rows.append(
                    _row(
                        trial_id=(
                            f"exp1:state_churn:{100 * churn_probability:g}:"
                            "seed:0000:event:000"
                        ),
                        method_id=method_id,
                        method_label=method_id,
                        series="state_churn",
                        task_size=24,
                        state_churn_probability=churn_probability,
                        formation_latency_ms=20.0,
                        success=churn_probability < 0.2,
                    )
                )

        findings = check_results(rows, experiment="exp1")

        self.assertNotIn("CONSTANT_LATENCY", {item.code for item in findings})

    def test_latency_series_success_is_not_treated_as_a_rate_anomaly(self) -> None:
        rows = []
        for task_size, latency in ((8, 10.0), (32, 30.0)):
            for method_id in EXPERIMENT_METHODS["exp1"]:
                rows.append(
                    _row(
                        trial_id=f"exp1:task_size:{task_size}:seed:0000:event:000",
                        method_id=method_id,
                        method_label=method_id,
                        task_size=task_size,
                        formation_latency_ms=latency,
                        success=True,
                    )
                )

        findings = check_results(rows, experiment="exp1")

        self.assertNotIn("ALWAYS_SUCCESS", {item.code for item in findings})


if __name__ == "__main__":
    unittest.main()
