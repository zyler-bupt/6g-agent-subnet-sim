from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.core.cross_layer import (
    CrossLayerTaskState,
    LayerObservation,
    LayerProposal,
)


APPLICATION_ACTIONS = (
    "KEEP_QUALITY",
    "INCREASE_QUALITY",
    "DECREASE_QUALITY",
    "INCREASE_APPLICATION_RATE",
    "DECREASE_APPLICATION_RATE",
    "CHANGE_PRIORITY",
)
TRANSPORT_ACTIONS = (
    "KEEP_TRANSPORT",
    "INCREASE_SEND_RATE",
    "DECREASE_SEND_RATE",
    "INCREASE_CONGESTION_WINDOW",
    "DECREASE_CONGESTION_WINDOW",
    "ENABLE_MULTIPATH",
    "DISABLE_MULTIPATH",
    "CHANGE_RELIABILITY_MODE",
)
NETWORK_ACTIONS = (
    "KEEP_ROUTE",
    "SWITCH_ROUTE",
    "CHANGE_PRIORITY",
    "RESERVE_BANDWIDTH",
    "RELEASE_BANDWIDTH",
)
PHYSICAL_ACTIONS = (
    "KEEP_RESOURCE",
    "ALLOCATE_RESOURCE",
    "RELEASE_RESOURCE",
    "REALLOCATE_RESOURCE",
    "SWITCH_ACCESS",
)


class LayerAgent(Protocol):
    layer: str

    def observe(
        self,
        state: CrossLayerTaskState,
        edge_id: str,
    ) -> LayerObservation:
        ...

    def propose(self, observation: LayerObservation) -> tuple[LayerProposal, ...]:
        ...


@dataclass(frozen=True)
class _ProposalAgent:
    layer: str
    actions: tuple[str, ...]

    def observe(
        self,
        state: CrossLayerTaskState,
        edge_id: str,
    ) -> LayerObservation:
        if edge_id not in state.constraints:
            raise KeyError(f"unknown cross-layer edge: {edge_id}")
        layer_states = getattr(state, self.layer)
        if edge_id not in layer_states:
            raise KeyError(f"missing {self.layer} state for edge {edge_id}")
        context = {
            "constraint": state.constraints[edge_id],
            "application": state.application[edge_id],
            "transport": state.transport[edge_id],
            "network": state.network[edge_id],
            "physical": state.physical[edge_id],
            "candidate_profiles": tuple(
                state.metadata.get("proposal_profiles", {})
                .get(edge_id, {})
                .get(self.layer, ())
            ),
            "gateway_ids": tuple(
                state.metadata.get("edge_gateways", {}).get(edge_id, ())
            ),
            "stable_version": state.metadata.get("stable_version", 1),
        }
        return LayerObservation(
            task_id=state.task_id,
            layer=self.layer,
            edge_id=edge_id,
            observed_state=layer_states[edge_id],
            scenario=state.scenario,
            pressure=state.pressure,
            context=context,
        )

    def propose(self, observation: LayerObservation) -> tuple[LayerProposal, ...]:
        if observation.layer != self.layer:
            raise ValueError(
                f"{self.layer} Agent cannot process {observation.layer} observation"
            )
        profiles = observation.context.get("candidate_profiles", ())
        proposals = tuple(
            self._proposal_from_profile(observation, profile, index)
            for index, profile in enumerate(profiles)
        )
        if not proposals:
            raise ValueError(
                f"no {self.layer} candidate profiles for {observation.edge_id}"
            )
        if not any(proposal.is_keep for proposal in proposals):
            raise ValueError(f"{self.layer} proposal set must include a KEEP action")
        return proposals

    def _proposal_from_profile(
        self,
        observation: LayerObservation,
        profile: dict[str, object],
        index: int,
    ) -> LayerProposal:
        action = str(profile["action"])
        if action not in self.actions:
            raise ValueError(f"unsupported {self.layer} action: {action}")
        edge_id = observation.edge_id
        parameters = dict(profile.get("parameters", {}))
        parameters.setdefault(
            "observed_version",
            int(observation.context.get("stable_version", 1)),
        )
        read_set = frozenset(
            str(value)
            for value in profile.get(
                "read_set",
                (
                    f"{self.layer}:{edge_id}:state",
                    f"task:{observation.task_id}:qos",
                ),
            )
        )
        default_write_set: tuple[str, ...]
        if action.startswith("KEEP_"):
            default_write_set = ()
        else:
            default_write_set = (f"{self.layer}:{edge_id}:configuration",)
        write_set = frozenset(
            str(value)
            for value in profile.get("write_set", default_write_set)
        )
        gateways = frozenset(
            str(value)
            for value in profile.get(
                "affected_gateways",
                observation.context.get("gateway_ids", ()),
            )
        )
        route_id = observation.context["network"].route_id
        session_id = observation.context["transport"].session_id
        return LayerProposal(
            proposal_id=(
                f"{observation.task_id}:{edge_id}:{self.layer}:"
                f"{index:02d}:{action}"
            ),
            task_id=observation.task_id,
            layer=self.layer,
            action=action,
            target_objects=frozenset(
                str(value)
                for value in profile.get(
                    "target_objects",
                    (f"edge:{edge_id}",),
                )
            ),
            read_set=read_set,
            write_set=write_set,
            expected_qos_gain=float(profile.get("expected_qos_gain", 0.0)),
            expected_cost=float(profile.get("expected_cost", 0.0)),
            confidence=float(profile.get("confidence", 1.0)),
            required_bandwidth_mbps=float(
                profile.get(
                    "required_bandwidth_mbps",
                    observation.context["application"].required_rate_mbps,
                )
            ),
            required_physical_capacity_mbps=float(
                profile.get(
                    "required_physical_capacity_mbps",
                    observation.context["transport"].send_rate_mbps,
                )
            ),
            expected_latency_ms=float(
                profile.get(
                    "expected_latency_ms",
                    observation.context["network"].latency_ms,
                )
            ),
            expected_loss_rate=float(
                profile.get(
                    "expected_loss_rate",
                    observation.context["network"].packet_loss_rate,
                )
            ),
            affected_edges=frozenset((edge_id,)),
            affected_sessions=frozenset((session_id,)),
            affected_routes=frozenset((route_id,)),
            affected_gateways=gateways,
            parameters=parameters,
        )


class ApplicationProposalAgent(_ProposalAgent):
    def __init__(self) -> None:
        super().__init__(layer="application", actions=APPLICATION_ACTIONS)


class TransportProposalAgent(_ProposalAgent):
    def __init__(self) -> None:
        super().__init__(layer="transport", actions=TRANSPORT_ACTIONS)


class NetworkProposalAgent(_ProposalAgent):
    def __init__(self) -> None:
        super().__init__(layer="network", actions=NETWORK_ACTIONS)


class PhysicalProposalAgent(_ProposalAgent):
    def __init__(self) -> None:
        super().__init__(layer="physical", actions=PHYSICAL_ACTIONS)


def collect_layer_proposals(
    state: CrossLayerTaskState,
) -> tuple[LayerProposal, ...]:
    """Observe stable state and return one immutable, shared proposal pool."""

    agents: tuple[_ProposalAgent, ...] = (
        ApplicationProposalAgent(),
        TransportProposalAgent(),
        NetworkProposalAgent(),
        PhysicalProposalAgent(),
    )
    proposals: list[LayerProposal] = []
    for edge_id in sorted(state.constraints):
        for agent in agents:
            proposals.extend(agent.propose(agent.observe(state, edge_id)))
    return tuple(proposals)
