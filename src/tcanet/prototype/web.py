"""TCANet web console: one live page for the whole prototype.

``python -m src.tcanet.prototype.web [--host 0.0.0.0] [--port 8080]``

The server is a bus client like any agent: it follows the controller and
agent reports (``WebHub``), streams them to browsers over SSE, and turns
operator buttons into the same ``tcanetctl`` actions (``ctl.py``) — faults
still reach the controller only through agent reports.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from aiohttp import web

from src.tcanet.prototype import config, ctl
from src.tcanet.prototype.addressing import build_plan, gateway_ns, rule_commands, rule_params
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.webhub import SUBSCRIBE_TOPICS, WebHub
from src.tcanet.subnet import PathRecord, compile_forwarding

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
SCENARIO = "paper_fig1"


class Console:
    def __init__(self, *, mode: str, bus_path: Path, runtime=None) -> None:
        self.mode = mode
        self.bus_path = bus_path
        self.hub = WebHub(SCENARIO, mode=mode, heartbeat_timeout_s=config.heartbeat_timeout_s())
        self.world, self.task = self.hub.world, self.hub.task
        self.plan = build_plan(self.world)
        self.runtime = runtime or ctl.ProcessRuntime(mode, self.world, self.plan, SCENARIO)
        self.bus: BusClient | None = None
        self.ops_bus: BusClient | None = None  # separate client: the broker never echoes to the sender
        self.clients: set[asyncio.Queue] = set()
        self._tasks: list[asyncio.Task] = []
        self._action_lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------
    async def start(self, _app=None) -> None:
        self.bus = await BusClient.connect(self.bus_path, "web", SUBSCRIBE_TOPICS, retries=600, delay=0.1)
        self.ops_bus = await BusClient.connect(self.bus_path, "web-ops")
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._ticker())]

    async def stop(self, _app=None) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in (self.bus, self.ops_bus):
            if client is not None:
                await client.close()

    def _broadcast(self, event: dict) -> None:
        for queue in list(self.clients):
            if queue.qsize() < 2000:
                queue.put_nowait(event)

    async def _pump(self) -> None:
        while (msg := await self.bus.recv()) is not None:
            event = self.hub.apply(msg)
            if event is not None:
                self._broadcast(event)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._broadcast(self.hub.status(time.time()))

    # --- routes -------------------------------------------------------
    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def state(self, _request: web.Request) -> web.Response:
        return web.json_response(self.hub.snapshot(time.time()))

    async def stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        self.clients.add(queue)
        try:
            await response.write(_sse(self.hub.snapshot(time.time())))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                    await response.write(_sse(event))
                except asyncio.TimeoutError:
                    await response.write(b": keep-alive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(queue)
        return response

    async def action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        try:
            coro = self._action(body)
        except (KeyError, ValueError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        if self._action_lock.locked():
            coro.close()
            return web.json_response({"ok": False, "error": "another action is running"}, status=409)
        async with self._action_lock:
            try:
                await coro
            except Exception as exc:  # report, keep serving
                return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
        return web.json_response({"ok": True, "op": body.get("op")})

    def _action(self, body: dict):
        """Validate an operator request and return the coroutine to run."""
        op, bus, rt, world, plan = body.get("op"), self.ops_bus, self.runtime, self.world, self.plan
        gateways = set(world.graph.gateways)
        links = {link.link_id for link in world.graph.links}
        if op == "submit":
            return ctl.submit(bus, SCENARIO)
        if op == "demand":
            dep = _pick(body, "dep", {d.dep_id for d in self.task.dag.dependencies})
            return ctl.demand(bus, dep, _number(body, "mbps", 1, 60))
        if op in ("fail_gateway", "restore_gateway"):
            return ctl.set_gateway(bus, rt, plan, _pick(body, "gateway", gateways), op == "restore_gateway")
        if op in ("fail_link", "restore_link"):
            return ctl.set_link(bus, rt, plan, _pick(body, "link", links), op == "restore_link")
        if op == "degrade":
            return ctl.degrade(bus, rt, world, plan, _pick(body, "link", links), _number(body, "loss", 0, 50))
        if op == "clear":
            return ctl.degrade(bus, rt, world, plan, _pick(body, "link", links), 0.0)
        if op in ("kill", "revive"):
            agent = _pick(body, "agent", set(self.hub.agent_ids))
            return (ctl.kill if op == "kill" else ctl.revive)(bus, rt, agent)
        if op == "hidden_loss_then_fail":
            return self._hidden_loss_then_fail(_pick(body, "link", links), _pick(body, "gateway", gateways))
        if op == "reset":
            return ctl.reset(bus, rt, world, plan)
        raise ValueError(f"unknown op {op!r}")

    async def _hidden_loss_then_fail(self, link: str, gateway: str) -> None:
        await ctl.degrade(self.ops_bus, self.runtime, self.world, self.plan, link, 10.0)
        await ctl.set_gateway(self.ops_bus, self.runtime, self.plan, gateway, False)

    async def verify(self, request: web.Request) -> web.Response:
        gateway = request.query.get("gateway", "G1")
        if gateway not in self.world.graph.gateways:
            return web.json_response({"error": "unknown gateway"}, status=400)
        expected = self.expected_ft(gateway)
        kernel = None
        if self.mode == "netns":
            ns = gateway_ns(gateway)
            kernel = {"ip rule show": await _run(["ip", "-n", ns, "rule", "show"])}
            for dep in sorted(d.dep_id for d in self.task.dag.dependencies):
                table = 100 + sorted(d.dep_id for d in self.task.dag.dependencies).index(dep)
                kernel[f"ip route show table {table}"] = await _run(
                    ["ip", "-n", ns, "route", "show", "table", str(table)])
        return web.json_response({"gateway": gateway, "mode": self.mode,
                                  "version": self.hub.subnet.get("version", 0),
                                  "expected": expected, "kernel": kernel})

    def expected_ft(self, gateway: str) -> list[str]:
        """The controller's ``FT^g_m`` for ``gateway`` as Linux commands."""
        paths = {dep: PathRecord(dep, tuple(path)) for dep, path in (self.hub.subnet.get("paths") or {}).items()}
        if not paths:
            return []
        lines = []
        for entry in compile_forwarding(self.task.dag, paths).values():
            if entry.gateway_id != gateway:
                continue
            cmds = rule_commands(rule_params(entry, self.task, self.plan), "add")
            lines.append(cmds[0].text())
            lines.append(cmds[-1].text())
        return lines


def _pick(body: dict, key: str, allowed: set[str]) -> str:
    value = body.get(key)
    if value not in allowed:
        raise ValueError(f"{key} must be one of {sorted(allowed)}")
    return value


def _number(body: dict, key: str, low: float, high: float) -> float:
    value = float(body[key])
    if not low <= value <= high:
        raise ValueError(f"{key} must be within [{low}, {high}]")
    return value


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()


async def _run(argv: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return out.decode(errors="replace").strip()


def build_app(console: Console) -> web.Application:
    app = web.Application()
    app.on_startup.append(console.start)
    app.on_cleanup.append(console.stop)
    app.router.add_get("/", console.index)
    app.router.add_get("/api/state", console.state)
    app.router.add_get("/api/stream", console.stream)
    app.router.add_post("/api/action", console.action)
    app.router.add_get("/api/verify", console.verify)
    app.router.add_static("/static/", STATIC_DIR)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet web console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TCANET_WEB_PORT", "8080")))
    args = parser.parse_args()
    console = Console(mode=config.read_mode(), bus_path=config.bus_path())
    web.run_app(build_app(console), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
