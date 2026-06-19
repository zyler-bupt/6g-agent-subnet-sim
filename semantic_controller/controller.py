from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from semantic_controller.context import build_semantic_context
from semantic_controller.evaluator import evaluate_feasibility, evaluate_goal, select_video_policy
from semantic_controller.interfaces import GoalRecognizer, TaskPlanner
from semantic_controller.predictors import ApplicationPredictor, NetworkPredictor
from semantic_controller.recognizer import RuleBasedGoalRecognizer
from semantic_controller.planner import ConstrainedRulePlanner
from semantic_controller.schemas import (
    ApplicationState,
    GoalEvaluation,
    GoalID,
    GoalStatus,
    NetworkState,
    SemanticPipelineResult,
    UserPreferences,
)
from semantic_controller.trigger import detect_semantic_trigger


class SemanticController:
    def __init__(
        self,
        application_predictor: ApplicationPredictor | None = None,
        network_predictor: NetworkPredictor | None = None,
        goal_recognizer: GoalRecognizer | None = None,
        task_planner: TaskPlanner | None = None,
    ) -> None:
        self.application_predictor = application_predictor or ApplicationPredictor()
        self.network_predictor = network_predictor or NetworkPredictor()
        self.goal_recognizer = goal_recognizer or RuleBasedGoalRecognizer()
        self.task_planner = task_planner or ConstrainedRulePlanner()

    def handle_user_input(
        self,
        raw_user_input: str,
        *,
        session_id: str = "meeting-01",
        recent_dialogue: list[str] | None = None,
        application: ApplicationState | None = None,
        network: NetworkState | None = None,
        user_preferences: UserPreferences | None = None,
        app_history_mbps: list[float] | None = None,
        network_history_mbps: list[float] | None = None,
    ) -> SemanticPipelineResult:
        context = build_semantic_context(
            raw_user_input,
            session_id=session_id,
            recent_dialogue=recent_dialogue,
            application=application,
            network=network,
            user_preferences=user_preferences,
        )
        trigger = detect_semantic_trigger(raw_user_input, context.application)
        if not trigger.triggered:
            goal = self.goal_recognizer.recognize(context)
            evaluation = GoalEvaluation(
                goal_id=GoalID.UNKNOWN,
                status=GoalStatus.NEED_CLARIFICATION,
                reason=trigger.reason,
                completed_steps=0,
                failed_steps=0,
            )
            return SemanticPipelineResult(
                trigger=trigger,
                context=context,
                goal=goal,
                plan=None,
                evaluation=evaluation,
            )

        goal = self.goal_recognizer.recognize(context)
        plan = self.task_planner.build_plan(goal, plan_id=f"plan-{context.event_id}")
        if goal.need_clarification:
            evaluation = GoalEvaluation(
                goal_id=goal.goal_id,
                status=GoalStatus.NEED_CLARIFICATION,
                reason=goal.clarification_question or "需要用户澄清目标",
                completed_steps=1,
                failed_steps=0,
            )
            return SemanticPipelineResult(
                trigger=trigger,
                context=context,
                goal=goal,
                plan=plan,
                evaluation=evaluation,
            )

        app_history = app_history_mbps or _default_app_history(context)
        net_history = network_history_mbps or _default_network_history(context)
        horizon = 5
        with ThreadPoolExecutor(max_workers=2) as executor:
            app_future = executor.submit(self.application_predictor.predict, app_history, horizon)
            net_future = executor.submit(self.network_predictor.predict, net_history, horizon)
            app_prediction = app_future.result(timeout=5)
            network_prediction = net_future.result(timeout=5)

        feasibility = evaluate_feasibility(app_prediction, network_prediction)
        recommendation = select_video_policy(context, feasibility)
        evaluation = evaluate_goal(
            goal.goal_id,
            feasibility,
            recommendation,
            completed_steps=len(plan.steps),
            failed_steps=0,
        )
        return SemanticPipelineResult(
            trigger=trigger,
            context=context,
            goal=goal,
            plan=plan,
            predictions=[app_prediction, network_prediction],
            feasibility=feasibility,
            recommendation=recommendation,
            evaluation=evaluation,
        )


def _default_app_history(context: ApplicationState | object) -> list[float]:
    app = context.application if hasattr(context, "application") else context
    measured = float(app.measured_rate_mbps)
    target = float(app.target_bitrate_mbps)
    return [
        max(0.1, measured * 0.92),
        max(0.1, measured * 0.95),
        measured,
        (measured + target) / 2,
        target,
    ]


def _default_network_history(context: NetworkState | object) -> list[float]:
    network = context.network if hasattr(context, "network") else context
    latest = float(network.latest_bandwidth_mbps)
    return [
        max(0.1, latest * 0.95),
        max(0.1, latest * 0.98),
        latest,
        max(0.1, latest * 0.97),
        latest,
    ]
