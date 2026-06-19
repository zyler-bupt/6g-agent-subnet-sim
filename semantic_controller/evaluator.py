from __future__ import annotations

from semantic_controller.schemas import (
    ActionRecommendation,
    Feasibility,
    FeasibilityResult,
    GoalEvaluation,
    GoalID,
    GoalStatus,
    PredictionResult,
    SemanticContext,
)


def evaluate_feasibility(
    app_prediction: PredictionResult,
    network_prediction: PredictionResult,
    *,
    alpha: float = 0.8,
    beta: float = 1.1,
    risky_margin_mbps: float = 2.0,
) -> FeasibilityResult:
    app_rate = app_prediction.mean_value
    network_bandwidth = network_prediction.mean_value
    safe_margin = alpha * network_bandwidth - beta * app_rate

    if safe_margin > risky_margin_mbps:
        feasibility = Feasibility.SUFFICIENT
        reason = "预测网络承载能力有充足安全余量"
    elif safe_margin > 0:
        feasibility = Feasibility.RISKY
        reason = "预测网络可以承载业务，但安全余量较小"
    else:
        feasibility = Feasibility.INSUFFICIENT
        reason = "预测网络安全余量不足，提升画质可能导致卡顿"

    return FeasibilityResult(
        feasibility=feasibility,
        predicted_app_rate_mbps=round(app_rate, 3),
        predicted_network_bandwidth_mbps=round(network_bandwidth, 3),
        safe_margin_mbps=round(safe_margin, 3),
        reason=reason,
    )


def select_video_policy(
    context: SemanticContext,
    feasibility: FeasibilityResult,
) -> ActionRecommendation:
    current_resolution = context.application.resolution
    current_bitrate = context.application.target_bitrate_mbps

    if feasibility.feasibility == Feasibility.SUFFICIENT:
        return ActionRecommendation(
            recommended_resolution=_next_resolution(current_resolution),
            recommended_bitrate_mbps=round(max(current_bitrate * 1.4, current_bitrate + 0.8), 3),
            reason="预测安全余量充足，可以提高一级视频质量",
        )

    if feasibility.feasibility == Feasibility.RISKY:
        return ActionRecommendation(
            recommended_resolution=current_resolution,
            recommended_bitrate_mbps=round(max(current_bitrate, feasibility.predicted_app_rate_mbps * 0.9), 3),
            reason="预测安全余量较小，建议维持分辨率并适度控制码率",
        )

    return ActionRecommendation(
        recommended_resolution=current_resolution,
        recommended_bitrate_mbps=round(max(current_bitrate * 0.8, 0.5), 3),
        reason="预测网络承载不足，不建议提升分辨率，必要时降低码率",
    )


def evaluate_goal(
    goal_id: GoalID,
    feasibility: FeasibilityResult | None,
    recommendation: ActionRecommendation | None,
    *,
    completed_steps: int,
    failed_steps: int = 0,
) -> GoalEvaluation:
    if failed_steps:
        return GoalEvaluation(
            goal_id=goal_id,
            status=GoalStatus.FAILED,
            reason="存在失败步骤，无法完成目标评价",
            completed_steps=completed_steps,
            failed_steps=failed_steps,
        )
    if feasibility is None or recommendation is None:
        return GoalEvaluation(
            goal_id=goal_id,
            status=GoalStatus.NEED_CLARIFICATION,
            reason="缺少可行性结果或策略建议",
            completed_steps=completed_steps,
            failed_steps=failed_steps,
        )
    if feasibility.feasibility == Feasibility.SUFFICIENT:
        status = GoalStatus.SATISFIED
        reason = "当前预测条件支持目标策略"
    elif feasibility.feasibility == Feasibility.RISKY:
        status = GoalStatus.PARTIALLY_SATISFIED
        reason = "目标可部分满足，但需要保守策略避免卡顿"
    else:
        status = GoalStatus.NOT_SATISFIED
        reason = "当前预测网络条件不足以安全满足目标"
    return GoalEvaluation(
        goal_id=goal_id,
        status=status,
        reason=reason,
        completed_steps=completed_steps,
        failed_steps=failed_steps,
    )


def _next_resolution(resolution: str) -> str:
    order = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]
    if resolution not in order:
        return "1080p"
    idx = order.index(resolution)
    return order[min(idx + 1, len(order) - 1)]

