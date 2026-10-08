"""Dependency-aware affected-set closure (paper Sec. IV-B, Eq. 13-16).

Given a runtime event, TCANet first identifies the initial affected set
``E^aff_m,0``: dependencies with demand or QoS changes, dependencies
connected to failed endpoints, those whose paths traverse failed links or
gateways, and those associated with failed supporting agents.

To capture indirect effects it then builds the directed dependency
relation ``D_m = D^res_m ∪ D^cfg_m``:

* ``(e, e') in D^res_m`` if changing ``e`` can alter the residual capacity
  of a shared resource used by ``e'``;
* ``(e, e') in D^cfg_m`` if changing ``e`` can modify forwarding, binding
  or precondition state used by ``e'``;

and expands the affected set iteratively,

    E^aff_{m,k+1} = E^aff_{m,k} ∪ { e' : ∃e ∈ E^aff_{m,k}, (e, e') ∈ D_m },

until the fixpoint ``E^aff_m``.  Resource usage is still evaluated over
the entire task; only the reconfiguration/verification scope is scoped.
Task dependencies distant in the DAG may still interact through shared
resources or configuration state.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.tcanet.binding import failed_support_dependencies
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import SubnetState


@dataclass(frozen=True)
class RuntimeEvent:
    """One runtime event triggering elastic reconfiguration (Sec. IV-B)."""

    kind: str  # demand_change | qos_change | endpoint_failure | link_failure | gateway_failure | support_failure
    payload: dict[str, object] = field(default_factory=dict)

    @property
    def dep_id(self) -> str:
        return str(self.payload.get("dep_id", ""))

    @property
    def link_id(self) -> str:
        return str(self.payload.get("link_id", ""))

    @property
    def gateway_id(self) -> str:
        return str(self.payload.get("gateway_id", ""))

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(str(item) for item in self.payload.get("agent_ids", ()))


def demand_change(dep_id: str, factor: float) -> RuntimeEvent:
    return RuntimeEvent("demand_change", {"dep_id": dep_id, "factor": factor})


def qos_change(dep_id: str) -> RuntimeEvent:
    return RuntimeEvent("qos_change", {"dep_id": dep_id})


def endpoint_failure(*agent_ids: str) -> RuntimeEvent:
    return RuntimeEvent("endpoint_failure", {"agent_ids": agent_ids})


def link_failure(link_id: str) -> RuntimeEvent:
    return RuntimeEvent("link_failure", {"link_id": link_id})


def gateway_failure(gateway_id: str) -> RuntimeEvent:
    return RuntimeEvent("gateway_failure", {"gateway_id": gateway_id})


def support_failure(*agent_ids: str) -> RuntimeEvent:
    return RuntimeEvent("support_failure", {"agent_ids": agent_ids})


@dataclass(frozen=True)
class DependencyRelations:
    """``D_m = D^res ∪ D^cfg`` with per-pair reasons."""

    resource_pairs: dict[tuple[str, str], str] = field(default_factory=dict)
    config_pairs: dict[tuple[str, str], str] = field(default_factory=dict)

    def __contains__(self, pair: tuple[str, str]) -> bool:
        return pair in self.resource_pairs or pair in self.config_pairs

    def reason(self, pair: tuple[str, str]) -> str:
        if pair in self.resource_pairs:
            return self.resource_pairs[pair]
        return self.config_pairs.get(pair, "")

    def all_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(set(self.resource_pairs) | set(self.config_pairs)))


def initial_affected_set(
    task: TaskSpecification,
    subnet: SubnetState,
    world: World,
    event: RuntimeEvent,
) -> dict[str, str]:
    """``E^aff_m,0`` — directly perturbed dependencies with reasons."""
    affected: dict[str, str] = {}
    if event.kind in {"demand_change", "qos_change"}:
        affected[event.dep_id] = (
            "demand change" if event.kind == "demand_change" else "QoS change"
        )
    if event.kind == "endpoint_failure":
        for dep in task.dag.dependencies:
            if set(event.agent_ids) & {dep.source, dep.target}:
                affected[dep.dep_id] = "failed endpoint"
    if event.kind == "link_failure":
        for dep_id, record in subnet.paths.items():
            if event.link_id in record.link_ids:
                affected[dep_id] = "path traverses failed link"
    if event.kind == "gateway_failure":
        for dep_id, record in subnet.paths.items():
            if event.gateway_id in record.gateway_path:
                affected[dep_id] = "path traverses failed gateway"
        agents_at = {
            agent_id
            for agent_id, agent in world.support_agents.items()
            if agent.gateway_id == event.gateway_id
        }
        for dep_id in failed_support_dependencies(
            task.dag, subnet.bindings, agents_at
        ):
            affected.setdefault(
                dep_id, "supporting agent at failed gateway"
            )
    if event.kind == "support_failure":
        for dep_id in failed_support_dependencies(
            task.dag, subnet.bindings, set(event.agent_ids)
        ):
            affected[dep_id] = "failed supporting agent"
    return affected


def dependency_relations(
    task: TaskSpecification,
    subnet: SubnetState,
    world: World | None = None,
) -> DependencyRelations:
    """Build ``D^res`` (shared resources) and ``D^cfg`` (configuration state).

    ``D^res`` links dependencies whose gateway paths share a link resource
    or whose endpoints share an access resource.  ``D^cfg`` links
    dependencies that share an intermediate gateway's forwarding state or
    a supporting-agent binding.
    """
    deps = [dep.dep_id for dep in task.dag.dependencies]
    resource_pairs: dict[tuple[str, str], str] = {}
    config_pairs: dict[tuple[str, str], str] = {}

    link_users: dict[str, set[str]] = {}
    access_users: dict[str, set[str]] = {}
    gateway_users: dict[str, set[str]] = {}
    binding_users: dict[str, set[str]] = {}
    for dep in task.dag.dependencies:
        record = subnet.paths.get(dep.dep_id)
        if record is None:
            continue
        for link_id in record.link_ids:
            link_users.setdefault(link_id, set()).add(dep.dep_id)
        for gateway_id in record.gateway_path:
            gateway_users.setdefault(gateway_id, set()).add(dep.dep_id)
        if record.gateway_path:
            first, last = record.gateway_path[0], record.gateway_path[-1]
            access_ids = (
                world.access_resource_ids(first, last)
                if world is not None
                else (f"access:{first}", f"access:{last}")
            )
            for access_id in access_ids:
                access_users.setdefault(access_id, set()).add(dep.dep_id)
        binding = subnet.bindings.binding(dep.dep_id)
        for agent_id in binding.as_tuple():
            if agent_id:
                binding_users.setdefault(agent_id, set()).add(dep.dep_id)

    def _register(
        table: dict[tuple[str, str], str],
        users: dict[str, set[str]],
        reason_fmt: str,
    ) -> None:
        for members in users.values():
            for left in sorted(members):
                for right in sorted(members):
                    if left == right:
                        continue
                    table.setdefault((left, right), reason_fmt)

    _register(resource_pairs, link_users, "shared link resource")
    _register(resource_pairs, access_users, "shared access resource")
    _register(
        config_pairs,
        {
            gateway: members
            for gateway, members in gateway_users.items()
            if any(
                gateway in subnet.paths[dep_id].gateway_path[1:-1]
                for dep_id in members
            )
        },
        "shared gateway forwarding state",
    )
    _register(config_pairs, binding_users, "shared supporting-agent binding")
    return DependencyRelations(
        resource_pairs=resource_pairs,
        config_pairs=config_pairs,
    )


@dataclass(frozen=True)
class ClosureRound:
    """One expansion iteration ``k -> k+1`` (for the demo animation)."""

    index: int
    added: dict[str, str]  # dep_id -> pulling reason


@dataclass(frozen=True)
class ClosureResult:
    initial: dict[str, str]
    rounds: tuple[ClosureRound, ...]
    final: frozenset[str]

    @property
    def expanded_beyond_initial(self) -> bool:
        return len(self.final) > len(self.initial)


def expand_affected_set(
    initial: dict[str, str],
    relations: DependencyRelations,
) -> ClosureResult:
    """Iterate Eq. 15 until the fixed point ``E^aff_m``."""
    current = dict(initial)
    rounds: list[ClosureRound] = []
    index = 0
    while True:
        newly: dict[str, str] = {}
        for left in sorted(current):
            for right, reason in sorted(relations.config_pairs.items()):
                if right in newly:
                    continue
                if left == right[0] and right[1] not in current:
                    newly[right[1]] = f"{reason} (via {left})"
            for right, reason in sorted(relations.resource_pairs.items()):
                if right in newly:
                    continue
                if left == right[0] and right[1] not in current:
                    newly[right[1]] = f"{reason} (via {left})"
        if not newly:
            break
        index += 1
        rounds.append(ClosureRound(index=index, added=dict(newly)))
        current.update(newly)
    return ClosureResult(
        initial=dict(initial),
        rounds=tuple(rounds),
        final=frozenset(current),
    )
