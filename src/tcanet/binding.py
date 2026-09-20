"""Supporting-agent bindings ``Phi_m(e)`` (paper Eq. 2).

The binding associates at most one tAgent, one nAgent and one pAgent with
each dependency; an entry may be empty when the supporting role is not
required.  Supporting agents provide observations or proposals but do not
introduce additional data-plane hops — the binding is distinct from the
gateway path between the two endpoints (paper Fig. 2(a)).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.tcanet.spec import Dependency, Layer, TaskDAG, World


@dataclass(frozen=True)
class SupportBinding:
    """``Phi_m(e) = <a^t, a^n, a^p>`` for one dependency (Eq. 2)."""

    dep_id: str
    t_agent_id: str | None = None
    n_agent_id: str | None = None
    p_agent_id: str | None = None

    def as_tuple(self) -> tuple[str | None, str | None, str | None]:
        return (self.t_agent_id, self.n_agent_id, self.p_agent_id)


@dataclass(frozen=True)
class BindingTable:
    """``Phi_m`` — one binding record per dependency."""

    records: dict[str, SupportBinding] = field(default_factory=dict)  # dep_id -> binding

    def binding(self, dep_id: str) -> SupportBinding:
        return self.records[dep_id]

    def diff(self, other: "BindingTable") -> set[str]:
        """Dependency ids whose binding record changed (for ``|Delta Phi|``)."""
        changed: set[str] = set()
        for dep_id in set(self.records) | set(other.records):
            if self.records.get(dep_id) != other.records.get(dep_id):
                changed.add(dep_id)
        return changed


def default_bindings(dag: TaskDAG, world: World) -> BindingTable:
    """Bind the nearest online supporting agent of each role per dependency.

    Co-located endpoints use local delivery and need not instantiate all
    three supporting roles (paper Sec. II-A): the transport and network
    roles are left empty for same-gateway dependencies.
    """
    records: dict[str, SupportBinding] = {}
    for dep in dag.dependencies:
        source = dag.endpoint(dep.source)
        target = dag.endpoint(dep.target)
        collocated = source.gateway_id == target.gateway_id
        records[dep.dep_id] = SupportBinding(
            dep_id=dep.dep_id,
            t_agent_id=None if collocated else _pick(world, Layer.TRANSPORT, source.gateway_id),
            n_agent_id=None if collocated else _pick(world, Layer.NETWORK, source.gateway_id),
            p_agent_id=_pick(world, Layer.PHYSICAL, source.gateway_id),
        )
    return BindingTable(records=records)


def _pick(world: World, layer: Layer, gateway_id: str) -> str | None:
    """First online supporting agent of ``layer`` at ``gateway_id``."""
    for agent in sorted(world.agents_by_layer(layer), key=lambda item: item.agent_id):
        if agent.gateway_id == gateway_id and agent.online:
            return agent.agent_id
    return None


def failed_support_dependencies(
    dag: TaskDAG,
    bindings: BindingTable,
    failed_agent_ids: set[str],
) -> set[str]:
    """Dependencies associated with failed supporting agents (Sec. IV-B)."""
    affected: set[str] = set()
    for dep in dag.dependencies:
        binding = bindings.binding(dep.dep_id)
        if any(agent_id in failed_agent_ids for agent_id in binding.as_tuple()):
            affected.add(dep.dep_id)
    return affected


def dependencies_of(dag: TaskDAG, agent_id: str) -> tuple[Dependency, ...]:
    return tuple(dep for dep in dag.dependencies if agent_id in (dep.source, dep.target))
