from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GoalID(str, Enum):
    IMPROVE_VIDEO_QUALITY = "IMPROVE_VIDEO_QUALITY"
    REDUCE_VIDEO_STALL = "REDUCE_VIDEO_STALL"
    GUARANTEE_SMOOTH_STREAMING = "GUARANTEE_SMOOTH_STREAMING"
    SAVE_NETWORK_BANDWIDTH = "SAVE_NETWORK_BANDWIDTH"
    CHECK_NETWORK_FEASIBILITY = "CHECK_NETWORK_FEASIBILITY"
    UNKNOWN = "UNKNOWN"


class TaskType(str, Enum):
    APP_DEMAND_FORECAST = "APP_DEMAND_FORECAST"
    NETWORK_BANDWIDTH_FORECAST = "NETWORK_BANDWIDTH_FORECAST"
    CROSS_LAYER_FEASIBILITY_CHECK = "CROSS_LAYER_FEASIBILITY_CHECK"
    VIDEO_POLICY_SELECTION = "VIDEO_POLICY_SELECTION"
    GOAL_EVALUATION = "GOAL_EVALUATION"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"


class AgentID(str, Enum):
    SEMANTIC_CLIENT = "semantic-client"
    AGENT_CONTROLLER = "agent-controller"
    APPLICATION_AGENT = "application-agent"
    NETWORK_AGENT = "network-agent"


