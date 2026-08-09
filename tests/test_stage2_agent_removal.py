from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from dataclasses import replace

from src.agents.phy_agent import PhyAgent
from src.controller.feasibility import check_four_layer_feasibility
from src.controller.networking import AgentController
from src.core.events import EventInjector, EventStage
from src.core.rules import compute_rule_delta
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology
from src.core.models import BusinessEdge, QoSRequirements, TaskSpec


REMOVED_LEAF = "agent-terminal-feedback"
REMOVED_INTERMEDIATE = "agent-edge-recognition"


def _controller_and_verifier() -> tuple[AgentController, SyntheticTaskSubnetVerifier]:
    # Keep the verification fixture comfortably above the rescue task's 18 Mbps
    # edge requirement; QoS-failure behavior is tested separately by injection.
    provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
    return (
        AgentController(build_rescue_topology(provider)),
        SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0),
    )


async def _build() -> tuple[AgentController, SyntheticTaskSubnetVerifier, object]:
    controller, verifier = _controller_and_verifier()
    subnet, _metrics = await controller.build_task_subnet(rescue_task())
    return controller, verifier, subnet


async def _remove(
    agent_id: str,
    *,
    task: TaskSpec | None = None,
    reject_stage_gateway: str | None = None,
):
    controller, verifier = _controller_and_verifier()
    subnet, _metrics = await controller.build_task_subnet(task or rescue_task())
    if reject_stage_gateway is not None:
        controller.gateways[reject_stage_gateway].reject_stage_versions.add(
            subnet.version + 1
        )
    event = EventInjector(prefix="test").agent_remove(
        subnet.task.task_id,
        agent_id,
    )
    result = await controller.handle_runtime_event(
        event,
        run_id=7,
        seed=11,
        verifier=verifier,
    )
    return controller, subnet, event, result


def _custom_task(task_id: str, edges: tuple[BusinessEdge, ...]) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        goal="stage-2 removal topology",
        app_agents=(
            "agent-drone-capture",
            "agent-edge-recognition",
            "agent-cloud-planning",
            "agent-terminal-feedback",
        ),
        biz_edges=edges,
        qos=QoSRequirements(
            max_latency_ms=160.0,
            max_loss_rate=0.03,
            min_reliability=0.99,
            min_bandwidth_mbps=20.0,
            data_volume_mb=100.0,
            priority=2,
        ),
    )


def _edge(source: str, target: str, suffix: str, rate: float = 2.0) -> BusinessEdge:
    return BusinessEdge(
        source=source,
        target=target,
        flow_type=suffix,
        data_rate_mbps=rate,
        latency_budget_ms=140.0,
        priority=2,
    )


