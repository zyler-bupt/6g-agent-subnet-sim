from __future__ import annotations

import unittest

from viz.trace import build_scenario_trace, list_scenarios


class VizTraceTests(unittest.TestCase):
    def test_lists_three_scenarios(self) -> None:
        keys = {s["key"] for s in list_scenarios()}
        self.assertEqual(keys, {"tier1", "tier2", "tier3"})

    def test_trace_has_full_step_sequence(self) -> None:
        for key in ("tier1", "tier2", "tier3"):
            trace = build_scenario_trace(key)
            self.assertTrue(trace["nodes"])
            self.assertTrue(trace["edges"])
            kinds = [s["kind"] for s in trace["steps"]]
            for required in ("input", "networked", "predict", "event", "adjust", "recovered", "compare"):
                self.assertIn(required, kinds, f"{key} missing step {required}")

    def test_tier_strategies_match_design(self) -> None:
        expected = {
            "tier1": "tier1 局部调参",
            "tier2": "tier2 替换支撑Agent",
            "tier3": "tier3 调整通信关系",
        }
        for key, label in expected.items():
            trace = build_scenario_trace(key)
            adjust = next(s for s in trace["steps"] if s["kind"] == "adjust")
            self.assertEqual(dict(adjust["badges"])["策略"], label)

    def test_minimal_interruption_below_full_rebuild(self) -> None:
        for key in ("tier1", "tier2", "tier3"):
            trace = build_scenario_trace(key)
            compare = next(s for s in trace["steps"] if s["kind"] == "compare")["compare"]
            self.assertLess(compare["minimal"]["interruption"], compare["full"]["interruption"])
            self.assertTrue(compare["minimal"]["qos"])

    def test_tier3_marks_failed_node_and_reroute_op(self) -> None:
        trace = build_scenario_trace("tier3")
        adjust = next(s for s in trace["steps"] if s["kind"] == "adjust")
        self.assertIn("nagent-gw-ue", adjust["failed_nodes"])
        self.assertTrue(adjust["changed_nodes"])  # a standby Agent was brought in


if __name__ == "__main__":
    unittest.main()
