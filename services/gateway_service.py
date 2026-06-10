from __future__ import annotations

import os
import random
import time
from urllib.parse import urlparse

from models import (
    GatewayRule,
    MessageType,
    agent_card_from_dict,
    gateway_rule_from_dict,
    message_from_dict,
    rule_delta_from_dict,
    to_jsonable,
)
from services.common import JsonHandler, env_json, http_json, run_server


class GatewayService:
    def __init__(self) -> None:
        self.gateway_id = os.environ["GATEWAY_ID"]
        self.node = os.environ["NODE"]
        self.local_agents: dict[str, str] = env_json("LOCAL_AGENTS", {})
        self.gateway_urls: dict[str, str] = env_json("GATEWAY_URLS", {})
        self.rules: dict[tuple[str, str, str, MessageType], GatewayRule] = {}
        self.metrics = {
            "forward_success_count": 0,
            "blocked_flow_count": 0,
            "drop_count": 0,
            "business_latency_ms_total": 0.0,
            "business_latency_samples": 0,
            "throughput_kbytes": 0.0,
            "loaded_rule_count": 0,
            "delta_update_count": 0,
        }

    def handle(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        parsed = urlparse(path)
        parts = [part for part in parsed.path.split("/") if part]

        if method == "GET" and parsed.path == "/health":
            return 200, {"status": "ok", "gateway_id": self.gateway_id}
        if method == "GET" and len(parts) == 3 and parts[:2] == ["agents", parts[1]] and parts[2] == "card":
            return self._agent_card(parts[1])
        if method == "GET" and parsed.path == "/support-agents":
            return self._support_agents()
        if method == "POST" and parsed.path == "/rules":
            return self._load_rules(payload)
        if method == "POST" and parsed.path == "/rules/delta":
            return self._apply_delta(payload)
        if method == "POST" and parsed.path == "/messages/business":
            return self._deliver_business(payload)
        if method == "GET" and parsed.path == "/metrics":
            return 200, self._metrics()
        return 404, {"error": "not_found", "path": parsed.path}

    def _agent_card(self, agent_id: str) -> tuple[int, dict]:
        url = self.local_agents.get(agent_id)
        if url is None:
            return 404, {"error": "agent_not_found", "agent_id": agent_id}
        status, body = http_json("GET", f"{url}/card")
        if status != 200:
            return status, body
        card = agent_card_from_dict(body["agent_card"])
        if card.status != "available":
            return 409, {"error": "agent_unavailable", "agent_id": agent_id, "status": card.status}
        return 200, {"agent_card": to_jsonable(card)}

    def _support_agents(self) -> tuple[int, dict]:
        cards = []
        for agent_id in self.local_agents:
            status, body = self._agent_card(agent_id)
            if status == 200:
                card = agent_card_from_dict(body["agent_card"])
                if card.agent_role.value == "support":
                    cards.append(to_jsonable(card))
        return 200, {"agents": cards}

    def _load_rules(self, payload: dict) -> tuple[int, dict]:
        rules = [gateway_rule_from_dict(item) for item in payload.get("rules", [])]
        for rule in rules:
            self.rules[rule.key] = rule
        self.metrics["loaded_rule_count"] += len(rules)
        return 200, {"status": "READY", "gateway_id": self.gateway_id, "loaded_rules": len(rules)}

    def _apply_delta(self, payload: dict) -> tuple[int, dict]:
        delta = rule_delta_from_dict(payload["delta"])
        removed = 0
        for key in delta.remove_rule_keys:
            if self.rules.pop(key, None) is not None:
                removed += 1
        for rule in [*delta.add_rules, *delta.update_rules]:
            self.rules[rule.key] = rule
        changed = removed + len(delta.add_rules) + len(delta.update_rules)
        self.metrics["delta_update_count"] += changed
        return 200, {
            "status": "READY-UPDATE",
            "gateway_id": self.gateway_id,
            "changed_rules": changed,
        }

    def _deliver_business(self, payload: dict) -> tuple[int, dict]:
        message = message_from_dict(payload["message"])
        internal_forward = payload.get("internal_forward", False)
        key = (message.task_id, message.source, message.target, MessageType.BUSINESS)
        rule = self.rules.get(key)
        if rule is None or rule.action != "allow":
            self.metrics["blocked_flow_count"] += 1
            return 403, {"accepted": False, "reason": "business flow blocked by allowed_flows"}

        start = time.perf_counter()
        if not internal_forward and rule.source_gateway == self.gateway_id:
            dropped = self._apply_link_model(rule, message.payload)
            if dropped:
                self.metrics["drop_count"] += 1
                return 503, {"accepted": False, "reason": "dropped by link model", "policy": rule.policy}

        if rule.target_gateway != self.gateway_id and not internal_forward:
            target_url = self.gateway_urls[rule.target_gateway]
            status, body = http_json(
                "POST",
                f"{target_url}/messages/business",
                {"message": to_jsonable(message), "internal_forward": True},
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._record_success(elapsed_ms, message.payload)
            body.update({"source_gateway": self.gateway_id, "latency_ms": elapsed_ms})
            return status, body

        target_url = self.local_agents.get(message.target)
        if target_url is None:
            return 502, {"accepted": False, "reason": "target agent is not local to gateway"}
        status, body = http_json("POST", f"{target_url}/messages/business", to_jsonable(message))
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._record_success(elapsed_ms, message.payload)
        body.update({
            "gateway_id": self.gateway_id,
            "latency_ms": elapsed_ms,
            "policy": rule.policy,
            "priority": rule.priority,
        })
        return status, body

    def _apply_link_model(self, rule: GatewayRule, payload: dict) -> bool:
        if rule.drop_rate > 0 and random.random() < rule.drop_rate:
            return True
        size_kbytes = float(payload.get("size_kbytes", 1.0))
        transfer_ms = (size_kbytes * 8 / max(rule.bandwidth_mbps, 1)) * 1000 / 1024
        delay_ms = rule.latency_ms + transfer_ms
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        return False

    def _record_success(self, latency_ms: float, payload: dict) -> None:
        self.metrics["forward_success_count"] += 1
        self.metrics["business_latency_ms_total"] += latency_ms
        self.metrics["business_latency_samples"] += 1
        self.metrics["throughput_kbytes"] += float(payload.get("size_kbytes", 1.0))

    def _metrics(self) -> dict:
        samples = self.metrics["business_latency_samples"]
        avg_latency = self.metrics["business_latency_ms_total"] / samples if samples else 0.0
        return {
            "gateway_id": self.gateway_id,
            **self.metrics,
            "business_latency_ms": avg_latency,
            "active_rule_count": len(self.rules),
        }


def main() -> None:
    service = GatewayService()
    JsonHandler.routes_owner = service
    run_server(JsonHandler, int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

