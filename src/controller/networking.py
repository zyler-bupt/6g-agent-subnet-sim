from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import perf_counter

from src.agents.base import BaseAgent
from src.agents.phy_agent import PhyAgent
from src.controller.cost import CostModel
from src.controller.elastic import AdjustmentResult, ElasticAdjuster
from src.controller.feasibility import check_four_layer_feasibility
from src.controller.risk import RiskCalculator
from src.core.gateway import Gateway, entry_key
from src.core.events import RuntimeEvent
from src.core.models import (
    AgentCard,
    AgentConfirmAck,
    AgentLayer,
    ApplicationAgentState,
    ExperimentMetrics,
    FlowMatch,
    GatewayAck,
    GatewayRouteAction,
    GatewayRouteEntry,
    GatewayState,
    NetworkAgentState,
    PathSupportSpec,
    PhysicalAgentState,
    PhysicalResourceBinding,
    SessionSpec,
    SessionSupportSpec,
    TaskSpec,
    TaskState,
    TaskSubnet,
    TransportAgentState,
)

# Capability required from a support Agent of each layer, used when replacing a
# failed support Agent with a standby of the same capability.
_SUPPORT_CAPABILITY = {
    "trans": "transport_session",
    "net": "network_bearer",
}


@dataclass(frozen=True)
class _EdgeSupport:
    t_agent: AgentCard
    n_agent: AgentCard
    p_agents: tuple[AgentCard, ...]
    gateway_path: tuple[str, ...]


@dataclass(frozen=True)
class SubnetCompilation:
    subnet: TaskSubnet
    mapping_finished_at: float
    layer_binding_finished_at: float
    feasibility_finished_at: float
    compile_finished_at: float


