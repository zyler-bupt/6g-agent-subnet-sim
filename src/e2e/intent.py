from __future__ import annotations

from typing import Any, Protocol

from src.core.models import TaskSpec
from src.e2e.models import SemanticIntent
from src.sim.scenarios import rescue_task


class IntentParser(Protocol):
    def parse(self, raw_user_intent: str) -> Any:
        ...


class TaskSpecMapper(Protocol):
    def map_task(self, semantic_result: Any) -> TaskSpec:
        ...


class RuleIntentParser:
    """Small deterministic intent parser for reproducible timing experiments."""

    _RESCUE_TERMS = ("救援", "无人机", "rescue", "emergency")

    def parse(self, raw_user_intent: str) -> SemanticIntent:
        text = raw_user_intent.strip()
        if not text:
            raise ValueError("user intent must not be empty")
        lowered = text.lower()
        if not any(term in lowered for term in self._RESCUE_TERMS):
            raise ValueError("rule semantic mode currently supports only the rescue scenario")
        return SemanticIntent(scenario="rescue", raw_text=text)


class ScenarioTaskSpecMapper:
    def map_task(self, semantic_result: Any) -> TaskSpec:
        if isinstance(semantic_result, TaskSpec):
            return semantic_result
        scenario = getattr(semantic_result, "scenario", None)
        if scenario == "rescue":
            return rescue_task()
        raise ValueError(f"unsupported semantic scenario: {scenario!r}")


def parse_with(parser: Any, raw_user_intent: str) -> Any:
    if hasattr(parser, "parse"):
        return parser.parse(raw_user_intent)
    if hasattr(parser, "handle_user_input"):
        return parser.handle_user_input(raw_user_intent)
    raise TypeError("semantic controller must provide parse() or handle_user_input()")
