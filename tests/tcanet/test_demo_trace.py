"""Demo-layer tests: CLI narrative runs; trace schema fits the viz frontend."""
from __future__ import annotations

import contextlib
import io
import unittest

from src.tcanet.demo.trace import TCANET_SCENARIOS, build_tcanet_trace
from viz.trace import build_scenario_trace

_STEP_EDGE_FIELDS = ("active_edges", "focus_edges", "changed_edges")
_STEP_NODE_FIELDS = ("active_nodes", "focus_nodes", "failed_nodes", "changed_nodes")


class TcanetTraceTests(unittest.TestCase):
    def test_all_scenarios_produce_valid_traces(self) -> None:
        for key in TCANET_SCENARIOS:
            with self.subTest(scenario=key):
                trace = build_tcanet_trace(key)
                self.assertTrue(trace["nodes"])
                self.assertTrue(trace["edges"])
                self.assertGreaterEqual(len(trace["steps"]), 7)

    def test_step_references_resolve(self) -> None:
        """Frontend resolves step ids against the node/edge lists."""
        for key in TCANET_SCENARIOS:
            with self.subTest(scenario=key):
                trace = build_tcanet_trace(key)
                node_ids = {n["id"] for n in trace["nodes"]}
                edge_ids = {e["id"] for e in trace["edges"]}
                for step in trace["steps"]:
                    for field in _STEP_EDGE_FIELDS:
                        self.assertTrue(
                            set(step[field]) <= edge_ids,
                            f"{key}/{step['kind']}: dangling {field}",
                        )
                    for field in _STEP_NODE_FIELDS:
                        self.assertTrue(
                            set(step[field]) <= node_ids,
                            f"{key}/{step['kind']}: dangling {field}",
                        )

    def test_formation_ends_accepted_and_versioned(self) -> None:
        trace = build_tcanet_trace("tcanet_formation")
        last = trace["steps"][-1]
        badges = dict(last["badges"])
        self.assertEqual(badges["版本"], "v0 → v1")
        self.assertTrue(last["qos"])

    def test_recovery_story_steps_present(self) -> None:
        trace = build_tcanet_trace("tcanet_recovery")
        kinds = [s["kind"] for s in trace["steps"]]
        for required in ("event", "closure0", "closure1", "selection", "recovered"):
            self.assertIn(required, kinds)
        event = next(s for s in trace["steps"] if s["kind"] == "event")
        self.assertIn("G2", event["failed_nodes"])
        recovered = next(s for s in trace["steps"] if s["kind"] == "recovered")
        self.assertTrue(recovered["qos"])
        badges = dict(recovered["badges"])
        # Record-level comparison against full rebuild (Eq. 10 Mod):
        # 4/16 changed records vs a rebuild's 16/16.
        self.assertEqual(badges["对比"], "全量重建 16/16")
        self.assertEqual(badges["Mod_m"], "4/16")

    def test_rebind_story_shows_two_attempts(self) -> None:
        trace = build_tcanet_trace("tcanet_rebind")
        badges = [
            dict(s["badges"]).get("B_r")
            for s in trace["steps"]
            if s["kind"] in ("adjust", "adjust2")
        ]
        self.assertEqual(badges, ["1/3", "2/3"])
        rejected = next(s for s in trace["steps"] if s["kind"] == "verify")
        self.assertFalse(rejected["qos"])

    def test_viz_dispatch_routes_tcanet_keys(self) -> None:
        own = build_scenario_trace("tcanet_coordination")
        self.assertEqual(own["scenario"], "tcanet_coordination")
        for field in ("nodes", "edges", "steps", "name", "trigger"):
            self.assertIn(field, own)


class CliNarrativeTests(unittest.TestCase):
    def test_cli_runs_all_acts(self) -> None:
        from src.tcanet.demo import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.main()
        output = buffer.getvalue()
        for marker in (
            "任务输入",
            "子网构建",
            "联合决策",
            "弹性重构",
            "总结",
            "v4",
        ):
            self.assertIn(marker, output)


if __name__ == "__main__":
    unittest.main()
