"""TCANet controller process: Algorithm 1 over the live agent system.

The controller learns the network only from agent reports (``report.*``,
``hello``/``heartbeat``, ``ack``) and task input from the orchestrator
(``task.*``).  It never subscribes to ``ctl.*``/``sim.*``/``ops.*``: faults
injected by the operator are seen only through their effects.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import time

from rich.console import Console

from src.tcanet.closure import RuntimeEvent
from src.tcanet.metrics import modification_stats
from src.tcanet.prototype import config
from src.tcanet.prototype import log_format as fmt
from src.tcanet.prototype.addressing import build_plan, rule_params
from src.tcanet.prototype.aggregator import EventDetector, StateAggregator
from src.tcanet.prototype.assess import BusMeasurement
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.bus_dataplane import AckWaiter, BusDataplane
from src.tcanet.scenario import run_formation, with_demand
from src.tcanet.scenario_fig1 import load
from src.tcanet.subnet import SubnetState
from src.tcanet.verify import RecoveryController, RecoveryHooks

CONTROLLER_TOPICS = ("report.", "hello", "heartbeat", "ack", "task.")


class ControllerApp:
    def __init__(self, bus: BusClient, *, scenario: str = "paper_fig1", console: Console | None = None,
                 auto_capture: bool = False) -> None:
        self.bus = bus
        self.scenario = scenario
        self.console = console or Console(highlight=False)
        self.auto_capture = auto_capture
        self.acks = AckWaiter()
        self.queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.subnet: SubnetState | None = None
        self.history: list[dict] = []
        self._reset_state()

    def _reset_state(self) -> None:
        self.world, self.task = load(self.scenario)
        self.plan = build_plan(self.world)
        self.aggregator = StateAggregator(self.world)
        self.detector = EventDetector(self.world, debounce_s=config.detect_debounce_s())
        self.measure = BusMeasurement(self.aggregator.flowbook)
        self.dataplane = BusDataplane(self.bus, self.acks, self.plan, lambda: self.task,
                                      on_action=self._log_action)
        self.subnet = None

    async def log(self, item: dict) -> None:
        self.history.append(item)
        self.console.print(fmt.render(item))
        await self.bus.publish("ctrl.log", item)

    async def _log_action(self, payload: dict, ok: bool, detail: str) -> None:
        await self.log(fmt.apply_line(payload, ok, detail))

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.subscribe(*CONTROLLER_TOPICS)
        await self.log(fmt.entry("formation", f"TCANet controller ready — scenario {self.scenario}",
                                 [f"agents report via {config.bus_path()}"], "ok"))
        tasks = [asyncio.create_task(self._pump(stop)), asyncio.create_task(self._tick(stop)),
                 asyncio.create_task(self._worker(stop))]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            msg = await self.bus.recv()
            if msg is None:
                stop.set()
                return
            topic = msg["topic"]
            if topic == "ack":
                self.acks.feed(msg["payload"])
            elif topic == "task.submit":
                await self.queue.put(("submit",))
            elif topic == "task.withdraw":
                await self.queue.put(("withdraw",))
            else:
                self.detector.feed(self.aggregator.apply(msg))

    async def _tick(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.1 * config.time_scale())
            now = time.time()
            self.detector.feed(self.aggregator.silent_agents(now, config.heartbeat_timeout_s()))
            for event, evidence_ts in self.detector.poll(now):
                await self.queue.put(("event", event, evidence_ts, now))

    async def _worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            item = await self.queue.get()
            try:
                if item[0] == "submit":
                    await self.form()
                elif item[0] == "withdraw":
                    await self.withdraw()
                elif item[0] == "event":
                    await self.reconfigure(*item[1:])
            except Exception as exc:  # keep the controller alive on the demo floor
                await self.log(fmt.entry("event", f"controller error: {type(exc).__name__}: {exc}", [], "fail"))

    async def _publish_subnet(self) -> None:
        subnet = self.subnet
        await self.bus.publish("ctrl.subnet", {
            "task_id": self.task.dag.task_id,
            "version": subnet.version if subnet else 0,
            "paths": {dep: list(rec.gateway_path) for dep, rec in (subnet.paths.items() if subnet else ())},
            "forwarding": sorted(subnet.forwarding) if subnet else [],
            "bindings": {dep: list(b.as_tuple()) for dep, b in (subnet.bindings.records.items() if subnet else ())},
        })

    async def _capture(self, tag: str) -> None:
        if self.auto_capture:
            await self.bus.publish("ui.capture", {"tag": tag})

    async def form(self) -> None:
        if self.subnet is not None:
            await self.log(fmt.entry("formation", "task already active; withdraw first", [], "warn"))
            return
        deps = [dep.dep_id for dep in self.task.dag.dependencies]
        await self.log(fmt.entry("formation", f"T_m received: {self.task.dag.task_id}",
                                 [f"E^b,old = ∅ → E^aff = E^b,new = {{{', '.join(deps)}}}   (Eq. 13–14)"]))
        started = time.time()
        observed: list = []

        async def measure(staged, world, dep_ids):
            observed[:] = await self.measure(staged, world, dep_ids)
            return tuple(observed)

        outcome = await run_formation(self.task, self.world, dataplane=self.dataplane, measure=measure)
        await self.log(fmt.assess_entry(outcome.window, tuple(observed), self.task))
        if outcome.subnet is None:
            await self.log(fmt.rollback_entry(1, 0))
            await self._publish_subnet()
            return
        self.subnet = outcome.subnet
        await self.log(fmt.commit_entry(self.subnet, (time.time() - started) * 1000.0, None, "formation"))
        await self._publish_subnet()
        await self.bus.publish("ctrl.metrics", {"kind": "formation", "version": 1,
                                                "latency_ms": (time.time() - started) * 1000.0})
        await self._capture("formed")

    async def withdraw(self) -> None:
        if self.subnet is not None:
            for rule_id in sorted(self.subnet.forwarding):
                entry = self.subnet.forwarding[rule_id]
                if not self.world.gateway_online(entry.gateway_id):
                    continue
                payload = {"action_id": f"withdraw:{rule_id}#{time.time_ns()}", "kind": "remove_rule",
                           "executor": f"network-{entry.gateway_id}", "target": rule_id, "value": "withdrawn",
                           "params": rule_params(entry, self.task, self.plan)}
                await self.dataplane.send(payload, self.world)
        self._reset_state()
        await self._publish_subnet()
        await self.log(fmt.entry("withdraw", "task withdrawn; controller state reset", [], "warn"))

    async def reconfigure(self, event: RuntimeEvent, evidence_ts: float, detected_ts: float) -> None:
        if self.subnet is None:
            await self.log(fmt.entry("event", f"ignored (no active subnet): {event.kind}", [], "warn"))
            return
        await self.log(fmt.event_entry(event, evidence_ts, detected_ts))
        await self._capture("fault")
        if event.kind == "demand_change":
            new = self.aggregator.demands[event.dep_id]
            self.task = with_demand(self.task, event.dep_id, new)
        deps = [dep.dep_id for dep in self.task.dag.dependencies]

        async def on_closure(closure):
            await self.log(fmt.scope_entry(closure, deps))

        async def on_decision(trace, _selected):
            await self.log(fmt.selection_entry(trace))

        async def on_window(window, observations):
            await self.log(fmt.assess_entry(window, observations, self.task))

        async def on_rollback(attempt):
            await self.log(fmt.rollback_entry(self.subnet.version + 1, self.subnet.version))

        controller = RecoveryController(max_attempts=config.K_MAX, window_ms=self.measure.window_ms,
                                        measure=self.measure, dataplane=self.dataplane)
        before = self.subnet
        result = await controller.recover(
            self.task, before, self.world, event,
            hooks=RecoveryHooks(on_closure=on_closure, on_decision=on_decision,
                                on_window=on_window, on_rollback=on_rollback),
        )
        if not result.recovered:
            if result.attempts and result.attempts[-1].selection and result.attempts[-1].selection.selected is None:
                await self.log(fmt.selection_entry(result.attempts[-1].selection))
            await self.log(fmt.entry("commit", f"reconfiguration failed: {result.error}; v{before.version} retained",
                                     [], "fail"))
            return
        self.subnet = result.subnet
        for action in result.attempts[-1].selection.selected.actions:
            if action.action == "ADJUST_RATE":
                self.task = with_demand(self.task, action.target, float(action.parameters["rate_mbps"]))
        latency_ms = (time.time() - evidence_ts) * 1000.0
        stats = modification_stats(before, self.subnet)
        await self.log(fmt.commit_entry(self.subnet, latency_ms, stats, "recovery"))
        await self._publish_subnet()
        await self.bus.publish("ctrl.metrics", {"kind": "recovery", "version": self.subnet.version,
                                                "latency_ms": latency_ms, "changed": stats.changed,
                                                "installed": stats.installed, "ratio": stats.ratio,
                                                "attempts": len(result.attempts)})
        await self._capture("recovered")


async def _main(args: argparse.Namespace) -> None:
    bus = await BusClient.connect(config.bus_path(), "controller")
    app = ControllerApp(bus, scenario=args.scenario, auto_capture=args.auto_capture)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await app.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet controller")
    parser.add_argument("--scenario", default="paper_fig1")
    parser.add_argument("--auto-capture", action="store_true")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
