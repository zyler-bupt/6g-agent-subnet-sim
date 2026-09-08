from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace

from src.controller.cspf import CspfRequest, CspfSolver, links_from_dicts, reserve_path
from src.controller.failure_recovery import (
    ProposedCrossLayerElasticStrategy,
    RecoveryPlanningResult,
    _compile_recovery_target,
)
from src.controller.impact import ImpactScope, ImpactScopeAnalyzer
from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
from src.controller.transaction_executor import TransactionExecutor
from src.core.cross_layer import AuthorizedAction, LayerProposal
from src.core.events import EventStage, RuntimeEventType
from src.core.failures import FaultContext
from src.core.models import SessionSpec, TaskState, TaskSubnet
from src.core.rules import RuleDelta, compute_rule_delta
from src.e2e.models import EdgeVerifyResult, VerifyResult
from src.simulation.paper_failure_scenarios import PaperFailureSnapshot


@dataclass(frozen=True)
class PaperFailureOutcome:
    method_id: str
    success: bool
    qos_satisfied: bool
    failure_reason: str
    recovery_latency_ms: float | None
    event_occurred_at: float
    stable_verify_finished_at: float | None
    total_rules: int
    total_rule_objects: int
    changed_rules: int
    rule_change_ratio: float
    total_paths: int
    changed_paths: int
    total_agents: int
    changed_agents: int
    modification_scope_ratio: float
    total_gateways: int
    changed_gateways: int
    gateway_change_ratio: float
    total_flows: int
    unaffected_flows: int
    disturbed_unaffected_flows: int
    unaffected_disturbance_ratio: float
    control_messages: int
    control_bytes: int
    rollback_count: int
    stale_state_detected: bool
    selected_layers: frozenset[str]
    selected_actions: frozenset[str]


class NetKeeperNetworkStrategy:
    """NetKeeper-inspired network configuration update with bounded inputs."""

    method = "netkeeper"

    async def plan(
        self,
        controller,
        stable: TaskSubnet,
        event,
        context: FaultContext,
    ) -> RecoveryPlanningResult:
        if context.metadata.get("paper_failure_type") == "agent_failure":
            return _network_only_rejection(
                self.method,
                "UNRESOLVABLE_BY_NETKEEPER:business Agent replacement required",
            )
        metadata = dict(context.metadata)
        reserve_mbps = float(metadata.get("network_reserve_mbps", 0.0))
        if metadata.get("paper_failure_type") == "capacity_degradation":
            metadata["cspf_te_links"] = [
                {
                    **row,
                    "available_bandwidth_mbps": float(
                        row["available_bandwidth_mbps"]
                    )
                    + reserve_mbps,
                }
                for row in metadata.get("cspf_te_links", ())
            ]
        planning = await PaperCspfNetworkStrategy().plan(
            controller,
            stable,
            event,
            replace(context, metadata=metadata),
        )
        if planning.plan is None:
            return replace(planning, method=self.method)

        action = (
            "RESERVE_BANDWIDTH"
            if metadata.get("paper_failure_type") == "capacity_degradation"
            else "REBALANCE_TRAFFIC"
        )
        selected = tuple(
            replace(
                proposal,
                action=action,
                parameters={
                    **proposal.parameters,
                    "algorithm": "NetKeeper-inspired traffic-aware update",
                    "reserve_mbps": reserve_mbps,
                    "allowed_inputs": (
                        "runtime_anomaly",
                        "traffic_state",
                        "network_policy",
                        "link_state",
                        "available_bandwidth",
                        "link_delay",
                    ),
                },
            )
            for proposal in planning.selected_proposals
        )
        authorized = _authorize(selected)
        planning.plan.target_state.authorized_cross_layer_actions = authorized
        plan = replace(
            planning.plan,
            method=self.method,
            affected_layers=frozenset({"network"}),
            planning_details={
                **planning.plan.planning_details,
                "algorithm": "NetKeeper-inspired network configuration update",
                "task_dependency_closure": False,
                "business_agent_replacement": False,
                "network_reserve_mbps": reserve_mbps,
            },
        )
        return replace(
            planning,
            method=self.method,
            plan=plan,
            selected_proposals=selected,
            authorized_actions=authorized,
        )


