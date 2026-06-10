from __future__ import annotations

from agents import business_agent, n_agent, p_agent
from gateway import NodeGateway
from models import BusinessFlow, Intent, QoSRequirements, SupportRequirements


def create_disaster_response_scenario() -> tuple[Intent, dict[str, NodeGateway]]:
    ue_gateway = NodeGateway(gateway_id="gw-ue", node="ue-drone-node")
    mec_gateway = NodeGateway(gateway_id="gw-mec", node="mec-edge-node")
    cloud_gateway = NodeGateway(gateway_id="gw-cloud", node="cloud-node")

    drone_capture = business_agent(
        agent_id="agent-drone-capture",
        name="无人机视频采集Agent",
        capabilities=("video_capture", "scene_upload"),
        node="ue-drone-node",
        gateway_id="gw-ue",
    )
    edge_recognition = business_agent(
        agent_id="agent-edge-recognition",
        name="边缘人员识别Agent",
        capabilities=("person_detection", "hazard_recognition"),
        node="mec-edge-node",
        gateway_id="gw-mec",
    )
    cloud_planning = business_agent(
        agent_id="agent-cloud-planning",
        name="云端路径规划Agent",
        capabilities=("route_planning", "global_decision"),
        node="cloud-node",
        gateway_id="gw-cloud",
    )
    terminal_feedback = business_agent(
        agent_id="agent-terminal-feedback",
        name="终端反馈Agent",
        capabilities=("decision_feedback", "field_guidance"),
        node="ue-drone-node",
        gateway_id="gw-ue",
    )

    ue_pagent = p_agent(
        agent_id="pagent-ue-link",
        name="无人机接入链路pAgent",
        node="ue-drone-node",
        gateway_id="gw-ue",
        link_id="wireless-link-drone-gnb",
    )
    mec_nagent = n_agent(
        agent_id="nagent-mec-cloud-path",
        name="MEC-Cloud路径nAgent",
        node="mec-edge-node",
        gateway_id="gw-mec",
        path_id="path-ue-mec-cloud",
    )

    for agent in [drone_capture, terminal_feedback, ue_pagent]:
        ue_gateway.register_agent(agent)
    for agent in [edge_recognition, mec_nagent]:
        mec_gateway.register_agent(agent)
    cloud_gateway.register_agent(cloud_planning)

    intent = Intent(
        task_id="task-disaster-001",
        semantic_goal="灾害现场人员识别、路径规划与高清回传",
        business_agents=(
            "agent-drone-capture",
            "agent-edge-recognition",
            "agent-cloud-planning",
            "agent-terminal-feedback",
        ),
        business_graph=(
            BusinessFlow(
                source="agent-drone-capture",
                target="agent-edge-recognition",
                flow_type="video_stream",
                priority=3,
                bandwidth_mbps=20,
                latency_budget_ms=80,
            ),
            BusinessFlow(
                source="agent-edge-recognition",
                target="agent-cloud-planning",
                flow_type="recognition_result",
                priority=2,
                bandwidth_mbps=5,
                latency_budget_ms=120,
            ),
            BusinessFlow(
                source="agent-cloud-planning",
                target="agent-terminal-feedback",
                flow_type="route_decision",
                priority=2,
                bandwidth_mbps=2,
                latency_budget_ms=150,
            ),
        ),
        qos_requirements=QoSRequirements(
            max_latency_ms=150,
            min_bandwidth_mbps=20,
            reliability=0.99,
            priority=3,
        ),
        support_requirements=SupportRequirements(
            physical=("link_quality", "channel_available"),
            network=("path_state", "bandwidth_mbps", "congestion_level"),
        ),
    )

    gateways = {
        ue_gateway.gateway_id: ue_gateway,
        mec_gateway.gateway_id: mec_gateway,
        cloud_gateway.gateway_id: cloud_gateway,
    }
    return intent, gateways

