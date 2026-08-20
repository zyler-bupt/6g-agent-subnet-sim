from __future__ import annotations

import unittest
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import tempfile
from time import perf_counter
from unittest.mock import patch
from pathlib import Path

from experiments.exp3_business_elasticity import run_exp3
from experiments.paper_protocol import EXPERIMENT_METHODS
from src.controller.business_reconfiguration import (
    _changed_agent_counts,
    _changed_path_counts,
    make_business_change_event,
    plan_business_change,
    run_business_reconfiguration_method,
)
from src.controller.transaction_executor import TransactionExecutor
from src.simulation.business_change_generator import (
    BusinessChangeType,
    exact_dependency_closure,
    generate_business_change,
    sample_affected_scope_bucket,
)


class BusinessChangeGeneratorTests(unittest.TestCase):
    def test_change_is_materialized_before_exact_bucket_assignment(self) -> None:
        for bucket in (10, 20, 30, 40, 50):
            with self.subTest(bucket=bucket):
                snapshot = sample_affected_scope_bucket(bucket, seed=5, event_id=1)
                recomputed = exact_dependency_closure(
                    snapshot.before_task,
                    snapshot.change,
                )

                self.assertEqual(snapshot.affected_edge_ids, recomputed.edge_ids)
                self.assertAlmostEqual(
                    snapshot.affected_scope_ratio,
                    len(recomputed.edge_ids) / len(snapshot.before_task.biz_edges),
                )
                self.assertLessEqual(
                    abs(snapshot.affected_scope_ratio * 100.0 - bucket),
                    5.0,
                )
                self.assertGreater(snapshot.generation_attempts, 0)
                self.assertNotEqual(snapshot.before_task, snapshot.after_task)

    def test_event_schedule_balances_all_four_business_change_types(self) -> None:
        events = [
            generate_business_change(seed=2, event_id=index)
            for index in range(8)
        ]

        self.assertEqual(
            Counter(item.change.change_type for item in events),
            Counter({kind: 2 for kind in BusinessChangeType}),
        )

    def test_snapshot_is_deterministic_and_uses_the_shared_modular_scenario(self) -> None:
        first = sample_affected_scope_bucket(30, seed=3, event_id=2)
        replay = sample_affected_scope_bucket(30, seed=3, event_id=2)

        self.assertEqual(first.fingerprint, replay.fingerprint)
        self.assertEqual(first.affected_edge_ids, replay.affected_edge_ids)
        self.assertEqual(len(first.before_task.app_agents), 24)
        self.assertEqual(len(first.before_task.biz_edges), 38)
        self.assertEqual(len(first.formation_snapshot.topology.gateway_ids), 12)
        self.assertGreaterEqual(first.formation_snapshot.cross_gateway_edge_ratio, 0.60)
        self.assertLessEqual(first.formation_snapshot.cross_gateway_edge_ratio, 0.70)

    def test_dependency_closure_is_materialized_in_the_target_configuration(self) -> None:
        snapshot = sample_affected_scope_bucket(30, seed=6, event_id=1)
        before = {edge.edge_id: edge for edge in snapshot.before_task.biz_edges}
        after = {edge.edge_id: edge for edge in snapshot.after_task.biz_edges}
        surviving_closure = snapshot.affected_edge_ids & set(before) & set(after)

        self.assertTrue(surviving_closure)
        self.assertTrue(
            all(before[edge_id] != after[edge_id] for edge_id in surviving_closure)
        )

    def test_all_change_types_compile_from_the_same_valid_initial_state(self) -> None:
        for event_id, change_type in enumerate(BusinessChangeType):
            with self.subTest(change_type=change_type.value):
                snapshot = generate_business_change(seed=7, event_id=event_id)

                self.assertEqual(snapshot.change.change_type, change_type)
                self.assertEqual(snapshot.before_task.task_id, snapshot.after_task.task_id)
                self.assertTrue(snapshot.affected_edge_ids)
                self.assertTrue(snapshot.change.changed_object_ids)


class BusinessChangePilotGridTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pilot_changes_and_targets_are_feasible_and_nearly_balanced(self) -> None:
        counts = Counter()
        event_count = 0
        for bucket_index, bucket in enumerate((10, 20, 30, 40, 50)):
            for seed in range(5):
                for event_id in range(2):
                    schedule_index = bucket_index * 10 + seed * 2 + event_id
                    snapshot = sample_affected_scope_bucket(
                        bucket,
                        seed,
                        event_id,
                        schedule_index=schedule_index,
                    )
                    controller, _verifier, _provider = snapshot.instantiate()
                    await controller.compile_task_subnet(snapshot.before_task, version=1)
                    await controller.compile_task_subnet(snapshot.after_task, version=2)
                    counts[snapshot.change.change_type] += 1
                    event_count += 1

        self.assertEqual(event_count, 50)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)


class BusinessStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_scope_excludes_unselected_path_and_agent_differences(self) -> None:
        snapshot, controller, stable = await self._stable_snapshot(10, seed=0, event_id=0)
        local = await plan_business_change(controller, stable, snapshot, "local_only")
        unselected_edge_id = next(
            edge_id
            for edge_id in stable.business_edges
            if edge_id not in local.selected_edge_ids
        )
        synthetic_target = deepcopy(stable)
        session_index = next(
            index
            for index, session in enumerate(synthetic_target.sessions)
            if session.business_edge_id == unselected_edge_id
        )
        session = synthetic_target.sessions[session_index]
        synthetic_target.sessions[session_index] = replace(
            session,
            gateway_path=tuple(reversed(session.gateway_path)),
        )
        synthetic_target.application_agents[session.source] = replace(
            synthetic_target.application_agents[session.source],
            task_stage="reconfigured",
        )

        _, changed_paths = _changed_path_counts(
            stable,
            synthetic_target,
            local.selected_edge_ids,
        )
        _, changed_agents = _changed_agent_counts(
            stable,
            synthetic_target,
            local.selected_edge_ids,
            snapshot.change.changed_object_ids,
        )

        self.assertEqual(changed_paths, 0)
        self.assertEqual(changed_agents, 0)

    async def test_local_only_is_one_hop_and_never_escalates(self) -> None:
        snapshot, controller, stable = await self._stable_snapshot(30, seed=2, event_id=1)

        plan = await plan_business_change(
            controller,
            stable,
            snapshot,
            "local_only",
        )

        self.assertEqual(plan.scope_policy, "changed_object_plus_one_hop")
        self.assertEqual(plan.escalation_tier, 1)
        self.assertLessEqual(plan.affected_gateways, plan.verification_gateways)
        self.assertEqual(plan.transaction_gateways, plan.verification_gateways)
        one_hop_agents = {
            agent_id
            for task in (stable.task, plan.target_state.task)
            for edge in task.biz_edges
            if edge.edge_id in plan.selected_edge_ids
            for agent_id in (edge.source, edge.target)
        }
        expected_gateways = {
            state.gateway_id
            for subnet in (stable, plan.target_state)
            for agent_id, state in subnet.application_agents.items()
            if agent_id in one_hop_agents
        }
        self.assertEqual(plan.affected_gateways, expected_gateways)

    async def test_netren_does_not_read_task_dependency_closure(self) -> None:
        snapshot, controller, stable = await self._stable_snapshot(30, seed=4, event_id=2)

        with patch(
            "src.controller.business_reconfiguration.exact_dependency_closure",
            side_effect=AssertionError("NetRen* may not use the task closure"),
        ):
            plan = await plan_business_change(
                controller,
                stable,
                snapshot,
                "netren",
            )

        self.assertEqual(plan.scope_policy, "network_flow_resynthesis")
        self.assertEqual(plan.escalation_tier, 2)

    async def test_full_rebuild_redeploys_every_old_rule(self) -> None:
        snapshot, controller, stable = await self._stable_snapshot(20, seed=1, event_id=0)

        proposed = await plan_business_change(
            controller,
            stable,
            snapshot,
            "proposed",
        )
        full = await plan_business_change(
            controller,
            stable,
            snapshot,
            "full_rebuild",
        )

        self.assertTrue(set(stable.rules).issubset(full.changed_rule_ids))
        self.assertLess(len(proposed.changed_rule_ids), len(full.changed_rule_ids))
        self.assertTrue(full.full_rule_install)
        self.assertFalse(proposed.full_rule_install)

    async def test_proposed_rule_scope_grows_with_exact_affected_scope(self) -> None:
        small, small_controller, small_stable = await self._stable_snapshot(
            10,
            seed=3,
            event_id=3,
        )
        large, large_controller, large_stable = await self._stable_snapshot(
            50,
            seed=3,
            event_id=3,
        )

        small_plan = await plan_business_change(
            small_controller,
            small_stable,
            small,
            "proposed",
        )
        large_plan = await plan_business_change(
            large_controller,
            large_stable,
            large,
            "proposed",
        )

        self.assertLess(
            len(small_plan.changed_rule_ids),
            len(large_plan.changed_rule_ids),
        )

    async def test_all_methods_use_shared_transaction_verification(self) -> None:
        snapshot = sample_affected_scope_bucket(30, seed=2, event_id=1)
        outcomes = {}
        plans = {}
        for method_id in ("proposed", "netren", "local_only", "full_rebuild"):
            controller, verifier, _provider = snapshot.instantiate()
            stable, metrics = await controller.build_task_subnet(
                snapshot.before_task,
                verifier=verifier,
                run_id=1,
                seed=snapshot.seed,
            )
            self.assertTrue(metrics.networking_success, metrics.failure_reason)
            plan = await plan_business_change(
                controller,
                stable,
                snapshot,
                method_id,
            )
            event = make_business_change_event(snapshot, occurred_at=perf_counter())
            outcome = await TransactionExecutor(
                controller,
                verifier=verifier,
            ).execute(
                stable,
                plan,
                event,
                run_id=2,
                seed=snapshot.seed,
                received_at=perf_counter(),
            )
            outcomes[method_id] = outcome
            plans[method_id] = plan

        self.assertTrue(outcomes["proposed"].success)
        self.assertTrue(outcomes["netren"].success)
        self.assertTrue(outcomes["full_rebuild"].success)
        self.assertFalse(outcomes["local_only"].success)
        self.assertTrue(outcomes["local_only"].rollback_triggered)
        self.assertIn("verification failed", outcomes["local_only"].failure_reason)
        self.assertEqual(
            {plan.target_state.task for plan in plans.values()},
            {snapshot.after_task},
        )

    async def _stable_snapshot(self, bucket: int, seed: int, event_id: int):
        snapshot = sample_affected_scope_bucket(bucket, seed, event_id)
        controller, verifier, _provider = snapshot.instantiate()
        stable, metrics = await controller.build_task_subnet(
            snapshot.before_task,
            verifier=verifier,
            run_id=1,
            seed=seed,
        )
        self.assertTrue(metrics.networking_success, metrics.failure_reason)
        return snapshot, controller, stable