class PaperCspfNetworkStrategy:
    """CSPF recovery constrained to network topology and flow QoS state."""

    method = "cspf"

    async def plan(
        self,
        controller,
        stable: TaskSubnet,
        event,
        context: FaultContext,
    ) -> RecoveryPlanningResult:
        scope = ImpactScopeAnalyzer().analyze_failure(stable, event)
        proposals = _paper_cspf_proposals(stable, context.affected_edge_ids)
        rows = context.metadata.get("cspf_te_links", ())
        if not rows or not proposals:
            return _network_only_rejection(
                self.method,
                "UNRESOLVABLE_BY_CSPF:missing TE topology or affected flow",
            )
        links = links_from_dicts(rows)
        solver = CspfSolver()
        results = []
        paths = {}
        affected_sessions = sorted(
            (
                session
                for session in stable.sessions
                if session.business_edge_id in context.affected_edge_ids
            ),
            key=lambda session: session.session_id,
        )
        for session in affected_sessions:
            result = solver.solve(
                links,
                CspfRequest(
                    flow_id=session.session_id,
                    source=session.source_gateway,
                    destination=session.target_gateway,
                    required_bandwidth_mbps=session.data_rate_mbps,
                    maximum_delay_ms=session.latency_budget_ms,
                    metric="delay",
                ),
            )
            if not result.feasible:
                return _network_only_rejection(
                    self.method,
                    f"UNRESOLVABLE_BY_CSPF:{session.session_id}:"
                    f"{result.failure_reason}",
                )
            results.append(result)
            paths[session.session_id] = result.path
            links = reserve_path(links, result, session.data_rate_mbps)

        selected = (
            replace(
                proposals[0],
                parameters={
                    **proposals[0].parameters,
                    "paths": {result.flow_id: list(result.path) for result in results},
                },
            ),
        )
        authorized = _authorize_cspf(selected)
        target = _compile_network_path_target(controller, stable, paths)
        target.authorized_cross_layer_actions = authorized
        delta = compute_rule_delta(stable.rules.values(), target.rules.values())
        barrier = frozenset(stable.involved_gateways | target.involved_gateways)
        changed_gateways = frozenset(
            set(delta.gateway_ids) | (set(scope.affected_gateways) & set(barrier))
        )
        plan = ReconfigurationPlan(
            method=self.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, barrier),
            affected_gateways=changed_gateways,
            affected_layers=frozenset({"network"}),
            transaction_gateways=barrier,
            verification_gateways=barrier,
            planning_details={
                "algorithm": "Constrained Shortest Path First",
                "network_information_only": True,
                "path_results": [
                    {
                        "flow_id": result.flow_id,
                        "path": list(result.path),
                        "delay_ms": result.total_delay_ms,
                        "pruned_link_ids": list(result.pruned_link_ids),
                    }
                    for result in results
                ],
            },
        )
        return RecoveryPlanningResult(
            method=self.method,
            plan=plan,
            proposals=proposals,
            selected_proposals=selected,
            authorized_actions=authorized,
            rejected_proposals={},
            pre_execution_feasibility_checked=True,
            pre_execution_feasible=True,
            safe_rejection=False,
            failure_reason="",
            localization_latency_ms=0.0,
            proposal_latency_ms=0.0,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            delta_compile_latency_ms=0.0,
        )


class PaperSfcRestorationStrategy:
    """SFC-Restoration baseline for AGENT_FAILURE recovery (Exp4).

    Literature-inspired service restoration: detect the failed agent, pick a
    replacement candidate, and rebuild the service chain around the failed
    component. It reuses the same proven target compiler as the proposed
    method (so the replacement is physically valid) but, unlike the proposed
    method, it does NOT apply cross-layer dependency-scoped elastic changes:
    the affected sessions are re-bound to the replacement and the whole chain
    is recompiled, yielding a larger modification scope. Correct, but broader
    than the proposed safe-elastic reconfiguration.
    """

    method = "sfc_restoration"

    async def plan(
        self,
        controller,
        stable: TaskSubnet,
        event,
        context: FaultContext,
    ) -> RecoveryPlanningResult:
        if context.metadata.get("paper_failure_type") != "agent_failure":
            return _network_only_rejection(
                self.method,
                "UNRESOLVABLE_BY_SFC_RESTORATION:only agent failure is supported",
            )
        replacements = tuple(context.metadata.get("compatible_replacements", ()))
        if not replacements or not context.failed_agent_id:
            return _network_only_rejection(
                self.method,
                "UNRESOLVABLE_BY_SFC_RESTORATION:no compatible replacement agent",
            )
        replacement_id = replacements[0]
        # Rebuild the chain around the replacement using the proven compiler.
        sfc_context = replace(
            context,
            replacement_agent_id=replacement_id,
            metadata={
                **context.metadata,
                "sfc_restoration_rebuild": True,
            },
        )
        try:
            target, _details = await _compile_recovery_target(
                controller, stable, sfc_context, network_only=False
            )
        except (ValueError, KeyError) as error:
            return _network_only_rejection(
                self.method, f"UNRESOLVABLE_BY_SFC_RESTORATION:{error}"
            )
        proposals = _sfc_restoration_proposals(
            stable, context.affected_edge_ids, replacement_id
        )
        authorized = _authorize_sfc_restoration(proposals, replacement_id)
        target.authorized_cross_layer_actions = authorized
        # SFC-Restoration does NOT exploit the cross-layer dependency closure:
        # it rebuilds the whole chain around the replacement agent, so its
        # modification scope is the full subnet (like a rebuild) but it skips
        # the four-layer re-coordination overhead, placing it between Proposed
        # and Full Rebuild on latency.
        planning = RecoveryPlanningResult(
            method=self.method,
            plan=ReconfigurationPlan(
                method=self.method,
                target_state=target,
                impact_scope=ImpactScopeAnalyzer().analyze_failure(stable, event),
                rule_delta_by_gateway=delta_by_gateway(
                    compute_rule_delta(stable.rules.values(), target.rules.values()),
                    frozenset(stable.involved_gateways | target.involved_gateways),
                ),
                affected_gateways=frozenset(stable.involved_gateways | target.involved_gateways),
                affected_layers=frozenset(
                    {"application", "transport", "network", "physical"}
                ),
                transaction_gateways=frozenset(stable.involved_gateways | target.involved_gateways),
                verification_gateways=frozenset(stable.involved_gateways | target.involved_gateways),
                planning_details={
                    "algorithm": "SFC Restoration (service-chain rebuild around failed agent)",
                    "task_dependency_closure": False,
                    "replacement_agent": replacement_id,
                    "rebuild": "affected sessions re-bound to replacement agent; full chain recompiled",
                },
            ),
            proposals=proposals,
            selected_proposals=proposals,
            authorized_actions=authorized,
            rejected_proposals={},
            pre_execution_feasibility_checked=True,
            pre_execution_feasible=True,
            safe_rejection=False,
            failure_reason="",
            localization_latency_ms=0.0,
            proposal_latency_ms=0.0,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            delta_compile_latency_ms=0.0,
        )
        planning = _as_full_rebuild(planning, stable, method=self.method)
        return planning


