from __future__ import annotations

import asyncio
from dataclasses import replace
from time import perf_counter

from src.agents.base import BaseAgent
from src.controller.cost import CostModel
from src.controller.elastic import AdjustmentResult, ElasticAdjuster
from src.controller.risk import RiskCalculator
from src.core.gateway import Gateway
from src.core.models import (
    AgentCard,
    AgentLayer,
    ExperimentMetrics,
    FlowMatch,
    GatewayAck,
    GatewayRouteAction,
    GatewayRouteEntry,
    SessionSpec,
    TaskSpec,
    TaskState,
    TaskSubnet,
)

# Capability required from a support Agent of each layer, used when replacing a
# failed support Agent with a standby of the same capability.
_SUPPORT_CAPABILITY = {
    "trans": "transport_session",
    "net": "network_bearer",
}


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

    async def build_task_subnet(self, task: TaskSpec) -> tuple[TaskSubnet, ExperimentMetrics]:
        start = perf_counter()
        subnet = TaskSubnet(task=task, app_agents=set(task.app_agents), state=TaskState.NETWORKING)
        app_cards = await self._confirm_app_members(task)
        support_cards = await self._select_support_agents(task, app_cards)
        sessions = self._map_edges_to_sessions(task, app_cards, support_cards)

        subnet.trans_agents = {session.t_agent_id for session in sessions}
        subnet.net_agents = {session.n_agent_id for session in sessions}
        subnet.edges = self._build_edges(task, sessions)
        subnet.sessions = sessions
        subnet.involved_gateways = _involved_gateways(sessions)
        subnet.gateway_routes = self._build_gateway_route_tables(task, sessions, app_cards)
        subnet.gateway_acks = await self._install_on_gateways(
            task,
            subnet.involved_gateways,
            sessions,
            subnet.gateway_routes,
        )
        subnet.state = TaskState.NETWORKED if all(ack.accepted for ack in subnet.gateway_acks) else TaskState.FAILED
        self.tasks[task.task_id] = subnet

        elapsed_ms = (perf_counter() - start) * 1000
        metrics = ExperimentMetrics(
            networking_success=subnet.state == TaskState.NETWORKED,
            networking_latency_ms=elapsed_ms,
            session_count=len(sessions),
            involved_gateway_count=len(subnet.involved_gateways),
        )
        return subnet, metrics

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
        subnet.trans_agents = {s.t_agent_id for s in new_sessions}
        subnet.net_agents = {s.n_agent_id for s in new_sessions}
        subnet.edges = self._build_edges(task, new_sessions)
        subnet.involved_gateways |= affected_gateways | _involved_gateways(new_sessions)
        subnet.state = TaskState.NETWORKED if not unresolved else TaskState.DEGRADED
        app_cards = self._current_app_cards(task)
        subnet.gateway_routes = self._build_gateway_route_tables(task, new_sessions, app_cards)
        await self._apply_sessions(subnet, tuple(s for s in new_sessions if s.status == "rehomed"))

        predictions = await self.run_agent_loop(subnet, timestamp + 0.2)
        risk_after = self.risk_calculator.risk(task, predictions)

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
        """Full-rebuild baseline: tear down the whole subnet and rebuild it.

        Unlike the minimal adjustment, this really re-confirms every member,
        re-installs every gateway and re-establishes every session. The returned
        cost reflects that full work (computed with the same shared cost model),
        so it is a fair, executed baseline rather than a hand-picked number.
        """
        task = subnet.task

        # 1. Real teardown of the entire existing subnet on every involved gateway.
        for gateway_id in subnet.involved_gateways:
            gateway = self.gateways[gateway_id]
            gateway.update_count += 1
            for session in subnet.sessions:
                gateway.installed_sessions.pop(session.session_id, None)
                for key, entry in list(gateway.route_table.items()):
                    if entry.session_id == session.session_id:
                        gateway.route_table.pop(key)

        # 2. Real rebuild from scratch (confirm members, select support, install).
        new_subnet, _ = await self.build_task_subnet(task)

        # 3. Re-evaluate risk on the freshly built subnet.
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
            changed_agents=len(new_subnet.app_agents | new_subnet.trans_agents | new_subnet.net_agents),
            changed_edges=len(new_subnet.edges),
            changed_gateways=len(new_subnet.involved_gateways),
            service_interruption_ms=self.cost_model.interruption_ms(operations),
            operations=operations,
        )
        return new_subnet, risk_after, result

    async def _confirm_app_members(self, task: TaskSpec) -> dict[str, AgentCard]:
        cards: dict[str, AgentCard] = {}
        for agent_id in task.app_agents:
            card = await self._find_agent(agent_id)
            if card is None or card.layer != AgentLayer.APPLICATION:
                raise ValueError(f"application agent unavailable: {agent_id}")
            cards[agent_id] = card
        return cards

    async def _select_support_agents(
        self,
        task: TaskSpec,
        app_cards: dict[str, AgentCard],
    ) -> dict[str, tuple[AgentCard, AgentCard]]:
        selected: dict[str, tuple[AgentCard, AgentCard]] = {}
        for edge in task.biz_edges:
            source = app_cards[edge.source]
            t_agent = await self._first_support("transport_session", preferred_gateway=source.gateway_id)
            n_agent = await self._first_support("network_bearer", preferred_gateway=source.gateway_id)
            if t_agent is None or n_agent is None:
                raise ValueError(f"support agents unavailable for edge {edge.source}->{edge.target}")
            selected[f"{edge.source}->{edge.target}"] = (t_agent, n_agent)
        return selected

    def _map_edges_to_sessions(
        self,
        task: TaskSpec,
        app_cards: dict[str, AgentCard],
        support_cards: dict[str, tuple[AgentCard, AgentCard]],
    ) -> list[SessionSpec]:
        sessions = []
        for index, edge in enumerate(task.biz_edges, start=1):
            source = app_cards[edge.source]
            target = app_cards[edge.target]
            t_agent, n_agent = support_cards[f"{edge.source}->{edge.target}"]
            gateway_path = self._gateway_path(source.gateway_id, target.gateway_id)
            sessions.append(
                SessionSpec(
                    session_id=f"{task.task_id}-sess-{index}",
                    task_id=task.task_id,
                    source=edge.source,
                    target=edge.target,
                    t_agent_id=t_agent.agent_id,
                    n_agent_id=n_agent.agent_id,
                    source_gateway=source.gateway_id,
                    target_gateway=target.gateway_id,
                    latency_budget_ms=edge.latency_budget_ms,
                    data_rate_mbps=edge.data_rate_mbps,
                    path_id="->".join(gateway_path),
                    gateway_path=gateway_path,
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
        return edges

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
            path_id=session.path_id,
            gateway_path=session.gateway_path,
            hop_index=hop_index,
            latency_budget_ms=session.latency_budget_ms,
            min_bandwidth_mbps=session.data_rate_mbps,
            status=session.status,
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

    async def _first_support(self, capability: str, preferred_gateway: str | None = None) -> AgentCard | None:
        gateway_order = []
        if preferred_gateway is not None:
            gateway_order.append(self.gateways[preferred_gateway])
        gateway_order.extend(gateway for key, gateway in self.gateways.items() if key != preferred_gateway)
        for gateway in gateway_order:
            cards = await gateway.confirm_support(capability)
            if cards:
                return cards[0]
        return None

    def _agents_for_subnet(self, subnet: TaskSubnet) -> list[BaseAgent]:
        ids = subnet.app_agents | subnet.trans_agents | subnet.net_agents
        agents: list[BaseAgent] = []
        for gateway in self.gateways.values():
            for agent_id in ids:
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


def _session_gateways(session: SessionSpec) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)


def _involved_gateways(sessions: list[SessionSpec]) -> set[str]:
    return {
        gateway_id
        for session in sessions
        for gateway_id in _session_gateways(session)
    }