class PaperExp3RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_affected_agent_count_is_shared_and_matches_exact_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = await run_exp3(
                "pilot",
                Path(directory),
                seeds=(0,),
                buckets=(10,),
            )

        expected_by_event = {
            event_id: len(
                sample_affected_scope_bucket(
                    10,
                    seed=0,
                    event_id=event_id,
                    schedule_index=event_id,
                ).closure.agent_ids
            )
            for event_id in (0, 1)
        }
        paired_rows: dict[str, list] = {}
        for row in rows:
            paired_rows.setdefault(row.trial_id, []).append(row)
            self.assertEqual(row.series, "affected_agents")
            self.assertEqual(row.affected_agent_count, expected_by_event[row.event_id])
        self.assertTrue(
            all(
                len({row.affected_agent_count for row in paired}) == 1
                for paired in paired_rows.values()
            )
        )

    async def test_pilot_writes_complete_paired_rows_with_exact_scope_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = await run_exp3(
                "pilot",
                root,
                seeds=(0,),
                buckets=(10,),
            )

            self.assertEqual(len(rows), 2 * 4)
            self.assertTrue(
                (root / "raw" / "pilot" / "exp3" / "trials.csv").exists()
            )
            by_trial = {}
            for row in rows:
                by_trial.setdefault(row.trial_id, []).append(row)
                self.assertTrue(row.business_change_type)
                self.assertIsNotNone(row.affected_scope_ratio)
                self.assertIsNotNone(row.affected_scope_bucket_percent)
                self.assertLessEqual(
                    abs(row.affected_scope_ratio * 100.0 - 10.0),
                    5.0,
                )
                self.assertIsNotNone(row.rule_change_ratio)
                self.assertIsNotNone(row.gateway_change_ratio)
                self.assertIsNotNone(row.unaffected_disturbance_ratio)
                if row.success:
                    self.assertGreater(row.reconfiguration_latency_ms, 0.0)
                    self.assertAlmostEqual(
                        row.reconfiguration_latency_ms,
                        1000.0
                        * (row.stable_verify_finished_at - row.event_occurred_at),
                    )
                else:
                    self.assertIsNone(row.reconfiguration_latency_ms)
                    self.assertIsNone(row.stable_verify_finished_at)
            self.assertEqual(len(by_trial), 2)
            self.assertTrue(
                all(
                    {row.method_id for row in paired}
                    == set(EXPERIMENT_METHODS["exp3"])
                    for paired in by_trial.values()
                )
            )
            for paired in by_trial.values():
                self.assertEqual(len({row.scenario_fingerprint for row in paired}), 1)
                self.assertEqual(len({row.event_fingerprint for row in paired}), 1)

    async def test_full_rebuild_has_higher_change_cost_than_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = await run_exp3(
                "pilot",
                Path(directory),
                seeds=(1,),
                buckets=(20, 50),
            )

        proposed = [row for row in rows if row.method_id == "proposed"]
        full = [row for row in rows if row.method_id == "full_rebuild"]
        self.assertEqual(len(proposed), len(full))
        self.assertLess(
            sum(row.rule_change_ratio for row in proposed) / len(proposed),
            sum(row.rule_change_ratio for row in full) / len(full),
        )
        self.assertLess(
            sum(row.reconfiguration_latency_ms for row in proposed) / len(proposed),
            sum(row.reconfiguration_latency_ms for row in full) / len(full),
        )
        for proposed_row, full_row in zip(proposed, full):
            self.assertEqual(proposed_row.trial_id, full_row.trial_id)
            for row in (proposed_row, full_row):
                self.assertIsNotNone(row.total_paths)
                self.assertIsNotNone(row.changed_paths)
                self.assertIsNotNone(row.total_agents)
                self.assertIsNotNone(row.changed_agents)
                self.assertAlmostEqual(
                    row.modification_scope_ratio,
                    (
                        row.changed_rules + row.changed_paths + row.changed_agents
                    )
                    / (row.total_rule_objects + row.total_paths + row.total_agents),
                )
            self.assertGreaterEqual(full_row.changed_rules, proposed_row.changed_rules)
            self.assertGreaterEqual(
                full_row.modification_scope_ratio,
                proposed_row.modification_scope_ratio,
            )

    async def test_local_only_does_not_report_more_changed_objects_than_full_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rows = await run_exp3(
                "pilot",
                Path(directory),
                seeds=(1,),
                buckets=(20,),
            )

        by_trial: dict[str, dict[str, object]] = {}
        for row in rows:
            by_trial.setdefault(row.trial_id, {})[row.method_id] = row
        for methods in by_trial.values():
            local = methods["local_only"]
            full = methods["full_rebuild"]
            self.assertLessEqual(local.changed_paths, full.changed_paths)
            self.assertLessEqual(local.changed_agents, full.changed_agents)

    async def test_full_rebuild_reports_every_path_and_agent_as_redeployed(self) -> None:
        snapshot = sample_affected_scope_bucket(30, seed=103, event_id=0)

        rebuilt = await run_business_reconfiguration_method(snapshot, "full_rebuild")

        self.assertEqual(rebuilt.changed_paths, rebuilt.total_paths)
        self.assertEqual(rebuilt.changed_agents, rebuilt.total_agents)
        self.assertAlmostEqual(rebuilt.rule_change_ratio, 1.0)
        self.assertLessEqual(rebuilt.modification_scope_ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