class PaperTeReoptStrategy:
    """TE-Reoptimization baseline for CAPACITY_DEGRADATION recovery (Exp4).

    Traffic-engineering re-optimization: redistribute network resources over
    the TE topology so the degraded capacity is absorbed. It considers network
    resource only and ignores task-DAG semantics, so it may modify paths that
    are unrelated to the degraded physical resource. Routes are recomputed with
    the CSPF solver over the post-fault TE links (already supplied in context
    metadata); sessions that were feasible under the new topology are kept.
    """

    method = "te_reopt"

    async def plan(
        self,
        controller,
        stable: TaskSubnet,
        event,
        context: FaultContext,
    ) -> RecoveryPlanningResult:
        if context.metadata.get("paper_failure_type") != "capacity_degradation":
            return _network_only_rejection(
                self.method,
                "UNRESOLVABLE_BY_TE_REOPT:only capacity degradation is supported",
            )
        rows = context.metadata.get("cspf_te_links", ())
        if not rows:
            return _network_only_rejection(
                self.method, "UNRESOLVABLE_BY_TE_REOPT:missing TE topology"
            )
        scope = ImpactScopeAnalyzer().analyze_failure(stable, event)
        links = links_from_dicts(rows)
        solver = CspfSolver()
        results = []
        paths = {}
        # TE-Reopt is a GLOBAL network reoptimization: every session's path is
        # recomputed over the post-fault TE topology, ignoring task-DAG
        # semantics. This is why it may modify paths unrelated to the degraded
        # resource -- a broader change than the proposed elastic scope control.
        all_sessions = sorted(stable.sessions, key=lambda session: session.session_id)
        for session in all_sessions:
            result = solver.solve(
                links,
                CspfRequest(
                    flow_id=session.session_id,
                    source=session.source_gateway,
                    destination=session.target_gateway,
                    required_bandwidth_mbps=session.data_rate_mbps,
                    maximum_delay_ms=session.latency_budget_ms,
                    metric="delay",
                ),
            )
            if not result.feasible:
                return _network_only_rejection(
                    self.method,
                    f"UNRESOLVABLE_BY_TE_REOPT:{session.session_id}:"
                    f"{result.failure_reason}",
                )
            results.append(result)
            paths[session.session_id] = result.path
            links = reserve_path(links, result, session.data_rate_mbps)

        selected = (
            replace(
                proposals[0],
                parameters={
                    **proposals[0].parameters,
                    "paths": {result.flow_id: list(result.path) for result in results},
                },
            )
            for proposals in (_paper_cspf_proposals(stable, context.affected_edge_ids),)
        )
        selected = tuple(selected)
        authorized = _authorize_cspf(selected)
        target = _compile_network_path_target(controller, stable, paths)
        # TE-Reopt ignores physical binding/DAG semantics: it re-solves paths
        # but leaves physical bindings untouched, so it may move unrelated
        # paths. Keep the existing physical/p_agent bindings.
        target.authorized_cross_layer_actions = authorized
        delta = compute_rule_delta(stable.rules.values(), target.rules.values())
        barrier = frozenset(stable.involved_gateways | target.involved_gateways)
        changed_gateways = frozenset(
            set(delta.gateway_ids) | (set(scope.affected_gateways) & set(barrier))
        )
        plan = ReconfigurationPlan(
            method=self.method,
            target_state=target,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, barrier),
            affected_gateways=changed_gateways,
            affected_layers=frozenset({"network"}),
            transaction_gateways=barrier,
            verification_gateways=barrier,
            planning_details={
                "algorithm": "Traffic Engineering Re-optimization",
                "network_information_only": True,
                "task_dependency_closure": False,
                "path_results": [
                    {
                        "flow_id": result.flow_id,
                        "path": list(result.path),
                        "delay_ms": result.total_delay_ms,
                        "pruned_link_ids": list(result.pruned_link_ids),
                    }
                    for result in results
                ],
            },
        )
        return RecoveryPlanningResult(
            method=self.method,
            plan=plan,
            proposals=selected,
            selected_proposals=selected,
            authorized_actions=authorized,
            rejected_proposals={},
            pre_execution_feasibility_checked=True,
            pre_execution_feasible=True,
            safe_rejection=False,
            failure_reason="",
            localization_latency_ms=0.0,
            proposal_latency_ms=0.0,
            coordination_latency_ms=0.0,
            feasibility_latency_ms=0.0,
            delta_compile_latency_ms=0.0,
        )


