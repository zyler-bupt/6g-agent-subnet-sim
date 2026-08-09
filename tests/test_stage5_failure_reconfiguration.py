from __future__ import annotations

import asyncio
import unittest

from experiments.exp4_failure_reconfiguration import run_one
from src.controller.failure_recovery import (
    FullRebuildFailureStrategy,
    ProposedCrossLayerElasticStrategy,
    ReactiveNetworkOnlyStrategy,
)
from src.controller.impact import ImpactScopeAnalyzer
from src.controller.transaction_executor import TransactionExecutor
from src.e2e.failure_verifier import FaultAwareTaskSubnetVerifier
from src.simulation.failure_scenario_generator import (
    FailureScenarioConfig,
    FailureScenarioGenerator,
    SimulationMonotonicClock,
)


def _config(fault_type: str, level: str, severity: float = 1.0, **kwargs):
    return FailureScenarioConfig(
        fault_type=fault_type,
        fault_level=level,
        severity=severity,
        num_agents=10,
        num_gateways=4,
        **kwargs,
    )


async def _prepared(config: FailureScenarioConfig, seed: int = 0):
    snapshot = FailureScenarioGenerator().generate(config, seed)
    controller, verifier, _provider = snapshot.instantiate()
    stable, metrics = await controller.build_task_subnet(snapshot.task, verifier=verifier)
    if not metrics.networking_success:
        raise AssertionError(metrics.failure_reason)
    event, context = snapshot.apply_fault(controller, stable)
    return snapshot, controller, stable, event, context


class FailureTimestampAndFairnessTests(unittest.TestCase):
    def test_fault_timestamp_is_recorded_when_effective(self) -> None:
        async def run() -> None:
            snapshot, _controller, _stable, event, context = await _prepared(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0)
            )
            self.assertEqual(event.occurred_at, snapshot.fault_effective_at)
            self.assertEqual(context.fault_effective_at, event.occurred_at)
            self.assertGreater(context.failure_detected_at, event.occurred_at)

        asyncio.run(run())

    def test_detection_time_is_shared_across_methods(self) -> None:
        snapshot = FailureScenarioGenerator().generate(
            _config("PHYSICAL_CAPACITY_DROP", "capacity_factor_0.4", 0.4),
            3,
        )
        observations = []
        for _method in ("proposed", "full_rebuild", "network_only"):
            _controller, _verifier, _provider = snapshot.instantiate()
            observations.append((snapshot.fault_effective_at, snapshot.failure_detected_at))
        self.assertEqual(len(set(observations)), 1)

    def test_failure_scenario_fingerprint_same_across_methods(self) -> None:
        snapshot = FailureScenarioGenerator().generate(
            _config("AGENT_FAILURE", "network"), 4
        )
        self.assertEqual(
            {snapshot.fingerprint for _ in ("proposed", "full_rebuild", "network_only")},
            {snapshot.fingerprint},
        )


class FailureScopeTests(unittest.TestCase):
    def test_agent_failure_scope(self) -> None:
        async def run() -> None:
            _snapshot, _controller, stable, event, _context = await _prepared(
                _config("AGENT_FAILURE", "application_different_gateway")
            )
            scope = ImpactScopeAnalyzer().analyze_failure(stable, event)
            self.assertTrue(scope.affected_business_edges)
            self.assertTrue(scope.affected_sessions)
            self.assertTrue(scope.affected_rules)

        asyncio.run(run())

    def test_link_failure_scope(self) -> None:
        async def run() -> None:
            _snapshot, _controller, stable, event, context = await _prepared(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0)
            )
            scope = ImpactScopeAnalyzer().analyze_failure(stable, event)
            self.assertEqual(scope.affected_business_edges, context.affected_edge_ids)
            self.assertGreater(len(scope.affected_gateways), 1)

        asyncio.run(run())

    def test_physical_capacity_drop_scope(self) -> None:
        async def run() -> None:
            _snapshot, _controller, stable, event, context = await _prepared(
                _config("PHYSICAL_CAPACITY_DROP", "capacity_factor_0.4", 0.4)
            )
            scope = ImpactScopeAnalyzer().analyze_failure(stable, event)
            self.assertIn(context.physical_agent_id, scope.affected_agents)
            self.assertTrue(scope.affected_physical_resources)

        asyncio.run(run())


