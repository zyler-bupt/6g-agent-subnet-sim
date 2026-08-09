from __future__ import annotations

import asyncio
import csv
import tempfile
import unittest
from pathlib import Path

from experiments.exp3_business_elasticity import run_experiment, run_method
from scripts.aggregate_exp3 import confidence_interval_95
from src.controller.networking import AgentController
from src.controller.strategies import (
    FullRebuildStrategy,
    LocalOnlyStrategy,
    ProposedIncrementalStrategy,
)
from src.controller.transaction_executor import TransactionExecutor
from src.core.events import EventInjector, EventStage
from src.e2e.models import VerifyResult
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology
from src.simulation.continuous_probe import ContinuousBusinessProbe
from src.simulation.scenario_generator import (
    ElasticScenarioGenerator,
    ScenarioConfig,
    classify_agent_types,
)


async def _rescue():
    provider = SyntheticMetricProvider(seed=3, base_app_rate_mbps=24.0)
    controller = AgentController(build_rescue_topology(provider))
    subnet, metrics = await controller.build_task_subnet(rescue_task())
    return controller, provider, subnet, metrics


class InitialBuildTransactionTests(unittest.TestCase):
    def test_initial_build_uses_versioned_transaction(self) -> None:
        async def run() -> None:
            controller, _provider, subnet, metrics = await _rescue()
            self.assertTrue(metrics.networking_success)
            self.assertEqual(subnet.version, 1)
            self.assertEqual(
                {
                    controller.gateways[gateway_id].get_stable_version(
                        subnet.task.task_id
                    )
                    for gateway_id in subnet.involved_gateways
                },
                {1},
            )
            self.assertTrue(
                all(
                    controller.gateways[gateway_id].get_staged_version(
                        subnet.task.task_id
                    )
                    is None
                    for gateway_id in subnet.involved_gateways
                )
            )
            stages = {record.event_stage for record in metrics.transaction_event_log}
            self.assertTrue(
                {
                    EventStage.STAGE_STARTED.value,
                    EventStage.VERIFY_FINISHED.value,
                    EventStage.ACTIVATE_FINISHED.value,
                    EventStage.POST_ACTIVATE_VERIFY_FINISHED.value,
                }
                <= stages
            )
            self.assertAlmostEqual(
                metrics.formation_latency_ms,
                (metrics.t_stable_verify_finished - metrics.t_task_received) * 1000,
            )

        asyncio.run(run())

    def test_initial_build_failure_leaves_version_zero(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            gateways = build_rescue_topology(provider)
            gateways["gw-mec"].reject_stage_versions.add(1)
            controller = AgentController(gateways)
            subnet, metrics = await controller.build_task_subnet(rescue_task())
            self.assertFalse(metrics.networking_success)
            self.assertEqual(subnet.version, 0)
            self.assertNotIn(subnet.task.task_id, controller.tasks)
            for gateway in gateways.values():
                self.assertEqual(gateway.get_stable_version(subnet.task.task_id), 0)
                self.assertIsNone(gateway.get_staged_version(subnet.task.task_id))

        asyncio.run(run())

    def test_initial_build_partial_stage_rolls_back(self) -> None:
        async def run() -> None:
            provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
            gateways = build_rescue_topology(provider)
            gateways["gw-cloud"].reject_stage_versions.add(1)
            controller = AgentController(gateways)
            subnet, metrics = await controller.build_task_subnet(rescue_task())
            self.assertTrue(metrics.rollback_triggered)
            self.assertTrue(metrics.rollback_success)
            self.assertEqual(subnet.status, "failed")
            self.assertTrue(
                all(not gateway.get_stable_rules(subnet.task.task_id) for gateway in gateways.values())
            )

        asyncio.run(run())


class StrategyComparisonTests(unittest.TestCase):
    def test_full_rebuild_updates_all_task_gateways(self) -> None:
        async def run() -> None:
            controller, _provider, subnet, _metrics = await _rescue()
            event = EventInjector().agent_remove(
                subnet.task.task_id,
                "agent-terminal-feedback",
            )
            plan = await FullRebuildStrategy(controller).plan(subnet, event)
            self.assertEqual(plan.affected_gateways, frozenset(subnet.involved_gateways))
            self.assertTrue(plan.full_rule_install)
            self.assertGreater(len(plan.rule_delta.updates), 0)

        asyncio.run(run())

    def test_incremental_updates_only_affected_gateways(self) -> None:
        async def run() -> None:
            controller, _provider, subnet, _metrics = await _rescue()
            event = EventInjector().agent_remove(
                subnet.task.task_id,
                "agent-terminal-feedback",
            )
            plan = await ProposedIncrementalStrategy(controller).plan(subnet, event)
            self.assertEqual(plan.affected_gateways, frozenset({"gw-ue", "gw-cloud"}))
            self.assertLess(len(plan.affected_gateways), len(subnet.involved_gateways))
            self.assertEqual(plan.rule_delta.gateway_ids, set(plan.affected_gateways))

        asyncio.run(run())

    def test_local_only_leaves_detectable_remote_residue(self) -> None:
        async def run() -> None:
            snapshot = ElasticScenarioGenerator().generate(
                ScenarioConfig(num_agents=10, num_gateways=4),
                0,
            )
            result, _events, _probes = await run_method(
                snapshot,
                "local_only",
                "test",
                run_index=1,
                probe_interval_ms=10.0,
            )
            self.assertFalse(result.success)
            self.assertGreater(result.residual_rules, 0)
            self.assertIn("residual_rules", result.failure_reason)

        asyncio.run(run())


class ScenarioFairnessTests(unittest.TestCase):
    def test_same_seed_produces_same_scenario(self) -> None:
        generator = ElasticScenarioGenerator()
        config = ScenarioConfig(num_agents=20, num_gateways=4)
        first = generator.generate(config, 9)
        second = generator.generate(config, 9)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_methods_share_identical_scenario(self) -> None:
        snapshot = ElasticScenarioGenerator().generate(ScenarioConfig(), 4)
        first, _verifier, _provider = snapshot.instantiate()
        second, _verifier2, _provider2 = snapshot.instantiate()
        self.assertEqual(first.gateway_paths, second.gateway_paths)
        self.assertEqual(snapshot.task, snapshot.task)
        self.assertEqual(snapshot.fingerprint, snapshot.fingerprint)

    def test_removed_agent_type_classification(self) -> None:
        generator = ElasticScenarioGenerator()
        observed = set()
        for position in ("leaf", "intermediate", "fan_in", "fan_out"):
            snapshot = generator.generate(
                ScenarioConfig(removed_agent_type=position),
                1,
            )
            classifications = classify_agent_types(snapshot.task)
            observed.add(classifications[snapshot.removed_agent_ids[0]])
        self.assertEqual(observed, {"leaf", "intermediate", "fan_in", "fan_out"})


class ProbeAndPhysicalTests(unittest.TestCase):
    def test_continuous_probe_detects_interruption(self) -> None:
        async def run() -> None:
            controller, _provider, subnet, _metrics = await _rescue()
            session = subnet.sessions[0]
            probe = ContinuousBusinessProbe(
                controller,
                subnet,
                {session.business_edge_id},
                interval_ms=1000.0,
            )
            await probe.start()
            gateway = controller.gateways[session.gateway_path[0]]
            rule = next(
                rule
                for rule in gateway.get_stable_rules(subnet.task.task_id).values()
                if rule.session_id == session.session_id
            )
            gateway.stable_rules.pop(rule.rule_id)
            await probe.sample("INJECTED_INTERRUPTION")
            gateway.stable_rules[rule.rule_id] = rule
            await probe.sample("RECOVERY_1")
            await probe.sample("RECOVERY_2")
            summary = await probe.stop()
            self.assertEqual(summary.unaffected_edges_interrupted, 1)
            self.assertGreaterEqual(summary.unaffected_interruption_ms, 0.0)

        asyncio.run(run())

    def test_unaffected_edge_metrics_are_recorded(self) -> None:
        async def run() -> None:
            snapshot = ElasticScenarioGenerator().generate(
                ScenarioConfig(num_agents=10),
                0,
            )
            result, _events, probes = await run_method(
                snapshot,
                "proposed",
                "test",
                run_index=2,
                probe_interval_ms=10.0,
            )
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(
                result.unaffected_edges_total,
                len(set(item.edge_id for item in probes)),
            )
            self.assertGreater(len(probes), 0)

        asyncio.run(run())

    def test_physical_resource_snapshot_restored_on_rollback(self) -> None:
        class FailPostActivationVerifier:
            def __init__(self) -> None:
                self.calls = 0

            async def verify(self, subnet):
                self.calls += 1
                ok = self.calls == 1
                return VerifyResult(
                    ok=ok,
                    verify_ms=0.0,
                    checked_edges=len(subnet.sessions),
                    passed_edges=len(subnet.sessions) if ok else 0,
                    mode="test-sequence",
                )

        async def run() -> None:
            controller, _provider, subnet, _metrics = await _rescue()
            before = {
                binding_id
                for gateway in controller.gateways.values()
                for agent in gateway.agents.values()
                for binding_id in getattr(agent, "resource_bindings", {})
            }
            event = EventInjector().agent_remove(
                subnet.task.task_id,
                "agent-terminal-feedback",
            )
            plan = await ProposedIncrementalStrategy(controller).plan(subnet, event)
            execution = await TransactionExecutor(
                controller,
                verifier=FailPostActivationVerifier(),
            ).execute(subnet, plan, event)
            after = {
                binding_id
                for gateway in controller.gateways.values()
                for agent in gateway.agents.values()
                for binding_id in getattr(agent, "resource_bindings", {})
            }
            self.assertFalse(execution.success)
            self.assertTrue(execution.rollback_success)
            self.assertEqual(before, after)

        asyncio.run(run())


class BatchAndStatisticsTests(unittest.TestCase):
    def test_batch_runner_preserves_failed_runs(self) -> None:
        async def run(directory: Path) -> None:
            config = {
                "simulation": {"timeout_ms": 10000},
                "task": {
                    "num_agents": 10,
                    "edge_ratio": 1.5,
                    "num_gateways": 4,
                    "cross_gateway_edge_ratio": 0.5,
                    "agent_removal_ratio": 0.1,
                    "removed_agent_type": "leaf",
                },
                "topology": {"multi_hop": True},
                "sweeps": {
                    "agent_counts": [10],
                    "gateway_counts": [4],
                    "cross_gateway_edge_ratios": [0.5],
                    "agent_removal_ratios": [0.1],
                    "removed_agent_types": ["leaf"],
                },
            }
            results = await run_experiment(
                config,
                methods=("local_only",),
                seeds=(0,),
                output_dir=directory,
            )
            with (directory / "raw" / "runs.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), len(results))
            self.assertTrue(any(not item.success for item in results))
            self.assertTrue(any(row["success"] == "False" for row in rows))

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(Path(directory)))

    def test_aggregate_confidence_interval(self) -> None:
        self.assertAlmostEqual(
            confidence_interval_95([1.0, 2.0, 3.0, 4.0]),
            1.96 * 1.2909944487358056 / 2.0,
        )
        self.assertEqual(confidence_interval_95([5.0]), 0.0)


if __name__ == "__main__":
    unittest.main()
