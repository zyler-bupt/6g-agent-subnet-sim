from __future__ import annotations

import unittest

from src.controller.formation_strategies import (
    ChurnEvent,
    run_formation_method,
)
from src.simulation.paper_scenarios import generate_formation_snapshot


class FormationStrategyTests(unittest.IsolatedAsyncioTestCase):
    """Catch baseline shortcuts and accidental method-specific scenarios."""

    async def test_proposed_and_no_batch_differ_only_in_gateway_scheduling(self) -> None:
        snapshot = generate_formation_snapshot(20, seed=4, event_id=0)

        proposed = await run_formation_method(snapshot, "proposed")
        no_batch = await run_formation_method(snapshot, "proposed_without_batch")

        self.assertTrue(proposed.success, proposed.failure_reason)
        self.assertTrue(no_batch.success, no_batch.failure_reason)
        self.assertEqual(proposed.path_fingerprint, no_batch.path_fingerprint)
        self.assertEqual(proposed.rule_fingerprint, no_batch.rule_fingerprint)
        self.assertEqual(proposed.verifier_fingerprint, no_batch.verifier_fingerprint)
        self.assertEqual(
            proposed.trace.primitive_cost_fingerprint,
            no_batch.trace.primitive_cost_fingerprint,
        )
        self.assertEqual(proposed.trace.churn_fingerprint, no_batch.trace.churn_fingerprint)
        self.assertEqual(proposed.trace.stage_mode, "parallel_gateway_batch")
        self.assertEqual(no_batch.trace.stage_mode, "sequential_gateway")
        self.assertEqual(
            len({item.start_ms for item in proposed.trace.operations if item.kind == "gateway_stage"}),
            1,
        )
        sequential_starts = [
            item.start_ms
            for item in no_batch.trace.operations
            if item.kind == "gateway_stage"
        ]
        self.assertEqual(sequential_starts, sorted(set(sequential_starts)))
        self.assertGreater(no_batch.formation_latency_ms, proposed.formation_latency_ms)

    async def test_cspf_and_a1_process_the_complete_dag_before_stable_success(self) -> None:
        snapshot = generate_formation_snapshot(16, seed=8, event_id=1)

        for method in ("cspf", "a1_agent_embedded"):
            with self.subTest(method=method):
                outcome = await run_formation_method(snapshot, method)
                self.assertTrue(outcome.success, outcome.failure_reason)
                self.assertTrue(outcome.path_constraints_satisfied)
                self.assertTrue(outcome.stable_verification_attempted)
                self.assertEqual(outcome.required_edge_count, len(snapshot.task.biz_edges))
                self.assertEqual(outcome.processed_edge_count, outcome.required_edge_count)
                self.assertEqual(outcome.total_flows, outcome.required_edge_count)
                self.assertGreater(outcome.total_rules, 0)

    async def test_all_methods_use_the_same_churn_trace_for_a_paired_trial(self) -> None:
        snapshot = generate_formation_snapshot(12, seed=6, event_id=1)

        outcomes = [
            await run_formation_method(snapshot, method, churn_probability=0.20)
            for method in (
                "proposed",
                "proposed_without_batch",
                "cspf",
                "a1_agent_embedded",
            )
        ]

        self.assertEqual(len({item.trace.churn_fingerprint for item in outcomes}), 1)
        self.assertEqual(len({item.trace.primitive_cost_fingerprint for item in outcomes}), 1)
        self.assertEqual(len({item.path_fingerprint for item in outcomes}), 1)

    async def test_stable_qos_failure_rolls_back_without_partial_activation(self) -> None:
        snapshot = generate_formation_snapshot(8, seed=2, event_id=0)
        first_edge = next(
            edge
            for edge in snapshot.task.biz_edges
            if snapshot.agent_gateway_mapping[edge.source]
            != snapshot.agent_gateway_mapping[edge.target]
        )
        path = snapshot.topology.shortest_path(
            snapshot.agent_gateway_mapping[first_edge.source],
            snapshot.agent_gateway_mapping[first_edge.target],
        )
        affected = next(
            link
            for link in snapshot.topology.links
            if {link.source, link.target} == {path[0], path[1]}
        )
        churn = (
            ChurnEvent(
                time_ms=0.0,
                link_id=affected.link_id,
                bandwidth_factor=0.05,
                delay_factor=3.0,
                queue_delay_ms=20.0,
                loss_addition=0.02,
            ),
        )

        outcome = await run_formation_method(
            snapshot,
            "proposed",
            churn_probability=0.20,
            churn_trace=churn,
        )

        self.assertFalse(outcome.success)
        self.assertFalse(outcome.qos_satisfied)
        self.assertTrue(outcome.stable_verification_attempted)
        self.assertEqual(outcome.rollback_count, 1)
        self.assertTrue(outcome.rollback_succeeded)
        self.assertTrue(outcome.stale_state_detected)

    async def test_proposed_latency_scales_with_actual_task_work(self) -> None:
        small = await run_formation_method(
            generate_formation_snapshot(8, seed=10, event_id=0),
            "proposed",
        )
        large = await run_formation_method(
            generate_formation_snapshot(32, seed=10, event_id=0),
            "proposed",
        )

        self.assertGreater(large.processed_edge_count, small.processed_edge_count)
        self.assertGreater(large.total_rules, small.total_rules)
        self.assertGreater(large.formation_latency_ms, small.formation_latency_ms)


if __name__ == "__main__":
    unittest.main()
