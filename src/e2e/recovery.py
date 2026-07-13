from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from src.controller.elastic import AdjustmentResult
from src.controller.networking import AgentController
from src.core.models import TaskState, TaskSubnet
from src.e2e.installers import (
    GatewayInstaller,
    NetnsGatewayTarget,
    rescue_netns_gateway_targets,
)
from src.e2e.models import E2ERecoveryMetrics, RecoveryDelta, VerifyResult
from src.e2e.recovery_planner import (
    AnomalyEvent,
    RecoveryPlan,
    RecoveryPlanner,
    build_anomaly_event,
    deterministic_recovery_plan,
    model_strategy_from_raw,
    validate_recovery_plan,
)
from src.e2e.verifiers import TaskSubnetVerifier
from src.metrics.netns import NetnsMetricProvider
from src.metrics.provider import MetricProvider
from src.metrics.synthetic import SyntheticMetricProvider
from testbed.netem import NetemController


SUPPORTED_FAULTS = ("agent_offline", "link_degrade", "gateway_rule_loss")


@dataclass(frozen=True)
class FaultContext:
    fault_type: str
    failed_agents: frozenset[str] = field(default_factory=frozenset)
    delta: RecoveryDelta = field(default_factory=RecoveryDelta)
    requires_business_probe: bool = False
    remediation: str = ""


class RecoveryFaultActuator(Protocol):
    async def inject(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        fault_type: str,
    ) -> FaultContext:
        ...

    async def detected(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
        probe: VerifyResult | None,
    ) -> bool:
        ...

    async def remediate(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
    ) -> None:
        ...

    async def cleanup(self) -> None:
        ...


@dataclass
class SyntheticFaultActuator:
    provider: SyntheticMetricProvider
    severity: float = 1.0
    failed_agent_id: str = "nagent-gw-mec"
    failed_gateway_id: str = "gw-mec"
    rule_loss_gateway_id: str = "gw-ue"

    async def inject(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        fault_type: str,
    ) -> FaultContext:
        _validate_fault(fault_type)
        if fault_type == "link_degrade":
            self.provider.inject_event(subnet.task.task_id, "bearer_degradation", self.severity)
            return FaultContext(
                fault_type=fault_type,
                requires_business_probe=True,
                remediation="simulated_path_restore",
            )
        if fault_type == "agent_offline":
            if not controller.gateways[self.failed_gateway_id].fail_agent(self.failed_agent_id):
                raise RuntimeError(f"failed to take agent offline: {self.failed_agent_id}")
            self.provider.inject_event(
                subnet.task.task_id,
                "bearer_degradation",
                self.severity,
            )
            return FaultContext(
                fault_type=fault_type,
                failed_agents=frozenset({self.failed_agent_id}),
                requires_business_probe=True,
                remediation="support_agent_replacement_and_path_restore",
            )

        session_id = subnet.sessions[0].session_id
        gateway = controller.gateways[self.rule_loss_gateway_id]
        for key, entry in list(gateway.route_table.items()):
            if entry.task_id == subnet.task.task_id and entry.session_id == session_id:
                gateway.route_table.pop(key)
        self.provider.inject_event(
            subnet.task.task_id,
            "bearer_degradation",
            self.severity,
        )
        subnet.state = TaskState.DEGRADED
        return FaultContext(
            fault_type=fault_type,
            delta=RecoveryDelta(
                changed_gateway_ids=(self.rule_loss_gateway_id,),
                changed_session_ids=(session_id,),
                reason="gateway route missing",
            ),
            requires_business_probe=True,
            remediation="gateway_route_reinstall",
        )

    async def detected(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
        probe: VerifyResult | None,
    ) -> bool:
        if context.fault_type == "agent_offline":
            return any(controller._agent_by_id(agent_id) is None or not controller._agent_by_id(agent_id).card.online for agent_id in context.failed_agents)
        if context.fault_type == "gateway_rule_loss":
            return bool(controller.missing_gateway_routes(subnet))
        return probe is not None and not probe.ok

    async def remediate(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
    ) -> None:
        if context.fault_type == "link_degrade":
            # In simulated mode this represents moving the task flow off the
            # degraded bearer after the controller has selected a recovery.
            self.provider.clear_event(subnet.task.task_id, "bearer_degradation")
        elif context.fault_type == "agent_offline":
            self.provider.clear_event(subnet.task.task_id, "bearer_degradation")
        elif context.fault_type == "gateway_rule_loss":
            self.provider.clear_event(subnet.task.task_id, "bearer_degradation")
            acknowledgements = await controller.reinstall_gateway_routes(
                subnet,
                set(context.delta.changed_gateway_ids),
            )
            if not all(ack.accepted for ack in acknowledgements):
                raise RuntimeError("in-process gateway route reinstall was rejected")

    async def cleanup(self) -> None:
        for events in self.provider._events.values():
            events.pop("bearer_degradation", None)