class RecoveryMethodTests(unittest.TestCase):
    def test_proposed_incremental_failure_recovery(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("AGENT_FAILURE", "application_different_gateway"), 1
            )
            row, _events, _proposals, _probes = await run_one(snapshot, "proposed", run_sequence=1)
            self.assertTrue(row.recovery_success)
            self.assertLessEqual(row.affected_gateways, row.num_gateways)

        asyncio.run(run())

    def test_full_rebuild_failure_recovery(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0), 1
            )
            row, _events, _proposals, _probes = await run_one(snapshot, "full_rebuild", run_sequence=1)
            self.assertTrue(row.recovery_success)
            self.assertEqual(row.affected_gateways, row.num_gateways)
            self.assertGreater(row.changed_rules, 0)

        asyncio.run(run())

    def test_network_only_uses_only_network_actions(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0), 2
            )
            row, _events, proposals, _probes = await run_one(snapshot, "network_only", run_sequence=1)
            self.assertTrue(row.recovery_success)
            self.assertEqual(row.selected_layers, "network")
            self.assertTrue(all(item["proposal"]["layer"] == "network" for item in proposals))

        asyncio.run(run())

    def test_link_failure_switches_route(self) -> None:
        async def run() -> None:
            _snapshot, controller, stable, event, context = await _prepared(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0)
            )
            result = await ProposedCrossLayerElasticStrategy().plan(controller, stable, event, context)
            self.assertIsNotNone(result.plan)
            for session in result.plan.target_state.sessions:
                if session.business_edge_id in context.affected_edge_ids:
                    self.assertNotIn(
                        context.failed_link,
                        set(zip(session.gateway_path, session.gateway_path[1:])),
                    )

        asyncio.run(run())

    def test_physical_failure_requires_cross_layer_action(self) -> None:
        async def run() -> None:
            _snapshot, controller, stable, event, context = await _prepared(
                _config("PHYSICAL_CAPACITY_DROP", "capacity_factor_0.4", 0.4)
            )
            proposed = await ProposedCrossLayerElasticStrategy().plan(controller, stable, event, context)
            network = await ReactiveNetworkOnlyStrategy().plan(controller, stable, event, context)
            self.assertIsNotNone(proposed.plan)
            self.assertIn("physical", {item.layer for item in proposed.selected_proposals if not item.is_keep})
            self.assertIsNone(network.plan)
            self.assertTrue(network.safe_rejection)

        asyncio.run(run())


class RecoveryVerificationTests(unittest.TestCase):
    def test_verification_rollback_ablation_exposes_post_plan_drift(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config(
                    "LINK_FAILURE",
                    "capacity_factor_0.6",
                    0.6,
                    post_plan_state_drift=True,
                ),
                0,
            )
            guarded, *_ = await run_one(snapshot, "no_scope", run_sequence=1)
            unguarded, *_ = await run_one(
                snapshot, "no_verification_rollback", run_sequence=2
            )
            self.assertTrue(guarded.rollback_triggered)
            self.assertTrue(guarded.rollback_success)
            self.assertFalse(unguarded.rollback_triggered)
            self.assertEqual(unguarded.new_version, unguarded.old_version + 1)
            self.assertFalse(unguarded.post_execution_verification_success)

        asyncio.run(run())

    def test_recovery_requires_qos_restoration(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("PHYSICAL_CAPACITY_DROP", "capacity_factor_0.2", 0.2), 0
            )
            row, _events, _proposals, _probes = await run_one(snapshot, "proposed", run_sequence=1)
            self.assertEqual(row.recovery_success, row.qos_recovered)
            self.assertTrue(row.post_execution_verification_success)

        asyncio.run(run())

    def test_continuous_probe_measures_interruption(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0), 0
            )
            row, _events, _proposals, probes = await run_one(snapshot, "proposed", run_sequence=1)
            affected = [item for item in probes if item.directly_affected]
            self.assertTrue(any(not item.qos_satisfied for item in affected))
            self.assertTrue(any(item.phase.startswith("HEALTH_WINDOW") and item.qos_satisfied for item in affected))
            self.assertGreater(row.service_interruption_ms, 0.0)

        asyncio.run(run())

    def test_unaffected_edges_are_monitored(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("AGENT_FAILURE", "transport"), 0
            )
            row, _events, _proposals, probes = await run_one(snapshot, "proposed", run_sequence=1)
            self.assertGreater(row.unaffected_edges, 0)
            self.assertTrue(any(not item.directly_affected for item in probes))

        asyncio.run(run())

    def test_failed_recovery_rolls_back(self) -> None:
        async def run() -> None:
            snapshot, controller, stable, event, context = await _prepared(
                _config("LINK_FAILURE", "capacity_factor_0.0", 0.0)
            )
            clock = SimulationMonotonicClock(context.failure_detected_at)
            planning = await ProposedCrossLayerElasticStrategy(clock=clock).plan(
                controller, stable, event, context
            )
            self.assertIsNotNone(planning.plan)
            failed_gateway = sorted(planning.plan.transaction_gateways)[0]
            controller.gateways[failed_gateway].reject_stage_versions.add(stable.version + 1)
            result = await TransactionExecutor(
                controller,
                verifier=FaultAwareTaskSubnetVerifier(context),
                clock=clock,
            ).execute(
                stable,
                planning.plan,
                event,
                received_at=context.failure_detected_at,
            )
            self.assertFalse(result.success)
            self.assertTrue(result.rollback_triggered)
            self.assertTrue(result.rollback_success)
            self.assertEqual(result.before_snapshot, result.after_snapshot)

        asyncio.run(run())

    def test_residual_rules_zero_after_recovery(self) -> None:
        async def run() -> None:
            snapshot = FailureScenarioGenerator().generate(
                _config("AGENT_FAILURE", "physical"), 5
            )
            row, _events, _proposals, _probes = await run_one(snapshot, "proposed", run_sequence=1)
            self.assertTrue(row.recovery_success)
            self.assertEqual(row.residual_rules, 0)
            self.assertTrue(row.staged_rules_empty)

        asyncio.run(run())

    def test_agent_failure_without_backup_is_unresolvable(self) -> None:
        async def run() -> None:
            _snapshot, controller, stable, event, context = await _prepared(
                _config(
                    "AGENT_FAILURE",
                    "application_same_gateway",
                    backup_available=False,
                )
            )
            result = await ProposedCrossLayerElasticStrategy().plan(
                controller, stable, event, context
            )
            self.assertTrue(result.safe_rejection)
            self.assertIsNone(result.plan)
            self.assertIn("UNRESOLVABLE", result.failure_reason)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
