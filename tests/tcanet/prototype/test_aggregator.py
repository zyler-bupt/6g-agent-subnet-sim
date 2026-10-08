from __future__ import annotations

import unittest

from src.tcanet.prototype.aggregator import EventDetector, FlowBook, StateAggregator
from src.tcanet.scenario_fig1 import load


def _msg(topic: str, payload: dict, ts: float, src: str = "x") -> dict:
    return {"topic": topic, "src": src, "ts": ts, "payload": payload}


def _link(link_id: str, up: bool, reporter: str, ts: float) -> dict:
    return _msg("report.link", {"link_id": link_id, "up": up, "reporter": reporter}, ts)


class AggregatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _task = load("paper_fig1")
        self.agg = StateAggregator(self.world)
        self.det = EventDetector(self.world, debounce_s=0.8)

    def _feed(self, msg: dict) -> None:
        self.det.feed(self.agg.apply(msg))

    def test_staggered_reports_classify_as_gateway_failure(self) -> None:
        for reporter, link in (("network-G1", "L1"), ("network-G2", "L1"),
                               ("network-G2", "L2"), ("network-G4", "L2")):
            self._feed(_link(link, True, reporter, 0.0))
        self._feed(_link("L1", False, "network-G1", 10.0))
        self.assertEqual(self.det.poll(10.3), [])
        self._feed(_link("L2", False, "network-G4", 10.5))  # one period later
        events = self.det.poll(10.9)
        self.assertEqual([(e.kind, e.gateway_id) for e, _ in events], [("gateway_failure", "G2")])
        self.assertEqual(events[0][1], 10.0)
        self.assertIn("G2", self.world.failed_gateways)

    def test_gateway_restores_when_links_report_up(self) -> None:
        self.test_staggered_reports_classify_as_gateway_failure()
        self._feed(_link("L1", True, "network-G1", 20.0))
        self.assertIn("G2", self.world.failed_gateways)
        self._feed(_link("L2", True, "network-G4", 20.5))
        self.assertNotIn("G2", self.world.failed_gateways)
        self.assertTrue(self.world.graph.link("L1").up)
        self.assertTrue(self.world.support_agents["network-G2"].online)

    def test_single_link_down_is_link_failure(self) -> None:
        self._feed(_link("L6", True, "network-G1", 0.0))
        self._feed(_link("L6", False, "network-G1", 5.0))
        events = self.det.poll(6.0)
        self.assertEqual([(e.kind, e.link_id) for e, _ in events], [("link_failure", "L6")])
        self.assertFalse(self.world.graph.link("L6").up)

    def test_silent_support_agent_is_support_failure(self) -> None:
        self._feed(_msg("heartbeat", {"agent_id": "physical-G4"}, 1.0))
        self.det.feed(self.agg.silent_agents(now=2.0, timeout_s=1.5))
        self.assertEqual(self.det.poll(2.0), [])
        self.det.feed(self.agg.silent_agents(now=3.0, timeout_s=1.5))
        events = self.det.poll(3.5)
        self.assertEqual([(e.kind, e.agent_ids) for e, _ in events],
                         [("support_failure", ("physical-G4",))])
        self.assertFalse(self.world.support_agents["physical-G4"].online)

    def test_agents_of_failed_gateway_are_not_separate_events(self) -> None:
        self.world.failed_gateways.add("G2")
        self._feed(_msg("heartbeat", {"agent_id": "network-G2"}, 1.0))
        self.det.feed(self.agg.silent_agents(now=5.0, timeout_s=1.5))
        self.assertEqual(self.det.poll(9.0), [])

    def test_demand_change_and_access_capacity(self) -> None:
        self._feed(_msg("report.app", {"dep_id": "e1", "demand_mbps": 15.0}, 1.0))
        self._feed(_msg("report.app", {"dep_id": "e1", "demand_mbps": 25.0}, 2.0))
        events = self.det.poll(2.0)
        self.assertEqual(events[0][0].kind, "demand_change")
        self.assertAlmostEqual(events[0][0].payload["factor"], 25 / 15)
        self._feed(_msg("report.access", {"gateway": "G4", "capacity_mbps": 45.0}, 3.0))
        self.assertEqual(self.world.resources["access-dl:G4"].capacity_mbps, 45.0)

    def test_flowbook_window(self) -> None:
        book = FlowBook(horizon_s=10.0)
        for ts in range(20):
            book.add("e1", float(ts), {"rx_mbps": ts})
        self.assertEqual(len(book.since("e1", 15.0)), 5)
        self.assertEqual(book.latest("e1")["rx_mbps"], 19)
        self.assertEqual(len(book.since("e1", 0.0)), 11)


if __name__ == "__main__":
    unittest.main()