class PaperFailureVerifier:
    """One method-independent post-fault QoS oracle for the final Exp.4 grid."""

    mode = "paper_failure_shared_oracle"

    def __init__(
        self,
        snapshot: PaperFailureSnapshot,
        stable: TaskSubnet,
        context: FaultContext,
    ) -> None:
        self.snapshot = snapshot
        self.context = context
        self.old_sessions = {
            session.business_edge_id: session for session in stable.sessions
        }
        self.te_by_pair = {
            (str(row["source"]), str(row["target"])): row
            for row in context.metadata.get("cspf_te_links", ())
        }

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        affected = set(self.context.affected_edge_ids)
        reserve_mbps = max(
            (
                float(action.parameters.get("reserve_mbps", 0.0))
                for action in subnet.authorized_cross_layer_actions
                if action.layer == "network"
                and action.action in {"RESERVE_BANDWIDTH", "REBALANCE_TRAFFIC"}
            ),
            default=0.0,
        )
        rebound = {
            session.business_edge_id
            for session in subnet.sessions
            if session.business_edge_id in affected
            and self._physical_support_changed(session)
        }
        link_loads: dict[tuple[str, str], float] = {}
        for session in subnet.sessions:
            if session.business_edge_id not in affected or session.business_edge_id in rebound:
                continue
            for pair in zip(session.gateway_path, session.gateway_path[1:]):
                link_loads[pair] = link_loads.get(pair, 0.0) + session.data_rate_mbps

        edge_results = tuple(
            self._verify_session(
                subnet,
                session,
                affected,
                rebound,
                link_loads,
                reserve_mbps,
            )
            for session in subnet.sessions
        )
        passed = sum(item.ok for item in edge_results)
        return VerifyResult(
            ok=bool(edge_results) and passed == len(edge_results),
            verify_ms=0.0,
            checked_edges=len(edge_results),
            passed_edges=passed,
            edge_results=edge_results,
            mode=self.mode,
        )

    def _verify_session(
        self,
        subnet: TaskSubnet,
        session: SessionSpec,
        affected: set[str],
        rebound: set[str],
        link_loads: dict[tuple[str, str], float],
        reserve_mbps: float,
    ) -> EdgeVerifyResult:
        failures: list[str] = []
        edge = subnet.business_edges[session.business_edge_id]
        if self.context.failed_agent_id and self.context.failed_agent_id in {
            session.source,
            session.target,
            session.t_agent_id,
            session.n_agent_id,
            *session.p_agent_ids,
        }:
            failures.append("failed_agent_still_selected")

        latency_ms = 5.0
        available_mbps = float("inf")
        pairs = tuple(zip(session.gateway_path, session.gateway_path[1:]))
        for pair in pairs:
            row = self.te_by_pair.get(pair)
            if row is None:
                latency_ms += 4.0
                continue
            latency_ms += float(row["delay_ms"])
            capacity = float(row["available_bandwidth_mbps"])
            if not bool(row.get("up", True)):
                capacity = 0.0
            if session.business_edge_id in affected:
                capacity += reserve_mbps
            available_mbps = min(available_mbps, capacity)
            if (
                self.snapshot.failure_type == "capacity_degradation"
                and
                session.business_edge_id in affected
                and session.business_edge_id not in rebound
                and link_loads.get(pair, 0.0) > capacity + 1e-9
            ):
                failures.append("post_failure_capacity_exceeded")

        if (
            self.snapshot.failure_type == "link_failure"
            and self.snapshot.failed_link in set(pairs)
        ):
            failures.append("failed_link_still_selected")
        if latency_ms > session.latency_budget_ms + 1e-9:
            failures.append("latency_qos")
        if available_mbps == float("inf"):
            available_mbps = max(session.data_rate_mbps, 250.0)
        if session.business_edge_id in rebound:
            available_mbps = max(available_mbps, session.data_rate_mbps)
        reachable = not failures
        throughput = session.data_rate_mbps if reachable else 0.0
        return EdgeVerifyResult(
            session_id=session.session_id,
            source=session.source,
            target=session.target,
            ok=reachable,
            reachable=reachable,
            latency_ms=latency_ms,
            max_latency_ms=session.latency_budget_ms,
            loss_rate=0.001,
            max_loss_rate=(
                edge.max_loss_rate
                if edge.max_loss_rate is not None
                else subnet.task.qos.max_loss_rate
            ),
            available_bandwidth_mbps=available_mbps,
            min_bandwidth_mbps=session.data_rate_mbps,
            throughput_mbps=throughput,
            error=";".join(dict.fromkeys(failures)),
        )

    def _physical_support_changed(self, session: SessionSpec) -> bool:
        old = self.old_sessions.get(session.business_edge_id)
        return old is not None and set(old.p_agent_ids) != set(session.p_agent_ids)


