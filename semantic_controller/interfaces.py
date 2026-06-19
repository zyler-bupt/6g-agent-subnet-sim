from __future__ import annotations

from typing import Protocol

from semantic_controller.schemas import GoalSpec, PlanSpec, SemanticContext


class GoalRecognizer(Protocol):
    def recognize(self, context: SemanticContext) -> GoalSpec:
        """Convert semantic context into a constrained GoalSpec."""


class TaskPlanner(Protocol):
    def build_plan(self, goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
        """Convert a GoalSpec into a validated executable plan."""

