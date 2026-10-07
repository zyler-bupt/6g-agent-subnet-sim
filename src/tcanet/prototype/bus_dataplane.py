"""``Dataplane`` over the agent bus: Apply/Rollback on real executors.

Every executable action of a staged configuration becomes a ``cmd.action``
addressed to the agent that owns it — FT entries to the gateway's NetAgent,
bindings to the supporting agent, layer actions to the App/Trans/Phy agent —
and Apply waits for each ``ack``.  New state is installed before superseded
state is withdrawn (make-before-break).  Rollback replays the inverse of
the actions that were acknowledged, in reverse order.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Awaitable, Callable

from src.tcanet.executor import ActionOutcome, ExecutionRecord, StagedDecision, execute_staged
from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, rule_params
from src.tcanet.prototype.bus import BusClient
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import ExecutableAction

_ORDER = {"install_rule": 0, "bind_support": 1, "layer_operation": 2, "remove_rule": 3}
_counter = itertools.count(1)


class AckWaiter:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def expect(self, action_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._futures[action_id] = future
        return future

    def feed(self, payload: dict) -> None:
        future = self._futures.pop(payload.get("action_id", ""), None)
        if future is not None and not future.done():
            future.set_result(payload)


def _dep(task: TaskSpecification, dep_id: str):
    return next((dep for dep in task.dag.dependencies if dep.dep_id == dep_id), None)


def resolve_executor(action: ExecutableAction, staged: StagedDecision, task: TaskSpecification) -> str | None:
    if action.kind in ("install_rule", "remove_rule"):
        return f"network-{action.executor}"
    if action.kind == "bind_support":
        return action.executor
    if action.executor == "application":
        dep = _dep(task, action.target)
        return f"app-{dep.source}" if dep is not None else None
    if action.executor == "transport":
        return staged.subnet.bindings.binding(action.target).t_agent_id
    if action.executor == "physical":
        return f"physical-{action.target}"
    return None


def _entry_at(forwarding: dict, gateway_id: str, dep_id: str):
    """The FT entry of ``dep_id`` at ``gateway_id`` (kernel state is keyed so)."""
    return next(
        (e for e in forwarding.values() if e.gateway_id == gateway_id and e.dep_id == dep_id),
        None,
    )


def _candidate(staged: StagedDecision, action: ExecutableAction):
    action_id = action.action_id.removeprefix("exec:")
    return next((item for item in staged.actions if item.action_id == action_id), None)


def command_payload(
    action: ExecutableAction,
    staged: StagedDecision,
    task: TaskSpecification,
    plan: AddressPlan,
    *,
    undo: bool = False,
) -> dict | None:
    """``cmd.action`` payload for ``action`` (or its inverse).

    FT state is keyed by (gateway, dependency): a removal superseded by an
    install at the same gateway is a no-op (``None``), and undoing an
    install restores whatever entry that gateway held before.
    """
    kind, executor = action.kind, resolve_executor(action, staged, task)
    params: dict = {}
    if kind in ("install_rule", "remove_rule"):
        if kind == "install_rule":
            entry = staged.subnet.forwarding[action.target]
            if undo:
                previous = _entry_at(staged.previous.forwarding, entry.gateway_id, entry.dep_id)
                kind, entry = ("install_rule", previous) if previous else ("remove_rule", entry)
        else:
            entry = staged.previous.forwarding[action.target]
            if _entry_at(staged.subnet.forwarding, entry.gateway_id, entry.dep_id) is not None:
                return None  # replaced in place by the install at the same gateway
            if undo:
                kind = "install_rule"
        params = rule_params(entry, task, plan)
    elif kind == "bind_support":
        params = {"dep_id": action.target, "role": action.value}
    else:
        candidate = _candidate(staged, action)
        params = dict(candidate.parameters) if candidate is not None else {}
        dep = _dep(task, action.target)
        if action.value == "SWITCH_MODE" and dep is not None:
            params["app_agent"] = f"app-{dep.source}"
        if undo:
            if action.value == "ADJUST_RATE":
                params["rate_mbps"] = params.get("base_mbps", params.get("rate_mbps"))
            elif action.value == "SWITCH_MODE":
                params["mode"] = "standard"
            elif action.value == "BOOST_ACCESS":
                params["boost_mbps"] = -float(params.get("boost_mbps", 0.0))
    return {
        "action_id": f"{'undo:' if undo else ''}{action.action_id}#{next(_counter)}",
        "kind": kind,
        "executor": executor,
        "target": action.target,
        "value": action.value,
        "params": params,
    }


Logger = Callable[[dict, bool, str], Awaitable[None]]


class BusDataplane:
    def __init__(
        self,
        bus: BusClient,
        acks: AckWaiter,
        plan: AddressPlan,
        task: Callable[[], TaskSpecification],
        *,
        on_action: Logger | None = None,
        ack_timeout_s: float | None = None,
    ) -> None:
        self.bus = bus
        self.acks = acks
        self.plan = plan
        self.task = task
        self.on_action = on_action
        self.ack_timeout_s = ack_timeout_s
        self._applied: dict[int, list[ExecutableAction]] = {}

    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord:
        checked = await execute_staged(staged, world)
        if not checked.ok:
            return checked
        await self.bus.publish("ctrl.staged", {"version": staged.subnet.version})
        applied: list[ExecutableAction] = []
        outcomes: list[ActionOutcome] = []
        self._applied[id(staged)] = applied
        for action in sorted(staged.executable, key=lambda a: (_ORDER.get(a.kind, 9), a.action_id)):
            payload = command_payload(action, staged, self.task(), self.plan)
            if payload is None:
                ok, detail = True, "replaced in place"
            else:
                ok, detail = await self.send(payload, world)
            outcomes.append(ActionOutcome(action.action_id, ok, detail))
            if not ok:
                return ExecutionRecord(False, tuple(outcomes), checked.serialization_order)
            applied.append(action)
        return ExecutionRecord(True, tuple(outcomes), checked.serialization_order)

    async def rollback(self, staged: StagedDecision, world: World) -> None:
        for action in reversed(self._applied.pop(id(staged), [])):
            payload = command_payload(action, staged, self.task(), self.plan, undo=True)
            if payload is not None:
                await self.send(payload, world)
        await self.bus.publish("ctrl.rollback", {"version": staged.subnet.version,
                                                 "restored": staged.previous.version})

    async def send(self, payload: dict, world: World) -> tuple[bool, str]:
        """Deliver one command and wait for its ack (or decide it locally)."""
        gateway = payload["params"].get("gateway") if payload["kind"].endswith("_rule") else None
        if gateway is not None and not world.gateway_online(gateway):
            if payload["kind"] == "remove_rule":
                ok, detail = True, f"withdrawn with failed gateway {gateway}"
            elif payload["action_id"].startswith("undo:"):
                ok, detail = True, f"not restored: gateway {gateway} offline"
            else:
                ok, detail = False, f"gateway {gateway} offline"
        elif payload["executor"] is None:
            ok, detail = False, "no executor for action"
        else:
            future = self.acks.expect(payload["action_id"])
            await self.bus.publish("cmd.action", payload)
            try:
                ack = await asyncio.wait_for(future, self.ack_timeout_s or config.ack_timeout_s())
                ok, detail = bool(ack["ok"]), str(ack.get("detail", ""))
            except asyncio.TimeoutError:
                ok, detail = False, f"{payload['executor']} did not acknowledge"
        if self.on_action is not None:
            await self.on_action(payload, ok, detail)
        return ok, detail