class AgentController:
    def __init__(
        self,
        gateways: dict[str, Gateway],
        risk_calculator: RiskCalculator | None = None,
        adjuster: ElasticAdjuster | None = None,
        cost_model: CostModel | None = None,
        gateway_paths: dict[tuple[str, str], tuple[str, ...]] | None = None,
    ) -> None:
        self.gateways = gateways
        self.cost_model = cost_model or CostModel()
        self.risk_calculator = risk_calculator or RiskCalculator()
        self.adjuster = adjuster or ElasticAdjuster(self.cost_model)
        self.gateway_paths = dict(gateway_paths or {})
        self.tasks: dict[str, TaskSubnet] = {}

    async def handle_runtime_event(
        self,
        event: RuntimeEvent,
        *,
        run_id: int = 1,
        seed: int = 0,
        verifier=None,
    ):
        """Dispatch a versioned runtime transaction through one Controller entry.

        Stage 2 intentionally supports only ``AGENT_REMOVE``.  The local import
        keeps the existing networking module independent from future event
        transaction implementations while preserving ``AgentController`` as the
        public control-plane entry point.
        """

        from src.controller.transactions import AgentRemovalTransaction

        return await AgentRemovalTransaction(self, verifier=verifier).handle(
            event,
            run_id=run_id,
            seed=seed,
        )

    async def build_task_subnet(
        self,
        task: TaskSpec,
        *,
        verifier=None,
        transactional_installer=None,
        run_id: int = 0,
        seed: int = 0,
    ) -> tuple[TaskSubnet, ExperimentMetrics]:
        """Build version 0 -> 1 through the same transaction used at runtime."""

        from src.controller.impact import ImpactScope
        from src.controller.reconfiguration import ReconfigurationPlan, delta_by_gateway
        from src.controller.transaction_executor import TransactionExecutor
        from src.core.events import EventLogRecord, EventStage, RuntimeEvent, RuntimeEventType
        from src.core.rules import RuleDelta

        task_received = perf_counter()
        previous = self.tasks.get(task.task_id)
        version = previous.version + 1 if previous is not None else 1
        compilation = await self.compile_task_subnet(task, version=version)
        subnet = compilation.subnet
        old_rules = previous.rules if previous is not None else {}
        new_rules = subnet.rules
        additions = tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) - set(old_rules))
        )
        # A repeated full construction reinstalls every surviving rule rather
        # than disguising a rebuild as a zero-cost diff.
        updates = tuple(
            new_rules[rule_id] for rule_id in sorted(set(new_rules) & set(old_rules))
        )
        deletions = tuple(
            old_rules[rule_id] for rule_id in sorted(set(old_rules) - set(new_rules))
        )
        delta = RuleDelta(additions=additions, updates=updates, deletions=deletions)
        gateways = frozenset(
            subnet.involved_gateways
            | (previous.involved_gateways if previous is not None else set())
        )
        scope = ImpactScope(
            affected_agents=frozenset(
                subnet.app_agents | subnet.trans_agents | subnet.net_agents | subnet.phy_agents
            ),
            affected_business_edges=frozenset(subnet.business_edges),
            affected_sessions=frozenset(session.session_id for session in subnet.sessions),
            affected_routes=frozenset(subnet.routes),
            affected_physical_resources=frozenset(subnet.physical_bindings),
            affected_gateways=gateways,
            affected_rules=frozenset(subnet.rules),
        )
        plan = ReconfigurationPlan(
            method="initial_build" if previous is None else "full_rebuild",
            target_state=subnet,
            impact_scope=scope,
            rule_delta_by_gateway=delta_by_gateway(delta, gateways),
            affected_gateways=gateways,
            affected_layers=frozenset({"application", "transport", "network", "physical"}),
            transaction_gateways=gateways,
            verification_gateways=gateways,
            full_rule_install=True,
        )
        event = RuntimeEvent(
            event_id=f"formation-{task.task_id}-v{version}",
            task_id=task.task_id,
            event_type=RuntimeEventType.TASK_BUILD.value,
            occurred_at=task_received,
            payload={"version": version},
        )

        def formation_record(stage: str, timestamp: float) -> EventLogRecord:
            return EventLogRecord(
                run_id=run_id,
                event_id=event.event_id,
                task_id=task.task_id,
                old_version=version - 1,
                new_version=version,
                timestamp=timestamp,
                component="AgentController",
                event_type=event.event_type,
                event_stage=stage,
                details={},
            )

        pre_stage_records = (
            formation_record("MAPPING_FINISHED", compilation.mapping_finished_at),
            formation_record(
                "LAYER_BINDING_FINISHED",
                compilation.layer_binding_finished_at,
            ),
            formation_record("FEASIBILITY_FINISHED", compilation.feasibility_finished_at),
            formation_record("COMPILE_FINISHED", compilation.compile_finished_at),
            formation_record(EventStage.DELTA_COMPILED.value, compilation.compile_finished_at),
        )
        execution = await TransactionExecutor(
            self,
            installer=transactional_installer,
            verifier=verifier,
        ).execute(
            previous,
            plan,
            event,
            run_id=run_id,
            seed=seed,
            received_at=task_received,
            pre_stage_records=pre_stage_records,
        )
        subnet = execution.state
        if execution.success:
            subnet.gateway_acks = [
                GatewayAck(
                    gateway_id=gateway_id,
                    task_id=task.task_id,
                    accepted=True,
                    operation="install",
                    installed_session_ids=tuple(
                        sorted(
                            self.gateways[gateway_id].get_stable_sessions(task.task_id)
                        )
                    ),
                    installed_route_ids=tuple(
                        sorted(self.gateways[gateway_id].get_stable_rules(task.task_id))
                    ),
                    session_count=len(
                        self.gateways[gateway_id].get_stable_sessions(task.task_id)
                    ),
                    route_count=len(
                        self.gateways[gateway_id].get_stable_rules(task.task_id)
                    ),
                    version=version,
                )
                for gateway_id in sorted(gateways)
            ]
        else:
            subnet.gateway_acks = [
                GatewayAck(
                    gateway_id=gateway_id,
                    task_id=task.task_id,
                    accepted=False,
                    reason=execution.failure_reason,
                    operation="install",
                    version=version,
                )
                for gateway_id in sorted(gateways)
            ]

        stage_started = execution.timestamp(EventStage.STAGE_STARTED)
        stage_finished = execution.timestamp(EventStage.STAGE_FINISHED)
        staged_verify_started = execution.timestamp(EventStage.VERIFY_STARTED)
        staged_verify_finished = execution.timestamp(EventStage.VERIFY_FINISHED)
        activate_started = execution.timestamp(EventStage.ACTIVATE_STARTED)
        activate_finished = execution.timestamp(EventStage.ACTIVATE_FINISHED)
        stable_verify_started = execution.timestamp(EventStage.POST_ACTIVATE_VERIFY_STARTED)
        stable_verify_finished = execution.timestamp(
            EventStage.POST_ACTIVATE_VERIFY_FINISHED
        )
        duration = lambda start, finish: max(0.0, (finish - start) * 1000.0) if finish else 0.0
        controller_compute_ms = duration(task_received, compilation.compile_finished_at)
        metrics = ExperimentMetrics(
            networking_success=execution.success,
            controller_compute_ms=controller_compute_ms,
            controller_build_ms=controller_compute_ms,
            networking_latency_ms=controller_compute_ms,
            formation_latency_ms=(
                duration(task_received, stable_verify_finished) if execution.success else 0.0
            ),
            session_count=len(subnet.sessions),
            involved_gateway_count=len(gateways),
            qos_satisfied=execution.success,
            mapping_latency_ms=duration(task_received, compilation.mapping_finished_at),
            binding_latency_ms=duration(
                compilation.mapping_finished_at,
                compilation.layer_binding_finished_at,
            ),
            feasibility_latency_ms=duration(
                compilation.layer_binding_finished_at,
                compilation.feasibility_finished_at,
            ),
            compile_latency_ms=duration(
                compilation.feasibility_finished_at,
                compilation.compile_finished_at,
            ),
            stage_latency_ms=duration(stage_started, stage_finished),
            verification_latency_ms=(
                duration(staged_verify_started, staged_verify_finished)
                + duration(stable_verify_started, stable_verify_finished)
            ),
            activation_latency_ms=duration(activate_started, activate_finished),
            t_task_received=task_received,
            t_mapping_finished=compilation.mapping_finished_at,
            t_layer_binding_finished=compilation.layer_binding_finished_at,
            t_feasibility_finished=compilation.feasibility_finished_at,
            t_compile_finished=compilation.compile_finished_at,
            t_stage_started=stage_started,
            t_stage_finished=stage_finished,
            t_staged_verify_finished=staged_verify_finished,
            t_activate_finished=activate_finished,
            t_stable_verify_finished=stable_verify_finished,
            control_messages=execution.control_messages,
            control_bytes=execution.control_bytes,
            rollback_triggered=execution.rollback_triggered,
            rollback_success=execution.rollback_success,
            failure_reason=execution.failure_reason,
            transaction_event_log=execution.event_log,
        )
        return subnet, metrics

    async def compile_task_subnet(
        self,
        task: TaskSpec,
        *,
        version: int,
        gateway_path_overrides: dict[tuple[str, str], tuple[str, ...]] | None = None,
        excluded_support_agents: frozenset[str] = frozenset(),
    ) -> SubnetCompilation:
        """Compile a complete task configuration without mutating gateways.

        This is the common planning primitive used by initial construction and
        the Full-Rebuild strategy.  Physical bindings are projected here and
        become real only after the shared transaction has activated.
        """

        subnet = TaskSubnet(task=task, version=version, state=TaskState.PLANNING)
        app_cards, app_acks = await self._confirm_app_members(task)
        mapping_finished_at = perf_counter()

        support_cards, support_acks = await self._select_support_agents(
            task,
            app_cards,
            gateway_path_overrides=gateway_path_overrides,
            excluded_support_agents=excluded_support_agents,
        )
        sessions = self._map_edges_to_sessions(task, app_cards, support_cards)
        subnet.agent_acks = app_acks + support_acks
        subnet.application_agents = self._build_application_states(task, app_cards)
        subnet.edges = self._build_edges(task, sessions)
        subnet.sessions = sessions
        subnet.involved_gateways = _involved_gateways(sessions)
        subnet.monitored_edge_ids = {edge.edge_id for edge in task.biz_edges}
        subnet.path_supports, subnet.session_supports = self._build_support_bindings(sessions)
        subnet.transport_agents, subnet.network_agents = self._build_support_states(
            task,
            sessions,
        )
        subnet.physical_bindings, subnet.physical_agents = self._plan_physical_bindings(
            task,
            sessions,
            version,
        )
        layer_binding_finished_at = perf_counter()

        feasibility = check_four_layer_feasibility(subnet)
        feasibility_finished_at = perf_counter()
        if not feasibility.feasible:
            raise ValueError(
                "four-layer feasibility failed: " + " | ".join(feasibility.violations)
            )
        subnet.gateway_routes = self._build_gateway_route_tables(
            task,
            sessions,
            app_cards,
            version=version,
        )
        compile_finished_at = perf_counter()
        return SubnetCompilation(
            subnet=subnet,
            mapping_finished_at=mapping_finished_at,
            layer_binding_finished_at=layer_binding_finished_at,
            feasibility_finished_at=feasibility_finished_at,
            compile_finished_at=compile_finished_at,
        )

    async def run_agent_loop(self, subnet: TaskSubnet, timestamp: float) -> list:
        agents = self._agents_for_subnet(subnet)
        predictions = []
        actions = []
        for agent in agents:
            agent_predictions, action = agent.run_step(subnet.task, timestamp)
            predictions.extend(agent_predictions)
            actions.append(action)
        subnet.predictions = {f"{item.agent_id}:{item.metric}": item for item in predictions}
        subnet.actions = actions
        return predictions

    async def evaluate_and_adjust(
        self,
        subnet: TaskSubnet,
        timestamp: float,
        failed_agents: set[str] | None = None,
    ) -> tuple[float, float, AdjustmentResult]:
        """Run-time evaluation and minimal, ordered elastic adjustment.

        Trigger conditions (per the design): risk over threshold, or a support
        Agent/gateway failure (F_m=1). Adjustment follows the ordered tiers:
          tier-1  local parameter tuning,
          tier-2  supplement/replace a failed support Agent with a local standby,
          tier-3  reroute the communication relation across subnets.
        Only the affected range is touched; unaffected parts stay intact.
        """
        task = subnet.task
        failed = set(failed_agents) if failed_agents is not None else self._failed_support_ids(subnet)

        predictions = await self.run_agent_loop(subnet, timestamp)
        risk_before = self.risk_calculator.risk(task, predictions)
        local_predictions = await self.run_agent_loop(subnet, timestamp + 0.1)
        risk_after_local = self.risk_calculator.risk(task, local_predictions)

        triggered = risk_before > task.qos.risk_threshold or bool(failed)
        if not triggered:
            return risk_before, risk_after_local, self._no_adjustment(subnet)

        # A dead support Agent cannot be healed by local tuning: go to tier-2/3.
        if failed:
            result, risk_after = await self._heal_failed_support(subnet, failed, timestamp)
            return risk_before, risk_after, result

        # Risk-only event: tier-1 local tuning, else session retune (tier-2 fallback).
        result = self.adjuster.choose_minimal_adjustment(subnet, risk_before, risk_after_local)
        if result.changed_sessions:
            await self._apply_sessions(subnet, result.changed_sessions)
            subnet.sessions = list(result.changed_sessions)
        return risk_before, risk_after_local, result

    @staticmethod
    def _no_adjustment(subnet: TaskSubnet) -> AdjustmentResult:
        return AdjustmentResult(
            strategy="no_adjustment",
            actions=tuple(subnet.actions),
            changed_sessions=(),
            changed_agents=0,
            changed_edges=0,
            changed_gateways=0,
            service_interruption_ms=0.0,
            operations={},
        )

    def _failed_support_ids(self, subnet: TaskSubnet) -> set[str]:
        failed: set[str] = set()
        for agent_id in subnet.trans_agents | subnet.net_agents:
            agent = self._agent_by_id(agent_id)
            if agent is None or not agent.card.online:
                failed.add(agent_id)
        return failed

    async def _heal_failed_support(
        self,
        subnet: TaskSubnet,
        failed: set[str],
        timestamp: float,
        *,
        evaluate_after: bool = True,
    ) -> tuple[AdjustmentResult, float]:
        task = subnet.task
        used_ids = (subnet.trans_agents | subnet.net_agents) - failed
        new_sessions: list[SessionSpec] = []
        affected_gateways: set[str] = set()
        local_replacements = 0
        cross_subnet_reroutes = 0
        unresolved: list[str] = []

        for session in subnet.sessions:
            updated = session
            touched = False
            for layer, current_id in (("trans", session.t_agent_id), ("net", session.n_agent_id)):
                if current_id not in failed:
                    continue
                card, is_local = await self._resolve_support(
                    _SUPPORT_CAPABILITY[layer], used_ids, home_gateway=session.source_gateway
                )
                if card is None:
                    unresolved.append(current_id)
                    continue
                used_ids.add(card.agent_id)
                if layer == "trans":
                    updated = replace(updated, t_agent_id=card.agent_id)
                else:
                    updated = replace(updated, n_agent_id=card.agent_id)
                touched = True
                if is_local:
                    local_replacements += 1
                else:
                    cross_subnet_reroutes += 1
                    affected_gateways.add(card.gateway_id)  # support now lives in another subnet
            if touched:
                updated = replace(updated, status="rehomed")
                affected_gateways.add(updated.source_gateway)
                affected_gateways.add(updated.target_gateway)
            new_sessions.append(updated)

        # Commit the healed subnet and push updates only to the affected gateways.
        subnet.sessions = new_sessions
        subnet.transport_agents, subnet.network_agents = self._build_support_states(
            task,
            new_sessions,
        )
        subnet.edges = self._build_edges(task, new_sessions)
        subnet.involved_gateways |= affected_gateways | _involved_gateways(new_sessions)
        subnet.state = TaskState.STABLE if not unresolved else TaskState.DEGRADED
        subnet.path_supports, subnet.session_supports = self._build_support_bindings(new_sessions)
        app_cards = self._current_app_cards(task)
        subnet.gateway_routes = self._build_gateway_route_tables(
            task,
            new_sessions,
            app_cards,
            version=subnet.version,
        )
        await self._apply_sessions(subnet, tuple(s for s in new_sessions if s.status == "rehomed"))
        subnet.gateways = self._gateway_state_snapshot(task.task_id, subnet.involved_gateways)

        if evaluate_after:
            predictions = await self.run_agent_loop(subnet, timestamp + 0.2)
            risk_after = self.risk_calculator.risk(task, predictions)
        else:
            risk_after = 0.0

        replaced = local_replacements + cross_subnet_reroutes
        changed_sessions = tuple(s for s in new_sessions if s.status == "rehomed")
        operations = {
            "agent_replace": replaced,
            "session_setup": len(changed_sessions),
            "gateway_install": len(affected_gateways),
            "reroute": cross_subnet_reroutes,
        }
        strategy = "communication_reroute" if cross_subnet_reroutes else "support_agent_replace"
        result = AdjustmentResult(
            strategy=strategy,
            actions=tuple(subnet.actions),
            changed_sessions=changed_sessions,
            changed_agents=replaced,
            changed_edges=len(changed_sessions),
            changed_gateways=len(affected_gateways),
            service_interruption_ms=self.cost_model.interruption_ms(operations),
            operations=operations,
        )
        return result, risk_after

    async def apply_failed_support_recovery(
        self,
        subnet: TaskSubnet,
        failed: set[str],
        timestamp: float,
    ) -> AdjustmentResult:
        """Apply a confirmed support-Agent recovery without probing the failed path.

        Detection and planning have already established the failure. Data-plane
        verification must run after the replacement and external route update,
        otherwise an intentional blackhole is misreported as a controller error.
        """
        result, _risk_after = await self._heal_failed_support(
            subnet,
            failed,
            timestamp,
            evaluate_after=False,
        )
        return result

    async def _resolve_support(
        self,
        capability: str,
        used_ids: set[str],
        home_gateway: str,
    ) -> tuple[AgentCard | None, bool]:
        """Find an online standby support Agent.

        Prefers a standby in the home subnet (tier-2 local replacement); falls
        back to another subnet (tier-3 cross-subnet reroute). Returns the card
        and a flag that is True when the replacement stays local.
        """
        home = self.gateways.get(home_gateway)
        if home is not None and home.online:
            for card in await home.confirm_support(capability):
                if card.agent_id not in used_ids:
                    return card, True
        for gateway_id, gateway in self.gateways.items():
            if gateway_id == home_gateway or not gateway.online:
                continue
            for card in await gateway.confirm_support(capability):
                if card.agent_id not in used_ids:
                    return card, False
        return None, False

    async def _apply_sessions(self, subnet: TaskSubnet, sessions: tuple[SessionSpec, ...]) -> None:
        if not sessions:
            return
        task_id = subnet.task.task_id
        affected_gateways = {
            gateway_id for session in sessions for gateway_id in _session_gateways(session)
        }
        changed_session_ids = {session.session_id for session in sessions}
        changed_routes = [
            entry
            for gateway_id in affected_gateways
            for entry in subnet.gateway_routes.get(gateway_id, [])
            if entry.session_id in changed_session_ids
        ]
        await asyncio.gather(
            *[
                self.gateways[gateway_id].apply_update(task_id, list(sessions), changed_routes)
                for gateway_id in affected_gateways
            ]
        )

    def missing_gateway_routes(self, subnet: TaskSubnet) -> dict[str, list[GatewayRouteEntry]]:
        """Return logical task routes absent from each in-process gateway."""
        missing: dict[str, list[GatewayRouteEntry]] = {}
        for gateway_id, expected in subnet.gateway_routes.items():
            gateway = self.gateways[gateway_id]
            absent = [entry for entry in expected if entry_key(entry) not in gateway.route_table]
            if absent:
                missing[gateway_id] = absent
        return missing

    async def reinstall_gateway_routes(
        self,
        subnet: TaskSubnet,
        gateway_ids: set[str],
    ) -> list[GatewayAck]:
        """Reinstall the full logical task slice on selected gateways."""
        acknowledgements = list(
            await asyncio.gather(
                *[
                    self.gateways[gateway_id].install_subnet(
                        subnet.task,
                        subnet.sessions,
                        subnet.gateway_routes.get(gateway_id, []),
                    )
                    for gateway_id in sorted(gateway_ids)
                ]
            )
        )
        by_gateway = {ack.gateway_id: ack for ack in subnet.gateway_acks}
        by_gateway.update({ack.gateway_id: ack for ack in acknowledgements})
        subnet.gateway_acks = [by_gateway[gateway_id] for gateway_id in sorted(by_gateway)]
        if all(ack.accepted for ack in subnet.gateway_acks):
            subnet.state = TaskState.STABLE
            subnet.gateways = self._gateway_state_snapshot(
                subnet.task.task_id,
                subnet.involved_gateways,
            )
        return acknowledgements

    def _agent_by_id(self, agent_id: str) -> BaseAgent | None:
        for gateway in self.gateways.values():
            agent = gateway.agents.get(agent_id)
            if agent is not None:
                return agent
        return None

    async def rebuild_task_subnet(
        self,
        subnet: TaskSubnet,
        timestamp: float,
    ) -> tuple[TaskSubnet, float, AdjustmentResult]:
        """Full-rebuild baseline using full compilation and transactional install.

        Unlike the minimal adjustment, this re-confirms every member,
        recompiles every session, and reinstalls every task rule.  The previous
        version remains active until the common transaction commits.
        """
        task = subnet.task

        # Recompile every object and reinstall every surviving rule through the
        # versioned transaction.  The old stable version stays live until the
        # new version passes verification, so a failed rebuild remains atomic.
        new_subnet, _ = await self.build_task_subnet(task)

        # Re-evaluate risk on the freshly built subnet.
        predictions = await self.run_agent_loop(new_subnet, timestamp)
        risk_after = self.risk_calculator.risk(task, predictions)

        operations = {
            "member_confirm": len(new_subnet.app_agents),
            "gateway_install": len(new_subnet.involved_gateways),
            "session_setup": len(new_subnet.sessions),
        }
        result = AdjustmentResult(
            strategy="full_rebuild",
            actions=tuple(new_subnet.actions),
            changed_sessions=tuple(new_subnet.sessions),
            changed_agents=len(
                new_subnet.app_agents
                | new_subnet.trans_agents
                | new_subnet.net_agents
                | new_subnet.phy_agents
            ),
            changed_edges=len(new_subnet.edges),
            changed_gateways=len(new_subnet.involved_gateways),
            service_interruption_ms=self.cost_model.interruption_ms(operations),
            operations=operations,
        )
        return new_subnet, risk_after, result

    async def _confirm_app_members(self, task: TaskSpec) -> tuple[dict[str, AgentCard], list[AgentConfirmAck]]:
        cards: dict[str, AgentCard] = {}
        acks: list[AgentConfirmAck] = []
        for agent_id in task.app_agents:
            card = await self._find_agent(agent_id)
            if card is None or card.layer != AgentLayer.APPLICATION:
                raise ValueError(f"application agent unavailable: {agent_id}")
            cards[agent_id] = card
            acks.append(_agent_ack(task.task_id, card, "app_member"))
        return cards, acks

    async def _select_support_agents(
        self,
        task: TaskSpec,
        app_cards: dict[str, AgentCard],
        *,
        gateway_path_overrides: dict[tuple[str, str], tuple[str, ...]] | None = None,
        excluded_support_agents: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, _EdgeSupport], list[AgentConfirmAck]]:
        selected: dict[str, _EdgeSupport] = {}
        ack_by_agent: dict[tuple[str, str], AgentConfirmAck] = {}
        for edge in task.biz_edges:
            source = app_cards[edge.source]
            target = app_cards[edge.target]
            gateway_path = (
                gateway_path_overrides.get((source.gateway_id, target.gateway_id))
                if gateway_path_overrides is not None
                else None
            ) or self._gateway_path(source.gateway_id, target.gateway_id)
            if gateway_path[0] != source.gateway_id or gateway_path[-1] != target.gateway_id:
                raise ValueError(
                    "gateway path override endpoints do not match business edge"
                )
            unknown = [item for item in gateway_path if item not in self.gateways]
            if unknown:
                raise ValueError(f"gateway path override references unknown gateways: {unknown}")
            t_agent = await self._first_support(
                "transport_session",
                preferred_gateway=source.gateway_id,
                allowed_gateways=(source.gateway_id,),
                excluded_agent_ids=excluded_support_agents,
            )
            n_agent = await self._first_support(
                "network_bearer",
                preferred_gateway=gateway_path[0],
                allowed_gateways=gateway_path,
                excluded_agent_ids=excluded_support_agents,
            )
            p_agents: list[AgentCard] = []
            for gateway_id in dict.fromkeys((source.gateway_id, target.gateway_id)):
                p_agent = await self._first_support(
                    "physical_access",
                    preferred_gateway=gateway_id,
                    allowed_gateways=(gateway_id,),
                    excluded_agent_ids=excluded_support_agents,
                )
                if p_agent is None:
                    raise ValueError(
                        f"physical support unavailable for edge {edge.source}->{edge.target} "
                        f"at {gateway_id}"
                    )
                p_agents.append(p_agent)
            if t_agent is None or n_agent is None:
                raise ValueError(f"support agents unavailable for edge {edge.source}->{edge.target}")
            selected[edge.edge_id] = _EdgeSupport(
                t_agent=t_agent,
                n_agent=n_agent,
                p_agents=tuple(p_agents),
                gateway_path=gateway_path,
            )
            ack_by_agent[(t_agent.agent_id, "transport_session")] = _agent_ack(
                task.task_id,
                t_agent,
                "transport_session",
            )
            ack_by_agent[(n_agent.agent_id, "network_bearer")] = _agent_ack(
                task.task_id,
                n_agent,
                "network_bearer",
            )
            for p_agent in p_agents:
                ack_by_agent[(p_agent.agent_id, "physical_access")] = _agent_ack(
                    task.task_id,
                    p_agent,
                    "physical_access",
                )
        return selected, list(ack_by_agent.values())

    def _map_edges_to_sessions(
        self,
        task: TaskSpec,
        app_cards: dict[str, AgentCard],
        support_cards: dict[str, _EdgeSupport],
    ) -> list[SessionSpec]:
        sessions = []
        for index, edge in enumerate(task.biz_edges, start=1):
            source = app_cards[edge.source]
            target = app_cards[edge.target]
            support = support_cards[edge.edge_id]
            sessions.append(
                SessionSpec(
                    session_id=f"{task.task_id}-sess-{index}",
                    task_id=task.task_id,
                    source=edge.source,
                    target=edge.target,
                    t_agent_id=support.t_agent.agent_id,
                    n_agent_id=support.n_agent.agent_id,
                    source_gateway=source.gateway_id,
                    target_gateway=target.gateway_id,
                    latency_budget_ms=edge.latency_budget_ms,
                    data_rate_mbps=edge.data_rate_mbps,
                    path_id="->".join(support.gateway_path),
                    gateway_path=support.gateway_path,
                    business_edge_id=edge.edge_id,
                    p_agent_ids=tuple(agent.agent_id for agent in support.p_agents),
                )
            )
        return sessions

    @staticmethod
    def _build_edges(task: TaskSpec, sessions: list[SessionSpec]) -> set[tuple[str, str, str]]:
        edges = {(edge.source, edge.target, "biz") for edge in task.biz_edges}
        for session in sessions:
            edges.add((session.source, session.t_agent_id, "uses_trans"))
            edges.add((session.t_agent_id, session.n_agent_id, "uses_net"))
            edges.add((session.n_agent_id, session.target, "supports_delivery"))
            for p_agent_id in session.p_agent_ids:
                edges.add((p_agent_id, session.source, "supports_physical_access"))
        return edges

    def _build_support_bindings(
        self,
        sessions: list[SessionSpec],
    ) -> tuple[dict[str, PathSupportSpec], dict[str, SessionSupportSpec]]:
        path_supports: dict[str, PathSupportSpec] = {}
        session_supports: dict[str, SessionSupportSpec] = {}
        for session in sessions:
            gateway_path = _session_gateways(session)
            path_support_id = _path_support_id(session)
            session_support_id = _session_support_id(session)
            path_supports[path_support_id] = PathSupportSpec(
                support_id=path_support_id,
                path_id=session.path_id,
                gateway_path=gateway_path,
                n_agent_id=session.n_agent_id,
                monitored_links=_path_links(gateway_path),
                p_agent_ids=session.p_agent_ids,
            )
            session_supports[session.session_id] = SessionSupportSpec(
                support_id=session_support_id,
                session_id=session.session_id,
                t_agent_id=session.t_agent_id,
                path_support_id=path_support_id,
                path_id=session.path_id,
                gateway_path=gateway_path,
                p_agent_ids=session.p_agent_ids,
            )
        return path_supports, session_supports

    @staticmethod
    def _build_application_states(
        task: TaskSpec,
        app_cards: dict[str, AgentCard],
    ) -> dict[str, ApplicationAgentState]:
        states: dict[str, ApplicationAgentState] = {}
        for agent_id, card in app_cards.items():
            outgoing = [edge for edge in task.biz_edges if edge.source == agent_id]
            incident = [
                edge
                for edge in task.biz_edges
                if edge.source == agent_id or edge.target == agent_id
            ]
            states[agent_id] = ApplicationAgentState(
                agent_id=agent_id,
                gateway_id=card.gateway_id,
                node=card.node,
                data_rate_mbps=sum(edge.data_rate_mbps for edge in outgoing),
                priority=max(
                    (edge.priority for edge in incident),
                    default=task.qos.priority,
                ),
                online=card.online,
            )
        return states

    def _build_support_states(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
    ) -> tuple[dict[str, TransportAgentState], dict[str, NetworkAgentState]]:
        transport: dict[str, TransportAgentState] = {}
        network: dict[str, NetworkAgentState] = {}
        for agent_id in sorted({session.t_agent_id for session in sessions}):
            assigned = [session for session in sessions if session.t_agent_id == agent_id]
            agent = self._agent_by_id(agent_id)
            if agent is None:
                continue
            transport[agent_id] = TransportAgentState(
                agent_id=agent_id,
                gateway_id=agent.card.gateway_id,
                session_ids=tuple(sorted(session.session_id for session in assigned)),
                send_rate_mbps=sum(session.data_rate_mbps for session in assigned),
                reliability=task.qos.min_reliability,
                online=agent.card.online,
            )
        for agent_id in sorted({session.n_agent_id for session in sessions}):
            assigned = [session for session in sessions if session.n_agent_id == agent_id]
            agent = self._agent_by_id(agent_id)
            if agent is None:
                continue
            capacity = float(
                agent.card.state.values.get(
                    "available_capacity_mbps",
                    max(100.0, task.qos.min_bandwidth_mbps),
                )
            )
            network[agent_id] = NetworkAgentState(
                agent_id=agent_id,
                gateway_id=agent.card.gateway_id,
                path_ids=tuple(sorted({session.path_id for session in assigned})),
                available_capacity_mbps=capacity,
                online=agent.card.online,
            )
        return transport, network

    def _allocate_physical_bindings(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
        version: int,
    ) -> tuple[
        dict[str, PhysicalResourceBinding],
        dict[str, PhysicalResourceBinding],
    ]:
        requirements: dict[str, list[tuple[str, float]]] = {}
        for session in sessions:
            for agent_id in session.p_agent_ids:
                requirements.setdefault(agent_id, []).append(
                    (session.business_edge_id, session.data_rate_mbps)
                )

        agents: dict[str, PhyAgent] = {}
        for agent_id, items in requirements.items():
            agent = self._agent_by_id(agent_id)
            if not isinstance(agent, PhyAgent):
                raise ValueError(f"physical Agent unavailable: {agent_id}")
            agents[agent_id] = agent
            target_ids = {
                f"{task.task_id}:{edge_id}:{agent_id}:physical"
                for edge_id, _required in items
            }
            retained = sum(
                binding.reserved_capacity_mbps
                for binding_id, binding in agent.resource_bindings.items()
                if binding.active and binding_id not in target_ids
            )
            requested = sum(required for _edge_id, required in items)
            if not agent.card.online or retained + requested > agent.total_capacity_mbps + 1e-9:
                raise ValueError(
                    f"physical capacity exceeded at {agent_id}: "
                    f"required={requested:g}, retained={retained:g}, "
                    f"capacity={agent.total_capacity_mbps:g}"
                )

        bindings: dict[str, PhysicalResourceBinding] = {}
        previous: dict[str, PhysicalResourceBinding] = {}
        try:
            for agent_id, items in requirements.items():
                agent = agents[agent_id]
                for edge_id, required in items:
                    binding_id = f"{task.task_id}:{edge_id}:{agent_id}:physical"
                    old = agent.resource_bindings.get(binding_id)
                    if old is not None:
                        previous[binding_id] = old
                    binding = agent.allocate_resource(
                        task.task_id,
                        edge_id,
                        required,
                        version,
                    )
                    bindings[binding.binding_id] = binding
        except Exception:
            self._restore_physical_bindings(bindings, previous)
            raise
        return bindings, previous

    def _plan_physical_bindings(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
        version: int,
    ) -> tuple[
        dict[str, PhysicalResourceBinding],
        dict[str, PhysicalAgentState],
    ]:
        """Return a capacity-checked physical target snapshot without mutation."""

        requirements: dict[str, list[tuple[str, float]]] = {}
        for session in sessions:
            for agent_id in session.p_agent_ids:
                requirements.setdefault(agent_id, []).append(
                    (session.business_edge_id, session.data_rate_mbps)
                )

        bindings: dict[str, PhysicalResourceBinding] = {}
        states: dict[str, PhysicalAgentState] = {}
        for agent_id, items in sorted(requirements.items()):
            agent = self._agent_by_id(agent_id)
            if not isinstance(agent, PhyAgent):
                raise ValueError(f"physical Agent unavailable: {agent_id}")
            retained = {
                binding_id: binding
                for binding_id, binding in agent.resource_bindings.items()
                if binding.active and binding.task_id != task.task_id
            }
            requested = sum(required for _edge_id, required in items)
            retained_capacity = sum(
                binding.reserved_capacity_mbps for binding in retained.values()
            )
            if (
                not agent.card.online
                or retained_capacity + requested > agent.total_capacity_mbps + 1e-9
            ):
                raise ValueError(
                    f"physical capacity exceeded at {agent_id}: "
                    f"required={requested:g}, retained={retained_capacity:g}, "
                    f"capacity={agent.total_capacity_mbps:g}"
                )
            desired: dict[str, PhysicalResourceBinding] = {}
            for edge_id, required in items:
                binding_id = f"{task.task_id}:{edge_id}:{agent_id}:physical"
                desired[binding_id] = PhysicalResourceBinding(
                    binding_id=binding_id,
                    task_id=task.task_id,
                    edge_id=edge_id,
                    agent_id=agent_id,
                    gateway_id=agent.card.gateway_id,
                    reserved_capacity_mbps=required,
                    version=version,
                )
            bindings.update(desired)
            current = agent.report_state()
            total_reserved = retained_capacity + requested
            states[agent_id] = replace(
                current,
                available_capacity_mbps=max(
                    0.0,
                    agent.total_capacity_mbps - total_reserved,
                ),
                resource_utilization=min(
                    1.0,
                    total_reserved / max(agent.total_capacity_mbps, 1e-9),
                ),
                binding_ids=tuple(sorted({*retained, *desired})),
            )
        return bindings, states

    def _restore_physical_bindings(
        self,
        bindings: dict[str, PhysicalResourceBinding],
        previous: dict[str, PhysicalResourceBinding],
    ) -> None:
        for binding in bindings.values():
            agent = self._agent_by_id(binding.agent_id)
            if isinstance(agent, PhyAgent):
                agent.release_resource(binding.binding_id)
        for binding in previous.values():
            agent = self._agent_by_id(binding.agent_id)
            if isinstance(agent, PhyAgent):
                agent.restore_resource(binding)

    def _physical_states_for_sessions(
        self,
        sessions: list[SessionSpec],
    ) -> dict[str, PhysicalAgentState]:
        states: dict[str, PhysicalAgentState] = {}
        for agent_id in sorted(
            {agent_id for session in sessions for agent_id in session.p_agent_ids}
        ):
            agent = self._agent_by_id(agent_id)
            if isinstance(agent, PhyAgent):
                states[agent_id] = agent.report_state()
        return states

    def _gateway_state_snapshot(
        self,
        task_id: str,
        gateway_ids: set[str],
    ) -> dict[str, GatewayState]:
        return {
            gateway_id: GatewayState(
                gateway_id=gateway_id,
                online=self.gateways[gateway_id].online,
                stable_version=self.gateways[gateway_id].get_stable_version(task_id),
                staged_version=self.gateways[gateway_id].get_staged_version(task_id),
                stable_rule_ids=tuple(
                    sorted(self.gateways[gateway_id].get_stable_rules(task_id))
                ),
                staged_rule_ids=tuple(
                    sorted(self.gateways[gateway_id].get_staged_rules(task_id))
                ),
            )
            for gateway_id in sorted(gateway_ids)
        }

    async def _install_on_gateways(
        self,
        task: TaskSpec,
        gateway_ids: set[str],
        sessions: list[SessionSpec],
        route_tables: dict[str, list[GatewayRouteEntry]],
    ) -> list[GatewayAck]:
        return list(
            await asyncio.gather(
                *[
                    self.gateways[gateway_id].install_subnet(
                        task,
                        sessions,
                        route_tables.get(gateway_id, []),
                    )
                    for gateway_id in sorted(gateway_ids)
                ]
            )
        )

    def _build_gateway_route_tables(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
        app_cards: dict[str, AgentCard],
        *,
        version: int = 1,
    ) -> dict[str, list[GatewayRouteEntry]]:
        tables: dict[str, list[GatewayRouteEntry]] = {}
        edges = {
            (edge.source, edge.target): edge
            for edge in task.biz_edges
        }
        for session in sessions:
            edge = edges[(session.source, session.target)]
            gateway_path = _session_gateways(session)
            for hop_index, gateway_id in enumerate(gateway_path):
                next_hop_gateway = gateway_path[hop_index + 1] if hop_index + 1 < len(gateway_path) else None
                entry = self._route_entry_for_gateway(
                    task,
                    session,
                    edge.flow_type,
                    edge.priority,
                    app_cards,
                    gateway_id,
                    hop_index,
                    next_hop_gateway,
                    version,
                )
                tables.setdefault(gateway_id, []).append(entry)
        return tables

    def _route_entry_for_gateway(
        self,
        task: TaskSpec,
        session: SessionSpec,
        flow_type: str,
        priority: int,
        app_cards: dict[str, AgentCard],
        gateway_id: str,
        hop_index: int,
        next_hop_gateway: str | None,
        version: int,
    ) -> GatewayRouteEntry:
        target = app_cards[session.target]
        match = FlowMatch(
            src_agent=session.source,
            dst_agent=session.target,
            flow_type=flow_type,
            dst_port=target.port,
        )
        if gateway_id == session.target_gateway:
            action = GatewayRouteAction(
                mode="local_delivery",
                allow=True,
                local_agent=target.agent_id,
                local_agent_ip=target.ip,
                local_agent_port=target.port,
                dscp=_priority_to_dscp(priority),
                priority=priority,
            )
        else:
            if next_hop_gateway is None:
                raise ValueError(f"missing next hop for route {session.session_id} at {gateway_id}")
            next_hop = self.gateways[next_hop_gateway]
            action = GatewayRouteAction(
                mode="forward_to_gateway",
                allow=True,
                next_hop_gateway=next_hop.gateway_id,
                next_hop_gateway_ip=next_hop.gateway_ip,
                dscp=_priority_to_dscp(priority),
                priority=priority,
            )
        return GatewayRouteEntry(
            task_id=task.task_id,
            session_id=session.session_id,
            gateway_id=gateway_id,
            flow_id=f"{session.source}->{session.target}",
            match=match,
            action=action,
            t_agent_id=session.t_agent_id,
            n_agent_id=session.n_agent_id,
            p_agent_id=session.p_agent_ids[0] if session.p_agent_ids else None,
            session_support_id=_session_support_id(session),
            path_support_id=_path_support_id(session),
            path_id=session.path_id,
            gateway_path=session.gateway_path,
            hop_index=hop_index,
            latency_budget_ms=session.latency_budget_ms,
            min_bandwidth_mbps=session.data_rate_mbps,
            status=session.status,
            version=version,
            route_id=f"{session.session_id}:route",
            p_agent_ids=session.p_agent_ids,
        )

    def _gateway_path(self, source_gateway: str, target_gateway: str) -> tuple[str, ...]:
        if source_gateway == target_gateway:
            return (source_gateway,)
        path = self.gateway_paths.get((source_gateway, target_gateway), (source_gateway, target_gateway))
        if not path:
            raise ValueError(f"empty gateway path for {source_gateway}->{target_gateway}")
        if path[0] != source_gateway or path[-1] != target_gateway:
            raise ValueError(
                f"gateway path must start at {source_gateway} and end at {target_gateway}: {path}"
            )
        unknown = [gateway_id for gateway_id in path if gateway_id not in self.gateways]
        if unknown:
            raise ValueError(f"gateway path references unknown gateways: {unknown}")
        return tuple(path)

    def _current_app_cards(self, task: TaskSpec) -> dict[str, AgentCard]:
        cards = {}
        for agent_id in task.app_agents:
            agent = self._agent_by_id(agent_id)
            if agent is None:
                raise ValueError(f"application agent unavailable: {agent_id}")
            cards[agent_id] = agent.card
        return cards

    async def _find_agent(self, agent_id: str) -> AgentCard | None:
        for gateway in self.gateways.values():
            card = await gateway.confirm_member(agent_id)
            if card is not None:
                return card
        return None

    async def _first_support(
        self,
        capability: str,
        preferred_gateway: str | None = None,
        allowed_gateways: tuple[str, ...] | None = None,
        excluded_agent_ids: frozenset[str] = frozenset(),
    ) -> AgentCard | None:
        gateway_ids: list[str] = []
        if allowed_gateways is None:
            gateway_ids = list(self.gateways)
        else:
            gateway_ids = list(dict.fromkeys(allowed_gateways))
        if preferred_gateway is not None and preferred_gateway in gateway_ids:
            gateway_ids.remove(preferred_gateway)
            gateway_ids.insert(0, preferred_gateway)
        gateway_order = [self.gateways[gateway_id] for gateway_id in gateway_ids]
        for gateway in gateway_order:
            if not gateway.online:
                continue
            cards = [
                card
                for card in await gateway.confirm_support(capability)
                if card.agent_id not in excluded_agent_ids
            ]
            if cards:
                return cards[0]
        return None

    def _agents_for_subnet(self, subnet: TaskSubnet) -> list[BaseAgent]:
        # Preserve the established a/t/n sampling order for seeded experiments;
        # physical observations are appended and use their own state model.
        ids = subnet.app_agents | subnet.trans_agents | subnet.net_agents
        agents: list[BaseAgent] = []
        for gateway in self.gateways.values():
            for agent_id in ids:
                agent = gateway.agents.get(agent_id)
                if agent is not None and agent not in agents:
                    agents.append(agent)
        for gateway in self.gateways.values():
            for agent_id in subnet.phy_agents:
                agent = gateway.agents.get(agent_id)
                if agent is not None and agent not in agents:
                    agents.append(agent)
        return agents


def _priority_to_dscp(priority: int) -> int:
    if priority >= 3:
        return 0xB8
    if priority == 2:
        return 0x68
    return 0x00


def _agent_ack(task_id: str, card: AgentCard, purpose: str) -> AgentConfirmAck:
    return AgentConfirmAck(
        task_id=task_id,
        gateway_id=card.gateway_id,
        agent_id=card.agent_id,
        accepted=True,
        purpose=purpose,
        layer=card.layer,
        role=card.role,
        endpoint=card.endpoint,
        ip=card.ip,
        port=card.port,
        capabilities=card.capabilities,
    )


def _session_gateways(session: SessionSpec) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)


def _path_links(gateway_path: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(zip(gateway_path, gateway_path[1:]))


def _session_support_id(session: SessionSpec) -> str:
    return f"{session.session_id}:session-support"


def _path_support_id(session: SessionSpec) -> str:
    return f"{session.session_id}:path-support"


def _involved_gateways(sessions: list[SessionSpec]) -> set[str]:
    return {
        gateway_id
        for session in sessions
        for gateway_id in _session_gateways(session)
    }
