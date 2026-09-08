from __future__ import annotations

from typing import Protocol

from semantic_controller.schemas import (
    GoalCandidate,
    GoalSpec,
    PlanSpec,
    SemanticContext,
    SemanticEmbedding,
)


class GoalRecognizer(Protocol):
    def recognize(self, context: SemanticContext) -> GoalSpec:
        """Convert semantic context into a constrained GoalSpec."""


class EvidenceGoalRecognizer(Protocol):
    def recognize_with_evidence(
        self,
        context: SemanticContext,
    ) -> tuple[GoalSpec, SemanticEmbedding, list[GoalCandidate]]:
        """Recognize a goal and return embedding retrieval evidence."""


class TaskPlanner(Protocol):
    def build_plan(self, goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
        """Convert a GoalSpec into a validated executable plan."""
