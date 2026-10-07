"""Post-update verification window and bounded recovery (paper Sec. IV-C).

After a joint decision is installed, TCANet opens the verification window
``W_m``.  Within the window it checks that

1. the updates were installed successfully,
2. superseded forwarding state was withdrawn,
3. every affected dependency remains reachable, and
4. freshly measured service levels satisfy the hard requirements ``q^H``.

Observations that have not arrived yet keep the check **pending**; if the
window expires with observations still missing, or a measurement violates
``q^H``, the update is rejected.  A rejected candidate is excluded and the
controller re-evaluates the remaining alternatives against the latest
observed state, for at most ``B_r`` attempts (default 3) before the task
escalates to re-formation.
"""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Protocol

from src.tcanet.candidates import (
    CandidateAction,
    TaskObservation,
    composite_joint_decisions,
    coordination_candidates,
    rebind_candidates,
)
from src.tcanet.closure import (
    ClosureResult,
    RuntimeEvent,
    dependency_relations,
    expand_affected_set,
    initial_affected_set,
)
from src.tcanet.executor import ExecutionRecord, StagedDecision, execute_staged, stage_decision
from src.tcanet.dataplane import Dataplane, NullDataplane
from src.tcanet.feasibility import evaluate_joint_decision, project_state
from src.tcanet.selection import (
    CandidateEvaluation,
    SelectionTrace,
    modification_scope,
    soft_violation,
    two_stage_select,
)
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import SubnetState

DEFAULT_MAX_ATTEMPTS = 3  # B_r (paper Sec. IV-C)
DEFAULT_WINDOW_MS = 2000.0  # W_m


@dataclass(frozen=True)
class DepObservation:
    """One measured service level for a dependency inside ``W_m``."""

    dep_id: str
    observed_at_ms: float
    throughput_mbps: float | None = None
    delay_ms: float | None = None
    loss_rate: float | None = None


class MeasurementProvider(Protocol):
    """Source of post-update measurements for the verification window."""

    def __call__(
        self, staged: StagedDecision, world: World, dep_ids: tuple[str, ...]
    ) -> tuple[DepObservation, ...] | Awaitable[tuple[DepObservation, ...]]: ...


def projected_measurements(
    staged: StagedDecision,
    world: World,
    dep_ids: tuple[str, ...],
    *,
    task: TaskSpecification | None = None,
    arrival_ms: float = 50.0,
    skew: dict[str, float] | None = None,
) -> tuple[DepObservation, ...]:
    """Measurements synthesized from the projected post-action state.

    ``skew`` optionally perturbs measured delay per dependency so demos and
    tests can inject verified-good or violating measurements.
    """
    if task is None:
        raise ValueError("projected_measurements requires the task")
    projection = project_state(task, world, staged.previous, staged.actions)
    skew = skew or {}
    return tuple(
        DepObservation(
            dep_id=dep_id,
            observed_at_ms=arrival_ms,
            throughput_mbps=projection.dependencies[dep_id].delivered_mbps,
            delay_ms=projection.dependencies[dep_id].delay_ms
            + skew.get(dep_id, 0.0),
            loss_rate=projection.dependencies[dep_id].loss_rate,
        )
        for dep_id in sorted(dep_ids)
        if dep_id in projection.dependencies
    )


@dataclass(frozen=True)
class WindowEvent:
    """One timeline entry of the verification window."""

    at_ms: float
    kind: str  # installed | withdrawn | pending | observed | expired | violated
    detail: str


@dataclass(frozen=True)
class WindowResult:
    """Outcome of one verification window."""

    accepted: bool
    pending: tuple[str, ...]
    violations: tuple[str, ...]
    timeline: tuple[WindowEvent, ...]