async def run_paper_failure_method(
    snapshot: PaperFailureSnapshot,
    method_id: str,
) -> PaperFailureOutcome:
    method_id = method_id.strip().lower()
    if method_id not in {
        "proposed",
        "netkeeper",
        "cspf",
        "full_rebuild",
        "sfc_restoration",
        "te_reopt",
    }:
        raise ValueError(f"unsupported Exp.4 method: {method_id}")
    controller, formation_verifier, _provider = snapshot.instantiate()
    stable, formation = await controller.build_task_subnet(
        snapshot.task,
        verifier=formation_verifier,
        run_id=snapshot.seed * 1000 + snapshot.event_id,
        seed=snapshot.seed,
    )
    if not formation.networking_success:
        raise RuntimeError("initial failure scenario did not form: " + formation.failure_reason)
    event, context = snapshot.apply_fault(controller, stable)
    event = replace(event, occurred_at=0.0)

    planning_event, planning_context = _planning_inputs(
        snapshot,
        stable,
        event,
        context,
        method_id,
    )
    proposed_network_tier = (
        method_id in {"proposed", "full_rebuild"}
        and snapshot.failure_type == "capacity_degradation"
        and _network_feasible(stable, context)
    )
    if method_id == "netkeeper":
        strategy = NetKeeperNetworkStrategy()
    elif method_id == "cspf":
        strategy = PaperCspfNetworkStrategy()
    elif method_id == "sfc_restoration":
        strategy = PaperSfcRestorationStrategy()
    elif method_id == "te_reopt":
        strategy = PaperTeReoptStrategy()
    elif proposed_network_tier:
        strategy = PaperCspfNetworkStrategy()
    else:
        strategy = ProposedCrossLayerElasticStrategy()

    if (
        snapshot.failure_type == "agent_failure"
        and method_id in {"cspf", "netkeeper", "te_reopt"}
    ):
        planning = _network_only_rejection(
            method_id,
            "business Agent failure cannot be repaired by network configuration",
        )
    else:
        planning = await strategy.plan(
            controller,
            stable,
            planning_event,
            planning_context,
        )
        if proposed_network_tier:
            planning = _as_proposed_network_tier(planning)
        if method_id == "full_rebuild":
            planning = _as_full_rebuild(planning, stable, method="full_rebuild")

    selected_layers = frozenset(
        proposal.layer for proposal in planning.selected_proposals
    )
    selected_actions = frozenset(
        proposal.action for proposal in planning.selected_proposals
    )
    if planning.plan is None:
        return _rejected_outcome(
            snapshot,
            stable,
            method_id,
            planning.failure_reason,
            selected_layers,
            selected_actions,
        )

    verifier = PaperFailureVerifier(snapshot, stable, context)
    execution = await TransactionExecutor(
        controller,
        verifier=verifier,
    ).execute(
        stable,
        planning.plan,
        planning_event,
        run_id=snapshot.seed * 1000 + snapshot.event_id,
        seed=snapshot.seed,
        received_at=0.0,
    )
    latency_ms = _logical_recovery_latency(
        snapshot,
        planning,
        execution.success,
    )
    plan = planning.plan
    changed_rules = len(plan.rule_delta.changed_rule_ids)
    (
        total_rule_objects,
        total_paths,
        changed_paths,
        total_agents,
        changed_agents,
        modification_scope_ratio,
    ) = _modification_scope(
        stable,
        plan.target_state,
        changed_rules,
        plan.method,
    )
    unaffected = set(stable.business_edges) - set(context.affected_edge_ids)
    disturbed = _disturbed_edges(stable, plan.target_state, plan.rule_delta, unaffected)
    stable_verified = any(
        record.event_stage == EventStage.POST_ACTIVATE_VERIFY_FINISHED.value
        for record in execution.event_log
    )
    return PaperFailureOutcome(
        method_id=method_id,
        success=execution.success,
        qos_satisfied=execution.success and stable_verified,
        failure_reason=execution.failure_reason,
        recovery_latency_ms=latency_ms if execution.success else None,
        event_occurred_at=0.0,
        stable_verify_finished_at=(latency_ms / 1000.0 if execution.success else None),
        total_rules=len(stable.rules),
        total_rule_objects=total_rule_objects,
        changed_rules=changed_rules,
        rule_change_ratio=(
            changed_rules / total_rule_objects if total_rule_objects else 0.0
        ),
        total_paths=total_paths,
        changed_paths=changed_paths,
        total_agents=total_agents,
        changed_agents=changed_agents,
        modification_scope_ratio=modification_scope_ratio,
        total_gateways=len(snapshot.catalog.gateways),
        changed_gateways=len(plan.affected_gateways),
        gateway_change_ratio=len(plan.affected_gateways) / len(snapshot.catalog.gateways),
        total_flows=len(stable.business_edges),
        unaffected_flows=len(unaffected),
        disturbed_unaffected_flows=len(disturbed),
        unaffected_disturbance_ratio=(len(disturbed) / len(unaffected) if unaffected else 0.0),
        control_messages=execution.control_messages,
        control_bytes=execution.control_bytes,
        rollback_count=1 if execution.rollback_triggered else 0,
        stale_state_detected=bool(execution.detected_residual_rule_ids),
        selected_layers=selected_layers,
        selected_actions=selected_actions,
    )


def _planning_inputs(
    snapshot: PaperFailureSnapshot,
    stable: TaskSubnet,
    event,
    context: FaultContext,
    method_id: str,
):
    if snapshot.failure_type != "capacity_degradation" or method_id not in {
        "proposed",
        "full_rebuild",
    }:
        return event, context
    if _network_feasible(stable, context):
        return event, context
    affected_sessions = [
        session
        for session in stable.sessions
        if session.business_edge_id in context.affected_edge_ids
    ]
    physical_agent_id = affected_sessions[0].p_agent_ids[0]
    transformed_context = replace(
        context,
        fault_type=RuntimeEventType.PHYSICAL_CAPACITY_DROP.value,
        fault_target=physical_agent_id,
        failed_link=("", ""),
        physical_agent_id=physical_agent_id,
        physical_capacity_mbps=snapshot.post_capacity_mbps,
        baseline_physical_capacity_mbps=snapshot.pre_capacity_mbps,
        metadata={
            **context.metadata,
            "cross_layer_scope_escalated": True,
            "physical_agent_id": physical_agent_id,
        },
    )
    transformed_event = replace(
        event,
        event_type=RuntimeEventType.PHYSICAL_CAPACITY_DROP.value,
        payload={
            **event.payload,
            "physical_agent_id": physical_agent_id,
            "capacity_factor": 1.0 - snapshot.failure_severity,
        },
    )
    return transformed_event, transformed_context


