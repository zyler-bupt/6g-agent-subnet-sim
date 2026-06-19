from __future__ import annotations

from semantic_controller.schemas import AgentID, GoalID, GoalSpec, PlanSpec, PlanStep, TaskType


def build_plan(goal: GoalSpec, *, plan_id: str | None = None) -> PlanSpec:
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