@dataclass
class NetnsFaultActuator:
    provider: NetnsMetricProvider
    severity: float = 1.0
    event_namespace: str = "h-router"
    event_dev: str = "rt-cloud0"
    delay_ms: float = 160.0
    loss_percent: float = 10.0
    sudo: bool = False
    command_timeout_s: float = 8.0
    failed_agent_id: str = "nagent-gw-mec"
    failed_gateway_id: str = "gw-mec"
    rule_loss_gateway_id: str = "gw-ue"
    gateway_namespaces: dict[str, str] = field(
        default_factory=lambda: {
            "gw-ue": "h-term",
            "gw-mec": "h-edge",
            "gw-cloud": "h-cloud",
        }
    )
    gateway_route_targets: dict[str, NetnsGatewayTarget] = field(
        default_factory=rescue_netns_gateway_targets
    )
    _netem_active: bool = field(default=False, init=False)
    _blackholes: list[tuple[str, str]] = field(default_factory=list, init=False)

    async def inject(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        fault_type: str,
    ) -> FaultContext:
        _validate_fault(fault_type)
        if fault_type == "link_degrade":
            netem = NetemController(
                namespace=self.event_namespace,
                dev=self.event_dev,
                sudo=self.sudo,
            )
            netem.replace(
                delay_ms=self.delay_ms,
                loss_percent=self.loss_percent,
            )
            self._netem_active = True
            self.provider._cache.clear()
            return FaultContext(
                fault_type=fault_type,
                requires_business_probe=True,
                remediation="clear_netem",
            )
        if fault_type == "agent_offline":
            if not controller.gateways[self.failed_gateway_id].fail_agent(self.failed_agent_id):
                raise RuntimeError(f"failed to take agent offline: {self.failed_agent_id}")
            affected = next(
                session for session in subnet.sessions if session.n_agent_id == self.failed_agent_id
            )
            self._install_blackhole(subnet, affected.session_id, affected.source_gateway)
            self.provider._cache.clear()
            return FaultContext(
                fault_type=fault_type,
                failed_agents=frozenset({self.failed_agent_id}),
                requires_business_probe=True,
                remediation="support_agent_replacement_and_route_reinstall",
            )

        session_id = subnet.sessions[0].session_id
        gateway = controller.gateways[self.rule_loss_gateway_id]
        for key, entry in list(gateway.route_table.items()):
            if entry.task_id == subnet.task.task_id and entry.session_id == session_id:
                gateway.route_table.pop(key)
        self._install_blackhole(subnet, session_id, self.rule_loss_gateway_id)
        self.provider._cache.clear()
        subnet.state = TaskState.DEGRADED
        return FaultContext(
            fault_type=fault_type,
            delta=RecoveryDelta(
                changed_gateway_ids=(self.rule_loss_gateway_id,),
                changed_session_ids=(session_id,),
                reason="netns blackhole replaced task route",
            ),
            requires_business_probe=True,
            remediation="gateway_route_reinstall",
        )

    def _install_blackhole(
        self,
        subnet: TaskSubnet,
        session_id: str,
        gateway_id: str,
    ) -> None:
        target_ip = next(
            entry.action.next_hop_gateway_ip
            for entry in subnet.gateway_routes[gateway_id]
            if entry.session_id == session_id and entry.action.next_hop_gateway_ip
        )
        namespace = self.gateway_namespaces.get(gateway_id)
        if namespace is None:
            raise RuntimeError(f"no namespace mapping for {gateway_id}")
        command = [
            "ip",
            "-n",
            namespace,
            "route",
            "replace",
            "blackhole",
            f"{target_ip}/32",
        ]
        if self.sudo:
            command.insert(0, "sudo")
        self._run_command(command)
        self._blackholes.append((gateway_id, target_ip))

    async def detected(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
        probe: VerifyResult | None,
    ) -> bool:
        if context.fault_type == "agent_offline":
            agent = controller._agent_by_id(self.failed_agent_id)
            return agent is None or not agent.card.online
        return probe is not None and not probe.ok

    async def remediate(
        self,
        controller: AgentController,
        subnet: TaskSubnet,
        context: FaultContext,
    ) -> None:
        if context.fault_type == "link_degrade":
            netem = NetemController(
                namespace=self.event_namespace,
                dev=self.event_dev,
                sudo=self.sudo,
            )
            netem.clear()
            self._netem_active = False
            self.provider._cache.clear()
        elif context.fault_type == "gateway_rule_loss":
            acknowledgements = await controller.reinstall_gateway_routes(
                subnet,
                set(context.delta.changed_gateway_ids),
            )
            if not all(ack.accepted for ack in acknowledgements):
                raise RuntimeError("in-process gateway route reinstall was rejected")

    async def cleanup(self) -> None:
        if self._netem_active:
            netem = NetemController(
                namespace=self.event_namespace,
                dev=self.event_dev,
                sudo=self.sudo,
            )
            try:
                netem.clear()
            except (OSError, subprocess.CalledProcessError):
                pass
            self._netem_active = False

        for gateway_id, target_ip in self._blackholes:
            target = self.gateway_route_targets.get(gateway_id)
            if target is None:
                continue
            command = [
                "ip",
                "-n",
                target.namespace,
                "route",
                "replace",
                f"{target_ip}/32",
                "via",
                target.router_ip,
                "dev",
                target.device,
                "proto",
                "static",
            ]
            if self.sudo:
                command.insert(0, "sudo")
            try:
                self._run_command(command)
            except (OSError, RuntimeError):
                pass
        self._blackholes.clear()

    def _run_command(self, command: list[str]) -> None:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.command_timeout_s,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "command failed: "
                + " ".join(command)
                + f"; exit={completed.returncode}; stderr={completed.stderr.strip()}"
            )


