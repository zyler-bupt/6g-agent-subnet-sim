"""Paper Fig. 1 rescue scenario: topology, task, events, episodes.

Topology (4 gateways, directed links)::

                 L1 (40, 8ms; 12 protected -> 28 avail)
    G1 ────────────────────────► G2
     │                           │
     │ L3 (30, 18ms)             │ L2 (40, 10ms)
     ▼                           ▼
    G3 ◄─────────────────────── G4
                 L4 (30, 12ms)
    G3 ───────────────────────► G4   (L5, 30, 12ms)

Task (edge-intelligence rescue chain):

* ``drone``   @G1 — imagery capture (UAV)
* ``detect``  @G4 — victim detection
* ``assess``  @G4 — multimodal situation assessment (collocated w/ detect)
* ``dispatch``@G3 — rescue dispatch

Dependencies:

* ``e1`` drone->detect, 15 Mbps video (path G1->G2->G4)
* ``e2`` detect->assess, 8 Mbps (local delivery at G4)
* ``e3`` assess->dispatch, 5 Mbps (path G4->G3)
* ``e4`` detect->dispatch, 5 Mbps (path G4->G3)

Two runtime episodes (paper Fig. 1(b)-1(c)):

1. **Demand-capacity mismatch** — the drone upgrades to a high-resolution
   stream: ``r_{m,e1}`` grows 15 -> 25 Mbps.  On link L1 only 28 Mbps are
   available (40 capacity minus 12 protected), so the source-rate increase
   cannot be combined with the 20%-overhead reliable transport mode:
   ``25 x 1.2 = 30 > 28`` (paper Sec. III-A example, reproduced exactly).
2. **Transit-gateway failure** — G2 fails; every path traversing it (e1)
   is rerouted over G1->G3->G4 while the closure pulls in the remaining
   dependencies through the shared access resource at G4.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, replace
from time import perf_counter

from src.tcanet.binding import BindingTable, default_bindings
from src.tcanet.closure import (
    ClosureResult,
    RuntimeEvent,
    demand_change,
    gateway_failure,
    support_failure,
)
from src.tcanet.dataplane import Dataplane, NullDataplane
from src.tcanet.executor import ExecutionRecord, StagedDecision, execute_staged
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
from src.tcanet.subnet import ConstructionPlan, SubnetState, TaskSubnetBuilder
from src.tcanet.verify import (
    DEFAULT_MAX_ATTEMPTS,
    MeasurementProvider,
    RecoveryController,
    RecoveryResult,
    WindowResult,
    evaluate_window,
    projected_measurements,
)

GATEWAYS = ("G1", "G2", "G3", "G4")

# Links: (id, source, target, capacity, delay, protected load)
_LINKS = (
    ("L1", "G1", "G2", 40.0, 8.0, 12.0),  # 28 Mbps available (Eq. 7 example)
    ("L2", "G2", "G4", 40.0, 10.0, 0.0),
    ("L3", "G1", "G3", 30.0, 18.0, 0.0),
    ("L4", "G4", "G3", 30.0, 12.0, 0.0),
    ("L5", "G3", "G4", 30.0, 12.0, 0.0),
)

_ACCESS_CAPACITY = 60.0

E1_DEMAND_BASE = 15.0
E1_DEMAND_HIGH = 25.0  # 25 x 1.2 = 30 > 28 (paper Sec. III-A)


def build_world() -> World:
    """Fresh rescue world: topology, shared resources, support agents."""
    links = tuple(
        GatewayLink(
            link_id=link_id,
            source_gateway=source,
            target_gateway=target,
            capacity_mbps=capacity,
            delay_ms=delay,
        )
        for link_id, source, target, capacity, delay, _ in _LINKS
    )
    resources: dict[str, SharedResource] = {}
    for link_id, _s, _t, capacity, _d, protected in _LINKS:
        resources[f"link:{link_id}"] = SharedResource(
            resource_id=f"link:{link_id}",
            capacity_mbps=capacity,
            protected_load_mbps=protected,
        )
    for gateway in GATEWAYS:
        resources[f"access:{gateway}"] = SharedResource(
            resource_id=f"access:{gateway}", capacity_mbps=_ACCESS_CAPACITY
        )
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
    )


def rescue_task(world: World, e1_demand: float = E1_DEMAND_BASE) -> TaskSpecification:
    """``T_m`` for the rescue chain; also binds endpoints into ``world``."""
    drone = Endpoint("drone", "imagery capture (UAV)", "G1")
    detect = Endpoint("detect", "victim detection", "G4")
    assess = Endpoint("assess", "multimodal assessment", "G4")
    dispatch = Endpoint("dispatch", "rescue dispatch", "G3")
    deps = (
        Dependency(
            dep_id="e1", source="drone", target="detect",
            flow_type="video", demand_mbps=e1_demand,
        ),
        Dependency(
            dep_id="e2", source="detect", target="assess",
            flow_type="features", demand_mbps=8.0,
        ),
        Dependency(
            dep_id="e3", source="assess", target="dispatch",
            flow_type="request", demand_mbps=5.0,
        ),
        Dependency(
            dep_id="e4", source="detect", target="dispatch",
            flow_type="summary", demand_mbps=5.0,
        ),
    )
    dag = TaskDAG(
        task_id="rescue",
        goal="edge-intelligence guided rescue",
        endpoints=(drone, detect, assess, dispatch),
        dependencies=deps,
    )
    hard = HardRequirements(
        min_throughput_mbps=4.0, max_delay_ms=60.0, max_loss_rate=0.05
    )
    soft = (
        SoftTarget("mean_delay", "upper", 25.0, "mean_delay_ms"),
        SoftTarget("utilization", "upper", 0.85, "max_resource_utilization"),
        SoftTarget("total_demand", "lower", 40.0, "total_effective_demand_mbps"),
        SoftTarget("delivered_floor", "lower", 4.0, "min_delivered_mbps"),
    )
    task = TaskSpecification(dag=dag, hard=hard, soft=soft)
    world.endpoints = {
        endpoint.agent_id: endpoint for endpoint in dag.endpoints
    }
    return task


def with_demand(task: TaskSpecification, dep_id: str, demand: float):
    """Updated ``r_m`` after a demand-change event (frozen replace)."""
    deps = tuple(
        replace(dep, demand_mbps=demand) if dep.dep_id == dep_id else dep
        for dep in task.dag.dependencies
    )
    return replace(task, dag=replace(task.dag, dependencies=deps))


def fail_gateway(world: World, gateway_id: str) -> None:
    """Take a gateway down: its links, access resource and agents go too."""
    links = tuple(
        replace(link, up=False)
        if gateway_id in (link.source_gateway, link.target_gateway)
        else link
        for link in world.graph.links
    )
    world.graph = replace(world.graph, links=links)
    world.failed_gateways.add(gateway_id)
    for agent_id, agent in world.support_agents.items():
        if agent.gateway_id == gateway_id:
            world.support_agents[agent_id] = replace(agent, online=False)


def fail_support_agent(world: World, agent_id: str) -> None:
    """Take one supporting agent down (its gateway stays up)."""
    agent = world.support_agents.get(agent_id)
    if agent is not None:
        world.support_agents[agent_id] = replace(agent, online=False)


@dataclass(frozen=True)
class FormationOutcome:
    """Result of the initial subnet construction (paper Sec. IV-A)."""

    plan: ConstructionPlan
    staged: StagedDecision
    execution: ExecutionRecord
    window: WindowResult
    subnet: SubnetState | None  # accepted v1 on success
    formation_latency_ms: float


async def run_formation(
    task: TaskSpecification,
    world: World,
    *,
    dataplane: Dataplane | None = None,
    measure: MeasurementProvider | None = None,
) -> FormationOutcome:
    """Endpoint confirmation -> paths -> FT -> Apply -> Assess -> commit v1.

    A failed assessment rolls the staged state back (paper Alg. 1, line 19).
    """
    dataplane = dataplane or NullDataplane()
    started = perf_counter()
    bindings = default_bindings(task.dag, world)
    plan = await TaskSubnetBuilder().build(task, world, bindings)
    v0 = SubnetState(
        task_id=task.dag.task_id, version=0, bindings=BindingTable()
    )
    v1 = SubnetState(
        task_id=task.dag.task_id,
        version=plan.next_version,
        paths=plan.paths,
        forwarding=plan.forwarding,
        bindings=plan.bindings,
        accepted=False,
    )
    staged = StagedDecision(
        previous=v0, subnet=v1, actions=(), executable=plan.actions
    )
    execution = await dataplane.apply(staged, world)
    affected = tuple(sorted(dep.dep_id for dep in task.dag.dependencies))
    if measure is not None:
        observations = measure(staged, world, affected)
        if inspect.isawaitable(observations):
            observations = await observations
    else:
        observations = projected_measurements(
            staged, world, affected, task=task, arrival_ms=40.0
        )
    window = evaluate_window(
        task, world, staged, execution, affected, observations
    )
    if not window.accepted:
        await dataplane.rollback(staged, world)
    accepted = replace(v1, accepted=True) if window.accepted else None
    return FormationOutcome(
        plan=plan,
        staged=staged,
        execution=execution,
        window=window,
        subnet=accepted,
        formation_latency_ms=(perf_counter() - started) * 1000.0,
    )


def run_coordination(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
) -> RecoveryResult:
    """Episode 1: drone stream 15 -> 25 Mbps on a 28 Mbps available link."""
    event = demand_change("e1", E1_DEMAND_HIGH / E1_DEMAND_BASE)
    return asyncio.run(
        _recover(task, world, subnet, event)
    )


def run_gateway_recovery(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
) -> RecoveryResult:
    """Episode 2: transit gateway G2 fails (Fig. 1(b))."""
    fail_gateway(world, "G2")
    event = gateway_failure("G2")
    return asyncio.run(_recover(task, world, subnet, event))


class FirstAttemptViolating:
    """Measurement source whose first window reports a violating delay.

    Scripted (not measured) — kept for the in-process demos/tests only; the
    live prototype triggers rollback through real assessment.  Exercises the
    ``K_max`` exclusion loop: the first selected
    candidate fails verification, is excluded, and the next attempt
    re-evaluates the remaining alternatives on the latest state.
    """

    def __init__(self, task: TaskSpecification, dep_id: str, extra_delay_ms: float):
        self.task = task
        self.dep_id = dep_id
        self.extra_delay_ms = extra_delay_ms
        self.calls = 0

    def __call__(self, staged, world, dep_ids):
        self.calls += 1
        skew = {self.dep_id: self.extra_delay_ms} if self.calls == 1 else None
        return projected_measurements(
            staged, world, dep_ids, task=self.task, arrival_ms=60.0, skew=skew
        )


def run_support_recovery(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    *,
    demo_retry: bool = True,
) -> RecoveryResult:
    """Episode 3: the physical supporting agent at G4 fails (Sec. IV-B).

    With ``demo_retry`` the first verification window reports a violating
    measurement, demonstrating the ``K_max`` bounded-retry loop: the failed
    alternative is excluded and recovery succeeds on the second attempt.
    """
    failed_agent = "physical-G4"
    fail_support_agent(world, failed_agent)
    event = support_failure(failed_agent)

    async def _run() -> RecoveryResult:
        if demo_retry:
            measure = FirstAttemptViolating(task, "e2", extra_delay_ms=90.0)
            controller = RecoveryController(measure=measure)
        else:
            controller = RecoveryController()
        return await controller.recover(task, subnet, world, event)

    return asyncio.run(_run())


async def _recover(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    event: RuntimeEvent,
) -> RecoveryResult:
    controller = RecoveryController(max_attempts=DEFAULT_MAX_ATTEMPTS)
    return await controller.recover(task, subnet, world, event)


def event_description(event: RuntimeEvent) -> str:
    """One-line human description of a runtime event (for demos)."""
    if event.kind == "demand_change":
        factor = float(event.payload.get("factor", 1.0))
        return f"dependency {event.dep_id}: demand x{factor:.2f}"
    if event.kind == "qos_change":
        return f"dependency {event.dep_id}: QoS target change"
    if event.kind == "endpoint_failure":
        return f"endpoint failure: {', '.join(event.agent_ids)}"
    if event.kind == "link_failure":
        return f"link failure: {event.link_id}"
    if event.kind == "gateway_failure":
        return f"gateway failure: {event.gateway_id}"
    if event.kind == "support_failure":
        return f"supporting-agent failure: {', '.join(event.agent_ids)}"
    return event.kind