class FourLayerStateTests(unittest.TestCase):
    def test_physical_agent_is_bound_to_task(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            self.assertEqual(subnet.version, 1)
            self.assertEqual(subnet.status, "stable")
            self.assertTrue(subnet.physical_agents)
            self.assertEqual(subnet.phy_agents, set(subnet.physical_agents))
            for session in subnet.sessions:
                self.assertTrue(session.p_agent_ids)
                for p_agent_id in session.p_agent_ids:
                    self.assertIn(p_agent_id, subnet.physical_agents)
                    self.assertIsInstance(controller._agent_by_id(p_agent_id), PhyAgent)
                    self.assertTrue(
                        any(
                            binding.edge_id == session.business_edge_id
                            and binding.agent_id == p_agent_id
                            for binding in subnet.physical_bindings.values()
                        )
                    )

        asyncio.run(run())

    def test_physical_capacity_participates_in_feasibility(self) -> None:
        async def run() -> None:
            _controller, _verifier, subnet = await _build()
            candidate = deepcopy(subnet)
            p_agent_id = next(iter(candidate.physical_agents))
            candidate.physical_agents[p_agent_id] = replace(
                candidate.physical_agents[p_agent_id],
                total_capacity_mbps=0.5,
            )
            result = check_four_layer_feasibility(candidate)
            self.assertFalse(result.feasible)
            self.assertIn(
                f"physical_capacity_exceeded:{p_agent_id}",
                result.violations,
            )

        asyncio.run(run())

    def test_physical_resource_is_released_after_agent_removal(self) -> None:
        async def run() -> None:
            controller, old_subnet, _event, result = await _remove(REMOVED_LEAF)
            self.assertTrue(result.success, result.failure_reason)
            removed_ids = set(result.scope.affected_physical_resources)
            self.assertTrue(removed_ids)
            self.assertTrue(removed_ids <= set(old_subnet.physical_bindings))
            self.assertTrue(removed_ids.isdisjoint(result.subnet.physical_bindings))
            for gateway in controller.gateways.values():
                for agent in gateway.agents.values():
                    if isinstance(agent, PhyAgent):
                        self.assertTrue(removed_ids.isdisjoint(agent.resource_bindings))

        asyncio.run(run())


class VersionedGatewayTransactionTests(unittest.TestCase):
    def test_stage_does_not_modify_stable_rules(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            before = dict(gateway.get_stable_rules(subnet.task.task_id))
            deletion = next(iter(before.values()))
            ack = await gateway.stage_delta(
                subnet.task.task_id,
                2,
                (),
                (),
                (deletion,),
            )
            self.assertTrue(ack.accepted)
            self.assertEqual(gateway.get_stable_rules(subnet.task.task_id), before)
            self.assertNotIn(
                deletion.rule_id,
                gateway.get_staged_rules(subnet.task.task_id),
            )
            await gateway.rollback(subnet.task.task_id, 2)

        asyncio.run(run())

    def test_activate_promotes_staged_version(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            self.assertTrue((await gateway.stage_delta(task_id, 2, (), (), ())).accepted)
            self.assertTrue((await gateway.stage_sessions(task_id, 2, subnet.sessions)).accepted)
            ack = await gateway.activate(task_id, 2)
            self.assertTrue(ack.accepted)
            self.assertEqual(gateway.get_stable_version(task_id), 2)
            self.assertIsNone(gateway.get_staged_version(task_id))
            self.assertFalse(gateway.get_staged_rules(task_id))

        asyncio.run(run())

    def test_rollback_preserves_previous_stable_version(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            before = dict(gateway.get_stable_rules(task_id))
            deletion = next(iter(before.values()))
            await gateway.stage_delta(task_id, 2, (), (), (deletion,))
            await gateway.stage_sessions(task_id, 2, subnet.sessions)
            self.assertTrue((await gateway.activate(task_id, 2)).accepted)
            self.assertTrue((await gateway.rollback(task_id, 2)).accepted)
            self.assertEqual(gateway.get_stable_version(task_id), 1)
            self.assertEqual(gateway.get_stable_rules(task_id), before)

        asyncio.run(run())

    def test_old_version_cannot_override_new_version(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            await gateway.stage_delta(task_id, 2, (), (), ())
            await gateway.stage_sessions(task_id, 2, subnet.sessions)
            await gateway.activate(task_id, 2)
            gateway.finalize(task_id, 2)
            ack = await gateway.stage_delta(task_id, 1, (), (), ())
            self.assertFalse(ack.accepted)
            self.assertEqual(ack.reason, "stale_version")
            self.assertEqual(gateway.get_stable_version(task_id), 2)

        asyncio.run(run())

    def test_delayed_ack_cannot_activate_stale_version(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            for version in (2, 3):
                await gateway.stage_delta(task_id, version, (), (), ())
                await gateway.stage_sessions(task_id, version, subnet.sessions)
                self.assertTrue((await gateway.activate(task_id, version)).accepted)
                gateway.finalize(task_id, version)
            delayed = await gateway.activate(task_id, 2)
            self.assertFalse(delayed.accepted)
            self.assertIn(delayed.reason, {"stale_or_missing_stage", "stale_version"})
            self.assertEqual(gateway.get_stable_version(task_id), 3)

        asyncio.run(run())

    def test_stale_rollback_cannot_clear_newer_staged_version(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            await gateway.stage_delta(task_id, 2, (), (), ())
            await gateway.stage_sessions(task_id, 2, subnet.sessions)
            await gateway.activate(task_id, 2)
            gateway.finalize(task_id, 2)
            await gateway.stage_delta(task_id, 3, (), (), ())
            delayed = await gateway.rollback(task_id, 2)
            self.assertFalse(delayed.accepted)
            self.assertEqual(delayed.reason, "stale_or_mismatched_rollback")
            self.assertEqual(gateway.get_stable_version(task_id), 2)
            self.assertEqual(gateway.get_staged_version(task_id), 3)
            await gateway.rollback(task_id, 3)

        asyncio.run(run())

    def test_staged_conflicting_rules_are_rejected(self) -> None:
        async def run() -> None:
            controller, _verifier, subnet = await _build()
            gateway = controller.gateways["gw-ue"]
            task_id = subnet.task.task_id
            original = next(iter(gateway.get_stable_rules(task_id).values()))
            duplicate_match = replace(
                original,
                version=2,
                rule_id=f"{original.rule_id}:duplicate",
            )
            self.assertTrue(
                (await gateway.stage_delta(task_id, 2, (duplicate_match,), (), ())).accepted
            )
            validation = await gateway.validate_staged(task_id, 2)
            self.assertFalse(validation.accepted)
            self.assertTrue(validation.reason.startswith("conflicting_rule_match:"))
            await gateway.rollback(task_id, 2)

        asyncio.run(run())

    def test_partial_stage_failure_triggers_global_rollback(self) -> None:
        async def run() -> None:
            controller, old_subnet, _event, result = await _remove(
                REMOVED_LEAF,
                reject_stage_gateway="gw-cloud",
            )
            self.assertFalse(result.success)
            self.assertTrue(result.metrics.rollback_triggered)
            self.assertTrue(result.metrics.rollback_success)
            self.assertEqual(controller.tasks[old_subnet.task.task_id].version, 1)
            for gateway_id in old_subnet.involved_gateways:
                gateway = controller.gateways[gateway_id]
                self.assertEqual(gateway.get_stable_version(old_subnet.task.task_id), 1)
                self.assertIsNone(gateway.get_staged_version(old_subnet.task.task_id))
            stages = {record.event_stage for record in result.event_log}
            self.assertIn(EventStage.ROLLBACK_STARTED.value, stages)
            self.assertIn(EventStage.ROLLBACK_FINISHED.value, stages)

        asyncio.run(run())

    def test_partial_activation_failure_triggers_global_rollback(self) -> None:
        async def run() -> None:
            controller, verifier = _controller_and_verifier()
            subnet, _metrics = await controller.build_task_subnet(rescue_task())
            before = {
                gateway_id: dict(
                    controller.gateways[gateway_id].get_stable_rules(subnet.task.task_id)
                )
                for gateway_id in subnet.involved_gateways
            }
            controller.gateways["gw-cloud"].reject_activate_versions.add(2)
            event = EventInjector(prefix="activate-failure").agent_remove(
                subnet.task.task_id,
                REMOVED_LEAF,
            )
            result = await controller.handle_runtime_event(event, verifier=verifier)
            self.assertFalse(result.success)
            self.assertTrue(result.metrics.rollback_triggered)
            self.assertTrue(result.metrics.rollback_success)
            for gateway_id in subnet.involved_gateways:
                gateway = controller.gateways[gateway_id]
                self.assertEqual(gateway.get_stable_version(subnet.task.task_id), 1)
                self.assertEqual(
                    gateway.get_stable_rules(subnet.task.task_id),
                    before[gateway_id],
                )
                self.assertIsNone(gateway.get_staged_version(subnet.task.task_id))

        asyncio.run(run())

    def test_rule_delta_records_content_update_separately(self) -> None:
        async def run() -> None:
            _controller, _verifier, subnet = await _build()
            old_rule = next(iter(subnet.rules.values()))
            new_rule = replace(
                old_rule,
                version=2,
                action=replace(old_rule.action, priority=old_rule.action.priority + 1),
            )
            delta = compute_rule_delta((old_rule,), (new_rule,))
            self.assertEqual(delta.additions, ())
            self.assertEqual(delta.deletions, ())
            self.assertEqual(delta.updates, (new_rule,))

        asyncio.run(run())


class AgentRemovalTransactionTests(unittest.TestCase):
    def test_remove_leaf_agent(self) -> None:
        async def run() -> None:
            controller, old_subnet, _event, result = await _remove(REMOVED_LEAF)
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(old_subnet.version, 1)
            self.assertEqual(result.subnet.version, 2)
            self.assertNotIn(REMOVED_LEAF, result.subnet.business_agents)
            self.assertTrue(
                {
                    REMOVED_LEAF,
                    "tagent-gw-cloud",
                    "nagent-gw-cloud",
                    "pagent-gw-cloud",
                    "pagent-gw-ue",
                }
                <= set(result.scope.affected_agents)
            )
            self.assertFalse(
                any(
                    edge.source == REMOVED_LEAF or edge.target == REMOVED_LEAF
                    for edge in result.subnet.task.biz_edges
                )
            )
            self.assertEqual(
                {
                    controller.gateways[gateway_id].get_stable_version(
                        result.subnet.task.task_id
                    )
                    for gateway_id in old_subnet.involved_gateways
                },
                {2},
            )
            for gateway_id in old_subnet.involved_gateways:
                gateway = controller.gateways[gateway_id]
                self.assertIsNone(gateway.get_staged_version(result.subnet.task.task_id))
                self.assertFalse(gateway.get_staged_rules(result.subnet.task.task_id))

        asyncio.run(run())

    def test_remove_intermediate_agent(self) -> None:
        async def run() -> None:
            _controller, _old_subnet, _event, result = await _remove(
                REMOVED_INTERMEDIATE
            )
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(len(result.scope.affected_business_edges), 2)
            self.assertEqual(len(result.subnet.task.biz_edges), 1)
            remaining = result.subnet.task.biz_edges[0]
            self.assertEqual(
                (remaining.source, remaining.target),
                ("agent-cloud-planning", "agent-terminal-feedback"),
            )

        asyncio.run(run())

    def test_remove_fan_in_agent(self) -> None:
        async def run() -> None:
            task = _custom_task(
                "task-fan-in",
                (
                    _edge("agent-drone-capture", "agent-cloud-planning", "in-1"),
                    _edge("agent-edge-recognition", "agent-cloud-planning", "in-2"),
                    _edge("agent-drone-capture", "agent-edge-recognition", "unaffected"),
                ),
            )
            _controller, _old, _event, result = await _remove(
                "agent-cloud-planning",
                task=task,
            )
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(len(result.scope.affected_business_edges), 2)
            self.assertEqual(len(result.scope.unaffected_business_edges), 1)

        asyncio.run(run())

    def test_remove_fan_out_agent(self) -> None:
        async def run() -> None:
            task = _custom_task(
                "task-fan-out",
                (
                    _edge("agent-drone-capture", "agent-edge-recognition", "out-1"),
                    _edge("agent-drone-capture", "agent-cloud-planning", "out-2"),
                    _edge("agent-edge-recognition", "agent-cloud-planning", "unaffected"),
                ),
            )
            _controller, _old, _event, result = await _remove(
                "agent-drone-capture",
                task=task,
            )
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(len(result.scope.affected_business_edges), 2)
            self.assertEqual(len(result.scope.unaffected_business_edges), 1)

        asyncio.run(run())

    def test_remote_gateway_rules_are_removed(self) -> None:
        async def run() -> None:
            controller, _old, _event, result = await _remove(REMOVED_INTERMEDIATE)
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(
                set(result.scope.affected_gateways),
                {"gw-ue", "gw-mec", "gw-cloud"},
            )
            for gateway in controller.gateways.values():
                self.assertFalse(
                    any(
                        rule.src_agent == REMOVED_INTERMEDIATE
                        or rule.dst_agent == REMOVED_INTERMEDIATE
                        for rule in gateway.get_stable_rules(result.subnet.task.task_id).values()
                    )
                )

        asyncio.run(run())

    def test_transport_session_is_removed(self) -> None:
        async def run() -> None:
            controller, _old, _event, result = await _remove(REMOVED_INTERMEDIATE)
            self.assertTrue(result.success, result.failure_reason)
            removed_sessions = set(result.scope.affected_sessions)
            self.assertTrue(removed_sessions)
            self.assertTrue(
                removed_sessions.isdisjoint(
                    session.session_id for session in result.subnet.sessions
                )
            )
            for gateway in controller.gateways.values():
                self.assertTrue(removed_sessions.isdisjoint(gateway.installed_sessions))

        asyncio.run(run())

    def test_physical_binding_is_removed(self) -> None:
        async def run() -> None:
            _controller, _old, _event, result = await _remove(REMOVED_INTERMEDIATE)
            self.assertTrue(result.success, result.failure_reason)
            self.assertTrue(
                set(result.scope.affected_physical_resources).isdisjoint(
                    result.subnet.physical_bindings
                )
            )

        asyncio.run(run())

    def test_unaffected_edges_remain_reachable(self) -> None:
        async def run() -> None:
            controller, _old, _event, result = await _remove(REMOVED_INTERMEDIATE)
            self.assertTrue(result.success, result.failure_reason)
            for edge_id in result.scope.unaffected_business_edges:
                session = next(
                    session
                    for session in result.subnet.sessions
                    if session.business_edge_id == edge_id
                )
                for gateway_id in session.gateway_path:
                    self.assertTrue(
                        controller.gateways[gateway_id].flow_allowed(
                            result.subnet.task.task_id,
                            session.source,
                            session.target,
                        )
                    )
            self.assertEqual(result.metrics.unaffected_edges_interrupted, 0)
            self.assertEqual(result.metrics.unaffected_service_interruption_ms, 0.0)

        asyncio.run(run())

    def test_residual_rule_count_is_zero(self) -> None:
        async def run() -> None:
            controller, _old, _event, result = await _remove(REMOVED_LEAF)
            self.assertTrue(result.success, result.failure_reason)
            self.assertEqual(result.metrics.residual_rules, 0)
            self.assertFalse(
                any(
                    rule.src_agent == REMOVED_LEAF or rule.dst_agent == REMOVED_LEAF
                    for gateway in controller.gateways.values()
                    for rule in gateway.get_stable_rules(result.subnet.task.task_id).values()
                )
            )

        asyncio.run(run())

    def test_unauthorized_communication_remains_blocked(self) -> None:
        async def run() -> None:
            controller, _old, _event, result = await _remove(REMOVED_LEAF)
            self.assertTrue(result.success, result.failure_reason)
            task_id = result.subnet.task.task_id
            for gateway in controller.gateways.values():
                self.assertFalse(
                    gateway.flow_allowed(
                        task_id,
                        "agent-drone-capture",
                        "agent-cloud-planning",
                    )
                )
                self.assertFalse(
                    gateway.flow_allowed(
                        task_id,
                        "agent-cloud-planning",
                        REMOVED_LEAF,
                    )
                )

        asyncio.run(run())


class RuntimeEventLogTests(unittest.TestCase):
    def test_event_timestamp_precedes_processing(self) -> None:
        async def run() -> None:
            _controller, _old, event, result = await _remove(REMOVED_LEAF)
            received = next(
                record.timestamp
                for record in result.event_log
                if record.event_stage == EventStage.EVENT_RECEIVED.value
            )
            self.assertLessEqual(event.occurred_at, received)
            self.assertEqual(result.event_log[0].timestamp, event.occurred_at)
            self.assertEqual(
                [record.timestamp for record in result.event_log],
                sorted(record.timestamp for record in result.event_log),
            )

        asyncio.run(run())

    def test_elastic_latency_uses_event_occurred_at(self) -> None:
        async def run() -> None:
            _controller, _old, event, result = await _remove(REMOVED_LEAF)
            expected_ms = (
                result.metrics.activation_finished_at - event.occurred_at
            ) * 1000.0
            self.assertAlmostEqual(result.metrics.elastic_latency_ms, expected_ms, places=9)
            self.assertGreater(result.metrics.elastic_latency_ms, 0.0)

        asyncio.run(run())

    def test_all_transaction_stages_are_logged(self) -> None:
        async def run() -> None:
            _controller, _old, _event, success = await _remove(REMOVED_LEAF)
            success_stages = {record.event_stage for record in success.event_log}
            required_success = {
                EventStage.EVENT_OCCURRED.value,
                EventStage.EVENT_RECEIVED.value,
                EventStage.SCOPE_STARTED.value,
                EventStage.SCOPE_FINISHED.value,
                EventStage.DELTA_COMPILED.value,
                EventStage.STAGE_STARTED.value,
                EventStage.STAGE_FINISHED.value,
                EventStage.VERIFY_STARTED.value,
                EventStage.VERIFY_FINISHED.value,
                EventStage.ACTIVATE_STARTED.value,
                EventStage.ACTIVATE_FINISHED.value,
            }
            self.assertTrue(required_success <= success_stages)
            self.assertNotIn(EventStage.ROLLBACK_STARTED.value, success_stages)

            _controller, _old, _event, failed = await _remove(
                REMOVED_LEAF,
                reject_stage_gateway="gw-cloud",
            )
            failed_stages = {record.event_stage for record in failed.event_log}
            self.assertIn(EventStage.ROLLBACK_STARTED.value, failed_stages)
            self.assertIn(EventStage.ROLLBACK_FINISHED.value, failed_stages)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
