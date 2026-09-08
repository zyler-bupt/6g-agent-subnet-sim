from __future__ import annotations

import unittest

from pydantic import ValidationError

from semantic_controller.controller import SemanticController
from semantic_controller.evaluator import evaluate_feasibility, select_video_policy
from semantic_controller.planner import build_plan
from semantic_controller.predictors import ApplicationPredictor, NetworkPredictor, forecast_values
from semantic_controller.recognizer import recognize_goal
from semantic_controller.schemas import (
    AgentID,
    ApplicationState,
    Feasibility,
    GoalID,
    GoalSpec,
    GoalStatus,
    NetworkState,
    PlanSpec,
    PlanStep,
    PredictionResult,
    SemanticContext,
    TaskType,
    UserPreferences,
)
from semantic_controller.trigger import detect_semantic_trigger


class SemanticControllerTests(unittest.TestCase):
    def test_trigger_positive_video_quality(self) -> None:
        result = detect_semantic_trigger("当前画面太模糊了，希望清晰一些")
        self.assertTrue(result.triggered)
        self.assertEqual(result.trigger_type, "video_quality")
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_trigger_rejects_concept_discussion(self) -> None:
        result = detect_semantic_trigger("解释一下带宽是什么意思")
        self.assertFalse(result.triggered)
        self.assertIn("rather than controlling", result.reason)

    def test_goal_recognition_quality(self) -> None:
        context = SemanticContext(
            event_id="evt-test",
            timestamp=1,
            session_id="meeting-01",
            raw_user_input="当前画面太模糊了，希望清晰一些，但不要卡",
            user_preferences=UserPreferences(prefer_quality=True),
        )
        goal = recognize_goal(context)
        self.assertEqual(goal.goal_id, GoalID.IMPROVE_VIDEO_QUALITY)
        self.assertFalse(goal.need_clarification)
        self.assertTrue(goal.constraints["avoid_stall"])

    def test_unknown_goal_requires_clarification(self) -> None:
        with self.assertRaises(ValidationError):
            GoalSpec(
                goal_id=GoalID.UNKNOWN,
                goal_description="unknown",
                confidence=0.2,
                need_clarification=False,
            )

    def test_plan_contains_dual_agent_forecasts(self) -> None:
        goal = GoalSpec(
            goal_id=GoalID.IMPROVE_VIDEO_QUALITY,
            goal_description="提高画质",
            confidence=0.9,
            required_information=["future_app_rate", "future_network_bandwidth"],
        )
        plan = build_plan(goal)
        task_types = [step.task_type for step in plan.steps]
        self.assertIn(TaskType.APP_DEMAND_FORECAST, task_types)
        self.assertIn(TaskType.NETWORK_BANDWIDTH_FORECAST, task_types)
        self.assertNotIn("pAgent", plan.model_dump_json())
        self.assertEqual(plan.steps[0].assigned_agent, AgentID.APPLICATION_AGENT)
        self.assertEqual(plan.steps[1].assigned_agent, AgentID.NETWORK_AGENT)

    def test_plan_rejects_wrong_agent_assignment(self) -> None:
        with self.assertRaises(ValidationError):
            PlanSpec(
                plan_id="bad-plan",
                goal_id=GoalID.IMPROVE_VIDEO_QUALITY,
                steps=[
                    PlanStep(
                        step_id="s1",
                        task_type=TaskType.APP_DEMAND_FORECAST,
                        assigned_agent=AgentID.NETWORK_AGENT,
                    )
                ],
            )

    def test_plan_rejects_cycle(self) -> None:
        with self.assertRaises(ValidationError):
            PlanSpec(
                plan_id="cycle-plan",
                goal_id=GoalID.IMPROVE_VIDEO_QUALITY,
                steps=[
                    PlanStep(
                        step_id="s1",
                        task_type=TaskType.APP_DEMAND_FORECAST,
                        assigned_agent=AgentID.APPLICATION_AGENT,
                        dependencies=["s2"],
                    ),
                    PlanStep(
                        step_id="s2",
                        task_type=TaskType.NETWORK_BANDWIDTH_FORECAST,
                        assigned_agent=AgentID.NETWORK_AGENT,
                        dependencies=["s1"],
                    ),
                ],
            )

    def test_forecast_values_follow_recent_trend(self) -> None:
        values = forecast_values([1, 2, 3, 4, 5], 3)
        self.assertEqual(values, [4.0, 5.0, 6.0])

    def test_predictors_return_expected_metrics(self) -> None:
        app = ApplicationPredictor().predict([1.0, 1.1, 1.2], 2)
        net = NetworkPredictor().predict([5.0, 4.8, 4.6], 2)
        self.assertEqual(app.metric, "app_rate_mbps")
        self.assertEqual(net.metric, "network_bandwidth_mbps")
        self.assertEqual(app.horizon, 2)
        self.assertEqual(len(net.values), 2)

    def test_predictors_accept_small_data_methods(self) -> None:
        app = ApplicationPredictor(forecast_method="kalman").predict([1.0, 1.4, 1.2], 3)
        net = NetworkPredictor(forecast_method="holt").predict([5.0, 4.8, 4.6], 3)
        self.assertEqual(len(app.values), 3)
        self.assertEqual(len(net.values), 3)

    def test_feasibility_and_policy(self) -> None:
        app = PredictionResult(
            agent_id=AgentID.APPLICATION_AGENT,
            metric="app_rate_mbps",
            horizon=2,
            values=[2.0, 2.0],
        )
        net = PredictionResult(
            agent_id=AgentID.NETWORK_AGENT,
            metric="network_bandwidth_mbps",
            horizon=2,
            values=[8.0, 8.0],
        )
        feasibility = evaluate_feasibility(app, net)
        self.assertEqual(feasibility.feasibility, Feasibility.SUFFICIENT)
        policy = select_video_policy(
            SemanticContext(
                event_id="evt-test",
                timestamp=1,
                session_id="meeting-01",
                raw_user_input="提高画质",
            ),
            feasibility,
        )
        self.assertEqual(policy.recommended_resolution, "1080p")

    def test_end_to_end_quality_request(self) -> None:
        controller = SemanticController()
        result = controller.handle_user_input(
            "当前画面太模糊了，希望清晰一些，但不要卡",
            application=ApplicationState(
                resolution="720p",
                target_bitrate_mbps=2.0,
                measured_rate_mbps=1.8,
            ),
            network=NetworkState(latest_bandwidth_mbps=6.2, rtt_ms=35, loss_percent=0.2),
            user_preferences=UserPreferences(prefer_quality=True),
        )
        self.assertTrue(result.trigger.triggered)
        self.assertEqual(result.goal.goal_id, GoalID.IMPROVE_VIDEO_QUALITY)
        self.assertIsNotNone(result.plan)
        self.assertEqual(len(result.predictions), 2)
        self.assertIsNotNone(result.feasibility)
        self.assertIsNotNone(result.recommendation)
        self.assertIn(
            result.evaluation.status,
            {
                GoalStatus.SATISFIED,
                GoalStatus.PARTIALLY_SATISFIED,
                GoalStatus.NOT_SATISFIED,
            },
        )

    def test_end_to_end_negative_input(self) -> None:
        controller = SemanticController()
        result = controller.handle_user_input("我昨天看了一篇关于视频清晰度的论文")
        self.assertFalse(result.trigger.triggered)
        self.assertIsNone(result.plan)
        self.assertIsNone(result.semantic_embedding)
        self.assertEqual(result.goal_candidates, [])
        self.assertEqual(result.evaluation.status, GoalStatus.NEED_CLARIFICATION)

    def test_controller_accepts_injected_goal_recognizer_and_planner(self) -> None:
        class FixedRecognizer:
            def recognize(self, context: SemanticContext) -> GoalSpec:
                return GoalSpec(
                    goal_id=GoalID.SAVE_NETWORK_BANDWIDTH,
                    goal_description="降低当前会议带宽占用",
                    confidence=0.99,
                    required_information=["future_app_rate", "future_network_bandwidth"],
                )

        class RecordingPlanner:
            def __init__(self) -> None:
                self.seen_goal_id: GoalID | None = None

            def build_plan(self, goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
                self.seen_goal_id = goal.goal_id
                return build_plan(goal, plan_id=plan_id)

        planner = RecordingPlanner()
        controller = SemanticController(
            goal_recognizer=FixedRecognizer(),
            task_planner=planner,
        )
        result = controller.handle_user_input("网络比较紧张，希望节省带宽")
        self.assertEqual(result.goal.goal_id, GoalID.SAVE_NETWORK_BANDWIDTH)
        self.assertEqual(planner.seen_goal_id, GoalID.SAVE_NETWORK_BANDWIDTH)
        self.assertIsNotNone(result.plan)


if __name__ == "__main__":
    unittest.main()