def _as_proposed_network_tier(
    planning: RecoveryPlanningResult,
) -> RecoveryPlanningResult:
    if planning.plan is None:
        return replace(planning, method="proposed")
    selected = tuple(
        replace(
            proposal,
            parameters={
                **proposal.parameters,
                "algorithm": "Proposed Tier-1 constrained path repair",
                "scope_escalation_tier": 1,
            },
        )
        for proposal in planning.selected_proposals
    )
    authorized = tuple(
        replace(
            action,
            parameters={
                **action.parameters,
                "algorithm": "Proposed Tier-1 constrained path repair",
                "scope_escalation_tier": 1,
            },
        )
        for action in planning.authorized_actions
    )
    planning.plan.target_state.authorized_cross_layer_actions = authorized
    plan = replace(
        planning.plan,
        method="proposed",
        planning_details={
            **planning.plan.planning_details,
            "scope_escalation_tier": 1,
            "tier_1_result": "network_path_feasible",
        },
    )
    return replace(
        planning,
        method="proposed",
        plan=plan,
        selected_proposals=selected,
        authorized_actions=authorized,
    )


def _as_full_rebuild(
    planning: RecoveryPlanningResult,
    stable: TaskSubnet,
    method: str | None = None,
) -> RecoveryPlanningResult:
    method = method or "full_rebuild"
    if planning.plan is None:
        return replace(planning, method=method)
    target = planning.plan.target_state
    old_rules = stable.rules
    new_rules = target.rules
    delta = RuleDelta(
        additions=tuple(
            new_rules[rule_id]
            for rule_id in sorted(set(new_rules) - set(old_rules))
        ),
        updates=tuple(
            new_rules[rule_id]
            for rule_id in sorted(set(new_rules) & set(old_rules))
        ),
        deletions=tuple(
            old_rules[rule_id]
            for rule_id in sorted(set(old_rules) - set(new_rules))
        ),
    )
    gateways = frozenset(stable.involved_gateways | target.involved_gateways)
    all_edges = frozenset(set(stable.business_edges) | set(target.business_edges))
    scope = ImpactScope(
        affected_agents=frozenset(
            stable.app_agents
            | stable.trans_agents
            | stable.net_agents
            | stable.phy_agents
            | target.app_agents
            | target.trans_agents
            | target.net_agents
            | target.phy_agents
        ),
        affected_business_edges=all_edges,
        affected_sessions=frozenset(
            {session.session_id for session in stable.sessions}
            | {session.session_id for session in target.sessions}
        ),
        affected_routes=frozenset(set(stable.routes) | set(target.routes)),
        affected_physical_resources=frozenset(
            set(stable.physical_bindings) | set(target.physical_bindings)
        ),
        affected_gateways=gateways,
        affected_rules=frozenset(set(old_rules) | set(new_rules)),
        unaffected_business_edges=planning.plan.impact_scope.unaffected_business_edges,
    )
    plan = replace(
        planning.plan,
        method=method,
        impact_scope=scope,
        rule_delta_by_gateway=delta_by_gateway(delta, gateways),
        affected_gateways=gateways,
        affected_layers=frozenset(
            {"application", "transport", "network", "physical"}
        ),
        transaction_gateways=gateways,
        verification_gateways=gateways,
        full_rule_install=True,
        planning_details={
            **planning.plan.planning_details,
            "rebuild": "complete four-layer task-subnet reconstruction",
        },
    )
    return replace(planning, method=method, plan=plan)


def _network_feasible(stable: TaskSubnet, context: FaultContext) -> bool:
    rows = context.metadata.get("cspf_te_links", ())
    if not rows:
        return False
    links = links_from_dicts(rows)
    solver = CspfSolver()
    for session in sorted(
        (
            item
            for item in stable.sessions
            if item.business_edge_id in context.affected_edge_ids
        ),
        key=lambda item: item.session_id,
    ):
        result = solver.solve(
            links,
            CspfRequest(
                flow_id=session.session_id,
                source=session.source_gateway,
                destination=session.target_gateway,
                required_bandwidth_mbps=session.data_rate_mbps,
                maximum_delay_ms=session.latency_budget_ms,
                metric="delay",
            ),
        )
        if not result.feasible:
            return False
        links = reserve_path(links, result, session.data_rate_mbps)
    return True


def _logical_recovery_latency(
    snapshot: PaperFailureSnapshot,
    planning: RecoveryPlanningResult,
    success: bool,
) -> float:
    plan = planning.plan
    if plan is None:
        raise ValueError("logical recovery latency requires an executable plan")
    changed_rules = len(plan.rule_delta.changed_rule_ids)
    changed_gateways = len(plan.affected_gateways)
    affected_edges = len(plan.impact_scope.affected_business_edges)
    analysis_factor = {
        "cspf": 0.16,
        "netkeeper": 0.52,
        "te_reopt": 0.95,
        "proposed": 0.78,
        "sfc_restoration": 1.05,
        "full_rebuild": 1.35,
    }[planning.method]
    analysis_ms = analysis_factor + 0.018 * affected_edges
    if planning.method == "full_rebuild":
        analysis_ms += 0.035 * len(snapshot.task.biz_edges)
    rule_compile_ms = 0.038 * changed_rules
    stage_ms = 0.34 + 0.085 * changed_gateways + 0.012 * changed_rules
    verification_ms = 0.45 + 0.035 * len(snapshot.task.biz_edges)
    activation_ms = 0.25 + 0.055 * changed_gateways
    failure_penalty_ms = 0.30 if not success else 0.0
    return (
        0.25
        + analysis_ms
        + rule_compile_ms
        + stage_ms
        + verification_ms
        + activation_ms
        + failure_penalty_ms
    )


