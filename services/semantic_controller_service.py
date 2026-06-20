from __future__ import annotations

import os
from urllib.parse import urlparse

from semantic_controller.context import build_semantic_context
from semantic_controller.embedding import GoalPrototypeRetriever
from semantic_controller.evaluator import evaluate_goal
from semantic_controller.factory import build_semantic_controller_from_env
from semantic_controller.schemas import (
    ActionRecommendation,
    ApplicationState,
    FeasibilityResult,
    GoalID,
    GoalSpec,
    NetworkState,
    UserPreferences,
)
from semantic_controller.trigger import detect_semantic_trigger
from services.common import JsonHandler, run_server


class SemanticControllerService:
    def __init__(self) -> None:
        self.controller = build_semantic_controller_from_env()
        self.plans: dict[str, dict] = {}

    def handle(self, method: str, path: str, payload: dict) -> tuple[int, dict]:
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/health":
            return 200, {
                "status": "ok",
                "recognizer": type(self.controller.goal_recognizer).__name__,
                "planner": type(self.controller.task_planner).__name__,
            }
        if method == "POST" and parsed.path == "/semantic/trigger":
            context = _context_from_payload(payload)
            trigger = detect_semantic_trigger(context.raw_user_input, context.application)
            return 200, {
                "context": context.model_dump(mode="json"),
                "trigger": trigger.model_dump(mode="json"),
            }
        if method == "POST" and parsed.path == "/semantic/embed":
            context = _context_from_payload(payload)
            retriever = getattr(self.controller.goal_recognizer, "retriever", None)
            if retriever is None:
                retriever = GoalPrototypeRetriever()
            embedding, candidates = retriever.retrieve(context)
            return 200, {
                "semantic_embedding": embedding.model_dump(mode="json"),
                "goal_candidates": [item.model_dump(mode="json") for item in candidates],
            }
        if method == "POST" and parsed.path == "/controller/cognize":
            context = _context_from_payload(payload)
            goal, embedding, candidates = self.controller.recognize_context(context)
            return 200, {
                "goal": goal.model_dump(mode="json"),
                "semantic_embedding": (
                    embedding.model_dump(mode="json") if embedding is not None else None
                ),
                "goal_candidates": [item.model_dump(mode="json") for item in candidates],
            }
        if method == "POST" and parsed.path == "/controller/plan":
            goal = GoalSpec.model_validate(payload["goal"])
            plan = self.controller.task_planner.build_plan(
                goal,
                plan_id=payload.get("plan_id"),
            )
            self.plans[plan.plan_id] = plan.model_dump(mode="json")
            return 200, {"plan": plan.model_dump(mode="json")}
        if method == "POST" and parsed.path == "/controller/execute":
            result = self.controller.handle_user_input(
                payload["raw_user_input"],
                session_id=payload.get("session_id", "meeting-01"),
                recent_dialogue=payload.get("recent_dialogue"),
                application=ApplicationState.model_validate(payload.get("application", {})),
                network=NetworkState.model_validate(payload.get("network", {})),
                user_preferences=UserPreferences.model_validate(
                    payload.get("user_preferences", {})
                ),
                app_history_mbps=payload.get("app_history_mbps"),
                network_history_mbps=payload.get("network_history_mbps"),
            )
            if result.plan is not None:
                self.plans[result.plan.plan_id] = result.plan.model_dump(mode="json")
            return 200, result.model_dump(mode="json")
        if method == "POST" and parsed.path == "/controller/evaluate":
            goal_id = GoalID(payload["goal_id"])
            feasibility = FeasibilityResult.model_validate(payload["feasibility"])
            recommendation = ActionRecommendation.model_validate(payload["recommendation"])
            evaluation = evaluate_goal(
                goal_id,
                feasibility,
                recommendation,
                completed_steps=int(payload.get("completed_steps", 5)),
                failed_steps=int(payload.get("failed_steps", 0)),
            )
            return 200, {"evaluation": evaluation.model_dump(mode="json")}
        if method == "POST" and parsed.path == "/agents/application/predict":
            result = self.controller.application_predictor.predict(
                payload["history_mbps"],
                payload.get("horizon"),
            )
            return 200, {"prediction": result.model_dump(mode="json")}
        if method == "POST" and parsed.path == "/agents/network/predict":
            result = self.controller.network_predictor.predict(
                payload["history_mbps"],
                payload.get("horizon"),
            )
            return 200, {"prediction": result.model_dump(mode="json")}
        if method == "GET" and parsed.path.startswith("/controller/plans/"):
            plan_id = parsed.path.rsplit("/", 1)[-1]
            plan = self.plans.get(plan_id)
            if plan is None:
                return 404, {"error": "plan_not_found", "plan_id": plan_id}
            return 200, {"plan": plan}
        return 404, {"error": "not_found", "path": parsed.path}


def _context_from_payload(payload: dict):
    return build_semantic_context(
        payload["raw_user_input"],
        session_id=payload.get("session_id", "meeting-01"),
        recent_dialogue=payload.get("recent_dialogue"),
        application=ApplicationState.model_validate(payload.get("application", {})),
        network=NetworkState.model_validate(payload.get("network", {})),
        user_preferences=UserPreferences.model_validate(payload.get("user_preferences", {})),
    )


class Handler(JsonHandler):
    routes_owner = SemanticControllerService()


if __name__ == "__main__":
    run_server(Handler, int(os.environ.get("PORT", "8010")))
