from __future__ import annotations

from semantic_controller.schemas import AgentCard, AgentID, GoalID, TaskType


GOAL_KEYWORDS: dict[GoalID, tuple[str, ...]] = {
    GoalID.IMPROVE_VIDEO_QUALITY: (
        "清晰",
        "清楚",
        "模糊",
        "画质",
        "分辨率",
        "高清",
        "看不清",
    ),
    GoalID.REDUCE_VIDEO_STALL: ("卡", "卡顿", "延迟", "掉帧", "不流畅"),
    GoalID.GUARANTEE_SMOOTH_STREAMING: ("流畅", "稳定", "不要断", "不断", "稳一些"),
    GoalID.SAVE_NETWORK_BANDWIDTH: ("省流量", "节省带宽", "降低流量", "网络紧张"),
    GoalID.CHECK_NETWORK_FEASIBILITY: (
        "带宽",
        "能不能支持",
        "是否能承载",
        "网络够不够",
        "检查网络",
    ),
}


CONTROL_INTENT_TERMS = (
    "帮我",
    "希望",
    "调整",
    "调",
    "提高",
    "降低",
    "保证",
    "不要",
    "能不能",
    "需要",
)


NON_CONTROL_TERMS = (
    "论文",
    "解释",
    "是什么意思",
    "翻译",
    "定义",
    "概念",
    "昨天看",
)


DEFAULT_AGENT_CARDS = [
    AgentCard(
        agent_id=AgentID.APPLICATION_AGENT,
        capabilities=[TaskType.APP_DEMAND_FORECAST],
    ),
    AgentCard(
        agent_id=AgentID.NETWORK_AGENT,
        capabilities=[TaskType.NETWORK_BANDWIDTH_FORECAST],
    ),
    AgentCard(
        agent_id=AgentID.AGENT_CONTROLLER,
        capabilities=[
            TaskType.CROSS_LAYER_FEASIBILITY_CHECK,
            TaskType.VIDEO_POLICY_SELECTION,
            TaskType.GOAL_EVALUATION,
            TaskType.REQUEST_CLARIFICATION,
        ],
    ),
]
