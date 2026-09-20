"""Authorized candidate actions per layer (paper Sec. II-B, III-A, IV-B).

Each agent combines a reasoning module with a layer-specific tool adapter
and generates a bounded set of authorized candidate actions from local
observations.  Every candidate set ``U^l_m`` includes a no-change option;
each candidate specifies its target, preconditions, expected effect and
the state fields it reads or modifies.

For reconfiguration, candidates fall into three classes (paper Sec. IV-B):
parameter changes that retain the current paths and bindings, forwarding
restoration or supporting-agent rebinding on an unchanged path, and path
replacement or recompilation of affected dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Iterable

from src.tcanet.spec import Layer, TaskSpecification, World
from src.tcanet.subnet import SubnetState, solve_dependency_paths

# Recovery candidate classes (paper Sec. IV-B).
CLASS_PARAMETER = "parameter"                  # keep paths and bindings
CLASS_REBIND = "rebind_same_path"              # forwarding/binding restore
CLASS_PATH_REPLACE = "path_replace"            # path replacement/recompile

NO_CHANGE = "NO_CHANGE"


@dataclass(frozen=True)
class TaskObservation:
    """``x^0_m`` — task traffic, service metrics, shared-resource loads.

    Required telemetry is refreshed when measurements are stale (paper
    Sec. III-A); ``observed_version`` carries the subnet version the
    observation was taken at so staleness can be detected.
    """

    subnet_version: int
    per_dep_demand_mbps: dict[str, float] = field(default_factory=dict)
    per_dep_delay_ms: dict[str, float] = field(default_factory=dict)
    per_dep_loss: dict[str, float] = field(default_factory=dict)
    resource_load_mbps: dict[str, float] = field(default_factory=dict)
    executor_online: dict[str, bool] = field(default_factory=dict)
    observed_version: int = 0

    def stale_against(self, subnet: SubnetState) -> bool:
        return self.observed_version != subnet.version


@dataclass(frozen=True)
class CandidateAction:
    """One authorized candidate action ``u^l_m`` of layer ``l``."""

    action_id: str
    layer: Layer
    action: str
    target: str  # dependency id, gateway id or resource id
    preconditions: dict[str, str] = field(default_factory=dict)
    expected_effect: dict[str, float] = field(default_factory=dict)
    read_fields: frozenset[str] = frozenset()
    write_fields: frozenset[str] = frozenset()
    parameters: dict[str, float | int | str | bool] = field(default_factory=dict)
    recovery_class: str | None = None  # None for pure coordination decisions

    @property
    def is_no_change(self) -> bool:
        return self.action == NO_CHANGE


def no_change(layer: Layer) -> CandidateAction:
    return CandidateAction(
        action_id=f"{layer.value}:{NO_CHANGE}",
        layer=layer,
        action=NO_CHANGE,
        target="*",
        expected_effect={},
    )


def coordination_candidates(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    observation: TaskObservation,
    *,
    focus_dep_ids: Iterable[str] = (),
) -> dict[Layer, tuple[CandidateAction, ...]]:
    """Bounded per-layer candidate sets ``U^l_m`` incl. the no-change option.

    ``focus_dep_ids`` restricts application/transport proposals to the
    dependencies the runtime event concerns; network and physical
    candidates are generated for every dependency/gateway they can help.
    """
    focus = tuple(sorted(focus_dep_ids)) or tuple(
        sorted(dep.dep_id for dep in task.dag.dependencies)
    )

    application = [no_change(Layer.APPLICATION)]
    for dep_id in focus:
        current = observation.per_dep_demand_mbps.get(dep_id, 0.0)
        for rate in _rate_ladder(current):
            application.append(
                CandidateAction(
                    action_id=f"application:ADJUST_RATE:{dep_id}:{rate:g}",
                    layer=Layer.APPLICATION,
                    action="ADJUST_RATE",
                    target=dep_id,
                    expected_effect={"demand_mbps": rate},
                    read_fields=frozenset({f"application:rate:{dep_id}"}),
                    write_fields=frozenset({f"application:rate:{dep_id}"}),
                    parameters={"rate_mbps": rate, "base_mbps": current},
                    recovery_class=CLASS_PARAMETER,
                )
            )

    transport = [no_change(Layer.TRANSPORT)]
    for dep_id in focus:
        for mode, overhead, loss_factor in (
            ("standard", 1.00, 1.0),
            ("reliable", 1.20, 0.5),
            ("lightweight", 0.95, 1.5),
        ):
            transport.append(
                CandidateAction(
                    action_id=f"transport:SWITCH_MODE:{dep_id}:{mode}",
                    layer=Layer.TRANSPORT,
                    action="SWITCH_MODE",
                    target=dep_id,
                    expected_effect={
                        "overhead_factor": overhead,
                        "loss_factor": loss_factor,
                    },
                    read_fields=frozenset({f"transport:mode:{dep_id}"}),
                    write_fields=frozenset({f"transport:mode:{dep_id}"}),
                    parameters={
                        "mode": mode,
                        "overhead_factor": overhead,
                        "loss_factor": loss_factor,
                    },
                    recovery_class=CLASS_PARAMETER,
                )
            )

    network = [no_change(Layer.NETWORK)]
    for dep_id in focus:
        for alt in _alternate_paths(task, world, subnet, dep_id):
            network.append(
                CandidateAction(
                    action_id=(
                        f"network:REROUTE:{dep_id}:{'|'.join(alt.gateway_path)}"
                    ),
                    layer=Layer.NETWORK,
                    action="REROUTE",
                    target=dep_id,
                    expected_effect={"path_change": 1.0},
                    read_fields=frozenset({f"network:path:{dep_id}"}),
                    write_fields=frozenset({f"network:path:{dep_id}"}),
                    parameters={"gateway_path": alt.gateway_path},
                    recovery_class=CLASS_PATH_REPLACE,
                )
            )

    physical = [no_change(Layer.PHYSICAL)]
    for gateway_id in sorted({gw for gw in world.graph.gateways}):
        for boost in (5.0, 10.0):
            physical.append(
                CandidateAction(
                    action_id=f"physical:BOOST_ACCESS:{gateway_id}:{boost:g}",
                    layer=Layer.PHYSICAL,
                    action="BOOST_ACCESS",
                    target=gateway_id,
                    expected_effect={"capacity_delta_mbps": boost},
                    read_fields=frozenset({f"physical:access:{gateway_id}"}),
                    write_fields=frozenset({f"physical:access:{gateway_id}"}),
                    preconditions={"gateway_online": gateway_id},
                    parameters={"boost_mbps": boost},
                    recovery_class=CLASS_PARAMETER,
                )
            )

    return {
        Layer.APPLICATION: tuple(application),
        Layer.TRANSPORT: tuple(transport),
        Layer.NETWORK: tuple(network),
        Layer.PHYSICAL: tuple(physical),
    }


def _rate_ladder(current: float) -> tuple[float, ...]:
    """Bounded authorized rate proposals around the observed demand."""
    if current <= 0.0:
        return (10.0, 15.0)
    return (round(current * 0.7, 1), round(current * 1.2, 1), round(current * 1.6, 1))


_ALT_CACHE: dict[tuple[str, str], tuple[tuple[str, ...], ...]] = {}


def _alternate_paths(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
    dep_id: str,
):
    """Alternate feasible gateway paths for ``dep_id`` (path-replacement class).

    Alternates are re-solved on the TE graph excluding one transit link of
    the current path at a time; results are cached per subnet version.
    """
    current = subnet.paths.get(dep_id)
    if current is None:
        return ()
    cache_key = (dep_id, f"v{subnet.version}:{current.path_id}")
    if cache_key in _ALT_CACHE:
        return tuple(
            type(current)(dep_id=dep_id, gateway_path=path)
            for path in _ALT_CACHE[cache_key]
        )
    alternates = []
    seen = {current.gateway_path}
    single = [dep for dep in task.dag.dependencies if dep.dep_id == dep_id]
    if not single:
        return ()
    dep = single[0]
    for link_id in current.link_ids:
        paths, failures = solve_dependency_paths(
            task, world, exclude_link_ids=frozenset({link_id})
        )
        alt = paths.get(dep_id)
        if alt is not None and alt.gateway_path not in seen:
            seen.add(alt.gateway_path)
            alternates.append(alt)
    _ALT_CACHE[cache_key] = tuple(item.gateway_path for item in alternates)
    return tuple(alternates)


def rebind_candidates(
    subnet: SubnetState,
    world: World,
    pairs: Iterable[tuple[str, str]],
) -> tuple[CandidateAction, ...]:
    """Supporting-agent rebinding on an unchanged path (class 2, Sec. IV-B).

    ``pairs`` names the (dependency, role) bindings to re-establish; the
    paper's rebind class is the response to failed supporting agents.
    """
    actions = []
    for dep_id, role in sorted(pairs):
        binding = subnet.bindings.binding(dep_id)
        current = {
            "t": binding.t_agent_id,
            "n": binding.n_agent_id,
            "p": binding.p_agent_id,
        }[role]
        layer = {
            "t": Layer.TRANSPORT,
            "n": Layer.NETWORK,
            "p": Layer.PHYSICAL,
        }[role]
        for agent in sorted(world.agents_by_layer(layer), key=lambda a: a.agent_id):
            if agent.agent_id == current or not world.agent_available(agent.agent_id):
                continue
            actions.append(
                CandidateAction(
                    action_id=f"{layer.value}:REBIND:{dep_id}:{agent.agent_id}",
                    layer=layer,
                    action="REBIND_SUPPORT",
                    target=dep_id,
                    expected_effect={"binding_change": 1.0},
                    read_fields=frozenset({f"binding:{role}:{dep_id}"}),
                    write_fields=frozenset({f"binding:{role}:{dep_id}"}),
                    parameters={"role": role, "agent_id": agent.agent_id},
                    recovery_class=CLASS_REBIND,
                )
            )
    return tuple(actions)


def joint_decisions(
    candidate_sets: dict[Layer, tuple[CandidateAction, ...]],
) -> tuple[tuple[CandidateAction, ...], ...]:
    """``u = (u^a, u^t, u^n, u^p)`` over the per-layer candidate sets."""
    ordered = (
        candidate_sets.get(Layer.APPLICATION, (no_change(Layer.APPLICATION),)),
        candidate_sets.get(Layer.TRANSPORT, (no_change(Layer.TRANSPORT),)),
        candidate_sets.get(Layer.NETWORK, (no_change(Layer.NETWORK),)),
        candidate_sets.get(Layer.PHYSICAL, (no_change(Layer.PHYSICAL),)),
    )
    return tuple(product(*ordered))


def layer_options(
    layer: Layer,
    singles: tuple[CandidateAction, ...],
    rebinds: tuple[CandidateAction, ...],
) -> tuple[tuple[CandidateAction, ...], ...]:
    """Decision options one layer can contribute.

    Besides the single candidates (no-change, parameter and path classes),
    a layer whose bindings failed contributes composite rebind bundles:
    one action per offline (dependency, role) pair, so a single joint
    decision can re-establish several bindings at once (``|Delta Phi| > 1``).
    """
    pairs: dict[tuple[str, str], list[CandidateAction]] = {}
    for action in rebinds:
        if action.layer is not layer:
            continue
        key = (action.target, str(action.parameters["role"]))
        pairs.setdefault(key, []).append(action)
    options: list[tuple[CandidateAction, ...]] = [
        (item,) for item in singles
    ]
    if pairs:
        keys = sorted(pairs)
        choices = [
            sorted(pairs[key], key=lambda item: item.action_id) for key in keys
        ]
        options.extend(
            tuple(combo) for combo in product(*choices)
        )
    return tuple(options)


def composite_joint_decisions(
    candidate_sets: dict[Layer, tuple[CandidateAction, ...]],
    rebinds: tuple[CandidateAction, ...],
) -> tuple[tuple[CandidateAction, ...], ...]:
    """Joint decisions with per-layer composite rebind bundles folded in."""
    from itertools import chain

    ordered = tuple(
        layer_options(
            layer,
            candidate_sets.get(layer, (no_change(layer),)),
            rebinds,
        )
        for layer in (
            Layer.APPLICATION,
            Layer.TRANSPORT,
            Layer.NETWORK,
            Layer.PHYSICAL,
        )
    )
    return tuple(
        tuple(chain.from_iterable(combo)) for combo in product(*ordered)
    )
