from __future__ import annotations

from src.agents import AppAgent, NetAgent, PhyAgentStub, TransAgent
from src.core.gateway import Gateway
from src.core.models import AgentCard, AgentLayer, AgentRole, AgentState
from src.metrics.provider import MetricProvider

_NODE_IP = {
    "ue-drone-node": "10.10.1.2",
    "mec-edge-node": "10.10.2.2",
    "cloud-control-node": "10.10.3.2",
}

_AGENT_PORT = {
    "agent-drone-capture": 9101,
    "agent-edge-recognition": 9201,
    "agent-cloud-planning": 9301,
    "agent-terminal-feedback": 9102,
}


def _card(
    agent_id: str,
    name: str,
    layer: AgentLayer,
    role: AgentRole,
    gateway_id: str,
    subnet_id: str,
    node: str,
    capabilities: tuple[str, ...],
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name=name,
        layer=layer,
        role=role,
        gateway_id=gateway_id,
        subnet_id=subnet_id,
        node=node,
        endpoint=f"sim://{gateway_id}/{agent_id}",
        capabilities=capabilities,
        state=AgentState({}),
        ip=_NODE_IP.get(node, ""),
        port=_AGENT_PORT.get(agent_id),
    )


def build_rescue_topology(metric_provider: MetricProvider) -> dict[str, Gateway]:
    ue = Gateway(
        gateway_id="gw-ue",
        subnet_id="terminal-subnet",
        node="ue-drone-node",
        gateway_ip=_NODE_IP["ue-drone-node"],
    )
    mec = Gateway(
        gateway_id="gw-mec",
        subnet_id="edge-subnet",
        node="mec-edge-node",
        gateway_ip=_NODE_IP["mec-edge-node"],
    )
    cloud = Gateway(
        gateway_id="gw-cloud",
        subnet_id="cloud-subnet",
        node="cloud-control-node",
        gateway_ip=_NODE_IP["cloud-control-node"],
    )

    ue.register(
        AppAgent(
            _card(
                "agent-drone-capture",
                "无人机视频采集Agent",
                AgentLayer.APPLICATION,
                AgentRole.BUSINESS,
                "gw-ue",
                "terminal-subnet",
                "ue-drone-node",
                ("video_capture", "scene_upload"),
            ),
            metric_provider,
        )
    )
    ue.register(
        AppAgent(
            _card(
                "agent-terminal-feedback",
                "现场反馈Agent",
                AgentLayer.APPLICATION,
                AgentRole.BUSINESS,
                "gw-ue",
                "terminal-subnet",
                "ue-drone-node",
                ("decision_feedback", "field_guidance"),
            ),
            metric_provider,
        )
    )
    mec.register(
        AppAgent(
            _card(
                "agent-edge-recognition",
                "边缘识别Agent",
                AgentLayer.APPLICATION,
                AgentRole.BUSINESS,
                "gw-mec",
                "edge-subnet",
                "mec-edge-node",
                ("person_detection", "hazard_recognition"),
            ),
            metric_provider,
        )
    )
    cloud.register(
        AppAgent(
            _card(
                "agent-cloud-planning",
                "云端决策调度Agent",
                AgentLayer.APPLICATION,
                AgentRole.BUSINESS,
                "gw-cloud",
                "cloud-subnet",
                "cloud-control-node",
                ("route_planning", "global_decision"),
            ),
            metric_provider,
        )
    )

    for gateway, subnet, node in [
        (ue, "terminal-subnet", "ue-drone-node"),
        (mec, "edge-subnet", "mec-edge-node"),
        (cloud, "cloud-subnet", "cloud-control-node"),
    ]:
        gateway.register(
            TransAgent(
                _card(
                    f"tagent-{gateway.gateway_id}",
                    f"{gateway.gateway_id}端到端传输Agent",
                    AgentLayer.TRANSPORT,
                    AgentRole.SUPPORT,
                    gateway.gateway_id,
                    subnet,
                    node,
                    ("transport_session", "rtt_monitor", "retransmission_control"),
                ),
                metric_provider,
            )
        )
        gateway.register(
            NetAgent(
                _card(
                    f"nagent-{gateway.gateway_id}",
                    f"{gateway.gateway_id}网络承载Agent",
                    AgentLayer.NETWORK,
                    AgentRole.SUPPORT,
                    gateway.gateway_id,
                    subnet,
                    node,
                    ("network_bearer", "bandwidth_monitor", "congestion_monitor"),
                ),
                metric_provider,
            )
        )

    # Standby network-bearer Agents at the edge and cloud subnets. They stay idle
    # until a primary support Agent fails, enabling a *local* tier-2 replacement.
    # gw-ue deliberately has no standby, so a failure there forces a tier-3
    # cross-subnet reroute instead.
    for gateway, subnet, node in [
        (mec, "edge-subnet", "mec-edge-node"),
        (cloud, "cloud-subnet", "cloud-control-node"),
    ]:
        gateway.register(
            NetAgent(
                _card(
                    f"nagent-{gateway.gateway_id}-standby",
                    f"{gateway.gateway_id}备用网络承载Agent",
                    AgentLayer.NETWORK,
                    AgentRole.SUPPORT,
                    gateway.gateway_id,
                    subnet,
                    node,
                    ("network_bearer", "bandwidth_monitor", "congestion_monitor"),
                ),
                metric_provider,
            )
        )

    ue.register(
        PhyAgentStub(
            _card(
                "pagent-stub-ue",
                "物理层占位pAgent",
                AgentLayer.PHYSICAL,
                AgentRole.SUPPORT,
                "gw-ue",
                "terminal-subnet",
                "ue-drone-node",
                ("phy_stub",),
            ),
            metric_provider,
        )
    )
    return {gateway.gateway_id: gateway for gateway in (ue, mec, cloud)}
