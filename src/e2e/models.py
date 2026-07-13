from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SemanticIntent:
    scenario: str
    raw_text: str


@dataclass(frozen=True)
class GatewayCommandLog:
    gateway_id: str
    command: tuple[str, ...]
    elapsed_ms: float
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class GatewayInstallResult:
    ok: bool
    install_ms: float
    updated_gateway_count: int
    updated_rule_count: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    mode: str = "simulated"
    gateway_times_ms: dict[str, float] = field(default_factory=dict)
    command_logs: tuple[GatewayCommandLog, ...] = field(default_factory=tuple)
    validated_rule_count: int = 0


@dataclass(frozen=True)
class EdgeVerifyResult:
    session_id: str
    source: str
    target: str
    ok: bool
    reachable: bool
    latency_ms: float | None = None
    max_latency_ms: float | None = None
    loss_rate: float | None = None
    max_loss_rate: float | None = None
    available_bandwidth_mbps: float | None = None
    min_bandwidth_mbps: float | None = None
    throughput_mbps: float | None = None
    error: str = ""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    verify_ms: float
    checked_edges: int
    passed_edges: int
    edge_results: tuple[EdgeVerifyResult, ...] = field(default_factory=tuple)
    mode: str = "synthetic"


@dataclass(frozen=True)
class E2EBuildMetrics:
    semantic_ms: float
    task_mapping_ms: float
    controller_build_ms: float
    gateway_install_ms: float
    business_verify_ms: float
    e2e_build_ms: float
    success: bool
    session_count: int
    involved_gateway_count: int
    checked_edges: int
    passed_edges: int
    install_mode: str
    verify_mode: str
    controller_success: bool
    install_success: bool
    verify_success: bool
    updated_rule_count: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecoveryDelta:
    changed_gateway_ids: tuple[str, ...] = field(default_factory=tuple)
    changed_session_ids: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""


@dataclass(frozen=True)
class E2ERecoveryMetrics:
    scenario: str
    fault_type: str
    run_id: int
    fault_detect_ms: float
    recovery_decision_ms: float
    delta_install_ms: float
    business_restore_ms: float
    e2e_recovery_ms: float
    estimated_interruption_ms: float
    changed_agent_count: int
    changed_edge_count: int
    changed_gateway_count: int
    full_rebuild_ms: float | None
    incremental_success: bool
    full_rebuild_success: bool | None
    healthy_windows: int
    verify_attempts: int
    strategy: str
    remediation: str
    install_mode: str
    verify_mode: str
    elastic_recovery_ms: float = 0.0
    model_analysis_ms: float = 0.0
    controller_validation_ms: float = 0.0
    controller_apply_ms: float = 0.0
    detection_source: str = "unknown"
    decision_mode: str = "rule"
    decision_source: str = "rule"
    diagnosed_fault_type: str = "unknown"
    model_strategy: str = ""
    applied_strategy: str = ""
    model_timeout: bool = False
    llm_plan_valid: bool = False
    llm_plan_adopted: bool = False
    fallback_reason: str = ""
    deadline_violated: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)