@dataclass(frozen=True)
class _RecoveryDecision:
    subnet: TaskSubnet
    delta: object
    applied_strategy: str
    changed_agents: int
    changed_edges: int
    changed_gateways: int
    estimated_interruption_ms: float
    full_install: bool = False


async def measure_e2e_recovery(
    *,
    scenario: str,
    run_id: int,
    controller: AgentController,
    subnet: TaskSubnet,
    provider: MetricProvider,
    installer: GatewayInstaller,
    verifier: TaskSubnetVerifier,
    actuator: RecoveryFaultActuator,
    fault_type: str,
    healthy_windows: int = 3,
    sample_interval_s: float = 0.2,
    detection_timeout_s: float = 10.0,
    recovery_timeout_s: float = 10.0,
    recovery_mode: str = "incremental",
    decision_mode: str = "rule",
    recovery_planner: RecoveryPlanner | None = None,
    detection_source: str = "auto",
    llm_confidence_threshold: float = 0.7,
    recovery_deadline_ms: float = 3000.0,
    full_rebuild_ms: float | None = None,
    full_rebuild_success: bool | None = None,
) -> E2ERecoveryMetrics:
    _validate_fault(fault_type)
    if recovery_mode not in {"incremental", "full_rebuild"}:
        raise ValueError(f"unsupported recovery mode: {recovery_mode}")
    if decision_mode not in {"rule", "llm"}:
        raise ValueError(f"unsupported decision mode: {decision_mode}")
    if healthy_windows <= 0:
        raise ValueError("healthy_windows must be positive")
    if not 0.0 <= llm_confidence_threshold <= 1.0:
        raise ValueError("llm_confidence_threshold must be between 0 and 1")
    if recovery_deadline_ms <= 0.0:
        raise ValueError("recovery_deadline_ms must be positive")

    context = await actuator.inject(controller, subnet, fault_type)
    fault_at = perf_counter()
    errors: list[str] = []

    detected = False
    detection_deadline = fault_at + detection_timeout_s
    probe: VerifyResult | None = None
    while perf_counter() < detection_deadline:
        state_detected = await actuator.detected(controller, subnet, context, probe)
        if state_detected and (
            not context.requires_business_probe or (probe is not None and not probe.ok)
        ):
            detected = True
            break
        probe = await verifier.verify(subnet)
        state_detected = await actuator.detected(controller, subnet, context, probe)
        if state_detected and (
            not context.requires_business_probe or not probe.ok
        ):
            detected = True
            break
        await asyncio.sleep(sample_interval_s)
    detected_at = perf_counter()
    fault_detect_ms = (detected_at - fault_at) * 1000.0
    if not detected:
        errors.append("fault detection timed out")
        return _failed_recovery_metrics(
            scenario=scenario,
            fault_type=fault_type,
            run_id=run_id,
            fault_detect_ms=fault_detect_ms,
            healthy_windows=healthy_windows,
            installer=installer,
            verifier=verifier,
            errors=errors,
            full_rebuild_ms=full_rebuild_ms,
            full_rebuild_success=full_rebuild_success,
            decision_mode=decision_mode,
        )

    # Detection is deliberately outside elastic_recovery_ms. Normalizing the
    # detector's evidence into AnomalyEvent is the first timed recovery action.
    decision_started = detected_at
    validation_started = perf_counter()
    event = build_anomaly_event(
        controller,
        subnet,
        probe,
        detected_at=detected_at,
        run_id=run_id,
        detection_source=detection_source,
    )
    controller_validation_ms = (perf_counter() - validation_started) * 1000.0
    model_analysis_ms = 0.0
    model_timeout = False
    model_strategy = ""
    llm_plan_valid = False
    llm_plan_adopted = False
    fallback_reason = ""

    if recovery_mode == "full_rebuild":
        plan = RecoveryPlan(
            diagnosed_fault_type=deterministic_recovery_plan(event).diagnosed_fault_type,
            conflict_types=event.observed_conflicts,
            affected_session_ids=event.affected_session_ids,
            strategy="full_rebuild",
            target_agent_id=None,
            affected_gateway_ids=tuple(sorted(event.gateway_state)),
            confidence=1.0,
            reason="independent full-rebuild baseline",
        )
        decision_source = "full_rebuild_baseline"
        effective_decision_mode = "rule"
    elif decision_mode == "llm":
        effective_decision_mode = "llm"
        if recovery_planner is None:
            fallback_reason = "planner_not_configured"
            plan = deterministic_recovery_plan(event)
            decision_source = "rule_fallback"
        else:
            proposal = await recovery_planner.propose(event)
            model_analysis_ms = proposal.analysis_ms
            model_timeout = proposal.timed_out
            model_strategy = model_strategy_from_raw(proposal.raw_response)
            validation_started = perf_counter()
            try:
                if proposal.error:
                    raise ValueError(proposal.error)
                plan = validate_recovery_plan(
                    proposal.raw_response,
                    event,
                    confidence_threshold=llm_confidence_threshold,
                )
                llm_plan_valid = True
                llm_plan_adopted = True
                decision_source = "llm"
            except (TypeError, ValueError) as error:
                fallback_reason = (
                    "model_timeout" if proposal.timed_out else f"invalid_model_plan: {error}"
                )
                plan = deterministic_recovery_plan(event)
                decision_source = "rule_fallback"
            controller_validation_ms += (perf_counter() - validation_started) * 1000.0
    else:
        effective_decision_mode = "rule"
        validation_started = perf_counter()
        plan = deterministic_recovery_plan(event)
        controller_validation_ms += (perf_counter() - validation_started) * 1000.0
        decision_source = "rule"

    apply_started = perf_counter()
    decision: _RecoveryDecision | None = None
    try:
        decision = await _execute_recovery_plan(
            plan=plan,
            event=event,
            context=context,
            controller=controller,
            subnet=subnet,
            timestamp=perf_counter() - fault_at,
        )
        subnet = decision.subnet
    except Exception as error:
        errors.append(f"controller failed to apply recovery plan: {error}")
        llm_plan_adopted = False
    controller_apply_ms = (perf_counter() - apply_started) * 1000.0
    decision_finished = perf_counter()
    recovery_decision_ms = (decision_finished - decision_started) * 1000.0

    install_started = perf_counter()
    install_result = None
    if decision is not None and not errors:
        try:
            await actuator.remediate(controller, subnet, context)
            if decision.full_install:
                install_result = await installer.install_subnet(subnet)
            else:
                install_result = await installer.apply_delta(subnet, decision.delta)
            if not install_result.ok:
                errors.extend(install_result.errors)
        except Exception as error:
            errors.append(str(error))
    install_finished = perf_counter()
    delta_install_ms = (install_finished - install_started) * 1000.0

    consecutive = 0
    attempts = 0
    restored = False
    restore_deadline = install_finished + recovery_timeout_s
    drive_agent_loop = isinstance(provider, SyntheticMetricProvider)
    while not errors and perf_counter() < restore_deadline:
        attempts += 1
        if drive_agent_loop:
            await controller.run_agent_loop(subnet, timestamp=float(attempts))
        verification = await verifier.verify(subnet)
        if verification.ok:
            consecutive += 1
            if consecutive >= healthy_windows:
                restored = True
                break
        else:
            consecutive = 0
        await asyncio.sleep(sample_interval_s)
    restored_at = perf_counter()
    if not restored and not errors:
        errors.append("business restoration timed out")

    elastic_recovery_ms = (restored_at - detected_at) * 1000.0
    applied_strategy = decision.applied_strategy if decision is not None else "not_applied"
    success = restored and not errors

    return E2ERecoveryMetrics(
        scenario=scenario,
        fault_type=fault_type,
        run_id=run_id,
        fault_detect_ms=fault_detect_ms,
        recovery_decision_ms=recovery_decision_ms,
        delta_install_ms=delta_install_ms,
        business_restore_ms=(restored_at - install_finished) * 1000.0,
        e2e_recovery_ms=(restored_at - fault_at) * 1000.0,
        estimated_interruption_ms=(decision.estimated_interruption_ms if decision else 0.0),
        changed_agent_count=(decision.changed_agents if decision else 0),
        changed_edge_count=(decision.changed_edges if decision else 0),
        changed_gateway_count=(decision.changed_gateways if decision else 0),
        full_rebuild_ms=full_rebuild_ms,
        incremental_success=success,
        full_rebuild_success=full_rebuild_success,
        healthy_windows=healthy_windows,
        verify_attempts=attempts,
        strategy=applied_strategy,
        remediation=context.remediation,
        install_mode=getattr(install_result, "mode", type(installer).__name__),
        verify_mode=str(getattr(verifier, "mode", type(verifier).__name__)),
        elastic_recovery_ms=elastic_recovery_ms,
        model_analysis_ms=model_analysis_ms,
        controller_validation_ms=controller_validation_ms,
        controller_apply_ms=controller_apply_ms,
        detection_source=event.detection_source,
        decision_mode=effective_decision_mode,
        decision_source=decision_source,
        diagnosed_fault_type=plan.diagnosed_fault_type,
        model_strategy=model_strategy,
        applied_strategy=applied_strategy,
        model_timeout=model_timeout,
        llm_plan_valid=llm_plan_valid,
        llm_plan_adopted=llm_plan_adopted,
        fallback_reason=fallback_reason,
        deadline_violated=(not success or elastic_recovery_ms > recovery_deadline_ms),
        errors=tuple(errors),
    )