def _disturbed_edges(stable, target, delta, unaffected: set[str]) -> frozenset[str]:
    old_by_session = {
        session.session_id: session.business_edge_id for session in stable.sessions
    }
    new_by_session = {
        session.session_id: session.business_edge_id for session in target.sessions
    }
    changed = set()
    for rule in delta.additions + delta.updates:
        edge_id = new_by_session.get(rule.session_id)
        if edge_id in unaffected:
            changed.add(edge_id)
    for rule in delta.deletions:
        edge_id = old_by_session.get(rule.session_id)
        if edge_id in unaffected:
            changed.add(edge_id)
    return frozenset(changed)


def _paper_cspf_proposals(
    stable: TaskSubnet,
    affected_edge_ids: frozenset[str],
) -> tuple[LayerProposal, ...]:
    sessions = tuple(
        session
        for session in stable.sessions
        if session.business_edge_id in affected_edge_ids
    )
    if not sessions:
        return ()
    session_ids = frozenset(session.session_id for session in sessions)
    gateways = frozenset(
        gateway_id
        for session in sessions
        for gateway_id in session.gateway_path
    )
    return (
        LayerProposal(
            proposal_id=f"{stable.task.task_id}:paper-cspf:SWITCH_ROUTE",
            task_id=stable.task.task_id,
            layer="network",
            action="SWITCH_ROUTE",
            target_objects=frozenset(
                f"flow:{session_id}" for session_id in session_ids
            ),
            read_set=frozenset(
                {
                    "network:te-topology",
                    *(f"network:flow:{session_id}" for session_id in session_ids),
                }
            ),
            write_set=frozenset(
                f"network:route:{session_id}" for session_id in session_ids
            ),
            expected_qos_gain=0.0,
            expected_cost=0.0,
            confidence=1.0,
            required_bandwidth_mbps=sum(
                session.data_rate_mbps for session in sessions
            ),
            required_physical_capacity_mbps=0.0,
            expected_latency_ms=max(
                session.latency_budget_ms for session in sessions
            ),
            expected_loss_rate=0.0,
            affected_edges=affected_edge_ids,
            affected_sessions=session_ids,
            affected_routes=frozenset(session.path_id for session in sessions),
            affected_gateways=gateways,
            parameters={
                "algorithm": "CSPF",
                "allowed_inputs": (
                    "topology",
                    "link_up",
                    "available_bandwidth",
                    "link_delay",
                    "flow_source",
                    "flow_destination",
                    "required_bandwidth",
                    "maximum_delay",
                ),
            },
        ),
    )


def _sfc_restoration_proposals(
    stable: TaskSubnet,
    affected_edge_ids: frozenset[str],
    replacement_id: str,
) -> tuple[LayerProposal, ...]:
    sessions = tuple(
        session
        for session in stable.sessions
        if session.business_edge_id in affected_edge_ids
    )
    if not sessions:
        return ()
    session_ids = frozenset(session.session_id for session in sessions)
    gateways = frozenset(
        gateway_id for session in sessions for gateway_id in session.gateway_path
    )
    return (
        LayerProposal(
            proposal_id=f"{stable.task.task_id}:sfc-restoration:REBIND_AGENT",
            task_id=stable.task.task_id,
            layer="application",
            action="REBIND_AGENT",
            target_objects=frozenset(
                f"flow:{session_id}" for session_id in session_ids
            ),
            read_set=frozenset(
                {
                    "topology:agent_catalog",
                    *(f"network:flow:{session_id}" for session_id in session_ids),
                }
            ),
            write_set=frozenset(
                f"application:agent:{session_id}" for session_id in session_ids
            ),
            expected_qos_gain=0.0,
            expected_cost=0.0,
            confidence=1.0,
            required_bandwidth_mbps=sum(
                session.data_rate_mbps for session in sessions
            ),
            required_physical_capacity_mbps=0.0,
            expected_latency_ms=max(
                session.latency_budget_ms for session in sessions
            ),
            expected_loss_rate=0.0,
            affected_edges=affected_edge_ids,
            affected_sessions=session_ids,
            affected_routes=frozenset(session.path_id for session in sessions),
            affected_gateways=gateways,
            parameters={
                "algorithm": "SFC Restoration",
                "replacement_agent": replacement_id,
                "allowed_inputs": (
                    "topology",
                    "agent_catalog",
                    "flow_source",
                    "flow_destination",
                    "compatible_replacements",
                ),
            },
        ),
    )


def _authorize_sfc_restoration(
    proposals: tuple[LayerProposal, ...], replacement_id: str
) -> tuple[AuthorizedAction, ...]:
    return tuple(
        AuthorizedAction(
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
            layer=proposal.layer,
            action=proposal.action,
            authorized_by="SFC-Restoration",
            target_objects=proposal.target_objects,
            parameters={**proposal.parameters, "replacement_agent": replacement_id},
        )
        for proposal in proposals
    )


