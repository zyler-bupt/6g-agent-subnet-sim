from __future__ import annotations

import csv
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from experiments.paper_protocol import (
    EXPERIMENT_METHODS,
    METHODS,
    mode_spec,
    stable_fingerprint,
)
from src.metrics.paper import PaperTrial, write_paper_trials


class PaperProtocolTests(unittest.TestCase):
    """Catch protocol drift that would invalidate paired paper comparisons."""

    def test_mode_specs_produce_registered_trial_counts(self) -> None:
        pilot = mode_spec("pilot")
        paper = mode_spec("paper")

        self.assertEqual(pilot.topology_seeds, 5)
        self.assertEqual(pilot.events_per_seed, 2)
        self.assertEqual(pilot.trials_per_point(rate_metric=True), 10)
        self.assertEqual(paper.topology_seeds, 30)
        self.assertEqual(paper.events_per_seed, 1)
        self.assertEqual(paper.rate_events_per_seed, 5)
        self.assertEqual(paper.trials_per_point(rate_metric=True), 150)

    def test_final_method_sets_and_adaptation_metadata_are_unambiguous(self) -> None:
        self.assertEqual(
            EXPERIMENT_METHODS["exp1"],
            ("proposed", "proposed_without_batch", "cspf", "a1_agent_embedded"),
        )
        self.assertEqual(METHODS["a1_agent_embedded"].label, "A1-Agent-Embedded*")
        self.assertEqual(METHODS["a1_agent_embedded"].source, "A1 Agent")
        self.assertTrue(METHODS["a1_agent_embedded"].adapted)
        self.assertEqual(METHODS["sanet_dw"].label, "SANet-DW*")
        self.assertEqual(METHODS["netren"].label, "NetRen*")
        self.assertEqual(METHODS["netkeeper"].label, "NetKeeper*")
        self.assertFalse(METHODS["proposed"].adapted)

    def test_stable_fingerprint_is_independent_of_mapping_insertion_order(self) -> None:
        left = {"topology": {"links": ["g1-g2", "g2-g3"]}, "seed": 7}
        right = {"seed": 7, "topology": {"links": ["g1-g2", "g2-g3"]}}

        self.assertEqual(stable_fingerprint(left), stable_fingerprint(right))
        self.assertNotEqual(stable_fingerprint(left), stable_fingerprint({**right, "seed": 8}))

    def test_paper_trial_schema_contains_every_required_audit_and_metric_field(self) -> None:
        names = {item.name for item in fields(PaperTrial)}
        required = {
            "experiment",
            "mode",
            "trial_id",
            "seed",
            "event_id",
            "method_id",
            "method_label",
            "method_source",
            "adapted",
            "topology_fingerprint",
            "scenario_fingerprint",
            "qos_fingerprint",
            "event_fingerprint",
            "task_received_at",
            "event_occurred_at",
            "stable_verify_finished_at",
            "task_size",
            "num_dag_edges",
            "num_gateways",
            "state_churn_probability",
            "conflict_density",
            "conflict_type",
            "ground_truth_feasible",
            "business_change_type",
            "affected_scope_ratio",
            "failure_type",
            "failure_severity",
            "formation_latency_ms",
            "resolution_latency_ms",
            "reconfiguration_latency_ms",
            "recovery_latency_ms",
            "success",
            "qos_satisfied",
            "safe_rejection",
            "total_rules",
            "changed_rules",
            "rule_change_ratio",
            "total_gateways",
            "changed_gateways",
            "gateway_change_ratio",
            "total_flows",
            "unaffected_flows",
            "disturbed_unaffected_flows",
            "unaffected_disturbance_ratio",
            "control_messages",
            "rollback_count",
            "stale_state_detected",
        }

        self.assertEqual(required - names, set())

    def test_csv_writer_keeps_not_applicable_metrics_empty(self) -> None:
        metadata = METHODS["proposed"]
        row = PaperTrial(
            experiment="exp1",
            mode="pilot",
            trial_id="exp1:size=8:seed=0:event=0",
            seed=0,
            event_id=0,
            method_id="proposed",
            method_label=metadata.label,
            method_source=metadata.source,
            adapted=metadata.adapted,
            topology_fingerprint="topology-hash",
            scenario_fingerprint="scenario-hash",
            qos_fingerprint="qos-hash",
            event_fingerprint="event-hash",
            success=True,
            formation_latency_ms=12.5,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trials.csv"
            write_paper_trials(output, [row])
            with output.open(encoding="utf-8", newline="") as handle:
                written = list(csv.DictReader(handle))

        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["formation_latency_ms"], "12.5")
        self.assertEqual(written[0]["conflict_density"], "")
        self.assertEqual(written[0]["recovery_latency_ms"], "")


if __name__ == "__main__":
    unittest.main()