class Feasibility(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    RISKY = "RISKY"
    INSUFFICIENT = "INSUFFICIENT"


class GoalStatus(str, Enum):
    SATISFIED = "SATISFIED"
    PARTIALLY_SATISFIED = "PARTIALLY_SATISFIED"
    NOT_SATISFIED = "NOT_SATISFIED"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"
    FAILED = "FAILED"


class ApplicationState(BaseModel):
    type: str = "video_conference"
    resolution: str = "720p"
    fps: int = Field(default=30, ge=1)
    target_bitrate_mbps: float = Field(default=1.5, ge=0)
    measured_rate_mbps: float = Field(default=1.5, ge=0)
    buffer_seconds: float = Field(default=2.0, ge=0)


class NetworkState(BaseModel):
    latest_bandwidth_mbps: float = Field(default=5.0, ge=0)
    rtt_ms: float = Field(default=35.0, ge=0)
    loss_percent: float = Field(default=0.0, ge=0)


class UserPreferences(BaseModel):
    prefer_quality: bool = False
    prefer_low_latency: bool = False
    allow_quality_degradation: bool = True


class SemanticContext(BaseModel):
    event_id: str
    timestamp: float
    session_id: str
    raw_user_input: str
    recent_dialogue: list[str] = Field(default_factory=list, max_length=3)
    application: ApplicationState = Field(default_factory=ApplicationState)
    network: NetworkState = Field(default_factory=NetworkState)
    user_preferences: UserPreferences = Field(default_factory=UserPreferences)

    @field_validator("raw_user_input")
    @classmethod
    def user_input_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_user_input must not be empty")
        return value.strip()


class SemanticTriggerResult(BaseModel):
    triggered: bool
    trigger_type: str = "none"
    confidence: float = Field(ge=0, le=1)
    matched_terms: list[str] = Field(default_factory=list)
    reason: str


class SemanticEmbedding(BaseModel):
    model: str
    dimension: int = Field(ge=1)
    values: list[float]

    @model_validator(mode="after")
    def validate_dimension(self) -> "SemanticEmbedding":
        if len(self.values) != self.dimension:
            raise ValueError("embedding values length must equal dimension")
        return self


class GoalCandidate(BaseModel):
    goal_id: GoalID
    similarity: float = Field(ge=-1, le=1)


class GoalSpec(BaseModel):
    goal_id: GoalID
    goal_description: str
    confidence: float = Field(ge=0, le=1)
    constraints: dict[str, Any] = Field(default_factory=dict)
    required_information: list[str] = Field(default_factory=list)
    need_clarification: bool = False
    clarification_question: str | None = None

    @field_validator("required_information")
    @classmethod
    def required_information_is_supported(cls, values: list[str]) -> list[str]:
        allowed = {"future_app_rate", "future_network_bandwidth"}
        unsupported = set(values) - allowed
        if unsupported:
            raise ValueError(f"unsupported required_information: {sorted(unsupported)}")
        return values

    @model_validator(mode="after")
    def validate_clarification(self) -> "GoalSpec":
        if self.need_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required when need_clarification is true")
        if self.goal_id == GoalID.UNKNOWN and not self.need_clarification:
            raise ValueError("UNKNOWN goals must request clarification")
        return self


class AgentCard(BaseModel):
    agent_id: AgentID
    capabilities: list[TaskType]


class PlanStep(BaseModel):
    step_id: str
    task_type: TaskType
    assigned_agent: AgentID
    dependencies: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    success_criteria: dict[str, Any] = Field(default_factory=dict)


class PlanSpec(BaseModel):
    plan_id: str
    goal_id: GoalID
    steps: list[PlanStep]

    @model_validator(mode="after")
    def validate_plan(self) -> "PlanSpec":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate step_id in plan")

        known_ids = set(step_ids)
        for step in self.steps:
            for dep in step.dependencies:
                if dep not in known_ids:
                    raise ValueError(f"unknown dependency {dep}")

        assignment = {
            TaskType.APP_DEMAND_FORECAST: AgentID.APPLICATION_AGENT,
            TaskType.NETWORK_BANDWIDTH_FORECAST: AgentID.NETWORK_AGENT,
            TaskType.CROSS_LAYER_FEASIBILITY_CHECK: AgentID.AGENT_CONTROLLER,
            TaskType.VIDEO_POLICY_SELECTION: AgentID.AGENT_CONTROLLER,
            TaskType.GOAL_EVALUATION: AgentID.AGENT_CONTROLLER,
            TaskType.REQUEST_CLARIFICATION: AgentID.AGENT_CONTROLLER,
        }
        for step in self.steps:
            expected = assignment[step.task_type]
            if step.assigned_agent != expected:
                raise ValueError(
                    f"{step.task_type.value} must be assigned to {expected.value}"
                )

        self._validate_acyclic()
        self._validate_workflow()
        return self

    def _validate_acyclic(self) -> None:
        deps = {step.step_id: set(step.dependencies) for step in self.steps}
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in permanent:
                return
            if step_id in temporary:
                raise ValueError("plan dependencies must be acyclic")
            temporary.add(step_id)
            for dep in deps[step_id]:
                visit(dep)
            temporary.remove(step_id)
            permanent.add(step_id)

        for step_id in deps:
            visit(step_id)

    def _validate_workflow(self) -> None:
        by_type: dict[TaskType, PlanStep] = {}
        for step in self.steps:
            if step.task_type in by_type:
                raise ValueError(f"duplicate task_type in plan: {step.task_type.value}")
            by_type[step.task_type] = step

        if self.goal_id == GoalID.UNKNOWN:
            if set(by_type) != {TaskType.REQUEST_CLARIFICATION}:
                raise ValueError("UNKNOWN goal plans may only request clarification")
            return

        required = {
            TaskType.APP_DEMAND_FORECAST,
            TaskType.NETWORK_BANDWIDTH_FORECAST,
            TaskType.CROSS_LAYER_FEASIBILITY_CHECK,
            TaskType.VIDEO_POLICY_SELECTION,
            TaskType.GOAL_EVALUATION,
        }
        if set(by_type) != required:
            missing = sorted(item.value for item in required - set(by_type))
            extra = sorted(item.value for item in set(by_type) - required)
            raise ValueError(f"invalid workflow tasks; missing={missing}, extra={extra}")

        app_id = by_type[TaskType.APP_DEMAND_FORECAST].step_id
        network_id = by_type[TaskType.NETWORK_BANDWIDTH_FORECAST].step_id
        feasibility = by_type[TaskType.CROSS_LAYER_FEASIBILITY_CHECK]
        if not {app_id, network_id}.issubset(set(feasibility.dependencies)):
            raise ValueError("cross-layer feasibility must depend on both forecasts")

        policy = by_type[TaskType.VIDEO_POLICY_SELECTION]
        if feasibility.step_id not in policy.dependencies:
            raise ValueError("video policy must depend on cross-layer feasibility")

        evaluation = by_type[TaskType.GOAL_EVALUATION]
        if policy.step_id not in evaluation.dependencies:
            raise ValueError("goal evaluation must depend on video policy")


class PredictionResult(BaseModel):
    agent_id: AgentID
    metric: Literal["app_rate_mbps", "network_bandwidth_mbps"]
    horizon: int = Field(ge=1)
    values: list[float]

    @model_validator(mode="after")
    def validate_values(self) -> "PredictionResult":
        if len(self.values) != self.horizon:
            raise ValueError("prediction values length must equal horizon")
        if any(value < 0 for value in self.values):
            raise ValueError("prediction values must be non-negative")
        return self

    @property
    def mean_value(self) -> float:
        return sum(self.values) / len(self.values)


class FeasibilityResult(BaseModel):
    feasibility: Feasibility
    predicted_app_rate_mbps: float
    predicted_network_bandwidth_mbps: float
    safe_margin_mbps: float
    reason: str


class ActionRecommendation(BaseModel):
    recommended_resolution: str
    recommended_bitrate_mbps: float = Field(ge=0)
    reason: str


class GoalEvaluation(BaseModel):
    goal_id: GoalID
    status: GoalStatus
    reason: str
    completed_steps: int
    failed_steps: int


class SemanticPipelineResult(BaseModel):
    trigger: SemanticTriggerResult
    context: SemanticContext
    semantic_embedding: SemanticEmbedding | None = None
    goal_candidates: list[GoalCandidate] = Field(default_factory=list)
    goal: GoalSpec
    plan: PlanSpec | None
    predictions: list[PredictionResult] = Field(default_factory=list)
    feasibility: FeasibilityResult | None = None
    recommendation: ActionRecommendation | None = None
    evaluation: GoalEvaluation
