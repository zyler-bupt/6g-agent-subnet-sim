from __future__ import annotations

import json
from dataclasses import dataclass, field

from semantic_controller.qwen import ChatClient, _extract_json_object
from semantic_controller.schemas import AgentID, GoalID, GoalSpec, PlanSpec, PlanStep, TaskType


class ConstrainedRulePlanner:
    def build_plan(self, goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
        resolved_plan_id = plan_id or f"plan-{goal.goal_id.value.lower()}"
        if goal.need_clarification or goal.goal_id == GoalID.UNKNOWN:
            return PlanSpec(
                plan_id=resolved_plan_id,
                goal_id=goal.goal_id,
                steps=[
                    PlanStep(
                        step_id="s1",
                        task_type=TaskType.REQUEST_CLARIFICATION,
                        assigned_agent=AgentID.AGENT_CONTROLLER,
                        success_criteria={"clarification_question_available": True},
                    )
                ],
            )

        return PlanSpec(
            plan_id=resolved_plan_id,
            goal_id=goal.goal_id,
            steps=[
                PlanStep(
                    step_id="s1",
                    task_type=TaskType.APP_DEMAND_FORECAST,
                    assigned_agent=AgentID.APPLICATION_AGENT,
                    inputs={"history_window": 15, "prediction_horizon": 5},
                    success_criteria={"prediction_available": True},
                ),
                PlanStep(
                    step_id="s2",
                    task_type=TaskType.NETWORK_BANDWIDTH_FORECAST,
                    assigned_agent=AgentID.NETWORK_AGENT,
                    inputs={"history_window": 15, "prediction_horizon": 5},
                    success_criteria={"prediction_available": True},
                ),
                PlanStep(
                    step_id="s3",
                    task_type=TaskType.CROSS_LAYER_FEASIBILITY_CHECK,
                    assigned_agent=AgentID.AGENT_CONTROLLER,
                    dependencies=["s1", "s2"],
                    success_criteria={"resource_margin_computed": True},
                ),
                PlanStep(
                    step_id="s4",
                    task_type=TaskType.VIDEO_POLICY_SELECTION,
                    assigned_agent=AgentID.AGENT_CONTROLLER,
                    dependencies=["s3"],
                    success_criteria={"policy_selected": True},
                ),
                PlanStep(
                    step_id="s5",
                    task_type=TaskType.GOAL_EVALUATION,
                    assigned_agent=AgentID.AGENT_CONTROLLER,
                    dependencies=["s4"],
                    success_criteria={"goal_status_available": True},
                ),
            ],
        )


@dataclass
class OpenManusTaskPlanner:
    """Restricted planning adapter: JSON planning only, with no general-purpose tools."""

    client: ChatClient
    fallback: ConstrainedRulePlanner = field(default_factory=ConstrainedRulePlanner)

    def build_plan(self, goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
        resolved_plan_id = plan_id or f"plan-{goal.goal_id.value.lower()}"
        messages = self._messages(goal, resolved_plan_id)
        try:
            return self._parse(self.client.complete(messages), resolved_plan_id, goal.goal_id)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as first_error:
            try:
                repaired = self.client.complete(
                    [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                "上一计划未通过PlanSpec和DAG校验。只返回修复后的JSON对象。"
                                f" 校验错误：{first_error}"
                            ),
                        },
                    ]
                )
                return self._parse(repaired, resolved_plan_id, goal.goal_id)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return self.fallback.build_plan(goal, plan_id=resolved_plan_id)

    @staticmethod
    def _parse(raw: str, plan_id: str, goal_id: GoalID) -> PlanSpec:
        payload = _extract_json_object(raw)
        payload["plan_id"] = plan_id
        payload["goal_id"] = goal_id.value
        return PlanSpec.model_validate(payload)

    @staticmethod
    def _messages(goal: GoalSpec, plan_id: str) -> list[dict[str, str]]:
        system = """你是受限的SANet双Agent任务规划模块。
只能使用application-agent、network-agent、agent-controller。
只能使用APP_DEMAND_FORECAST、NETWORK_BANDWIDTH_FORECAST、CROSS_LAYER_FEASIBILITY_CHECK、VIDEO_POLICY_SELECTION、GOAL_EVALUATION、REQUEST_CLARIFICATION。
不得生成pAgent、CSI、频谱、无线资源、Shell、文件、浏览器或代码执行步骤。
两个预测任务可并行；跨层评价必须依赖两个预测；策略依赖评价；目标评价依赖策略。
只返回符合PlanSpec的JSON对象。"""
        user = (
            f"plan_id={plan_id}\n"
            f"GoalSpec={goal.model_dump_json()}\n"
            "为该目标生成最小合法执行计划。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def build_plan(goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
    return ConstrainedRulePlanner().build_plan(goal, plan_id=plan_id)
