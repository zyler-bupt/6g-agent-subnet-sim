from __future__ import annotations

from dataclasses import dataclass, field

from agents import SimulatedAgent
from models import AgentCard, GatewayRule, Message, MessageType, RuleDelta


@dataclass
class DeliveryResult:
    accepted: bool
    reason: str
    rule: GatewayRule | None = None


@dataclass
class NodeGateway:
    gateway_id: str
    node: str
    agents: dict[str, SimulatedAgent] = field(default_factory=dict)
    rules: dict[tuple[str, str, str, MessageType], GatewayRule] = field(default_factory=dict)
    blocked_count: int = 0

    def register_agent(self, agent: SimulatedAgent) -> None:
        self.agents[agent.agent_id] = agent

    def query_agent(self, agent_id: str) -> AgentCard | None:
        agent = self.agents.get(agent_id)
        if agent is None or agent.card.status != "available":
            return None
        return agent.card

    def support_agents(self) -> list[AgentCard]:
        return [agent.card for agent in self.agents.values() if agent.card.agent_role.value == "support"]

    def load_rules(self, rules: list[GatewayRule]) -> str:
        for rule in rules:
            self.rules[rule.key] = rule
        return "READY"

    def apply_delta(self, delta: RuleDelta) -> str:
        for key in delta.remove_rule_keys:
            self.rules.pop(key, None)
        for rule in delta.add_rules:
            self.rules[rule.key] = rule
        for rule in delta.update_rules:
            self.rules[rule.key] = rule
        return "READY-UPDATE"

    def deliver(self, message: Message) -> DeliveryResult:
        if message.message_type != MessageType.BUSINESS:
            return DeliveryResult(True, "状态/控制消息不走业务白名单")

        rule = self.rules.get(
            (message.task_id, message.source, message.target, message.message_type)
        )
        if rule is None or rule.action != "allow":
            self.blocked_count += 1
            return DeliveryResult(False, "业务流不在 allowed_flows 白名单中")

        return DeliveryResult(
            True,
            f"{rule.relation} 转发成功，策略={rule.policy}，优先级={rule.priority}",
            rule,
        )

