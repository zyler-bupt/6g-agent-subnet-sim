"""Shared builders for tcanet mechanism tests."""
from __future__ import annotations

import asyncio

from src.tcanet.binding import default_bindings
from src.tcanet.spec import (
    Dependency,
    Endpoint,
    GatewayGraph,
    GatewayLink,
    HardRequirements,
    SharedResource,
    SoftTarget,
    SupportAgent,
    TaskDAG,
    TaskSpecification,
    World,
    Layer,
)
from src.tcanet.subnet import SubnetState, TaskSubnetBuilder


def mini_world() -> World:
    """Three gateways GA--GB--GC plus a direct GA--GC detour.

    Link capacities leave ``link:LA`` with 28 Mbps available after its
    protected load, matching the paper's Eq. 3 example.
    """
    links = (
        GatewayLink("LA", "GA", "GB", 40.0, 5.0),
        GatewayLink("LB", "GB", "GC", 30.0, 5.0),
        GatewayLink("LC", "GA", "GC", 20.0, 15.0),
    )
    resources = {
        "link:LA": SharedResource("link:LA", 40.0, 12.0),  # 28 available
        "link:LB": SharedResource("link:LB", 30.0),
        "link:LC": SharedResource("link:LC", 20.0),
        "access:GA": SharedResource("access:GA", 60.0),
        "access:GB": SharedResource("access:GB", 60.0),
        "access:GC": SharedResource("access:GC", 60.0),
    }
    agents = {
        f"{role.value}-{gw}": SupportAgent(
            agent_id=f"{role.value}-{gw}", layer=role, gateway_id=gw
        )
        for gw in ("GA", "GB", "GC")
        for role in (Layer.TRANSPORT, Layer.NETWORK, Layer.PHYSICAL)
    }
    world = World(
        graph=GatewayGraph(gateways=("GA", "GB", "GC"), links=links),
        resources=resources,
        support_agents=agents,
    )
    return world


def mini_task(world: World, d1_demand: float = 10.0) -> TaskSpecification:
    a = Endpoint("a", "source endpoint", "GA")
    b = Endpoint("b", "sink endpoint", "GC")
    dag = TaskDAG(
        task_id="mini",
        goal="mini task",
        endpoints=(a, b),
        dependencies=(
            Dependency("d1", "a", "b", "flow", d1_demand),
        ),
    )
    world.endpoints = {ep.agent_id: ep for ep in dag.endpoints}
    return TaskSpecification(
        dag=dag,
        hard=HardRequirements(
            min_throughput_mbps=4.0, max_delay_ms=60.0, max_loss_rate=0.05
        ),
        soft=(
            SoftTarget("delay", "upper", 25.0, "mean_delay_ms"),
            SoftTarget("util", "upper", 0.85, "max_resource_utilization"),
            SoftTarget("demand_floor", "lower", 8.0, "total_effective_demand_mbps"),
        ),
    )


def built_subnet(task: TaskSpecification, world: World) -> SubnetState:
    """Constructed v1 subnet (accepted) for the mini world."""
    bindings = default_bindings(task.dag, world)

    async def _build():
        return await TaskSubnetBuilder().build(task, world, bindings)

    plan = asyncio.run(_build())
    return SubnetState(
        task_id=task.dag.task_id,
        version=plan.next_version,
        paths=plan.paths,
        forwarding=plan.forwarding,
        bindings=plan.bindings,
        accepted=True,
    )
