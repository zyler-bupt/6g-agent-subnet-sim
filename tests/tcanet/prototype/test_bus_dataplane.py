from __future__ import annotations

import asyncio
import unittest

from src.tcanet.closure import gateway_failure
from src.tcanet.executor import stage_decision
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.aggregator import FlowBook
from src.tcanet.prototype.assess import BusMeasurement
from src.tcanet.prototype.bus_dataplane import (
    AckWaiter,
    BusDataplane,
    command_payload,
    resolve_executor,
)
from src.tcanet.candidates import CandidateAction
from src.tcanet.scenario import fail_gateway, run_formation
from src.tcanet.spec import Layer
from src.tcanet.scenario_fig1 import load
from src.tcanet.verify import RecoveryController
from tests.tcanet.prototype.harness import BusHarness


def _formed():
    world, task = load("paper_fig1")
    subnet = asyncio.run(run_formation(task, world)).subnet
    return world, task, subnet


def _g2_reroute(world, task, subnet):
    """The staged decision TCANet selects after G2 fails."""
    fail_gateway(world, "G2")
    result = asyncio.run(RecoveryController().recover(task, subnet, world, gateway_failure("G2")))
    return stage_decision(task, subnet, result.attempts[-1].selection.selected.actions, world)


class FakeAgents:
    """Acks every cmd.action except for agents listed as dead."""

    def __init__(self, client, dead=()):
        self.client, self.dead, self.seen = client, set(dead), []

    async def run(self):
        while True:
            msg = await self.client.recv()
            if msg is None:
                return
            if msg["topic"] == "cmd.action":
                self.seen.append(msg["payload"])
                if msg["payload"]["executor"] not in self.dead:
                    await self.client.publish("ack", {"action_id": msg["payload"]["action_id"],
                                                      "ok": True, "detail": "ok"})


async def _run_dataplane(world, task, staged, dead=(), rollback=False):
    async with BusHarness() as h:
        controller = await h.client("controller", "ack")
        agents = FakeAgents(await h.client("agents", "cmd.action"), dead)
        agents_task = asyncio.create_task(agents.run())
        acks = AckWaiter()

        async def pump():
            while (msg := await controller.recv()) is not None:
                acks.feed(msg["payload"])

        pump_task = asyncio.create_task(pump())
        dataplane = BusDataplane(controller, acks, build_plan(world), lambda: task,
                                 ack_timeout_s=0.3)
        record = await dataplane.apply(staged, world)
        if rollback:
            await dataplane.rollback(staged, world)
        await asyncio.sleep(0.05)
        for task_ in (agents_task, pump_task):
            task_.cancel()
        return record, agents.seen


class CommandTests(unittest.TestCase):
    def test_executor_resolution_and_inverse_rules(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        by_id = {action.action_id: action for action in staged.executable}
        replace_g1 = by_id["install:e1:G1:0"]
        self.assertEqual(resolve_executor(replace_g1, staged, task), "network-G1")
        plan = build_plan(world)
        forward = command_payload(replace_g1, staged, task, plan)
        self.assertEqual(forward["params"]["next_hop_gateway"], "G3")
        undo = command_payload(replace_g1, staged, task, plan, undo=True)
        self.assertEqual(undo["kind"], "install_rule")
        self.assertEqual(undo["params"]["next_hop_gateway"], "G2")
        fresh = command_payload(by_id["install:e1:G3:1"], staged, task, plan, undo=True)
        self.assertEqual(fresh["kind"], "remove_rule")
        removed = command_payload(by_id["remove:e1:G2:1"], staged, task, plan, undo=True)
        self.assertEqual(removed["kind"], "install_rule")


    def test_same_gateway_replacement_is_not_withdrawn(self) -> None:
        world, task, subnet = _formed()
        reroute = CandidateAction(
            action_id="network:REROUTE:e1:G1|G4", layer=Layer.NETWORK, action="REROUTE",
            target="e1", parameters={"gateway_path": ("G1", "G4")},
        )
        staged = stage_decision(task, subnet, (reroute,), world)
        by_id = {action.action_id: action for action in staged.executable}
        plan = build_plan(world)
        self.assertIsNone(command_payload(by_id["remove:e1:G4:2"], staged, task, plan))
        undo = command_payload(by_id["install:e1:G4:1"], staged, task, plan, undo=True)
        self.assertEqual((undo["kind"], undo["params"]["rule_id"]), ("install_rule", "e1:G4:2"))


class BusDataplaneTests(unittest.TestCase):
    def test_make_before_break_and_rollback(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, seen = asyncio.run(_run_dataplane(world, task, staged, rollback=True))
        self.assertTrue(record.ok, record)
        kinds = [p["kind"] for p in seen]
        self.assertEqual(kinds[0], "install_rule")
        self.assertTrue(any(p["action_id"].startswith("undo:") for p in seen))

    def test_remove_rule_at_failed_gateway_needs_no_ack(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, seen = asyncio.run(_run_dataplane(world, task, staged))
        removal = next(o for o in record.outcomes if o.action_id == "remove:e1:G2:1")
        self.assertTrue(removal.ok)
        self.assertIn("withdrawn with failed gateway G2", removal.detail)
        self.assertNotIn("network-G2", {p["executor"] for p in seen})

    def test_dead_executor_times_out_and_fails_batch(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, _seen = asyncio.run(_run_dataplane(world, task, staged, dead={"network-G3"}))
        self.assertFalse(record.ok)
        self.assertIn("did not acknowledge", record.outcomes[-1].detail)


class AssessTests(unittest.TestCase):
    def _measure(self, book, now=100.0):
        async def no_sleep(_s):
            return None

        measurement = BusMeasurement(book, window_s=2.0, settle_s=0.5,
                                     clock=lambda: now, sleep=no_sleep)
        return asyncio.run(measurement(None, None, ("e1", "e2")))

    def test_summaries(self) -> None:
        book = FlowBook()
        for i, (rate, owd, loss) in enumerate([(15, 20, 0.0), (14, 22, 0.02), (16, 30, 0.01), (15, 21, 0.0)]):
            book.add("e1", 100.6 + 0.4 * i, {"rx_mbps": rate, "owd_ms": owd, "loss": loss})
        book.add("e1", 100.1, {"rx_mbps": 0.0, "owd_ms": None, "loss": None})  # before settle
        (obs,) = self._measure(book)
        self.assertEqual(obs.dep_id, "e1")
        self.assertEqual(obs.throughput_mbps, 15)
        self.assertEqual(obs.delay_ms, 22)
        self.assertAlmostEqual(obs.loss_rate, 0.0075)
        self.assertLessEqual(obs.observed_at_ms, 2000.0)

    def test_no_packets_yields_violating_observation(self) -> None:
        book = FlowBook()
        book.add("e2", 101.0, {"rx_mbps": 0.0, "owd_ms": None, "loss": None})
        (obs,) = self._measure(book)
        self.assertEqual(obs.throughput_mbps, 0.0)
        self.assertEqual(obs.delay_ms, float("inf"))
        self.assertEqual(obs.loss_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
