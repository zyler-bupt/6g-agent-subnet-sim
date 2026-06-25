from __future__ import annotations

import asyncio
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
    GatewayAck,
    SessionSpec,
    TaskSpec,
    TaskState,
    TaskSubnet,
)


class AgentController:
    def __init__(
        self,
        gateways: dict[str, Gateway],
        risk_calculator: RiskCalculator | None = None,
        adjuster: ElasticAdjuster | None = None,
        cost_model: CostModel | None = None,
    ) -> None:
        self.gateways = gateways
        self.cost_model = cost_model or CostModel()
        self.risk_calculator = risk_calculator or RiskCalculator()
        self.adjuster = adjuster or ElasticAdjuster(self.cost_model)
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
        subnet.involved_gateways = {
            gateway for session in sessions for gateway in (session.source_gateway, session.target_gateway)
        }
        subnet.gateway_acks = await self._install_on_gateways(task, subnet.involved_gateways, sessions)
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

    async def evaluate_and_adjust(self, subnet: TaskSubnet, timestamp: float) -> tuple[float, float, AdjustmentResult]:
        predictions = await self.run_agent_loop(subnet, timestamp)
        risk_before = self.risk_calculator.risk(subnet.task, predictions)
        local_predictions = await self.run_agent_loop(subnet, timestamp + 0.1)
        risk_after = self.risk_calculator.risk(subnet.task, local_predictions)
        if risk_before <= subnet.task.qos.risk_threshold:
            result = AdjustmentResult(
                strategy="no_adjustment",
                actions=tuple(subnet.actions),
                changed_sessions=(),
                changed_agents=0,
                changed_edges=0,
                changed_gateways=0,
                service_interruption_ms=0.0,
            )
            return risk_before, risk_after, result

        result = self.adjuster.choose_minimal_adjustment(subnet, risk_before, risk_after)
        if result.changed_sessions:
            affected_gateways = {
                gateway_id
                for session in result.changed_sessions
                for gateway_id in (session.source_gateway, session.target_gateway)
            }
            await asyncio.gather(
                *[
                    self.gateways[gateway_id].apply_update(subnet.task.task_id, list(result.changed_sessions))
                    for gateway_id in affected_gateways
                ]
            )
            subnet.sessions = list(result.changed_sessions)
        return risk_before, risk_after, result

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
    ) -> list[GatewayAck]:
        return list(
            await asyncio.gather(
                *[self.gateways[gateway_id].install_subnet(task, sessions) for gateway_id in sorted(gateway_ids)]
            )
        )

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

