"""TransAgent: per-dependency transport-session state and profile actions."""
from __future__ import annotations

import asyncio

from src.tcanet.prototype import config
from src.tcanet.prototype.agents.base import AgentBase

_ALPHA = 0.3


class TransAgent(AgentBase):
    role = "transport"

    def __init__(self, agent_id: str, gateway: str, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.bound: set[str] = set()
        self.modes: dict[str, str] = {}
        self.sessions: dict[str, dict] = {}

    def topics(self) -> tuple[str, ...]:
        return super().topics() + ("report.flow", "ctrl.subnet")

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._summaries(stop),)

    async def on_message(self, msg: dict) -> None:
        payload = msg["payload"]
        if msg["topic"] == "ctrl.subnet":
            self.bound = {
                dep for dep, (t_agent, _n, _p) in payload.get("bindings", {}).items()
                if t_agent == self.agent_id
            }
        elif msg["topic"] == "report.flow" and payload["dep_id"] in self.bound:
            session = self.sessions.setdefault(payload["dep_id"], {"rx": 0.0, "loss": 0.0, "owd": 0.0})
            session["rx"] += _ALPHA * (payload["rx_mbps"] - session["rx"])
            if payload.get("loss") is not None:
                session["loss"] += _ALPHA * (payload["loss"] - session["loss"])
            if payload.get("owd_ms") is not None:
                session["owd"] += _ALPHA * (payload["owd_ms"] - session["owd"])

    async def _summaries(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(4 * config.sample_period_s())
            for dep in sorted(self.bound):
                session = self.sessions.get(dep)
                if session is None:
                    continue
                mode = self.modes.get(dep, "standard")
                await self.bus.publish("report.session", {"dep_id": dep, "mode": mode, **session})
                await self.log(f"session {dep}: {session['rx']:.1f} Mbps, loss {session['loss']*100:.1f}%, "
                               f"owd {session['owd']:.1f} ms [{mode}]")

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "SWITCH_MODE":
            params = payload["params"]
            self.modes[payload["target"]] = params["mode"]
            await self.bus.publish("cmd.session", {"dep_id": payload["target"], "mode": params["mode"],
                                                   "app_agent": params["app_agent"]})
            await self.log(f"transport profile {payload['target']} → {params['mode']}")
            return True, params["mode"]
        return await super().handle(payload)
