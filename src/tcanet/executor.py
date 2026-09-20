"""Staged execution of a selected joint decision (paper Sec. IV-B/IV-C).

Applying ``u*_m`` proceeds in three disciplined steps:

1. **Stage** — derive the next-version subnet ``S_m^(v+1)`` from the
   accepted state and the selected actions (path overrides, forwarding
   recompilation, binding overrides), together with the executable plan.
2. **Revalidate** — re-check preconditions against the latest observed
   state right before execution; if the world moved, the batch is
   returned for re-evaluation instead of being installed.
3. **Execute** — run the coordinated batch with shared-resource
   operations serialized (pending reservations are already accounted for
   by the feasibility projection), recording one outcome per action.

The version index only advances when the verification window accepts the
staged state (see ``verify.py``); until then the staged state stays
non-accepted and the accepted state remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.tcanet.binding import BindingTable, SupportBinding
from src.tcanet.candidates import CandidateAction
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import (
    ExecutableAction,
    ForwardingEntry,
    PathRecord,
    SubnetState,
    compile_forwarding,
)


@dataclass(frozen=True)
class StagedDecision:
    """The staged next-version state plus its executable plan."""

    previous: SubnetState  # accepted state the decision was staged against
    subnet: SubnetState  # accepted=False until the window accepts it
    actions: tuple[CandidateAction, ...]
    executable: tuple[ExecutableAction, ...]

    @property
    def superseded_rules(self) -> frozenset[str]:
        """Forwarding rules of the accepted state absent from the staged one."""
        return frozenset(set(self.previous.forwarding) - set(self.subnet.forwarding))


def stage_decision(
    task: TaskSpecification,
    subnet: SubnetState,
    actions: tuple[CandidateAction, ...],
) -> StagedDecision:
    """Derive ``S_m^(v+1)`` and the executable plan from ``u*_m``."""
    new_paths: dict[str, PathRecord] = dict(subnet.paths)
    binding_overrides: dict[str, SupportBinding] = {}
    for action in actions:
        if action.is_no_change:
            continue
        if action.action == "REROUTE":
            new_paths[action.target] = PathRecord(
                dep_id=action.target,
                gateway_path=tuple(
                    str(node) for node in action.parameters["gateway_path"]
                ),
            )
        elif action.action == "REBIND_SUPPORT":
            base = binding_overrides.get(
                action.target, subnet.bindings.binding(action.target)
            )
            role = str(action.parameters["role"])
            agent_id = str(action.parameters["agent_id"])
            binding_overrides[action.target] = SupportBinding(
                dep_id=action.target,
                t_agent_id=agent_id if role == "t" else base.t_agent_id,
                n_agent_id=agent_id if role == "n" else base.n_agent_id,
                p_agent_id=agent_id if role == "p" else base.p_agent_id,
            )

    forwarding = compile_forwarding(task.dag, new_paths)
    bindings = BindingTable(
        records={**subnet.bindings.records, **binding_overrides}
    )
    staged = SubnetState(
        task_id=subnet.task_id,
        version=subnet.next_version(),
        paths=new_paths,
        forwarding=forwarding,
        bindings=bindings,
        accepted=False,
    )
    return StagedDecision(
        previous=subnet,
        subnet=staged,
        actions=actions,
        executable=_executable_plan(subnet, staged, actions),
    )


def _executable_plan(
    current: SubnetState,
    staged: SubnetState,
    actions: tuple[CandidateAction, ...],
) -> tuple[ExecutableAction, ...]:
    """Concrete plan: rule removals, installs and support (re)bindings."""
    plan: list[ExecutableAction] = []
    for rule_id in sorted(set(current.forwarding) - set(staged.forwarding)):
        entry = current.forwarding[rule_id]
        plan.append(
            ExecutableAction(
                action_id=f"remove:{rule_id}",
                kind="remove_rule",
                executor=entry.gateway_id,
                target=rule_id,
                value="withdrawn",
                preconditions={"gateway_online": entry.gateway_id},
            )
        )
    for rule_id in sorted(staged.forwarding):
        entry = staged.forwarding[rule_id]
        if rule_id in current.forwarding and (
            current.forwarding[rule_id] == entry
        ):
            continue
        plan.append(
            ExecutableAction(
                action_id=f"install:{rule_id}",
                kind="install_rule",
                executor=entry.gateway_id,
                target=rule_id,
                value=entry.action_mode,
                preconditions={"gateway_online": entry.gateway_id},
            )
        )
    for dep_id in sorted(staged.bindings.records):
        old = current.bindings.binding(dep_id)
        new = staged.bindings.binding(dep_id)
        for role, agent_id in (
            ("t", new.t_agent_id),
            ("n", new.n_agent_id),
            ("p", new.p_agent_id),
        ):
            old_id = {"t": old.t_agent_id, "n": old.n_agent_id, "p": old.p_agent_id}[
                role
            ]
            if agent_id and agent_id != old_id:
                plan.append(
                    ExecutableAction(
                        action_id=f"bind:{dep_id}:{role}",
                        kind="bind_support",
                        executor=agent_id,
                        target=dep_id,
                        value=role,
                        preconditions={"agent_online": agent_id},
                    )
                )
    # Layer operations beyond forwarding/binding (rate/mode/boost) become
    # generic executor tasks named after the originating candidate.
    for action in actions:
        if action.is_no_change or action.action in {"REROUTE", "REBIND_SUPPORT"}:
            continue
        plan.append(
            ExecutableAction(
                action_id=f"exec:{action.action_id}",
                kind="layer_operation",
                executor=action.layer.value,
                target=action.target,
                value=action.action,
                preconditions=dict(action.preconditions),
            )
        )
    return tuple(plan)


@dataclass(frozen=True)
class ActionOutcome:
    """Recorded outcome of one executable action."""

    action_id: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ExecutionRecord:
    """Result of one coordinated batch execution."""

    ok: bool
    outcomes: tuple[ActionOutcome, ...]
    serialization_order: tuple[str, ...] = field(default_factory=())

    @property
    def failed(self) -> tuple[ActionOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.ok)


def _shared_resource_key(action: ExecutableAction) -> str:
    """Deterministic serialization key for shared-resource operations.

    Rule installs/removals touch a gateway's shared forwarding state and
    are serialized per gateway; other operations serialize per executor.
    """
    if action.kind in {"install_rule", "remove_rule"}:
        return f"gateway:{action.executor}"
    return f"executor:{action.executor}"


async def execute_staged(
    staged: StagedDecision,
    world: World,
) -> ExecutionRecord:
    """Revalidate preconditions and run the coordinated batch.

    Shared-resource operations are grouped by their serialization key and
    applied group by group in sorted order, mirroring the paper's
    requirement that concurrent operations on one shared resource be
    serialized.  Any failed precondition fails the whole batch (atomic
    coordinated batch) — the caller then re-evaluates on the latest state.
    """
    groups: dict[str, list[ExecutableAction]] = {}
    for action in staged.executable:
        groups.setdefault(_shared_resource_key(action), []).append(action)
    order: list[str] = []
    outcomes: list[ActionOutcome] = []

    for key in sorted(groups):
        order.append(key)
        for action in sorted(groups[key], key=lambda item: item.action_id):
            ok, detail = _check_preconditions(action, world)
            if not ok:
                outcomes.append(ActionOutcome(action.action_id, False, detail))
                return ExecutionRecord(
                    ok=False,
                    outcomes=tuple(outcomes),
                    serialization_order=tuple(order),
                )
            outcomes.append(ActionOutcome(action.action_id, True))
    return ExecutionRecord(
        ok=True,
        outcomes=tuple(outcomes),
        serialization_order=tuple(order),
    )


def _check_preconditions(
    action: ExecutableAction,
    world: World,
) -> tuple[bool, str]:
    # Withdrawing a rule from a gateway that went down needs no liveness:
    # its forwarding state was destroyed with the failure.
    if action.kind == "remove_rule":
        gateway = action.preconditions.get("gateway_online")
        if gateway is not None and not world.gateway_online(gateway):
            return True, f"gateway {gateway} lost; state withdrawn with failure"
        return True, ""
    for name, value in sorted(action.preconditions.items()):
        if name == "gateway_online":
            if not world.gateway_online(value):
                return False, f"gateway {value} offline"
        elif name == "agent_online":
            if not world.agent_available(value):
                return False, f"agent {value} unavailable"
    return True, ""
