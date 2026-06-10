from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from models import (
    AgentCard,
    AgentRole,
    GatewayRule,
    Intent,
    LayerType,
    MessageType,
    RuleDelta,
    SubnetState,
    agent_card_from_dict,
    gateway_rule_from_dict,
    intent_from_dict,
    message_from_dict,
    to_jsonable,
)
from services.common import JsonHandler, env_json, http_json, run_server


class ControllerService:
    def __init__(self) -> None:
        self.controller_id = os.environ.get("CONTROLLER_ID", "subnet-controller")
        self.gateway_urls: dict[str, str] = env_json("GATEWAY_URLS", {})
        self.tasks: dict[str, dict] = {}
        self.metrics: dict[str, float | int | str] = {
            "experiment_mode": "docker-http-simulation",
            "build_time_ms": 0.0,
            "agent_confirm_time_ms": 0.0,
            "rule_dispatch_time_ms": 0.0,
            "ready_gateway_count": 0,
            "rule_count": 0,
            "state_detection_time_ms": 0.0,
            "delta_generation_time_ms": 0.0,
            "delta_dispatch_time_ms": 0.0,
            "affected_gateway_count": 0,
            "updated_rule_count": 0,
            "service_interruption_ms": 0.0,
            "control_message_count": 0,
        }

    def handle(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/health":
            return 200, {"status": "ok", "controller_id": self.controller_id}
        if method == "POST" and parsed.path == "/tasks":
            return self._create_task(payload)
        if method == "POST" and parsed.path == "/messages/state":
            return self._handle_state(payload)
        if method == "POST" and parsed.path == "/events/agent-failure":
            return self._handle_agent_failure(payload)
        if method == "GET" and parsed.path == "/metrics":
            return 200, self.metrics
        if method == "GET" and parsed.path.startswith("/tasks/"):
            task_id = parsed.path.split("/")[-1]
            task = self.tasks.get(task_id)
            if task is None:
                return 404, {"error": "task_not_found", "task_id": task_id}
            return 200, to_jsonable(task)
        return 404, {"error": "not_found", "path": parsed.path}

    def _create_task(self, payload: dict) -> tuple[int, dict]:
        intent = intent_from_dict(payload["intent"])
        start = time.perf_counter()
        confirm_start = time.perf_counter()
        business_cards = self._confirm_business_agents(intent)
        self.metrics["agent_confirm_time_ms"] = (time.perf_counter() - confirm_start) * 1000

        support_cards = self._bind_support_agents(intent, business_cards)
        rules = self._generate_rules(intent, {card.agent_id: card for card in [*business_cards, *support_cards]})

        dispatch_start = time.perf_counter()
        ready_gateways = self._dispatch_rules(rules)
        self.metrics["rule_dispatch_time_ms"] = (time.perf_counter() - dispatch_start) * 1000
        self.metrics["build_time_ms"] = (time.perf_counter() - start) * 1000
        self.metrics["ready_gateway_count"] = len(ready_gateways)
        self.metrics["rule_count"] = len(rules)

        task = {
            "task_id": intent.task_id,
            "state": SubnetState.READY.value,
            "intent": to_jsonable(intent),
            "members": {card.agent_id: to_jsonable(card) for card in [*business_cards, *support_cards]},
            "rules": [to_jsonable(rule) for rule in rules],
            "latest_state": {},
            "ready_gateways": sorted(ready_gateways),
        }
        self.tasks[intent.task_id] = task
        return 200, {
            "status": "READY",
            "task_id": intent.task_id,
            "ready_gateways": sorted(ready_gateways),
            "metrics": self.metrics,
        }

    def _confirm_business_agents(self, intent: Intent) -> list[AgentCard]:
        cards = []
        for agent_id in intent.business_agents:
            card = self._query_agent_from_gateways(agent_id)
            if card is None:
                raise ValueError(f"business agent unavailable: {agent_id}")
            if card.agent_role != AgentRole.BUSINESS:
                raise ValueError(f"agent role mismatch: {agent_id}")
            cards.append(card)
        return cards

    def _bind_support_agents(self, intent: Intent, business_cards: list[AgentCard]) -> list[AgentCard]:
        selected: dict[str, AgentCard] = {}
        if intent.support_requirements.physical:
            for card in business_cards:
                if "ue" in card.node or "drone" in card.node:
                    support = self._find_support_agent(LayerType.PHYSICAL, card.gateway_id)
                    if support is not None:
                        selected[support.agent_id] = support
        if intent.support_requirements.network:
            for flow in intent.business_graph:
                source = self._card_for(flow.source, business_cards)
                target = self._card_for(flow.target, business_cards)
                if source.gateway_id != target.gateway_id:
                    support = self._find_support_agent(LayerType.NETWORK)
                    if support is not None:
                        selected[support.agent_id] = support
        return list(selected.values())

    def _generate_rules(self, intent: Intent, members: dict[str, AgentCard]) -> list[GatewayRule]:
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
                    flow_type=flow.flow_type,
                    latency_ms=8 if relation == "remote" else 2,
                    bandwidth_mbps=flow.bandwidth_mbps,
                )
            )

        for card in members.values():
            if card.agent_role == AgentRole.SUPPORT:
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
                        flow_type="state_report",
                    )
                )
        return rules

    def _dispatch_rules(self, rules: list[GatewayRule]) -> set[str]:
        gateway_rules: dict[str, list[GatewayRule]] = {}
        for rule in rules:
            gateway_rules.setdefault(rule.source_gateway, []).append(rule)
            if rule.target_gateway not in {"controller", rule.source_gateway}:
                gateway_rules.setdefault(rule.target_gateway, []).append(rule)

        ready = set()
        for gateway_id, items in gateway_rules.items():
            status, body = http_json(
                "POST",
                f"{self.gateway_urls[gateway_id]}/rules",
                {"rules": [to_jsonable(rule) for rule in items]},
            )
            self.metrics["control_message_count"] = int(self.metrics["control_message_count"]) + 1
            if status != 200 or body.get("status") != "READY":
                raise RuntimeError(f"gateway rule dispatch failed: {gateway_id} {body}")
            ready.add(gateway_id)
        return ready

    def _handle_state(self, payload: dict) -> tuple[int, dict]:
        detection_start = time.perf_counter()
        message = message_from_dict(payload["message"])
        task = self.tasks[message.task_id]
        task["latest_state"][message.source] = message.payload
        self.metrics["state_detection_time_ms"] = (time.perf_counter() - detection_start) * 1000

        if message.payload.get("layer") == LayerType.NETWORK.value and message.payload.get("path_state") == "congested":
            delta = self._network_congestion_delta(task, message.source)
        elif message.payload.get("layer") == LayerType.PHYSICAL.value and message.payload.get("link_quality") in {"poor", "down"}:
            delta = self._physical_degradation_delta(task, message.source)
        else:
            return 200, {"adjusted": False, "reason": "state does not trigger adjustment"}

        affected_gateways = self._dispatch_delta(delta)
        self._apply_delta_to_task(task, delta)
        return 200, {
            "adjusted": True,
            "delta": to_jsonable(delta),
            "affected_gateways": sorted(affected_gateways),
            "metrics": self.metrics,
        }

    def _network_congestion_delta(self, task: dict, source: str) -> RuleDelta:
        start = time.perf_counter()
        rules = [gateway_rule_from_dict(item) for item in task["rules"]]
        updates = tuple(
            GatewayRule(
                **{
                    **to_jsonable(rule),
                    "message_type": rule.message_type,
                    "priority": rule.priority + 1,
                    "policy": "compress-and-prioritize",
                    "latency_ms": rule.latency_ms + 20,
                    "bandwidth_mbps": max(1, min(rule.bandwidth_mbps, 8)),
                    "congestion_level": 0.87,
                }
            )
            for rule in rules
            if rule.message_type == MessageType.BUSINESS and rule.relation == "remote"
        )
        self.metrics["delta_generation_time_ms"] = (time.perf_counter() - start) * 1000
        return RuleDelta(task_id=task["task_id"], reason=f"{source} reported network congestion", update_rules=updates)

    def _physical_degradation_delta(self, task: dict, source: str) -> RuleDelta:
        start = time.perf_counter()
        rules = [gateway_rule_from_dict(item) for item in task["rules"]]
        updates = tuple(
            GatewayRule(
                **{
                    **to_jsonable(rule),
                    "message_type": rule.message_type,
                    "policy": "degrade-video-quality",
                    "bandwidth_mbps": max(1, min(rule.bandwidth_mbps, 6)),
                    "latency_ms": rule.latency_ms + 35,
                    "drop_rate": 0.05,
                }
            )
            for rule in rules
            if rule.message_type == MessageType.BUSINESS and rule.flow_type == "video_stream"
        )
        self.metrics["delta_generation_time_ms"] = (time.perf_counter() - start) * 1000
        return RuleDelta(task_id=task["task_id"], reason=f"{source} reported physical link degradation", update_rules=updates)

    def _handle_agent_failure(self, payload: dict) -> tuple[int, dict]:
        task = self.tasks[payload["task_id"]]
        failed_agent = payload["agent_id"]
        start = time.perf_counter()
        remove_keys = []
        for item in task["rules"]:
            rule = gateway_rule_from_dict(item)
            if rule.message_type == MessageType.BUSINESS and failed_agent in {rule.source, rule.target}:
                remove_keys.append((rule.task_id, rule.source, rule.target, rule.message_type))
        delta = RuleDelta(
            task_id=task["task_id"],
            reason=f"{failed_agent} unavailable; remove affected business flows",
            remove_rule_keys=tuple(remove_keys),
        )
        self.metrics["delta_generation_time_ms"] = (time.perf_counter() - start) * 1000
        affected_gateways = self._dispatch_delta(delta)
        self._apply_delta_to_task(task, delta)
        self.metrics["service_interruption_ms"] = self.metrics["delta_dispatch_time_ms"]
        return 200, {
            "adjusted": True,
            "delta": to_jsonable(delta),
            "affected_gateways": sorted(affected_gateways),
            "metrics": self.metrics,
        }

    def _dispatch_delta(self, delta: RuleDelta) -> set[str]:
        start = time.perf_counter()
        affected = self._affected_gateways(delta)
        for gateway_id in affected:
            gateway_delta = self._delta_for_gateway(delta, gateway_id)
            status, body = http_json(
                "POST",
                f"{self.gateway_urls[gateway_id]}/rules/delta",
                {"delta": to_jsonable(gateway_delta)},
            )
            self.metrics["control_message_count"] = int(self.metrics["control_message_count"]) + 1
            if status != 200 or body.get("status") != "READY-UPDATE":
                raise RuntimeError(f"gateway delta dispatch failed: {gateway_id} {body}")
        self.metrics["delta_dispatch_time_ms"] = (time.perf_counter() - start) * 1000
        self.metrics["affected_gateway_count"] = len(affected)
        self.metrics["updated_rule_count"] = delta.changed_rule_count
        return affected

    def _affected_gateways(self, delta: RuleDelta) -> set[str]:
        affected = set()
        task_rules = {
            (item["task_id"], item["source"], item["target"], MessageType(item["message_type"])): gateway_rule_from_dict(item)
            for item in self.tasks[delta.task_id]["rules"]
        }
        for rule in [*delta.add_rules, *delta.update_rules]:
            affected.add(rule.source_gateway)
            if rule.target_gateway != "controller":
                affected.add(rule.target_gateway)
        for key in delta.remove_rule_keys:
            rule = task_rules.get(key)
            if rule is not None:
                affected.add(rule.source_gateway)
                if rule.target_gateway != "controller":
                    affected.add(rule.target_gateway)
        return affected

    def _delta_for_gateway(self, delta: RuleDelta, gateway_id: str) -> RuleDelta:
        task_rules = {
            (item["task_id"], item["source"], item["target"], MessageType(item["message_type"])): gateway_rule_from_dict(item)
            for item in self.tasks[delta.task_id]["rules"]
        }

        def touches_gateway(rule: GatewayRule) -> bool:
            return gateway_id in {rule.source_gateway, rule.target_gateway}

        remove_keys = []
        for key in delta.remove_rule_keys:
            rule = task_rules.get(key)
            if rule is not None and touches_gateway(rule):
                remove_keys.append(key)

        return RuleDelta(
            task_id=delta.task_id,
            reason=delta.reason,
            add_rules=tuple(rule for rule in delta.add_rules if touches_gateway(rule)),
            remove_rule_keys=tuple(remove_keys),
            update_rules=tuple(rule for rule in delta.update_rules if touches_gateway(rule)),
        )

    @staticmethod
    def _apply_delta_to_task(task: dict, delta: RuleDelta) -> None:
        rules = {
            (item["task_id"], item["source"], item["target"], MessageType(item["message_type"])): gateway_rule_from_dict(item)
            for item in task["rules"]
        }
        for key in delta.remove_rule_keys:
            rules.pop(key, None)
        for rule in [*delta.add_rules, *delta.update_rules]:
            rules[rule.key] = rule
        task["rules"] = [to_jsonable(rule) for rule in rules.values()]

    def _query_agent_from_gateways(self, agent_id: str) -> AgentCard | None:
        for gateway_url in self.gateway_urls.values():
            status, body = http_json("GET", f"{gateway_url}/agents/{agent_id}/card")
            self.metrics["control_message_count"] = int(self.metrics["control_message_count"]) + 1
            if status == 200:
                return agent_card_from_dict(body["agent_card"])
        return None

    def _find_support_agent(self, layer: LayerType, gateway_id: str | None = None) -> AgentCard | None:
        gateway_items = (
            [(gateway_id, self.gateway_urls[gateway_id])]
            if gateway_id is not None
            else list(self.gateway_urls.items())
        )
        for _, gateway_url in gateway_items:
            status, body = http_json("GET", f"{gateway_url}/support-agents")
            self.metrics["control_message_count"] = int(self.metrics["control_message_count"]) + 1
            if status != 200:
                continue
            for item in body.get("agents", []):
                card = agent_card_from_dict(item)
                if card.layer_type == layer:
                    return card
        return None

    @staticmethod
    def _card_for(agent_id: str, cards: list[AgentCard]) -> AgentCard:
        for card in cards:
            if card.agent_id == agent_id:
                return card
        raise ValueError(f"missing agent card: {agent_id}")


def main() -> None:
    service = ControllerService()
    JsonHandler.routes_owner = service
    run_server(JsonHandler, int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