def evaluate_window(
    task: TaskSpecification,
    world: World,
    staged: StagedDecision,
    execution: ExecutionRecord,
    affected: tuple[str, ...],
    observations: tuple[DepObservation, ...],
    *,
    window_ms: float = DEFAULT_WINDOW_MS,
) -> WindowResult:
    """Run the ``W_m`` checks over the arrived observations."""
    timeline: list[WindowEvent] = []
    if execution.ok:
        timeline.append(
            WindowEvent(0.0, "installed", f"{len(execution.outcomes)} actions ok")
        )
    else:
        failed = ", ".join(item.action_id for item in execution.failed)
        return WindowResult(
            accepted=False,
            pending=(),
            violations=(f"installation_failed:{failed}",),
            timeline=tuple(timeline),
        )
    for rule_id in sorted(staged.superseded_rules):
        timeline.append(WindowEvent(0.0, "withdrawn", rule_id))

    by_dep = {
        obs.dep_id: obs for obs in observations if obs.observed_at_ms <= window_ms
    }
    late = tuple(
        sorted(
            obs.dep_id
            for obs in observations
            if obs.observed_at_ms > window_ms and obs.dep_id not in by_dep
        )
    )

    violations: list[str] = []
    pending: list[str] = []
    for dep_id in sorted(affected):
        dep = next(
            (d for d in task.dag.dependencies if d.dep_id == dep_id), None
        )
        if dep is None:
            continue
        reachability = _reachability_violation(world, staged, dep.source, dep.target)
        if reachability:
            violations.append(f"unreachable:{dep_id}:{reachability}")
        obs = by_dep.get(dep_id)
        if obs is None:
            pending.append(dep_id)
            timeline.append(
                WindowEvent(window_ms, "pending", f"{dep_id}: no measurement yet")
            )
            continue
        timeline.append(
            WindowEvent(
                obs.observed_at_ms,
                "observed",
                f"{dep_id}: {obs.throughput_mbps:.1f} Mbps / "
                f"{obs.delay_ms:.1f} ms / loss {obs.loss_rate:.3f}",
            )
        )
        requirements = task.requirements_for(dep)
        if obs.throughput_mbps is not None and (
            obs.throughput_mbps < requirements.min_throughput_mbps - 1e-9
        ):
            violations.append(f"throughput_hard:{dep_id}")
        if obs.delay_ms is not None and (
            obs.delay_ms > requirements.max_delay_ms + 1e-9
        ):
            violations.append(f"delay_hard:{dep_id}")
        if obs.loss_rate is not None and (
            obs.loss_rate > requirements.max_loss_rate + 1e-9
        ):
            violations.append(f"loss_hard:{dep_id}")

    if pending:
        timeline.append(
            WindowEvent(window_ms, "expired", f"window closed: {', '.join(pending)}")
        )
        return WindowResult(
            accepted=False,
            pending=tuple(pending),
            violations=tuple(violations),
            timeline=tuple(timeline),
        )
    for dep_id in late:
        timeline.append(WindowEvent(window_ms, "expired", f"{dep_id}: late"))
    for violation in violations:
        timeline.append(WindowEvent(window_ms, "violated", violation))
    return WindowResult(
        accepted=not violations,
        pending=(),
        violations=tuple(violations),
        timeline=tuple(timeline),
    )


def _reachability_violation(
    world: World,
    staged: StagedDecision,
    source: str,
    target: str,
) -> str:
    for agent_id in (source, target):
        endpoint = world.endpoints.get(agent_id)
        if endpoint is None or not endpoint.online:
            return f"endpoint {agent_id} offline"
    return ""


@dataclass(frozen=True)
class RecoveryAttempt:
    """One ``b <= B_r`` attempt: selection, execution, window outcome."""

    index: int
    selected_label: str
    selection: SelectionTrace | None
    execution: ExecutionRecord | None
    window: WindowResult | None
    excluded_after: tuple[str, ...] = ()

    @property
    def outcome(self) -> str:
        if self.execution is not None and not self.execution.ok:
            return "execution_failed"
        if self.window is not None and self.window.pending:
            return "window_expired_pending"
        if self.window is not None and self.window.violations:
            return "window_rejected"
        if self.window is not None:
            return "accepted"
        return "no_feasible_decision"


