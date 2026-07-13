from __future__ import annotations

from time import perf_counter
from typing import Any

from src.controller.networking import AgentController
from src.core.models import TaskSpec, TaskState, TaskSubnet
from src.e2e.installers import GatewayInstaller, SimulatedGatewayInstaller
from src.e2e.intent import RuleIntentParser, ScenarioTaskSpecMapper, parse_with
from src.e2e.models import E2EBuildMetrics, GatewayInstallResult, VerifyResult
from src.e2e.verifiers import SyntheticTaskSubnetVerifier, TaskSubnetVerifier
from src.metrics.provider import MetricProvider
from src.metrics.synthetic import SyntheticMetricProvider


async def build_task_subnet_e2e(
    user_intent: str | TaskSpec,
    controller: AgentController,
    metric_provider: MetricProvider,
    semantic_controller: Any | None = None,
    verifier: TaskSubnetVerifier | None = None,
    installer: GatewayInstaller | None = None,
    task_mapper: Any | None = None,
) -> tuple[TaskSubnet, E2EBuildMetrics, GatewayInstallResult, VerifyResult]:
    """Build a task subnet and time every stage through business verification."""

    e2e_started = perf_counter()
    errors: list[str] = []

    semantic_started = perf_counter()
    if isinstance(user_intent, TaskSpec):
        semantic_result: Any = user_intent
    else:
        parser = semantic_controller or RuleIntentParser()
        semantic_result = parse_with(parser, user_intent)
    semantic_ms = (perf_counter() - semantic_started) * 1000.0

    mapping_started = perf_counter()
    mapper = task_mapper or ScenarioTaskSpecMapper()
    task = mapper.map_task(semantic_result)
    task_mapping_ms = (perf_counter() - mapping_started) * 1000.0

    subnet, controller_metrics = await controller.build_task_subnet(task)
    controller_success = (
        controller_metrics.networking_success
        and subnet.state == TaskState.NETWORKED
        and all(ack.accepted for ack in subnet.gateway_acks)
    )

    resolved_installer = installer or SimulatedGatewayInstaller()
    if controller_success:
        install_result = await resolved_installer.install_subnet(subnet)
    else:
        install_result = GatewayInstallResult(
            ok=False,
            install_ms=0.0,
            updated_gateway_count=0,
            updated_rule_count=0,
            errors=("controller build did not reach NETWORKED",),
            mode="skipped",
        )
    errors.extend(install_result.errors)

    resolved_verifier = verifier
    if resolved_verifier is None:
        if not isinstance(metric_provider, SyntheticMetricProvider):
            raise TypeError("a verifier is required for non-synthetic metric providers")
        resolved_verifier = SyntheticTaskSubnetVerifier(metric_provider)
    if controller_success and install_result.ok:
        verify_result = await resolved_verifier.verify(subnet)
    else:
        verify_result = VerifyResult(
            ok=False,
            verify_ms=0.0,
            checked_edges=0,
            passed_edges=0,
            mode="skipped",
        )
    for edge in verify_result.edge_results:
        if edge.error:
            errors.append(f"{edge.session_id}: {edge.error}")

    e2e_build_ms = (perf_counter() - e2e_started) * 1000.0
    success = controller_success and install_result.ok and verify_result.ok
    metrics = E2EBuildMetrics(
        semantic_ms=semantic_ms,
        task_mapping_ms=task_mapping_ms,
        controller_build_ms=controller_metrics.controller_build_ms,
        gateway_install_ms=install_result.install_ms,
        business_verify_ms=verify_result.verify_ms,
        e2e_build_ms=e2e_build_ms,
        success=success,
        session_count=len(subnet.sessions),
        involved_gateway_count=len(subnet.involved_gateways),
        checked_edges=verify_result.checked_edges,
        passed_edges=verify_result.passed_edges,
        install_mode=install_result.mode,
        verify_mode=verify_result.mode,
        controller_success=controller_success,
        install_success=install_result.ok,
        verify_success=verify_result.ok,
        updated_rule_count=install_result.updated_rule_count,
        errors=tuple(errors),
    )
    return subnet, metrics, install_result, verify_result
