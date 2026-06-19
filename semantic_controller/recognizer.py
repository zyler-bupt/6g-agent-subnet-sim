from __future__ import annotations

from semantic_controller.ontology import GOAL_KEYWORDS
from semantic_controller.schemas import GoalID, GoalSpec, SemanticContext


def recognize_goal(context: SemanticContext) -> GoalSpec:
    text = context.raw_user_input
    scores: dict[GoalID, int] = {}
    for goal_id, terms in GOAL_KEYWORDS.items():
        scores[goal_id] = sum(1 for term in terms if term in text)

    best_goal = max(scores, key=scores.get)
    best_score = scores[best_goal]
    if best_score == 0:
        return GoalSpec(
            goal_id=GoalID.UNKNOWN,
            goal_description="无法将用户输入映射到当前支持的语义目标",
            confidence=0.2,
            required_information=[],
            need_clarification=True,
            clarification_question="请说明你希望改善画质、减少卡顿、保证流畅，还是节省带宽？",
        )

    confidence = min(0.95, 0.62 + best_score * 0.12)
    constraints = _infer_constraints(context, best_goal)
    return GoalSpec(
        goal_id=best_goal,
        goal_description=_goal_description(best_goal),
        confidence=confidence,
        constraints=constraints,
        required_information=["future_app_rate", "future_network_bandwidth"],
        need_clarification=False,
    )


def _goal_description(goal_id: GoalID) -> str:
    descriptions = {
        GoalID.IMPROVE_VIDEO_QUALITY: "在避免明显卡顿的前提下提高视频清晰度",
        GoalID.REDUCE_VIDEO_STALL: "降低当前视频卡顿和播放中断风险",
        GoalID.GUARANTEE_SMOOTH_STREAMING: "保证会议过程稳定流畅",
        GoalID.SAVE_NETWORK_BANDWIDTH: "在可接受体验下降低网络带宽占用",
        GoalID.CHECK_NETWORK_FEASIBILITY: "检查当前网络是否能承载目标视频策略",
    }
    return descriptions.get(goal_id, "未知目标")


def _infer_constraints(context: SemanticContext, goal_id: GoalID) -> dict[str, object]:
    text = context.raw_user_input
    constraints: dict[str, object] = {
        "avoid_stall": "不要卡" in text or goal_id in {
            GoalID.REDUCE_VIDEO_STALL,
            GoalID.GUARANTEE_SMOOTH_STREAMING,
        },
        "current_resolution": context.application.resolution,
    }
    if "1080" in text:
        constraints["min_resolution"] = "1080p"
    if context.user_preferences.prefer_low_latency:
        constraints["prefer_low_latency"] = True
    if context.user_preferences.prefer_quality:
        constraints["prefer_quality"] = True
    return constraints

