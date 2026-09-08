from __future__ import annotations

from dataclasses import dataclass, field

from src.agents import AppAgent, NetAgent, PhyAgent, TransAgent
from src.agents.base import BaseAgent
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

_AGENT_CLASS_BY_LAYER: dict[AgentLayer, type[BaseAgent]] = {
    AgentLayer.APPLICATION: AppAgent,
    AgentLayer.TRANSPORT: TransAgent,
    AgentLayer.NETWORK: NetAgent,
    AgentLayer.PHYSICAL: PhyAgent,
}


@dataclass(frozen=True)
class GatewaySpec:
    gateway_id: str
    subnet_id: str
    node: str
    gateway_ip: str
    gateway_port: int = 7000


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    name: str
    layer: AgentLayer
    role: AgentRole
    gateway_id: str
    capabilities: tuple[str, ...]
    subnet_id: str = ""
    node: str = ""
    endpoint: str = ""
    ip: str = ""
    port: int | None = None
    status: str = "online"
    state_values: dict[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class TopologyCatalog:
    gateways: tuple[GatewaySpec, ...] = field(default_factory=tuple)
    agents: tuple[AgentSpec, ...] = field(default_factory=tuple)

    def with_gateway(self, gateway: GatewaySpec) -> "TopologyCatalog":
        return TopologyCatalog(
            gateways=self.gateways + (gateway,),
            agents=self.agents,
        )

    def with_agent(self, agent: AgentSpec) -> "TopologyCatalog":
        return TopologyCatalog(
            gateways=self.gateways,
            agents=self.agents + (agent,),
        )

    def with_agents(self, *agents: AgentSpec) -> "TopologyCatalog":
        return TopologyCatalog(
            gateways=self.gateways,
            agents=self.agents + tuple(agents),
        )


def _card_from_spec(spec: AgentSpec, gateway: Gateway) -> AgentCard:
    subnet_id = spec.subnet_id or gateway.subnet_id
    node = spec.node or gateway.node
    return AgentCard(
        agent_id=spec.agent_id,
        name=spec.name,
        layer=spec.layer,
        role=spec.role,
        gateway_id=spec.gateway_id,
        subnet_id=subnet_id,
        node=node,
        endpoint=spec.endpoint or f"sim://{spec.gateway_id}/{spec.agent_id}",
        capabilities=spec.capabilities,
        status=spec.status,
        state=AgentState(dict(spec.state_values)),
        ip=spec.ip or gateway.gateway_ip,
        port=spec.port,
    )


def support_agent_specs(
    gateway_id: str,
    *,
    standby_network: bool = False,
) -> tuple[AgentSpec, ...]:
    agents = (
        AgentSpec(
            agent_id=f"tagent-{gateway_id}",
            name=f"{gateway_id}端到端传输Agent",
            layer=AgentLayer.TRANSPORT,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("transport_session", "rtt_monitor", "retransmission_control"),
        ),
        AgentSpec(
            agent_id=f"nagent-{gateway_id}",
            name=f"{gateway_id}网络承载Agent",
            layer=AgentLayer.NETWORK,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("network_bearer", "bandwidth_monitor", "congestion_monitor"),
        ),
        AgentSpec(
            agent_id=f"pagent-{gateway_id}",
            name=f"{gateway_id}物理接入Agent",
            layer=AgentLayer.PHYSICAL,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=(
                "physical_access",
                "radio_resource_monitor",
                "physical_reservation",
            ),
            state_values={
                "total_capacity_mbps": 100.0,
                "signal_quality": 0.98,
                "reliability": 0.999,
            },
        ),
    )
    if not standby_network:
        return agents
    return agents + (
        AgentSpec(
            agent_id=f"nagent-{gateway_id}-standby",
            name=f"{gateway_id}备用网络承载Agent",
            layer=AgentLayer.NETWORK,
            role=AgentRole.SUPPORT,
            gateway_id=gateway_id,
            capabilities=("network_bearer", "bandwidth_monitor", "congestion_monitor"),
        ),
    )


def rescue_topology_catalog() -> TopologyCatalog:
    # gw-mec and gw-cloud carry standby nAgents for local tier-2 recovery.
    # gw-ue deliberately has no standby, so a primary nAgent failure there still
    # exercises the tier-3 reroute path used by the experiments.
    catalog = TopologyCatalog(
        gateways=(
            GatewaySpec(
                gateway_id="gw-ue",
                subnet_id="terminal-subnet",
                node="ue-drone-node",
                gateway_ip=_NODE_IP["ue-drone-node"],
            ),
            GatewaySpec(
                gateway_id="gw-mec",
                subnet_id="edge-subnet",
                node="mec-edge-node",
                gateway_ip=_NODE_IP["mec-edge-node"],
            ),
            GatewaySpec(
                gateway_id="gw-cloud",
                subnet_id="cloud-subnet",
                node="cloud-control-node",
                gateway_ip=_NODE_IP["cloud-control-node"],
            ),
        ),
    )
    return catalog.with_agents(
        AgentSpec(
            "agent-drone-capture",
            "无人机视频采集Agent",
            AgentLayer.APPLICATION,
            AgentRole.BUSINESS,
            "gw-ue",
            ("video_capture", "scene_upload"),
            port=_AGENT_PORT["agent-drone-capture"],
        ),
        AgentSpec(
            "agent-terminal-feedback",
            "现场反馈Agent",
            AgentLayer.APPLICATION,
            AgentRole.BUSINESS,
            "gw-ue",
            ("decision_feedback", "field_guidance"),
            port=_AGENT_PORT["agent-terminal-feedback"],
        ),
        AgentSpec(
            "agent-edge-recognition",
            "边缘识别Agent",
            AgentLayer.APPLICATION,
            AgentRole.BUSINESS,
            "gw-mec",
            ("person_detection", "hazard_recognition"),
            port=_AGENT_PORT["agent-edge-recognition"],
        ),
        AgentSpec(
            "agent-cloud-planning",
            "云端决策调度Agent",
            AgentLayer.APPLICATION,
            AgentRole.BUSINESS,
            "gw-cloud",
            ("route_planning", "global_decision"),
            port=_AGENT_PORT["agent-cloud-planning"],
        ),
        *support_agent_specs("gw-ue"),
        *support_agent_specs("gw-mec", standby_network=True),
        *support_agent_specs("gw-cloud", standby_network=True),
    )


def build_topology_from_catalog(
    catalog: TopologyCatalog,
    metric_provider: MetricProvider,
) -> dict[str, Gateway]:
    gateways: dict[str, Gateway] = {}
    for spec in catalog.gateways:
        if spec.gateway_id in gateways:
            raise ValueError(f"duplicate gateway_id: {spec.gateway_id}")
        gateways[spec.gateway_id] = Gateway(
            gateway_id=spec.gateway_id,
            subnet_id=spec.subnet_id,
            node=spec.node,
            gateway_ip=spec.gateway_ip,
            gateway_port=spec.gateway_port,
        )

    seen_agents: set[str] = set()
    for spec in catalog.agents:
        gateway = gateways.get(spec.gateway_id)
        if gateway is None:
            raise ValueError(f"agent {spec.agent_id} references unknown gateway {spec.gateway_id}")
        if spec.agent_id in seen_agents:
            raise ValueError(f"duplicate agent_id: {spec.agent_id}")
        agent_class = _AGENT_CLASS_BY_LAYER[spec.layer]
        gateway.register(agent_class(_card_from_spec(spec, gateway), metric_provider))
        seen_agents.add(spec.agent_id)
    return gateways


def build_rescue_topology(metric_provider: MetricProvider) -> dict[str, Gateway]:
    return build_topology_from_catalog(rescue_topology_catalog(), metric_provider)
