"""Common agent process skeleton: hello, heartbeat, command dispatch, log."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from rich.console import Console

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

_COLORS = {"application": "green", "transport": "dark_orange", "network": "dodger_blue1",
           "physical": "magenta"}


class AgentBase:
    role = "agent"

    def __init__(self, agent_id: str, gateway: str, *, bus_path: Path,
                 fabric_factory: Callable[[BusClient], object]) -> None:
        self.agent_id = agent_id
        self.gateway = gateway
        self.bus_path = bus_path
        self._fabric_factory = fabric_factory
        self.bus: BusClient | None = None
        self.fabric = None
        self.console = Console(highlight=False)

    def topics(self) -> tuple[str, ...]:
        return ("cmd.action", "sim.")

    def background(self, stop: asyncio.Event) -> tuple:
        return ()

    async def run(self, stop: asyncio.Event) -> None:
        self.bus = await BusClient.connect(self.bus_path, self.agent_id, self.topics())
        self.fabric = self._fabric_factory(self.bus)
        await self.bus.publish("hello", {"agent_id": self.agent_id, "role": self.role,
                                         "gateway": self.gateway})
        await self.started()
        tasks = [asyncio.create_task(self._heartbeat(stop))]
        tasks += [asyncio.create_task(coro) for coro in self.background(stop)]
        try:
            while not stop.is_set():
                try:
                    msg = await self.bus.recv(timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    break
                await self.dispatch(msg)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stopped()
            await self.bus.close()

    async def started(self) -> None:
        await self.log(f"online at {self.gateway} ({type(self.fabric).__name__})")

    async def stopped(self) -> None:
        pass

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.bus.publish("heartbeat", {"agent_id": self.agent_id})
            await asyncio.sleep(config.heartbeat_s())

    async def dispatch(self, msg: dict) -> None:
        topic, payload = msg["topic"], msg["payload"]
        if topic == "cmd.action" and payload.get("executor") == self.agent_id:
            try:
                ok, detail = await self.handle(payload)
            except Exception as exc:  # report executor failure instead of dying
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            await self.bus.publish("ack", {"action_id": payload["action_id"], "ok": ok,
                                           "detail": detail, "executor": self.agent_id})
            return
        if topic.startswith("sim.") and hasattr(self.fabric, "feed"):
            await self.fabric.feed(msg)
            return
        await self.on_message(msg)

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "bind_support":
            role = {"t": "TransAgent", "n": "NetAgent", "p": "PhyAgent"}[payload["value"]]
            verb = "released" if payload["action_id"].startswith("undo:") else "bound"
            await self.log(f"Φ: {verb} as {role} of {payload['target']}")
            return True, verb
        return False, f"{self.agent_id} cannot execute {payload['kind']}"

    async def on_message(self, msg: dict) -> None:
        pass

    async def log(self, text: str, level: str = "info") -> None:
        color = _COLORS.get(self.role, "white")
        style = {"warn": "bold yellow", "fail": "bold red", "ok": "bold green"}.get(level, "")
        self.console.print(f"[{color}]{self.agent_id:>13}[/] [{style}]{text}[/]" if style
                           else f"[{color}]{self.agent_id:>13}[/] {text}")
        if self.bus is not None:
            await self.bus.publish("agent.log", {"agent_id": self.agent_id, "role": self.role,
                                                 "gateway": self.gateway, "text": text,
                                                 "level": level})