async def _execute_recovery_plan(
    *,
    plan: RecoveryPlan,
    event: AnomalyEvent,
    context: FaultContext,
    controller: AgentController,
    subnet: TaskSubnet,
    timestamp: float,
) -> _RecoveryDecision:
    if plan.strategy == "full_rebuild":
        rebuilt, _risk_after, adjustment = await controller.rebuild_task_subnet(
            subnet,
            timestamp=timestamp,
        )
        return _decision_from_adjustment(rebuilt, adjustment, full_install=True)

    if plan.strategy == "gateway_rule_reinstall":
        missing = controller.missing_gateway_routes(subnet)
        gateway_ids = tuple(sorted(missing))
        session_ids = tuple(
            sorted(
                {
                    entry.session_id
                    for entries in missing.values()
                    for entry in entries
                }
            )
        )
        if not gateway_ids:
            raise RuntimeError("no missing gateway routes remain to reinstall")
        delta = RecoveryDelta(
            changed_gateway_ids=gateway_ids,
            changed_session_ids=session_ids,
            reason=plan.reason,
        )
        return _RecoveryDecision(
            subnet=subnet,
            delta=delta,
            applied_strategy="gateway_rule_reinstall",
            changed_agents=0,
            changed_edges=len(session_ids),
            changed_gateways=len(gateway_ids),
            estimated_interruption_ms=controller.cost_model.interruption_ms(
                {"gateway_install": len(gateway_ids)}
            ),
        )

    if plan.strategy in {"support_agent_replace", "communication_reroute"}:
        failed_agents = {
            agent_id
            for agent_id, report in event.agent_state_reports.items()
            if report.get("selected") and not report.get("online")
        }
        if not failed_agents:
            raise RuntimeError("recovery plan selected Agent replacement without an offline Agent")
        adjustment = await controller.apply_failed_support_recovery(
            subnet,
            failed_agents,
            timestamp,
        )
        return _decision_from_adjustment(subnet, adjustment)

    if plan.strategy == "link_repair":
        return _RecoveryDecision(
            subnet=subnet,
            delta=context.delta,
            applied_strategy="link_repair",
            changed_agents=0,
            changed_edges=len(plan.affected_session_ids),
            changed_gateways=0,
            estimated_interruption_ms=controller.cost_model.interruption_ms(
                {"local_tune": 1}
            ),
        )

    if plan.strategy == "local_tuning":
        _risk_before, _risk_after, adjustment = await controller.evaluate_and_adjust(
            subnet,
            timestamp=timestamp,
        )
        return _decision_from_adjustment(subnet, adjustment)

    raise RuntimeError(f"unsupported executable recovery strategy: {plan.strategy}")


