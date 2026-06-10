from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from gateway import NodeGateway
from models import (
    AgentCard,
    AgentRole,
    GatewayRule,
    Intent,
    LayerType,
    Message,
    MessageType,
    Metrics,
    RuleDelta,
    SubnetState,
    TaskCommunicationGraph,
)


@dataclass
class TaskSubnet:
    task_id: str
    intent: Intent
    graph: TaskCommunicationGraph
    members: dict[str, AgentCard]
    rules: list[GatewayRule]
    state: SubnetState = SubnetState.CREATED
    latest_state: dict[str, dict] = field(default_factory=dict)


class SubnetController:
    def __init__(self, gateways: dict[str, NodeGateway], controller_id: str = "subnet-controller"):
        self.gateways = gateways
        self.controller_id = controller_id
        self.tasks: dict[str, TaskSubnet] = {}
        self.metrics = Metrics()

    def build_task_subnet(self, intent: Intent) -> TaskSubnet:
        print(f"[1] 组网意图输入 task_id={intent.task_id}")
        print(f"    业务目标: {intent.semantic_goal}")
        start = perf_counter()

        business_cards = self._confirm_business_agents(intent)
        support_cards = self._bind_support_agents(intent, business_cards)
        graph = self._build_graph(intent, support_cards)
        members = {card.agent_id: card for card in [*business_cards, *support_cards]}
        rules = self._generate_rules(intent, members, support_cards)

        subnet = TaskSubnet(
            task_id=intent.task_id,
            intent=intent,
            graph=graph,
            members=members,
            rules=rules,
        )
        self.tasks[intent.task_id] = subnet

        ready_gateways = self._dispatch_rules(intent.task_id, rules)
        subnet.state = SubnetState.READY
        elapsed_ms = (perf_counter() - start) * 1000
        self.metrics.build_time_ms = elapsed_ms
        self.metrics.total_rules = len(rules)
        self.metrics.involved_gateways = len(ready_gateways)

        print(f"[6] READY 确认: {sorted(ready_gateways)} -> 子网状态={subnet.state.value}")
        print(f"    建网耗时: {elapsed_ms:.2f} ms")
        return subnet

    def handle_state_message(self, message: Message) -> RuleDelta | None:
        subnet = self.tasks[message.task_id]
        subnet.latest_state[message.source] = message.payload
        print(f"[状态反馈] {message.source} -> {message.payload}")

        if message.payload.get("layer") != LayerType.NETWORK.value:
            return None
        if message.payload.get("path_state") != "congested":
            return None

        return self._handle_network_congestion(subnet, message)

    def _confirm_business_agents(self, intent: Intent) -> list[AgentCard]:
        print("[2] 业务 Agent 确认")
        cards: list[AgentCard] = []
        for agent_id in intent.business_agents:
            card = self._query_agent_from_any_gateway(agent_id)
            if card is None:
                raise ValueError(f"业务 Agent 不存在或不可访问: {agent_id}")
            if card.agent_role != AgentRole.BUSINESS:
                raise ValueError(f"组网意图中的业务 Agent 角色不正确: {agent_id}")
            print(f"    OK {card.agent_id} node={card.node} gateway={card.gateway_id}")
            cards.append(card)
        return cards

    def _bind_support_agents(
        self,
        intent: Intent,
        business_cards: list[AgentCard],
    ) -> list[AgentCard]:
        print("[3] pAgent/nAgent 按需绑定")
        selected: dict[str, AgentCard] = {}

        if intent.support_requirements.physical:
            for card in business_cards:
                if "ue" in card.node or "drone" in card.node:
                    p_card = self._find_support_agent(
                        layer=LayerType.PHYSICAL,
                        gateway_id=card.gateway_id,
                    )
                    if p_card is not None and p_card.agent_id not in selected:
                        selected[p_card.agent_id] = p_card
                        print(f"    绑定 pAgent {p_card.agent_id} 支撑 {card.agent_id}")

        if intent.support_requirements.network:
            for flow in intent.business_graph:
                source = self._card_for(flow.source, business_cards)
                target = self._card_for(flow.target, business_cards)
                if source.gateway_id != target.gateway_id:
                    n_card = self._find_support_agent(layer=LayerType.NETWORK)
                    if n_card is not None and n_card.agent_id not in selected:
                        selected[n_card.agent_id] = n_card
                        print(
                            f"    绑定 nAgent {n_card.agent_id} 支撑 {flow.source}->{flow.target}"
                        )

        return list(selected.values())

    def _build_graph(
        self,
        intent: Intent,
        support_cards: list[AgentCard],
    ) -> TaskCommunicationGraph:
        print("[4] 任务通信图生成")
        graph = TaskCommunicationGraph()
        graph.business_agents = set(intent.business_agents)
        graph.support_agents = {card.agent_id for card in support_cards}
        graph.business_edges = {(flow.source, flow.target) for flow in intent.business_graph}
        graph.support_edges = {
            (card.agent_id, self.controller_id)
            for card in support_cards
        }
        print(f"    A_b={sorted(graph.business_agents)}")
        print(f"    A_s={sorted(graph.support_agents)}")
        print(f"    E_b={sorted(graph.business_edges)}")
        print(f"    E_s={sorted(graph.support_edges)}")
        return graph

    def _generate_rules(
        self,
        intent: Intent,
        members: dict[str, AgentCard],
        support_cards: list[AgentCard],
    ) -> list[GatewayRule]:
        print("[5] 网关规则生成与下发")
        rules: list[GatewayRule] = []
        for flow in intent.business_graph:
            source = members[flow.source]
            target = members[flow.target]
            relation = "local" if source.gateway_id == target.gateway_id else "remote"
            rules.append(
                GatewayRule(
                    task_id=intent.task_id,
                    source=flow.source,
                    target=flow.target,
                    source_gateway=source.gateway_id,
                    target_gateway=target.gateway_id,
                    relation=relation,
                    message_type=MessageType.BUSINESS,
                    priority=flow.priority,
                )
            )

        for card in support_cards:
            rules.append(
                GatewayRule(
                    task_id=intent.task_id,
                    source=card.agent_id,
                    target=self.controller_id,
                    source_gateway=card.gateway_id,
                    target_gateway="controller",
                    relation="state-report",
                    message_type=MessageType.STATE,
                    priority=intent.qos_requirements.priority,
                )
            )

        for rule in rules:
            print(
                f"    rule {rule.source}->{rule.target} type={rule.message_type.value} "
                f"relation={rule.relation} gw={rule.source_gateway}->{rule.target_gateway}"
            )
        return rules

    def _dispatch_rules(self, task_id: str, rules: list[GatewayRule]) -> set[str]:
        gateway_rules: dict[str, list[GatewayRule]] = {}
        for rule in rules:
            gateway_rules.setdefault(rule.source_gateway, []).append(rule)
            if rule.target_gateway not in {"controller", rule.source_gateway}:
                gateway_rules.setdefault(rule.target_gateway, []).append(rule)

        ready_gateways: set[str] = set()
        for gateway_id, items in gateway_rules.items():
            status = self.gateways[gateway_id].load_rules(items)
            if status != "READY":
                raise RuntimeError(f"网关规则加载失败: {gateway_id}")
            ready_gateways.add(gateway_id)
            print(f"    {gateway_id} 加载 {len(items)} 条规则 -> {status}")
        return ready_gateways

    def _handle_network_congestion(self, subnet: TaskSubnet, message: Message) -> RuleDelta:
        print("[承载冲突] nAgent 上报路径拥塞，开始影响范围确定与最小改动选择")
        start = perf_counter()
        affected_rules = [
            rule
            for rule in subnet.rules
            if rule.message_type == MessageType.BUSINESS and rule.relation == "remote"
        ]
        updated_rules = tuple(
            GatewayRule(
                task_id=rule.task_id,
                source=rule.source,
                target=rule.target,
                source_gateway=rule.source_gateway,
                target_gateway=rule.target_gateway,
                relation=rule.relation,
                message_type=rule.message_type,
                priority=rule.priority + 1,
                action=rule.action,
                policy="compress-and-prioritize",
            )
            for rule in affected_rules
        )
        delta = RuleDelta(
            task_id=subnet.task_id,
            reason=f"{message.source} reported network congestion",
            update_rules=updated_rules,
        )
        involved_gateways = self._apply_delta_to_affected_gateways(delta)
        subnet.rules = [
            next((updated for updated in updated_rules if updated.key == rule.key), rule)
            for rule in subnet.rules
        ]
        elapsed_ms = (perf_counter() - start) * 1000
        self.metrics.delta_rules = delta.changed_rule_count
        self.metrics.delta_update_time_ms = elapsed_ms
        print(
            f"[增量更新] rule_delta={delta.changed_rule_count} "
            f"affected_gateways={sorted(involved_gateways)} 耗时={elapsed_ms:.2f} ms"
        )
        return delta

    def _apply_delta_to_affected_gateways(self, delta: RuleDelta) -> set[str]:
        involved: set[str] = set()
        for rule in [*delta.add_rules, *delta.update_rules]:
            involved.add(rule.source_gateway)
            if rule.target_gateway != "controller":
                involved.add(rule.target_gateway)

        for gateway_id in sorted(involved):
            gateway_delta = self._delta_for_gateway(delta, gateway_id)
            status = self.gateways[gateway_id].apply_delta(gateway_delta)
            if status != "READY-UPDATE":
                raise RuntimeError(f"网关增量更新失败: {gateway_id}")
            print(f"    {gateway_id} 加载 rule_delta -> {status}")
        return involved

    @staticmethod
    def _delta_for_gateway(delta: RuleDelta, gateway_id: str) -> RuleDelta:
        def touches_gateway(rule: GatewayRule) -> bool:
            return gateway_id in {rule.source_gateway, rule.target_gateway}

        return RuleDelta(
            task_id=delta.task_id,
            reason=delta.reason,
            add_rules=tuple(rule for rule in delta.add_rules if touches_gateway(rule)),
            remove_rule_keys=delta.remove_rule_keys,
            update_rules=tuple(rule for rule in delta.update_rules if touches_gateway(rule)),
        )

    def _query_agent_from_any_gateway(self, agent_id: str) -> AgentCard | None:
        for gateway in self.gateways.values():
            card = gateway.query_agent(agent_id)
            if card is not None:
                return card
        return None

    def _find_support_agent(
        self,
        layer: LayerType,
        gateway_id: str | None = None,
    ) -> AgentCard | None:
        gateways = (
            [self.gateways[gateway_id]]
            if gateway_id is not None
            else list(self.gateways.values())
        )
        for gateway in gateways:
            for card in gateway.support_agents():
                if card.layer_type == layer:
                    return card
        return None

    @staticmethod
    def _card_for(agent_id: str, cards: list[AgentCard]) -> AgentCard:
        for card in cards:
            if card.agent_id == agent_id:
                return card
        raise ValueError(f"未找到 AgentCard: {agent_id}")
