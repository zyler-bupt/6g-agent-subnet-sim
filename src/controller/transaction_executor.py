from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable

from src.agents.phy_agent import PhyAgent
from src.controller.feasibility import check_four_layer_feasibility
from src.controller.reconfiguration import ReconfigurationPlan
from src.core.events import EventLogRecord, EventStage, RuntimeEvent
from src.core.models import PhysicalResourceBinding, TaskState, TaskSubnet
from src.core.rules import RuleDelta, rule_content_key
from src.e2e.models import VerifyResult
from src.e2e.transactional_installers import (
    InMemoryTransactionalInstaller,
    TransactionalInstallResult,
    TransactionalInstaller,
)
from src.simulation.continuous_probe import (
    ContinuousBusinessProbe,
    ContinuousProbeSummary,
)

if TYPE_CHECKING:
    from src.controller.networking import AgentController
    from src.e2e.verifiers import TaskSubnetVerifier


@dataclass(frozen=True)
class TransactionExecutionResult:
    success: bool
    state: TaskSubnet
    plan: ReconfigurationPlan
    event_log: tuple[EventLogRecord, ...]
    failure_reason: str
    rollback_triggered: bool
    rollback_success: bool
    control_messages: int
    control_bytes: int
    staged_gateway_ids: tuple[str, ...]
    activated_gateway_ids: tuple[str, ...]
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    physical_bindings_before: dict[str, PhysicalResourceBinding]
    physical_bindings_after: dict[str, PhysicalResourceBinding]
    released_physical_resources: tuple[str, ...]
    residual_physical_resources: tuple[str, ...]
    detected_residual_rule_ids: tuple[str, ...]
    probe_summary: ContinuousProbeSummary

    def timestamp(self, stage: EventStage | str, *, last: bool = False) -> float:
        value = stage.value if isinstance(stage, EventStage) else str(stage)
        matches = [item.timestamp for item in self.event_log if item.event_stage == value]
        if not matches:
            return 0.0
        return matches[-1] if last else matches[0]