def _decision_from_adjustment(
    subnet: TaskSubnet,
    adjustment: AdjustmentResult,
    *,
    full_install: bool = False,
) -> _RecoveryDecision:
    return _RecoveryDecision(
        subnet=subnet,
        delta=adjustment,
        applied_strategy=adjustment.strategy,
        changed_agents=adjustment.changed_agents,
        changed_edges=adjustment.changed_edges,
        changed_gateways=adjustment.changed_gateways,
        estimated_interruption_ms=adjustment.estimated_interruption_ms,
        full_install=full_install,
    )


def _failed_recovery_metrics(
    *,
    scenario: str,
    fault_type: str,
    run_id: int,
    fault_detect_ms: float,
    healthy_windows: int,
    installer: GatewayInstaller,
    verifier: TaskSubnetVerifier,
    errors: list[str],
    full_rebuild_ms: float | None,
    full_rebuild_success: bool | None,
    decision_mode: str,
) -> E2ERecoveryMetrics:
    return E2ERecoveryMetrics(
        scenario=scenario,
        fault_type=fault_type,
        run_id=run_id,
        fault_detect_ms=fault_detect_ms,
        recovery_decision_ms=0.0,
        delta_install_ms=0.0,
        business_restore_ms=0.0,
        e2e_recovery_ms=fault_detect_ms,
        estimated_interruption_ms=0.0,
        changed_agent_count=0,
        changed_edge_count=0,
        changed_gateway_count=0,
        full_rebuild_ms=full_rebuild_ms,
        incremental_success=False,
        full_rebuild_success=full_rebuild_success,
        healthy_windows=healthy_windows,
        verify_attempts=0,
        strategy="not_started",
        remediation="not_started",
        install_mode=type(installer).__name__,
        verify_mode=str(getattr(verifier, "mode", type(verifier).__name__)),
        elastic_recovery_ms=0.0,
        detection_source="detection_timeout",
        decision_mode=decision_mode,
        decision_source="not_started",
        applied_strategy="not_started",
        deadline_violated=True,
        errors=tuple(errors),
    )


def _validate_fault(fault_type: str) -> None:
    if fault_type not in SUPPORTED_FAULTS:
        raise ValueError(f"unsupported fault type: {fault_type}")
