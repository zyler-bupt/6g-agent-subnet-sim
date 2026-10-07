"""Paper Fig. 1 (current draft) rescue scenario for the live prototype.

Task DAG: a1 (UAV) and a2 (roadside camera) stream to a3 (edge AI), which
alerts a4 (rescue vehicle).  Gateways G1-G4 reuse the rescue links L1-L5
and add the direct G1->G4 link L6 so a rejected detour still leaves a
second alternate (paper Alg. 1, lines 19-21).  Access resources are duplex:
a3 receives e1/e2 on G4's downlink and sends e3 on G4's uplink, so e3 is
not coupled to e1 through access capacity.
"""
from __future__ import annotations

from src.tcanet.spec import (
    Dependency,
    Endpoint,
    GatewayGraph,
    GatewayLink,
    HardRequirements,
    Layer,
    SharedResource,
    SoftTarget,
    SupportAgent,
    TaskDAG,
    TaskSpecification,
    World,
)

SCENARIO_ID = "paper_fig1"
GATEWAYS = ("G1", "G2", "G3", "G4")

# (id, source, target, capacity Mbps, delay ms, protected Mbps)
LINKS = (
    ("L1", "G1", "G2", 40.0, 8.0, 12.0),  # 28 Mbps available (Eq. 7 example)
    ("L2", "G2", "G4", 40.0, 10.0, 0.0),
    ("L3", "G1", "G3", 30.0, 18.0, 0.0),
    ("L4", "G4", "G3", 30.0, 12.0, 0.0),
    ("L5", "G3", "G4", 30.0, 12.0, 0.0),
    ("L6", "G1", "G4", 30.0, 20.0, 0.0),
)
ACCESS_CAPACITY_MBPS = 60.0

# (agent id, role description, gateway)
ENDPOINTS = (
    ("a1", "UAV imagery (drone)", "G1"),
    ("a2", "roadside camera", "G3"),
    ("a3", "edge AI", "G4"),
    ("a4", "rescue vehicle", "G3"),
)

# (dependency, source, target, flow type, demand Mbps)
DEPENDENCIES = (
    ("e1", "a1", "a3", "video", 15.0),
    ("e2", "a2", "a3", "video", 8.0),
    ("e3", "a3", "a4", "alert", 5.0),
)


def build_world() -> World:
    """Fresh Fig. 1 world: gateways, links, duplex access, support agents."""
    links = tuple(
        GatewayLink(link_id, source, target, capacity, delay)
        for link_id, source, target, capacity, delay, _ in LINKS
    )
    resources = {
        f"link:{link_id}": SharedResource(f"link:{link_id}", capacity, protected)
        for link_id, _s, _t, capacity, _d, protected in LINKS
    }
    for gateway in GATEWAYS:
        for direction in ("ul", "dl"):
            resource_id = f"access-{direction}:{gateway}"
            resources[resource_id] = SharedResource(resource_id, ACCESS_CAPACITY_MBPS)
    agents = {
        f"{role.value}-{gateway}": SupportAgent(
            agent_id=f"{role.value}-{gateway}",
            layer=role,
            gateway_id=gateway,
            name=f"{role.value}Agent@{gateway}",
        )
        for gateway in GATEWAYS
        for role in (Layer.TRANSPORT, Layer.NETWORK, Layer.PHYSICAL)
    }
    return World(
        graph=GatewayGraph(gateways=GATEWAYS, links=links),
        resources=resources,
        support_agents=agents,
        access_model="duplex",
        min_path_alternates=2,
    )


def fig1_task(world: World) -> TaskSpecification:
    """``T_m`` for the Fig. 1 DAG; also binds endpoints into ``world``."""
    endpoints = tuple(Endpoint(agent_id, name, gw) for agent_id, name, gw in ENDPOINTS)
    deps = tuple(
        Dependency(dep_id=dep_id, source=src, target=dst, flow_type=kind, demand_mbps=rate)
        for dep_id, src, dst, kind, rate in DEPENDENCIES
    )
    dag = TaskDAG(
        task_id=SCENARIO_ID,
        goal="edge-intelligence guided rescue",
        endpoints=endpoints,
        dependencies=deps,
    )
    task = TaskSpecification(
        dag=dag,
        hard=HardRequirements(min_throughput_mbps=4.0, max_delay_ms=60.0, max_loss_rate=0.05),
        soft=(
            SoftTarget("mean_delay", "upper", 25.0, "mean_delay_ms"),
            SoftTarget("utilization", "upper", 0.85, "max_resource_utilization"),
            SoftTarget("total_demand", "lower", 28.0, "total_effective_demand_mbps"),
            SoftTarget("delivered_floor", "lower", 4.0, "min_delivered_mbps"),
        ),
    )
    world.endpoints = {endpoint.agent_id: endpoint for endpoint in endpoints}
    return task


_SCENARIOS = {SCENARIO_ID: (build_world, fig1_task)}


def load(scenario_id: str) -> tuple[World, TaskSpecification]:
    """Fresh ``(world, task)`` for a registered scenario id."""
    build, make_task = _SCENARIOS[scenario_id]
    world = build()
    return world, make_task(world)