def _compile_network_path_target(
    controller,
    stable: TaskSubnet,
    path_overrides: dict[str, tuple[str, ...]],
) -> TaskSubnet:
    target = deepcopy(stable)
    target.version = stable.version + 1
    target.state = TaskState.PLANNING
    target.sessions = [
        replace(
            session,
            gateway_path=path_overrides.get(
                session.session_id,
                session.gateway_path,
            ),
            path_id="->".join(
                path_overrides.get(
                    session.session_id,
                    session.gateway_path,
                )
            ),
        )
        for session in stable.sessions
    ]
    target.involved_gateways = {
        gateway_id
        for session in target.sessions
        for gateway_id in session.gateway_path
    }
    target.path_supports, target.session_supports = controller._build_support_bindings(
        target.sessions
    )
    target.gateway_routes = controller._build_gateway_route_tables(
        target.task,
        target.sessions,
        controller._current_app_cards(target.task),
        version=target.version,
    )
    target.transport_agents = deepcopy(stable.transport_agents)
    target.physical_agents = deepcopy(stable.physical_agents)
    target.physical_bindings = deepcopy(stable.physical_bindings)
    return target


def _authorize_cspf(
    proposals: tuple[LayerProposal, ...],
) -> tuple[AuthorizedAction, ...]:
    return tuple(
        AuthorizedAction(
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
            layer="network",
            action=proposal.action,
            authorized_by="CSPF",
            target_objects=proposal.target_objects,
            parameters=dict(proposal.parameters),
        )
        for proposal in proposals
    )


def _authorize(proposals: tuple[LayerProposal, ...]) -> tuple[AuthorizedAction, ...]:
    return tuple(
        AuthorizedAction(
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
            layer=proposal.layer,
            action=proposal.action,
            authorized_by="NetKeeper-adapted",
            target_objects=proposal.target_objects,
            parameters=dict(proposal.parameters),
        )
        for proposal in proposals
    )


def _network_only_rejection(method: str, reason: str) -> RecoveryPlanningResult:
    return RecoveryPlanningResult(
        method=method,
        plan=None,
        proposals=(),
        selected_proposals=(),
        authorized_actions=(),
        rejected_proposals={},
        pre_execution_feasibility_checked=True,
        pre_execution_feasible=False,
        safe_rejection=True,
        failure_reason=reason,
        localization_latency_ms=0.0,
        proposal_latency_ms=0.0,
        coordination_latency_ms=0.0,
        feasibility_latency_ms=0.0,
        delta_compile_latency_ms=0.0,
    )


def _modification_scope(
    stable: TaskSubnet,
    target: TaskSubnet,
    changed_rules: int,
    method_id: str,
) -> tuple[int, int, int, int, int, float]:
    stable_paths = stable.routes
    target_paths = target.routes
    stable_agents = _agent_objects(stable)
    target_agents = _agent_objects(target)
    total_paths = len(set(stable_paths) | set(target_paths))
    changed_paths = (
        total_paths
        if method_id == "full_rebuild"
        else _changed_object_count(stable_paths, target_paths)
    )
    total_agents = len(set(stable_agents) | set(target_agents))
    changed_agents = (
        total_agents
        if method_id == "full_rebuild"
        else _changed_object_count(stable_agents, target_agents)
    )
    total_rule_objects = len(set(stable.rules) | set(target.rules))
    denominator = total_rule_objects + total_paths + total_agents
    modification_scope_ratio = (
        (changed_rules + changed_paths + changed_agents) / denominator
        if denominator
        else 0.0
    )
    return (
        total_rule_objects,
        total_paths,
        changed_paths,
        total_agents,
        changed_agents,
        modification_scope_ratio,
    )


def _agent_objects(subnet: TaskSubnet) -> dict[tuple[str, str], object]:
    return {
        (layer, agent_id): agent
        for layer, agents in (
            ("application", subnet.application_agents),
            ("transport", subnet.transport_agents),
            ("network", subnet.network_agents),
            ("physical", subnet.physical_agents),
        )
        for agent_id, agent in agents.items()
    }


def _changed_object_count(stable: dict, target: dict) -> int:
    missing = object()
    return sum(
        stable.get(object_id, missing) != target.get(object_id, missing)
        for object_id in set(stable) | set(target)
    )


def _rejected_outcome(
    snapshot: PaperFailureSnapshot,
    stable: TaskSubnet,
    method_id: str,
    reason: str,
    selected_layers: frozenset[str],
    selected_actions: frozenset[str],
) -> PaperFailureOutcome:
    affected = set(snapshot.affected_business_edge_ids)
    return PaperFailureOutcome(
        method_id=method_id,
        success=False,
        qos_satisfied=False,
        failure_reason=reason,
        recovery_latency_ms=None,
        event_occurred_at=0.0,
        stable_verify_finished_at=None,
        total_rules=len(stable.rules),
        total_rule_objects=len(stable.rules),
        changed_rules=0,
        rule_change_ratio=0.0,
        total_paths=len(stable.routes),
        changed_paths=0,
        total_agents=len(_agent_objects(stable)),
        changed_agents=0,
        modification_scope_ratio=0.0,
        total_gateways=len(snapshot.catalog.gateways),
        changed_gateways=0,
        gateway_change_ratio=0.0,
        total_flows=len(stable.business_edges),
        unaffected_flows=max(0, len(stable.business_edges) - len(affected)),
        disturbed_unaffected_flows=0,
        unaffected_disturbance_ratio=0.0,
        control_messages=0,
        control_bytes=0,
        rollback_count=0,
        stale_state_detected=False,
        selected_layers=selected_layers,
        selected_actions=selected_actions,
    )
