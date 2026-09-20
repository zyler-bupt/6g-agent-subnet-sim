"""Joint decision feasibility on the projected task state (paper Sec. III-A).

Before execution the controller evaluates the combined effect of
``u = (u^a, u^t, u^n, u^p)`` on the common observed task state ``x^0_m``,
yielding the post-action state ``x_hat_m(u)``.  A joint decision is
feasible only if its actions are mutually compatible, the required
executors are authorized and available, the projected state satisfies all
hard requirements ``q^H_m``, and the shared-resource constraint (Eq. 3)

    sum_e d_{m,e,r}(u) + d^prot_r  <=  C_r(u),   r in Omega^sh_m

holds with transport overhead included (paper example: a 15 Mbps flow on a
28 Mbps path cannot take a 25 Mbps source-rate increase together with a
20% overhead transport mode, since 25 x 1.2 = 30 > 28).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.tcanet.candidates import CandidateAction
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import SubnetState

_EPS = 1e-9
_BASE_LINK_LOSS = 0.001
_ACCESS_LATENCY_MS = 2.0
_CONGESTION_ONSET = 0.70
_CONGESTION_MS_PER_UNIT = 20.0


@dataclass(frozen=True)
class DependencyProjection:
    """Projected per-dependency service metrics inside ``x_hat_m(u)``."""

    dep_id: str
    gateway_path: tuple[str, ...]
    effective_demand_mbps: float
    delivered_mbps: float
    delay_ms: float
    loss_rate: float
    bottleneck_resource_id: str | None
    bottleneck_available_mbps: float


@dataclass(frozen=True)
class Projection:
    """``x_hat_m(u)`` — the post-action task state."""

    dependencies: dict[str, DependencyProjection]
    resource_load_mbps: dict[str, float]
    resource_capacity_mbps: dict[str, float]
    actions: tuple[CandidateAction, ...]

    @property
    def mean_delay_ms(self) -> float:
        if not self.dependencies:
            return 0.0
        return sum(item.delay_ms for item in self.dependencies.values()) / len(
            self.dependencies
        )

    @property
    def total_effective_demand_mbps(self) -> float:
        return sum(item.effective_demand_mbps for item in self.dependencies.values())

    @property
    def max_resource_utilization(self) -> float:
        if not self.resource_capacity_mbps:
            return 0.0
        return max(
            load / max(capacity, _EPS)
            for load, capacity in zip(
                (
                    self.resource_load_mbps.get(rid, 0.0)
                    for rid in self.resource_capacity_mbps
                ),
                self.resource_capacity_mbps.values(),
            )
        )

    @property
    def min_delivered_mbps(self) -> float:
        if not self.dependencies:
            return 0.0
        return min(item.delivered_mbps for item in self.dependencies.values())


@dataclass(frozen=True)
class FeasibilityResult:
    feasible: bool
    violations: tuple[str, ...]
    projection: Projection


def project_state(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    actions: tuple[CandidateAction, ...],
) -> Projection:
    """Project ``x_hat_m(u)`` from the observed state and joint decision."""
    rate_overrides: dict[str, float] = {}
    overhead: dict[str, float] = {}
    loss_factors: dict[str, float] = {}
    path_overrides: dict[str, tuple[str, ...]] = {}
    capacity_boost: dict[str, float] = {}
    for action in actions:
        if action.is_no_change:
            continue
        if action.action == "ADJUST_RATE":
            rate_overrides[action.target] = float(action.parameters["rate_mbps"])
        elif action.action == "SWITCH_MODE":
            overhead[action.target] = float(action.parameters["overhead_factor"])
            loss_factors[action.target] = float(action.parameters["loss_factor"])
        elif action.action == "REROUTE":
            path_overrides[action.target] = tuple(
                str(node) for node in action.parameters["gateway_path"]
            )
        elif action.action == "BOOST_ACCESS":
            capacity_boost[action.target] = capacity_boost.get(action.target, 0.0) + float(
                action.parameters["boost_mbps"]
            )

    resource_capacity: dict[str, float] = {}
    for resource_id, resource in world.resources.items():
        resource_capacity[resource_id] = resource.available_mbps
    for resource_id, boost in capacity_boost.items():
        access_id = f"access:{resource_id}"
        if access_id in resource_capacity:
            resource_capacity[access_id] += boost

    dependencies: dict[str, DependencyProjection] = {}
    resource_load: dict[str, float] = {rid: 0.0 for rid in resource_capacity}
    for dep in sorted(task.dag.dependencies, key=lambda item: item.dep_id):
        path_record = subnet.paths.get(dep.dep_id)
        path = path_overrides.get(
            dep.dep_id,
            path_record.gateway_path if path_record else (),
        )
        rate = rate_overrides.get(dep.dep_id, dep.demand_mbps)
        overhead_factor = overhead.get(dep.dep_id, 1.0)
        loss_factor = loss_factors.get(dep.dep_id, 1.0)
        effective = rate * overhead_factor

        delay = _ACCESS_LATENCY_MS
        loss = _BASE_LINK_LOSS * loss_factor
        bottleneck_id: str | None = None
        bottleneck_available = float("inf")
        for index in range(len(path) - 1 if len(path) > 1 else 0):
            source, target = path[index], path[index + 1]
            link = next(
                (
                    item
                    for item in world.graph.links
                    if item.source_gateway == source and item.target_gateway == target
                ),
                None,
            )
            if link is None or not link.up:
                delay += 8.0
                loss = min(1.0, loss + 0.10)
                continue
            delay += link.delay_ms
            loss = min(1.0, loss + _BASE_LINK_LOSS)
            rid = world.link_resource_id(link.link_id)
            available = resource_capacity.get(rid, link.capacity_mbps)
            if available < bottleneck_available:
                bottleneck_available = available
                bottleneck_id = rid
            resource_load[rid] = resource_load.get(rid, 0.0) + effective
        # Access capacity of the serving gateways is also a shared resource.
        for gateway_id in (path[0], path[-1]) if path else ():
            access_id = f"access:{gateway_id}"
            if access_id in resource_capacity:
                resource_load[access_id] = resource_load.get(access_id, 0.0) + effective

        delivered = effective if bottleneck_available == float("inf") else min(
            effective, bottleneck_available
        )
        dependencies[dep.dep_id] = DependencyProjection(
            dep_id=dep.dep_id,
            gateway_path=tuple(path),
            effective_demand_mbps=effective,
            delivered_mbps=delivered,
            delay_ms=delay,
            loss_rate=loss,
            bottleneck_resource_id=bottleneck_id,
            bottleneck_available_mbps=(
                0.0 if bottleneck_available == float("inf") else bottleneck_available
            ),
        )

    # Congestion delay is a joint consequence of the selected transport
    # action and the background load (paper Sec. III-A discussion).
    for item in dependencies.values():
        if item.bottleneck_resource_id is None:
            continue
        capacity = resource_capacity.get(item.bottleneck_resource_id, _EPS)
        load = resource_load.get(item.bottleneck_resource_id, 0.0)
        utilization = load / max(capacity, _EPS)
        if utilization > _CONGESTION_ONSET:
            dependencies[item.dep_id] = DependencyProjection(
                dep_id=item.dep_id,
                gateway_path=item.gateway_path,
                effective_demand_mbps=item.effective_demand_mbps,
                delivered_mbps=item.delivered_mbps,
                delay_ms=item.delay_ms
                + _CONGESTION_MS_PER_UNIT * (utilization - _CONGESTION_ONSET),
                loss_rate=item.loss_rate,
                bottleneck_resource_id=item.bottleneck_resource_id,
                bottleneck_available_mbps=item.bottleneck_available_mbps,
            )

    return Projection(
        dependencies=dependencies,
        resource_load_mbps=resource_load,
        resource_capacity_mbps=resource_capacity,
        actions=actions,
    )


def evaluate_joint_decision(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    actions: tuple[CandidateAction, ...],
) -> FeasibilityResult:
    """Joint feasibility check across the four layers (paper Sec. III-A)."""
    violations: list[str] = []

    # Mutual compatibility: two actions must not write the same state field.
    writers: dict[str, str] = {}
    for action in actions:
        if action.is_no_change:
            continue
        for field_name in sorted(action.write_fields):
            if field_name in writers:
                violations.append(
                    f"write_conflict:{field_name}:{writers[field_name]}+{action.action_id}"
                )
            else:
                writers[field_name] = action.action_id

    # Executor authorization and availability.
    for action in actions:
        if action.is_no_change:
            continue
        if action.action == "REBIND_SUPPORT":
            if not world.agent_available(str(action.parameters["agent_id"])):
                violations.append(f"executor_unavailable:{action.action_id}")
        gateway = action.preconditions.get("gateway_online")
        if gateway is not None and not world.gateway_online(gateway):
            violations.append(f"precondition_failed:{action.action_id}")

    # Binding precondition: a dependency bound to an unavailable supporting
    # agent must be rebound by this decision (Sec. IV-B rebind class).
    for dep in task.dag.dependencies:
        binding = subnet.bindings.binding(dep.dep_id)
        for role, agent_id in (
            ("t", binding.t_agent_id),
            ("n", binding.n_agent_id),
            ("p", binding.p_agent_id),
        ):
            if agent_id and not world.agent_available(agent_id):
                rebound = any(
                    action.action == "REBIND_SUPPORT"
                    and action.target == dep.dep_id
                    and str(action.parameters.get("role")) == role
                    for action in actions
                    if not action.is_no_change
                )
                if not rebound:
                    violations.append(f"binding_offline:{dep.dep_id}:{role}")

    projection = project_state(task, world, subnet, actions)

    # Hard requirements q^H per dependency.
    for dep in task.dag.dependencies:
        item = projection.dependencies[dep.dep_id]
        requirements = task.requirements_for(dep)
        if item.delivered_mbps + _EPS < requirements.min_throughput_mbps:
            violations.append(f"throughput_hard:{dep.dep_id}")
        if item.delay_ms > requirements.max_delay_ms + _EPS:
            violations.append(f"delay_hard:{dep.dep_id}")
        if item.loss_rate > requirements.max_loss_rate + _EPS:
            violations.append(f"loss_hard:{dep.dep_id}")

    # Shared-resource constraint (Eq. 3): protected load already deducted
    # from capacity via ``SharedResource.available_mbps``; the projected
    # task load includes transport overhead.
    for resource_id, capacity in projection.resource_capacity_mbps.items():
        load = projection.resource_load_mbps.get(resource_id, 0.0)
        if load > capacity + _EPS:
            violations.append(f"shared_resource:{resource_id}")

    return FeasibilityResult(
        feasible=not violations,
        violations=tuple(violations),
        projection=projection,
    )
