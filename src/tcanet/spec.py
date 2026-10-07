"""Task and network model for TCANet (paper Sec. II-A).

Implements the task specification

    T_m = <m, G_task_m, r_m(t), q^H_m, q^S_m>

where ``G_task_m`` is the validated task DAG of application endpoints
(aAgents) and business dependencies, ``r_m`` gives the traffic demand of
each dependency, ``q^H`` collects hard requirements that must all hold for
feasibility, and ``q^S`` collects soft objectives used to compare feasible
cross-layer decisions.

The gateway connectivity graph ``H = (G, E_gw)`` and the supporting-agent
catalog are also defined here; endpoints are served by gateways through the
binding ``eta(a)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Layer(str, Enum):
    """Cross-layer agent types (paper Sec. II-A)."""

    APPLICATION = "application"  # aAgent
    TRANSPORT = "transport"      # tAgent
    NETWORK = "network"          # nAgent
    PHYSICAL = "physical"        # pAgent


@dataclass(frozen=True)
class HardRequirements:
    """``q^H_m``: every entry must be satisfied for feasibility (Eq. 3)."""

    min_throughput_mbps: float
    max_delay_ms: float
    max_loss_rate: float


@dataclass(frozen=True)
class SoftTarget:
    """One soft objective in ``q^S_m`` with a normalized violation (Eq. 4).

    ``kind="upper"`` reads as x_i <= bound; ``kind="lower"`` as x_i >= bound.
    """

    name: str
    kind: str  # "upper" | "lower"
    bound: float
    metric: str  # projected-state metric this target reads

    def violation(self, value: float) -> float:
        """Normalized excess ``[.]_+`` (paper Eq. 4)."""
        if self.kind == "upper":
            return max(0.0, value - self.bound) / max(self.bound, 1e-9)
        if self.kind == "lower":
            return max(0.0, self.bound - value) / max(self.bound, 1e-9)
        raise ValueError(f"unsupported soft-target kind: {self.kind}")


@dataclass(frozen=True)
class Dependency:
    """One task-DAG edge ``e = (a_i, a_j)`` with demand ``r_{m,e}``."""

    dep_id: str
    source: str
    target: str
    flow_type: str
    demand_mbps: float
    max_delay_ms: float | None = None
    min_throughput_mbps: float | None = None
    max_loss_rate: float | None = None


@dataclass(frozen=True)
class Endpoint:
    """A task endpoint ``a`` executed by an aAgent at gateway ``eta(a)``."""

    agent_id: str
    name: str
    gateway_id: str
    online: bool = True


@dataclass(frozen=True)
class TaskDAG:
    """``G_task_m = (A^a_m, E^b_m)``: endpoints plus dependency edges."""

    task_id: str
    goal: str
    endpoints: tuple[Endpoint, ...]
    dependencies: tuple[Dependency, ...]

    def endpoint(self, agent_id: str) -> Endpoint:
        for endpoint in self.endpoints:
            if endpoint.agent_id == agent_id:
                return endpoint
        raise KeyError(agent_id)

    @property
    def hard_requirements_by_dep(self) -> dict[str, HardRequirements | None]:
        return {dep.dep_id: dep for dep in self.dependencies}


@dataclass(frozen=True)
class TaskSpecification:
    """``T_m`` — TCANet input (paper Eq. 1)."""

    dag: TaskDAG
    hard: HardRequirements
    soft: tuple[SoftTarget, ...] = ()

    def requirements_for(self, dep: Dependency) -> HardRequirements:
        """Per-dependency hard requirements override the task-level ``q^H``."""
        return HardRequirements(
            min_throughput_mbps=(
                dep.min_throughput_mbps
                if dep.min_throughput_mbps is not None
                else self.hard.min_throughput_mbps
            ),
            max_delay_ms=(
                dep.max_delay_ms
                if dep.max_delay_ms is not None
                else self.hard.max_delay_ms
            ),
            max_loss_rate=(
                dep.max_loss_rate
                if dep.max_loss_rate is not None
                else self.hard.max_loss_rate
            ),
        )


@dataclass(frozen=True)
class GatewayLink:
    """One gateway-to-gateway link in ``E_gw`` (directed)."""

    link_id: str
    source_gateway: str
    target_gateway: str
    capacity_mbps: float
    delay_ms: float
    up: bool = True


@dataclass(frozen=True)
class GatewayGraph:
    """``H = (G, E_gw)`` — gateways and inter-gateway links.

    Gateways forward and enforce configuration; agents are logical entities
    associated with gateways (paper Sec. II-A).
    """

    gateways: tuple[str, ...]
    links: tuple[GatewayLink, ...]

    def links_between(self, source: str, target: str) -> tuple[GatewayLink, ...]:
        return tuple(
            link
            for link in self.links
            if link.source_gateway == source and link.target_gateway == target
        )

    def link(self, link_id: str) -> GatewayLink:
        for link in self.links:
            if link.link_id == link_id:
                return link
        raise KeyError(link_id)


@dataclass(frozen=True)
class SharedResource:
    """A shared resource ``r`` with capacity and protected load (Eq. 3).

    ``d^prot_r`` is the load of other admitted tasks and is protected.
    """

    resource_id: str
    capacity_mbps: float
    protected_load_mbps: float = 0.0

    @property
    def available_mbps(self) -> float:
        return max(0.0, self.capacity_mbps - self.protected_load_mbps)


@dataclass(frozen=True)
class SupportAgent:
    """A supporting agent (tAgent / nAgent / pAgent) in the catalog."""

    agent_id: str
    layer: Layer
    gateway_id: str
    name: str = ""
    online: bool = True
    capabilities: tuple[str, ...] = ()


@dataclass
class World:
    """Mutable runtime state the controller observes and reconfigures.

    Holds link states, shared-resource capacities, the supporting-agent
    catalog and endpoint availability.  The controller never modifies task
    agents or dependencies (paper Sec. II-A); it only reads them.
    """

    graph: GatewayGraph
    resources: dict[str, SharedResource] = field(default_factory=dict)
    support_agents: dict[str, SupportAgent] = field(default_factory=dict)
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    failed_gateways: set[str] = field(default_factory=set)
    # "shared": one access resource per gateway (both directions).
    # "duplex": separate uplink/downlink access resources per gateway.
    access_model: str = "shared"
    # Minimum number of alternate paths offered per dependency (0 = only
    # the single-link-exclusion alternates).
    min_path_alternates: int = 0

    def access_resource_ids(
        self, source_gateway: str, target_gateway: str
    ) -> tuple[str, str]:
        """Access resources charged by a flow entering at ``source_gateway``
        and leaving at ``target_gateway``."""
        if self.access_model == "duplex":
            return (f"access-ul:{source_gateway}", f"access-dl:{target_gateway}")
        return (f"access:{source_gateway}", f"access:{target_gateway}")

    def link_resource_id(self, link_id: str) -> str:
        """Shared-resource identity of a link (its residual capacity)."""
        return f"link:{link_id}"

    def resource(self, resource_id: str) -> SharedResource:
        return self.resources[resource_id]

    def gateway_online(self, gateway_id: str) -> bool:
        """Runtime liveness of a gateway (``False`` once it failed)."""
        return (
            gateway_id in self.graph.gateways
            and gateway_id not in self.failed_gateways
        )

    def agent_available(self, agent_id: str) -> bool:
        """A supporting agent can execute when online at a live gateway."""
        agent = self.support_agents.get(agent_id)
        return (
            agent is not None
            and agent.online
            and self.gateway_online(agent.gateway_id)
        )

    def agents_by_layer(self, layer: Layer) -> tuple[SupportAgent, ...]:
        return tuple(
            agent for agent in self.support_agents.values() if agent.layer == layer
        )
