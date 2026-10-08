"""Web console: hub caption/state and the HTTP API over a real bus."""
from __future__ import annotations

import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from src.tcanet.prototype.web import Console, build_app
from src.tcanet.prototype.webhub import WebHub
from tests.tcanet.prototype.harness import BusHarness, wait_for


def _m(topic, payload, ts=100.0, src="x"):
    return {"topic": topic, "src": src, "ts": ts, "payload": payload}


def _log(step, title, level="info", lines=(), **data):
    return _m("ctrl.log", {"step": step, "title": title, "level": level, "lines": list(lines), "data": data})


class WebHubTests(unittest.TestCase):
    def test_caption_tells_the_recovery_story(self) -> None:
        hub = WebHub()
        for msg in (
            _log("event", "Event: gateway failure: G2", "fail", kind="gateway_failure"),
            _log("scope", "Determine scope", affected=["e1", "e2"]),
            _log("select", "Joint feasibility", lines=["c* = network:REROUTE:e1:G1|G3|G4  (min M = 4, Eq. 12)"]),
            _log("assess", "Assess (Eq. 5–6): FAIL loss_hard:e1", "fail"),
            _log("rollback", "Rollback v2 → v1; refresh s_m", "warn"),
            _log("assess", "Assess (Eq. 5–6): PASS", "ok"),
            _log("commit", "Commit v2", "ok", version=2, latency_ms=3440.0),
        ):
            hub.apply(msg)
        self.assertEqual(hub.caption, " → ".join([
            "Gateway G2 failed, detected by its neighbours' NetAgents", "scope {e1, e2}, e3 untouched",
            "reroute e1 via G1→G3→G4", "measured QoS failed (loss on e1)",
            "rolled back, trying next candidate", "measured QoS passed", "committed v2 in 3.4 s"]))
        self.assertEqual(hub.affected, [])

    def test_status_derives_failures_from_reports(self) -> None:
        hub = WebHub()
        for reporter, link in (("network-G1", "L1"), ("network-G4", "L2")):
            hub.apply(_m("report.link", {"link_id": link, "up": False, "reporter": reporter}))
        hub.apply(_m("heartbeat", {"agent_id": "network-G1"}, ts=99.5))
        hub.apply(_m("ctrl.subnet", {"version": 1, "paths": {"e1": ["G1", "G2", "G4"]}, "bindings": {}}))
        hub.apply(_m("report.flow", {"dep_id": "e1", "rx_mbps": 0.0, "owd_ms": None, "loss": None}))
        status = hub.status(now=100.0)
        self.assertEqual(status["failed_gateways"], ["G2"])
        self.assertTrue(status["agents"]["network-G1"])
        self.assertFalse(status["agents"]["network-G2"])
        self.assertEqual(status["qos"]["e1"], "violated")
        self.assertEqual(hub.snapshot(100.0)["scenario"]["gateways"], ["G1", "G2", "G3", "G4"])


class _NoRuntime:
    mode = "sim"

    async def kernel(self, cmds):  # pragma: no cover - not used by these actions
        raise AssertionError

    def kill(self, agent_id):
        return True

    def revive(self, agent_id):
        return True


class WebApiTests(unittest.TestCase):
    def test_page_state_and_actions(self) -> None:
        async def scenario():
            async with BusHarness() as h:
                console = Console(mode="sim", bus_path=h.sock, runtime=_NoRuntime())
                probe = await h.client("probe", "task.", "ops.fault")
                async with TestClient(TestServer(build_app(console))) as client:
                    page = await client.get("/")
                    self.assertIn("TCANet testbed", await page.text())
                    state = await (await client.get("/api/state")).json()
                    self.assertEqual(state["status"]["mode"], "sim")
                    ok = await (await client.post("/api/action", json={"op": "submit"})).json()
                    self.assertTrue(ok["ok"])
                    await wait_for(probe, lambda m: m["topic"] == "task.submit")
                    bad = await client.post("/api/action", json={"op": "fail_gateway", "gateway": "G9"})
                    self.assertEqual(bad.status, 400)
                    await client.post("/api/action", json={"op": "kill", "agent": "physical-G4"})
                    await wait_for(probe, lambda m: m["topic"] == "ops.fault" and m["payload"]["op"] == "kill")
                    await asyncio.sleep(0.1)
                    self.assertEqual(console.hub.faults[-1]["target"], "physical-G4")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
