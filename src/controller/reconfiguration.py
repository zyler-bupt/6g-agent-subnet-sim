from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.controller.impact import ImpactScope
from src.core.events import RuntimeEvent
from src.core.models import TaskSubnet
from src.core.rules import RuleDelta


@dataclass(frozen=True)
class ReconfigurationPlan:
    """Strategy output consumed by the shared transaction executor.

    ``affected_gateways`` reports the rule-changing scope used by experiments.
    ``transaction_gateways`` may additionally contain zero-delta participants
    needed for a task-wide version barrier.
    """

    method: str
    target_state: TaskSubnet
    impact_scope: ImpactScope
    rule_delta_by_gateway: dict[str, RuleDelta]
    affected_gateways: frozenset[str]
    affected_layers: frozenset[str]
    transaction_gateways: frozenset[str]
    verification_gateways: frozenset[str]
    full_rule_install: bool = False
    planning_details: dict[str, object] = field(default_factory=dict)

    @property
    def rule_delta(self) -> RuleDelta:
        additions = []
        updates = []
        deletions = []
        for gateway_id in sorted(self.rule_delta_by_gateway):
            delta = self.rule_delta_by_gateway[gateway_id]
            additions.extend(delta.additions)
            updates.extend(delta.updates)
            deletions.extend(delta.deletions)
        return RuleDelta(
            additions=tuple(additions),
            updates=tuple(updates),
            deletions=tuple(deletions),
        )


class ElasticUpdateStrategy(Protocol):
    method: str

    async def plan(
        self,
        stable_state: TaskSubnet,
        event: RuntimeEvent,
    ) -> ReconfigurationPlan:
        ...


def delta_by_gateway(
    delta: RuleDelta,
    gateway_ids: set[str] | frozenset[str],
) -> dict[str, RuleDelta]:
    return {
        gateway_id: delta.for_gateway(gateway_id)
        for gateway_id in sorted(gateway_ids)
    }
