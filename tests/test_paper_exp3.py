from __future__ import annotations

import unittest
from collections import Counter
from time import perf_counter
from unittest.mock import patch

from src.controller.business_reconfiguration import (
    make_business_change_event,
    plan_business_change,
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


if __name__ == "__main__":
    unittest.main()
