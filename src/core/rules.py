from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from src.core.models import GatewayRouteEntry, Rule


@dataclass(frozen=True)
class RuleDelta:
    additions: tuple[Rule, ...] = field(default_factory=tuple)
    updates: tuple[Rule, ...] = field(default_factory=tuple)
    deletions: tuple[Rule, ...] = field(default_factory=tuple)

    @property
    def changed_rule_ids(self) -> set[str]:
        return {
            rule.rule_id
            for rules in (self.additions, self.updates, self.deletions)
            for rule in rules
        }

    @property
    def gateway_ids(self) -> set[str]:
        return {
            rule.gateway_id
            for rules in (self.additions, self.updates, self.deletions)
            for rule in rules
        }

    def for_gateway(self, gateway_id: str) -> "RuleDelta":
        return RuleDelta(
            additions=tuple(rule for rule in self.additions if rule.gateway_id == gateway_id),
            updates=tuple(rule for rule in self.updates if rule.gateway_id == gateway_id),
            deletions=tuple(rule for rule in self.deletions if rule.gateway_id == gateway_id),
        )


def compute_rule_delta(
    old_rules: Iterable[Rule],
    new_rules: Iterable[Rule],
) -> RuleDelta:
    old_by_id = _index_rules(old_rules, "old")
    new_by_id = _index_rules(new_rules, "new")

    additions = tuple(
        new_by_id[rule_id]
        for rule_id in sorted(new_by_id.keys() - old_by_id.keys())
    )
    deletions = tuple(
        old_by_id[rule_id]
        for rule_id in sorted(old_by_id.keys() - new_by_id.keys())
    )
    updates = tuple(
        new_by_id[rule_id]
        for rule_id in sorted(old_by_id.keys() & new_by_id.keys())
        if rule_content_key(old_by_id[rule_id]) != rule_content_key(new_by_id[rule_id])
    )
    return RuleDelta(additions=additions, updates=updates, deletions=deletions)


def rule_content_key(rule: GatewayRouteEntry) -> tuple[object, ...]:
    """Stable content comparison; object identity and version are irrelevant."""

    return (
        rule.task_id,
        rule.rule_id,
        rule.gateway_id,
        rule.src_agent,
        rule.dst_agent,
        rule.next_hop,
        rule.route_id,
        rule.priority,
        rule.allowed,
        rule.action.mode,
        rule.action.next_hop_gateway_ip,
        rule.action.local_agent_ip,
        rule.action.local_agent_port,
        rule.match.flow_type,
        rule.match.protocol,
        rule.match.dst_port,
        rule.latency_budget_ms,
        rule.min_bandwidth_mbps,
        rule.t_agent_id,
        rule.n_agent_id,
        rule.p_agent_ids,
    )


def _index_rules(rules: Iterable[Rule], label: str) -> dict[str, Rule]:
    indexed: dict[str, Rule] = {}
    for rule in rules:
        if rule.rule_id in indexed:
            raise ValueError(f"duplicate {label} rule_id: {rule.rule_id}")
        indexed[rule.rule_id] = rule
    return indexed