@dataclass(frozen=True)
class RecoveryHooks:
    """Optional live-observation hooks into the recovery loop (demo layer).

    Each hook is awaited at the moment its mechanism stage completes; the
    recovery logic itself is untouched — with all hooks ``None`` the loop is
    bit-identical to the un-instrumented one.
    """

    on_closure: Callable[[ClosureResult], Awaitable[None]] | None = None
    on_decision: Callable[[SelectionTrace, CandidateEvaluation], Awaitable[None]] | None = None
    on_execution: Callable[[ExecutionRecord, StagedDecision], Awaitable[None]] | None = None
    on_window: Callable[[WindowResult, tuple[DepObservation, ...]], Awaitable[None]] | None = None
    on_rejected: Callable[[RecoveryAttempt, tuple[str, ...]], Awaitable[None]] | None = None
    on_rollback: Callable[[RecoveryAttempt], Awaitable[None]] | None = None


@dataclass(frozen=True)
class RecoveryResult:
    """Full outcome of one elastic-reconfiguration episode."""

    event: RuntimeEvent
    closure: ClosureResult
    attempts: tuple[RecoveryAttempt, ...]
    recovered: bool
    subnet: SubnetState | None  # accepted next-version state on success
    recovery_latency_ms: float | None
    error: str = ""


def observe_state(
    task: TaskSpecification,
    world: World,
    subnet: SubnetState,
) -> TaskObservation:
    """Refreshed ``x^0_m`` snapshot from the latest world/subnet state."""
    projection = project_state(task, world, subnet, ())
    return TaskObservation(
        subnet_version=subnet.version,
        per_dep_demand_mbps={
            dep.dep_id: dep.demand_mbps for dep in task.dag.dependencies
        },
        per_dep_delay_ms={
            dep_id: item.delay_ms for dep_id, item in projection.dependencies.items()
        },
        per_dep_loss={
            dep_id: item.loss_rate for dep_id, item in projection.dependencies.items()
        },
        resource_load_mbps=dict(projection.resource_load_mbps),
        executor_online={
            agent_id: agent.online
            for agent_id, agent in world.support_agents.items()
        },
        observed_version=subnet.version,
    )


