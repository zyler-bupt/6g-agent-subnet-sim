from __future__ import annotations

import json
import unittest

from semantic_controller.context import build_semantic_context
from semantic_controller.embedding import GoalPrototypeRetriever, HashingEmbeddingEncoder
from semantic_controller.planner import OpenManusTaskPlanner
from semantic_controller.qwen import QwenGoalRecognizer
from semantic_controller.recognizer import RuleBasedGoalRecognizer
from semantic_controller.schemas import GoalID, TaskType
from services.semantic_controller_service import SemanticControllerService


class SequenceChatClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class SemanticStage4Tests(unittest.TestCase):
    def test_embedding_retrieval_ranks_quality_goal_first(self) -> None:
        context = build_semantic_context("现在画面太模糊了，希望更清晰")
        retriever = GoalPrototypeRetriever(encoder=HashingEmbeddingEncoder(dimension=128))
        embedding, candidates = retriever.retrieve(context)
        self.assertEqual(embedding.dimension, 128)
        self.assertEqual(candidates[0].goal_id, GoalID.IMPROVE_VIDEO_QUALITY)

    def test_qwen_recognizer_accepts_valid_goal_spec(self) -> None:
        response = json.dumps(
            {
                "goal_id": "IMPROVE_VIDEO_QUALITY",
                "goal_description": "提高画质并避免卡顿",
                "confidence": 0.94,
                "constraints": {"avoid_stall": True},
                "required_information": [
                    "future_app_rate",
                    "future_network_bandwidth",
                ],
                "need_clarification": False,
                "clarification_question": None,
            },
            ensure_ascii=False,
        )
        client = SequenceChatClient([response])
        recognizer = QwenGoalRecognizer(client=client)
        goal, embedding, candidates = recognizer.recognize_with_evidence(
            build_semantic_context("帮我提高画质，但是不要卡")
        )
        self.assertEqual(goal.goal_id, GoalID.IMPROVE_VIDEO_QUALITY)
        self.assertGreater(embedding.dimension, 0)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(client.calls, 1)

    def test_qwen_recognizer_falls_back_after_invalid_json(self) -> None:
        client = SequenceChatClient(["not-json", "still-not-json"])
        recognizer = QwenGoalRecognizer(
            client=client,
            fallback=RuleBasedGoalRecognizer(),
        )
        goal = recognizer.recognize(build_semantic_context("希望节省带宽，降低流量"))
        self.assertEqual(goal.goal_id, GoalID.SAVE_NETWORK_BANDWIDTH)
        self.assertEqual(client.calls, 2)

    def test_restricted_planner_rejects_physical_task_and_falls_back(self) -> None:
        illegal_plan = json.dumps(
            {
                "steps": [
                    {
                        "step_id": "s1",
                        "task_type": "CSI_FORECAST",
                        "assigned_agent": "physical-agent",
                        "dependencies": [],
                    }
                ]
            }
        )
        client = SequenceChatClient([illegal_plan, illegal_plan])
        goal = RuleBasedGoalRecognizer().recognize(
            build_semantic_context("希望提高画面清晰度")
        )
        plan = OpenManusTaskPlanner(client=client).build_plan(goal, plan_id="plan-test")
        task_types = {step.task_type for step in plan.steps}
        self.assertNotIn("CSI_FORECAST", task_types)
        self.assertIn(TaskType.APP_DEMAND_FORECAST, task_types)
        self.assertIn(TaskType.NETWORK_BANDWIDTH_FORECAST, task_types)

    def test_semantic_service_execute_returns_full_pipeline(self) -> None:
        service = SemanticControllerService()
        status, body = service.handle(
            "POST",
            "/controller/execute",
            {"raw_user_input": "现在画面模糊，帮我提高画质但不要卡"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["goal"]["goal_id"], "IMPROVE_VIDEO_QUALITY")
        self.assertEqual(len(body["goal_candidates"]), 3)
        self.assertEqual(len(body["predictions"]), 2)
        plan_id = body["plan"]["plan_id"]
        status, stored = service.handle("GET", f"/controller/plans/{plan_id}", {})
        self.assertEqual(status, 200)
        self.assertEqual(stored["plan"]["plan_id"], plan_id)

    def test_semantic_service_exposes_embedding_and_agent_prediction(self) -> None:
        service = SemanticControllerService()
        status, embedded = service.handle(
            "POST",
            "/semantic/embed",
            {"raw_user_input": "帮我减少视频卡顿"},
        )
        self.assertEqual(status, 200)
        self.assertGreater(embedded["semantic_embedding"]["dimension"], 0)

        status, predicted = service.handle(
            "POST",
            "/agents/network/predict",
            {"history_mbps": [5.0, 5.2, 5.1], "horizon": 2},
        )
        self.assertEqual(status, 200)
        self.assertEqual(predicted["prediction"]["horizon"], 2)


if __name__ == "__main__":
    unittest.main()