class TransactionExecutor:
    """Shared stage/verify/activate/rollback path for all strategies."""

    def __init__(
        self,
        controller: "AgentController",
        *,
        installer: TransactionalInstaller | None = None,
        verifier: "TaskSubnetVerifier | None" = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.controller = controller
        self.installer = installer or InMemoryTransactionalInstaller()
        self.verifier = verifier
        self.clock = clock

    async def execute(
        self,
        stable_state: TaskSubnet | None,
        plan: ReconfigurationPlan,
        event: RuntimeEvent,
        *,
        run_id: int = 1,
        seed: int = 0,
        probe: ContinuousBusinessProbe | None = None,
        received_at: float | None = None,
        pre_stage_records: tuple[EventLogRecord, ...] = (),
    ) -> TransactionExecutionResult:
        old_version = stable_state.version if stable_state is not None else 0
        target = plan.target_state
        if target.version != old_version + 1:
            raise ValueError(
                f"candidate version must be {old_version + 1}, got {target.version}"
            )
        if target.task.task_id != event.task_id:
            raise ValueError("transaction event and target task do not match")

        records: list[EventLogRecord] = []

        def log(
            stage: EventStage | str,
            component: str,
            details: dict[str, Any] | None = None,
            *,
            timestamp: float | None = None,
        ) -> None:
            value = stage.value if isinstance(stage, EventStage) else str(stage)
            record = EventLogRecord(
                run_id=run_id,
                event_id=event.event_id,
                task_id=event.task_id,
                old_version=old_version,
                new_version=target.version,
                timestamp=self.clock() if timestamp is None else timestamp,
                component=component,
                event_type=event.event_type,
                event_stage=value,
                details=dict(details or {}),
            )
            if records and record.timestamp < records[-1].timestamp:
                raise ValueError(f"non-monotonic transaction timestamp at {value}")
            records.append(record)

        log(
            EventStage.EVENT_OCCURRED,
            "EventInjector",
            dict(event.payload),
            timestamp=event.occurred_at,
        )
        log(
            EventStage.EVENT_RECEIVED,
            "AgentController",
            {"method": plan.method},
            timestamp=received_at,
        )
        for record in pre_stage_records:
            if record.task_id != event.task_id or record.event_id != event.event_id:
                raise ValueError("pre-stage event record does not match transaction")
            if records and record.timestamp < records[-1].timestamp:
                raise ValueError("non-monotonic pre-stage event record")
            records.append(record)

        gateway_ids = set(plan.verification_gateways | plan.transaction_gateways)
        before_snapshot = gateway_configuration_snapshot(
            self.controller,
            event.task_id,
            gateway_ids,
        )
        physical_before = self._actual_task_bindings(event.task_id)
        physical_restore_snapshot: dict[str, PhysicalResourceBinding] | None = None
        control_messages = 0
        control_bytes = 0
        staged_gateways: list[str] = []
        activated_gateways: list[str] = []
        detected_residual_rules: set[str] = set()
        probe_summary = _empty_probe_summary()
        if probe is not None:
            await probe.start()
            await probe.sample("BEFORE_STAGE")

        target.state = TaskState.STAGED
        log(
            EventStage.STAGE_STARTED,
            "TransactionExecutor",
            {
                "method": plan.method,
                "gateways": sorted(plan.transaction_gateways),
                "affected_gateways": sorted(plan.affected_gateways),
                "installer": self.installer.mode,
            },
        )
        stage_results = await asyncio.gather(
            *(
                self.installer.stage(
                    self.controller.gateways[gateway_id],
                    event.task_id,
                    target.version,
                    plan.rule_delta_by_gateway.get(gateway_id, RuleDelta()),
                    tuple(
                        session
                        for session in target.sessions
                        if gateway_id in _session_gateways(session)
                    ),
                )
                for gateway_id in sorted(plan.transaction_gateways)
            )
        )
        for result in stage_results:
            control_messages += result.control_messages
            control_bytes += result.control_bytes
            if result.accepted:
                staged_gateways.append(result.gateway_id)
        stage_ok = all(result.accepted for result in stage_results)
        log(
            EventStage.STAGE_FINISHED,
            "TransactionExecutor",
            {
                "accepted": stage_ok,
                "results": [asdict(result) for result in stage_results],
            },
        )
        if probe is not None:
            await probe.sample("STAGE_FINISHED")
        if not stage_ok:
            reason = "stage failed: " + " | ".join(
                result.reason for result in stage_results if not result.accepted
            )
            rollback_ok, messages, byte_count = await self._rollback(
                event.task_id,
                target.version,
                plan.transaction_gateways,
                physical_restore_snapshot,
                log,
                probe,
            )
            control_messages += messages
            control_bytes += byte_count
            if probe is not None:
                probe_summary = await probe.stop()
            target.state = TaskState.FAILED
            return self._result(
                stable_state,
                plan,
                records,
                reason,
                True,
                rollback_ok,
                control_messages,
                control_bytes,
                staged_gateways,
                activated_gateways,
                before_snapshot,
                physical_before,
                detected_residual_rules,
                probe_summary,
            )

        target.state = TaskState.VERIFIED
        log(EventStage.VERIFY_STARTED, "TransactionExecutor", {"phase": "staged"})
        validation_results = await asyncio.gather(
            *(
                self.installer.validate(
                    self.controller.gateways[gateway_id],
                    event.task_id,
                    target.version,
                )
                for gateway_id in sorted(plan.transaction_gateways)
            )
        )
        for result in validation_results:
            control_messages += result.control_messages
            control_bytes += result.control_bytes
        errors, residuals, verify_result = await self._verify(
            stable_state,
            plan,
            event,
            staged=True,
        )
        detected_residual_rules.update(residuals)
        errors.extend(
            f"{result.gateway_id}:{result.reason}"
            for result in validation_results
            if not result.accepted
        )
        log(
            EventStage.VERIFY_FINISHED,
            "TransactionExecutor",
            {
                "phase": "staged",
                "ok": not errors,
                "errors": errors,
                "business_verify": _verify_details(verify_result),
            },
        )
        if probe is not None:
            await probe.sample("STAGED_VERIFY_FINISHED")
        if errors:
            reason = "staged verification failed: " + " | ".join(errors)
            rollback_ok, messages, byte_count = await self._rollback(
                event.task_id,
                target.version,
                plan.transaction_gateways,
                physical_restore_snapshot,
                log,
                probe,
            )
            control_messages += messages
            control_bytes += byte_count
            if probe is not None:
                probe_summary = await probe.stop()
            target.state = TaskState.FAILED
            return self._result(
                stable_state,
                plan,
                records,
                reason,
                True,
                rollback_ok,
                control_messages,
                control_bytes,
                staged_gateways,
                activated_gateways,
                before_snapshot,
                physical_before,
                detected_residual_rules,
                probe_summary,
            )

        target.state = TaskState.ACTIVATING
        log(
            EventStage.ACTIVATE_STARTED,
            "TransactionExecutor",
            {"gateways": sorted(plan.transaction_gateways)},
        )
        activation_results = await asyncio.gather(
            *(
                self.installer.activate(
                    self.controller.gateways[gateway_id],
                    event.task_id,
                    target.version,
                )
                for gateway_id in sorted(plan.transaction_gateways)
            )
        )
        for result in activation_results:
            control_messages += result.control_messages
            control_bytes += result.control_bytes
            if result.accepted:
                activated_gateways.append(result.gateway_id)
        if probe is not None:
            await probe.sample("GATEWAY_ACTIVATION_FINISHED")
        if not all(result.accepted for result in activation_results):
            reason = "activation failed: " + " | ".join(
                result.reason for result in activation_results if not result.accepted
            )
            rollback_ok, messages, byte_count = await self._rollback(
                event.task_id,
                target.version,
                plan.transaction_gateways,
                physical_restore_snapshot,
                log,
                probe,
            )
            control_messages += messages
            control_bytes += byte_count
            if probe is not None:
                probe_summary = await probe.stop()
            target.state = TaskState.FAILED
            return self._result(
                stable_state,
                plan,
                records,
                reason,
                True,
                rollback_ok,
                control_messages,
                control_bytes,
                staged_gateways,
                activated_gateways,
                before_snapshot,
                physical_before,
                detected_residual_rules,
                probe_summary,
            )

        try:
            physical_unchanged = (
                "physical" not in plan.affected_layers
                and target.physical_bindings == stable_state.physical_bindings
            )
            if not physical_unchanged:
                physical_restore_snapshot = self._replace_physical_bindings(target)
                target.physical_agents = self.controller._physical_states_for_sessions(
                    target.sessions
                )
        except ValueError as error:
            reason = f"physical activation failed: {error}"
            rollback_ok, messages, byte_count = await self._rollback(
                event.task_id,
                target.version,
                plan.transaction_gateways,
                physical_before,
                log,
                probe,
            )
            control_messages += messages
            control_bytes += byte_count
            if probe is not None:
                probe_summary = await probe.stop()
            target.state = TaskState.FAILED
            return self._result(
                stable_state,
                plan,
                records,
                reason,
                True,
                rollback_ok,
                control_messages,
                control_bytes,
                staged_gateways,
                activated_gateways,
                before_snapshot,
                physical_before,
                detected_residual_rules,
                probe_summary,
            )

        log(
            EventStage.ACTIVATE_FINISHED,
            "TransactionExecutor",
            {
                "gateway_activation_ok": True,
                "gateways": sorted(plan.transaction_gateways),
            },
        )
        log(
            EventStage.POST_ACTIVATE_VERIFY_STARTED,
            "TransactionExecutor",
            {"phase": "stable"},
        )
        post_errors, post_residuals, post_verify = await self._verify(
            stable_state,
            plan,
            event,
            staged=False,
        )
        detected_residual_rules.update(post_residuals)
        log(
            EventStage.POST_ACTIVATE_VERIFY_FINISHED,
            "TransactionExecutor",
            {
                "phase": "stable",
                "ok": not post_errors,
                "errors": post_errors,
                "business_verify": _verify_details(post_verify),
            },
        )
        if probe is not None:
            await probe.sample("STABLE_VERIFY_FINISHED")
        if post_errors:
            reason = "stable verification failed: " + " | ".join(post_errors)
            rollback_ok, messages, byte_count = await self._rollback(
                event.task_id,
                target.version,
                plan.transaction_gateways,
                physical_restore_snapshot,
                log,
                probe,
            )
            control_messages += messages
            control_bytes += byte_count
            if probe is not None:
                probe_summary = await probe.stop()
            target.state = TaskState.FAILED
            return self._result(
                stable_state,
                plan,
                records,
                reason,
                True,
                rollback_ok,
                control_messages,
                control_bytes,
                staged_gateways,
                activated_gateways,
                before_snapshot,
                physical_before,
                detected_residual_rules,
                probe_summary,
            )

        for gateway_id in sorted(plan.transaction_gateways):
            self.installer.finalize(
                self.controller.gateways[gateway_id],
                event.task_id,
                target.version,
            )
        target.state = TaskState.STABLE
        target.gateways = self.controller._gateway_state_snapshot(
            event.task_id,
            set(plan.verification_gateways | plan.transaction_gateways),
        )
        self.controller.tasks[event.task_id] = target
        log(
            "TRANSACTION_COMMITTED",
            "TransactionExecutor",
            {"stable_version": target.version, "method": plan.method},
        )
        if probe is not None:
            await probe.sample("TRANSACTION_COMMITTED")
            probe_summary = await probe.stop()
        return self._result(
            stable_state,
            plan,
            records,
            "",
            False,
            False,
            control_messages,
            control_bytes,
            staged_gateways,
            activated_gateways,
            before_snapshot,
            physical_before,
            detected_residual_rules,
            probe_summary,
        )

    async def _verify(
        self,
        stable_state: TaskSubnet | None,
        plan: ReconfigurationPlan,
        event: RuntimeEvent,
        *,
        staged: bool,
    ) -> tuple[list[str], set[str], VerifyResult | None]:
        target = plan.target_state
        errors: list[str] = []
        residual_rule_ids: set[str] = set()
        expected_sessions_by_gateway = {
            gateway_id: {
                session.session_id
                for session in target.sessions
                if gateway_id in _session_gateways(session)
            }
            for gateway_id in plan.verification_gateways
        }
        removed_agents = set(event.agent_ids)

        for gateway_id in sorted(plan.verification_gateways):
            gateway = self.controller.gateways[gateway_id]
            participant = gateway_id in plan.transaction_gateways
            if staged and participant:
                actual_rules = gateway.get_staged_rules(event.task_id)
                actual_sessions = gateway.get_staged_sessions(event.task_id)
                actual_version = gateway.get_staged_version(event.task_id)
            else:
                actual_rules = gateway.get_stable_rules(event.task_id)
                actual_sessions = gateway.get_stable_sessions(event.task_id)
                actual_version = gateway.get_stable_version(event.task_id)

            expected_rules = {
                rule.rule_id: rule
                for rule in target.gateway_routes.get(gateway_id, [])
            }
            extra = set(actual_rules) - set(expected_rules)
            missing = set(expected_rules) - set(actual_rules)
            residual_rule_ids.update(
                rule_id
                for rule_id in extra
                if not removed_agents
                or actual_rules[rule_id].src_agent in removed_agents
                or actual_rules[rule_id].dst_agent in removed_agents
            )
            if extra:
                errors.append(f"residual_rules:{gateway_id}:{','.join(sorted(extra))}")
            if missing:
                errors.append(f"missing_rules:{gateway_id}:{','.join(sorted(missing))}")
            for rule_id in set(actual_rules) & set(expected_rules):
                if rule_content_key(actual_rules[rule_id]) != rule_content_key(
                    expected_rules[rule_id]
                ):
                    errors.append(f"rule_content_mismatch:{gateway_id}:{rule_id}")

            expected_session_ids = expected_sessions_by_gateway.get(gateway_id, set())
            if set(actual_sessions) != expected_session_ids:
                errors.append(f"session_set_mismatch:{gateway_id}")
            if actual_version != target.version:
                errors.append(
                    f"version_mismatch:{gateway_id}:{actual_version}!={target.version}"
                )
            if not staged and gateway.get_staged_version(event.task_id) is not None:
                errors.append(f"staged_state_not_empty:{gateway_id}")

        errors.extend(_target_invariant_errors(target, removed_agents))
        verify_result: VerifyResult | None = None
        if self.verifier is not None and target.sessions:
            verify_result = await self.verifier.verify(target)
            if not verify_result.ok:
                errors.append("business_qos_verification_failed")
        return errors, residual_rule_ids, verify_result

    def _replace_physical_bindings(
        self,
        target: TaskSubnet,
    ) -> dict[str, PhysicalResourceBinding]:
        task_id = target.task.task_id
        previous = self._actual_task_bindings(task_id)
        self._remove_actual_task_bindings(task_id)
        installed: list[str] = []
        try:
            for binding in target.physical_bindings.values():
                agent = self.controller._agent_by_id(binding.agent_id)
                if not isinstance(agent, PhyAgent):
                    raise ValueError(f"physical Agent unavailable: {binding.agent_id}")
                agent.restore_resource(binding)
                installed.append(binding.binding_id)
        except Exception:
            self._remove_actual_task_bindings(task_id)
            for binding in previous.values():
                agent = self.controller._agent_by_id(binding.agent_id)
                if isinstance(agent, PhyAgent):
                    agent.restore_resource(binding)
            raise
        return previous

    def _restore_physical_bindings(
        self,
        task_id: str,
        bindings: dict[str, PhysicalResourceBinding],
    ) -> bool:
        self._remove_actual_task_bindings(task_id)
        try:
            for binding in bindings.values():
                agent = self.controller._agent_by_id(binding.agent_id)
                if not isinstance(agent, PhyAgent):
                    return False
                agent.restore_resource(binding)
        except ValueError:
            return False
        return True

    def _remove_actual_task_bindings(self, task_id: str) -> None:
        for gateway in self.controller.gateways.values():
            for agent in gateway.agents.values():
                if not isinstance(agent, PhyAgent):
                    continue
                for binding_id, binding in list(agent.resource_bindings.items()):
                    if binding.task_id == task_id:
                        agent.release_resource(binding_id)

    def _actual_task_bindings(
        self,
        task_id: str,
    ) -> dict[str, PhysicalResourceBinding]:
        return {
            binding_id: binding
            for gateway in self.controller.gateways.values()
            for agent in gateway.agents.values()
            if isinstance(agent, PhyAgent)
            for binding_id, binding in agent.resource_bindings.items()
            if binding.task_id == task_id and binding.active
        }

    async def _rollback(
        self,
        task_id: str,
        version: int,
        gateway_ids: frozenset[str],
        physical_snapshot: dict[str, PhysicalResourceBinding] | None,
        log,
        probe: ContinuousBusinessProbe | None,
    ) -> tuple[bool, int, int]:
        log(
            EventStage.ROLLBACK_STARTED,
            "TransactionExecutor",
            {"gateways": sorted(gateway_ids)},
        )
        results = await asyncio.gather(
            *(
                self.installer.rollback(
                    self.controller.gateways[gateway_id],
                    task_id,
                    version,
                )
                for gateway_id in sorted(gateway_ids)
            )
        )
        messages = sum(result.control_messages for result in results)
        byte_count = sum(result.control_bytes for result in results)
        physical_ok = True
        if physical_snapshot is not None:
            physical_ok = self._restore_physical_bindings(task_id, physical_snapshot)
        expected_version = version - 1
        versions_ok = all(
            self.controller.gateways[gateway_id].get_stable_version(task_id)
            == expected_version
            and self.controller.gateways[gateway_id].get_staged_version(task_id) is None
            for gateway_id in gateway_ids
        )
        ok = all(result.accepted for result in results) and physical_ok and versions_ok
        if probe is not None:
            await probe.sample("ROLLBACK_FINISHED")
        log(
            EventStage.ROLLBACK_FINISHED,
            "TransactionExecutor",
            {"success": ok, "stable_version": expected_version},
        )
        return ok, messages, byte_count

    def _result(
        self,
        stable_state: TaskSubnet | None,
        plan: ReconfigurationPlan,
        records: list[EventLogRecord],
        failure_reason: str,
        rollback_triggered: bool,
        rollback_success: bool,
        control_messages: int,
        control_bytes: int,
        staged_gateways: list[str],
        activated_gateways: list[str],
        before_snapshot: dict[str, Any],
        physical_before: dict[str, PhysicalResourceBinding],
        detected_residual_rules: set[str],
        probe_summary: ContinuousProbeSummary,
    ) -> TransactionExecutionResult:
        state = plan.target_state if not failure_reason else stable_state
        if state is None:
            state = plan.target_state
            state.version = 0
            state.state = TaskState.FAILED
        gateway_ids = set(plan.verification_gateways | plan.transaction_gateways)
        after_snapshot = gateway_configuration_snapshot(
            self.controller,
            plan.target_state.task.task_id,
            gateway_ids,
        )
        physical_after = self._actual_task_bindings(plan.target_state.task.task_id)
        expected_physical = set(state.physical_bindings)
        residual_physical = tuple(sorted(set(physical_after) - expected_physical))
        released = tuple(sorted(set(physical_before) - set(physical_after)))
        return TransactionExecutionResult(
            success=not failure_reason,
            state=state,
            plan=plan,
            event_log=tuple(records),
            failure_reason=failure_reason,
            rollback_triggered=rollback_triggered,
            rollback_success=rollback_success,
            control_messages=control_messages,
            control_bytes=control_bytes,
            staged_gateway_ids=tuple(sorted(set(staged_gateways))),
            activated_gateway_ids=tuple(sorted(set(activated_gateways))),
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            physical_bindings_before=physical_before,
            physical_bindings_after=physical_after,
            released_physical_resources=released,
            residual_physical_resources=residual_physical,
            detected_residual_rule_ids=tuple(sorted(detected_residual_rules)),
            probe_summary=probe_summary,
        )


def gateway_configuration_snapshot(
    controller: "AgentController",
    task_id: str,
    gateway_ids: set[str],
) -> dict[str, Any]:
    return {
        gateway_id: {
            "stable_version": controller.gateways[gateway_id].get_stable_version(task_id),
            "staged_version": controller.gateways[gateway_id].get_staged_version(task_id),
            "stable_rule_ids": sorted(
                controller.gateways[gateway_id].get_stable_rules(task_id)
            ),
            "staged_rule_ids": sorted(
                controller.gateways[gateway_id].get_staged_rules(task_id)
            ),
            "stable_session_ids": sorted(
                controller.gateways[gateway_id].get_stable_sessions(task_id)
            ),
            "staged_session_ids": sorted(
                controller.gateways[gateway_id].get_staged_sessions(task_id)
            ),
        }
        for gateway_id in sorted(gateway_ids)
    }


def _target_invariant_errors(
    target: TaskSubnet,
    removed_agents: set[str],
) -> list[str]:
    errors: list[str] = []
    edges = target.business_edges
    sessions_by_edge: dict[str, list] = {}
    for session in target.sessions:
        sessions_by_edge.setdefault(session.business_edge_id, []).append(session)
    for edge_id in edges:
        if len(sessions_by_edge.get(edge_id, [])) != 1:
            errors.append(f"business_edge_session_count:{edge_id}")
    if set(sessions_by_edge) - set(edges):
        errors.append("session_for_unauthorized_edge")
    authorized_pairs = {(edge.source, edge.target) for edge in edges.values()}
    for rule in target.rules.values():
        if not rule.allowed or (rule.src_agent, rule.dst_agent) not in authorized_pairs:
            errors.append(f"unauthorized_rule:{rule.rule_id}")
    for session in target.sessions:
        rule_gateways = {
            rule.gateway_id
            for rule in target.rules.values()
            if rule.session_id == session.session_id
        }
        if rule_gateways != set(_session_gateways(session)):
            errors.append(f"incomplete_rules:{session.session_id}")
    if removed_agents:
        if removed_agents & set(target.application_agents):
            errors.append("removed_agent_still_member")
        if any(
            removed_agents & {edge.source, edge.target}
            for edge in edges.values()
        ):
            errors.append("removed_agent_edge_residual")
        if any(
            removed_agents & {session.source, session.target}
            for session in target.sessions
        ):
            errors.append("removed_agent_session_residual")
        if any(
            removed_agents & {rule.src_agent, rule.dst_agent}
            for rule in target.rules.values()
        ):
            errors.append("removed_agent_rule_residual")
    errors.extend(check_four_layer_feasibility(target).violations)
    return errors


def _session_gateways(session) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)


def _verify_details(result: VerifyResult | None) -> dict[str, Any]:
    if result is None:
        return {"mode": "logical", "ok": True, "checked_edges": 0}
    return {
        "mode": result.mode,
        "ok": result.ok,
        "checked_edges": result.checked_edges,
        "passed_edges": result.passed_edges,
        "verify_ms": result.verify_ms,
    }


def _empty_probe_summary() -> ContinuousProbeSummary:
    return ContinuousProbeSummary(
        samples=(),
        unaffected_edges_total=0,
        unaffected_edges_interrupted=0,
        unaffected_interruption_ms=0.0,
        unaffected_latency_change_ms=0.0,
        unaffected_packet_loss_change=0.0,
    )