class RecoveryController:
    """Elastic reconfiguration driver: closure -> selection -> execute -> W_m.

    Each attempt selects over the candidate sets restricted to the affected
    closure, stages and executes the winning joint decision, then runs the
    verification window.  Failed candidates are excluded and the next
    attempt re-evaluates against the latest observed state, for at most
    ``B_r`` attempts.
    """

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_ms: float = DEFAULT_WINDOW_MS,
        measure: MeasurementProvider | None = None,
        dataplane: Dataplane | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_ms = window_ms
        self._measure = measure
        self._dataplane = dataplane or NullDataplane()

    async def recover(
        self,
        task: TaskSpecification,
        subnet: SubnetState,
        world: World,
        event: RuntimeEvent,
        hooks: RecoveryHooks | None = None,
    ) -> RecoveryResult:
        started = time.perf_counter()
        initial = initial_affected_set(task, subnet, world, event)
        relations = dependency_relations(task, subnet, world)
        closure = expand_affected_set(initial, relations)
        affected = tuple(sorted(closure.final))
        if hooks is not None and hooks.on_closure is not None:
            await hooks.on_closure(closure)

        excluded: set[str] = set()
        attempts: list[RecoveryAttempt] = []
        failed_agents = {
            agent_id
            for agent_id, agent in world.support_agents.items()
            if not agent.online
        }
        rebind_pairs = sorted(
            (dep_id, role)
            for dep_id in affected
            if dep_id in subnet.bindings.records
            for role, agent_id in (
                ("t", subnet.bindings.binding(dep_id).t_agent_id),
                ("n", subnet.bindings.binding(dep_id).n_agent_id),
                ("p", subnet.bindings.binding(dep_id).p_agent_id),
            )
            if agent_id and agent_id in failed_agents
        )
        for index in range(1, self.max_attempts + 1):
            observation = observe_state(task, world, subnet)
            candidate_sets = coordination_candidates(
                task, world, subnet, observation, focus_dep_ids=affected
            )
            rebinds = rebind_candidates(subnet, world, rebind_pairs)

            evaluations: list[CandidateEvaluation] = []
            for decision in composite_joint_decisions(candidate_sets, rebinds):
                if any(action.action_id in excluded for action in decision):
                    continue
                feasibility = evaluate_joint_decision(
                    task, world, subnet, decision
                )
                evaluations.append(
                    CandidateEvaluation(
                        actions=decision,
                        feasibility=feasibility,
                        soft_violation=soft_violation(
                            task.soft, feasibility.projection
                        ),
                        modification_scope=modification_scope(
                            subnet, decision, task
                        ),
                    )
                )
            trace = two_stage_select(tuple(evaluations))
            if hooks is not None and hooks.on_decision is not None and trace.selected is not None:
                await hooks.on_decision(trace, trace.selected)
            if trace.selected is None:
                attempts.append(
                    RecoveryAttempt(
                        index=index,
                        selected_label="",
                        selection=trace,
                        execution=None,
                        window=None,
                    )
                )
                break

            selected = trace.selected
            staged = stage_decision(task, subnet, selected.actions, world)
            execution = await self._dataplane.apply(staged, world)
            if hooks is not None and hooks.on_execution is not None:
                await hooks.on_execution(execution, staged)
            if not execution.ok:
                excluded.update(
                    action.action_id
                    for action in selected.actions
                    if not action.is_no_change
                )
                attempts.append(
                    RecoveryAttempt(
                        index=index,
                        selected_label=selected.label,
                        selection=trace,
                        execution=execution,
                        window=None,
                        excluded_after=tuple(sorted(excluded)),
                    )
                )
                await self._dataplane.rollback(staged, world)
                if hooks is not None and hooks.on_rollback is not None:
                    await hooks.on_rollback(attempts[-1])
                continue

            if self._measure is not None:
                observations = self._measure(staged, world, affected)
                if inspect.isawaitable(observations):
                    observations = await observations
            else:
                observations = projected_measurements(
                    staged, world, affected, task=task
                )
            window = evaluate_window(
                task,
                world,
                staged,
                execution,
                affected,
                observations,
                window_ms=self.window_ms,
            )
            attempts.append(
                RecoveryAttempt(
                    index=index,
                    selected_label=selected.label,
                    selection=trace,
                    execution=execution,
                    window=window,
                )
            )
            if window.accepted:
                if hooks is not None and hooks.on_window is not None:
                    await hooks.on_window(window, observations)
                accepted_subnet = replace(staged.subnet, accepted=True)
                return RecoveryResult(
                    event=event,
                    closure=closure,
                    attempts=tuple(attempts),
                    recovered=True,
                    subnet=accepted_subnet,
                    recovery_latency_ms=(time.perf_counter() - started) * 1000.0,
                )
            excluded.update(
                action.action_id
                for action in selected.actions
                if not action.is_no_change
            )
            attempts[-1] = replace(
                attempts[-1], excluded_after=tuple(sorted(excluded))
            )
            if hooks is not None and hooks.on_window is not None:
                await hooks.on_window(window, observations)
            if hooks is not None and hooks.on_rejected is not None:
                await hooks.on_rejected(
                    attempts[-1], tuple(sorted(excluded))
                )
            await self._dataplane.rollback(staged, world)
            if hooks is not None and hooks.on_rollback is not None:
                await hooks.on_rollback(attempts[-1])

        return RecoveryResult(
            event=event,
            closure=closure,
            attempts=tuple(attempts),
            recovered=False,
            subnet=None,
            recovery_latency_ms=None,
            error=f"recovery failed after {len(attempts)} attempt(s)",
        )


def accept_staged(subnet: SubnetState) -> SubnetState:
    """Advance the accepted version once the window has accepted."""
    return replace(subnet, accepted=True)
