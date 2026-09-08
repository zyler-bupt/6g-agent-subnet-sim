from __future__ import annotations

from src.core.models import BusinessEdge, QoSRequirements, TaskSpec


def rescue_task() -> TaskSpec:
    return TaskSpec(
        task_id="task-rescue-001",
        goal="应急救援：无人机/摄像头采集 -> 边缘识别 -> 云端决策调度 -> 现场反馈",
        app_agents=(
            "agent-drone-capture",
            "agent-edge-recognition",
            "agent-cloud-planning",
            "agent-terminal-feedback",
        ),
        biz_edges=(
            BusinessEdge(
                source="agent-drone-capture",
                target="agent-edge-recognition",
                flow_type="video_stream",
                data_rate_mbps=18.0,
                latency_budget_ms=80.0,
                priority=3,
            ),
            BusinessEdge(
                source="agent-edge-recognition",
                target="agent-cloud-planning",
                flow_type="recognition_result",
                data_rate_mbps=5.0,
                latency_budget_ms=120.0,
                priority=2,
            ),
            BusinessEdge(
                source="agent-cloud-planning",
                target="agent-terminal-feedback",
                flow_type="dispatch_decision",
                data_rate_mbps=2.0,
                latency_budget_ms=150.0,
                priority=2,
            ),
        ),
        qos=QoSRequirements(
            max_latency_ms=120.0,
            max_loss_rate=0.03,
            min_reliability=0.99,
            min_bandwidth_mbps=20.0,
            data_volume_mb=800.0,
            priority=3,
            risk_threshold=1.0,
        ),
    )

