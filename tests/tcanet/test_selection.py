"""Two-stage lexicographic selection tests (paper Eq. 4-7)."""
from __future__ import annotations

import unittest

from src.tcanet.candidates import CandidateAction, no_change
from src.tcanet.feasibility import FeasibilityResult, Projection
from src.tcanet.selection import CandidateEvaluation, two_stage_select
from src.tcanet.spec import Layer, SoftTarget


def _action(action_id: str, layer: Layer = Layer.APPLICATION) -> CandidateAction:
    return CandidateAction(
        action_id=action_id, layer=layer, action="OP", target="d1"
    )


def _feasible(ok: bool) -> FeasibilityResult:
    projection = Projection(
        dependencies={},
        resource_load_mbps={},
        resource_capacity_mbps={},
        actions=(),
    )
    return FeasibilityResult(
        feasible=ok, violations=() if ok else ("x",), projection=projection
    )


def _eval(action_id: str, v: float, r: int, ok: bool = True) -> CandidateEvaluation:
    return CandidateEvaluation(
        actions=(no_change(Layer.APPLICATION), _action(action_id)),
        feasibility=_feasible(ok),
        soft_violation=v,
        modification_scope=r,
    )


class SoftTargetViolationTests(unittest.TestCase):
    def test_upper_target_only_counts_excess(self) -> None:
        target = SoftTarget("delay", "upper", 20.0, "mean_delay_ms")
        self.assertEqual(target.violation(15.0), 0.0)
        self.assertAlmostEqual(target.violation(30.0), 0.5)

    def test_lower_target_only_counts_shortfall(self) -> None:
        target = SoftTarget("floor", "lower", 10.0, "min_delivered_mbps")
        self.assertEqual(target.violation(12.0), 0.0)
        self.assertAlmostEqual(target.violation(7.5), 0.25)

    def test_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            SoftTarget("x", "sideways", 1.0, "m").violation(0.0)


class TwoStageSelectTests(unittest.TestCase):
    def test_stage1_v_filters_before_stage2_r(self) -> None:
        trace = two_stage_select(
            (_eval("low_v_high_r", 0.0, 9), _eval("low_v_low_r", 0.0, 2),
             _eval("best_v_but_infeasible", 0.0, 0, ok=False),
             _eval("worse_v", 0.5, 0))
        )
        self.assertEqual(trace.selected.actions[1].action_id, "low_v_low_r")
        self.assertEqual(trace.stage1_min_v, 0.0)
        self.assertEqual(trace.stage2_min_r, 2)
        self.assertEqual(len(trace.stage1_survivors), 2)

    def test_r_never_overrides_v(self) -> None:
        """A decision with larger R but smaller V must still win (Eq. 6-7)."""
        trace = two_stage_select(
            (_eval("small_r_bad_v", 0.9, 0), _eval("big_r_good_v", 0.0, 7))
        )
        self.assertEqual(trace.selected.actions[1].action_id, "big_r_good_v")

    def test_no_feasible_decision(self) -> None:
        trace = two_stage_select((_eval("a", 0.0, 0, ok=False),))
        self.assertIsNone(trace.selected)

    def test_tie_prefers_fewest_changed_layers(self) -> None:
        one = CandidateEvaluation(
            actions=(no_change(Layer.APPLICATION), _action("zz:one")),
            feasibility=_feasible(True),
            soft_violation=0.0,
            modification_scope=1,
        )
        two = CandidateEvaluation(
            actions=(
                no_change(Layer.APPLICATION),
                _action("aa:two"),
                _action("bb:two"),
            ),
            feasibility=_feasible(True),
            soft_violation=0.0,
            modification_scope=1,
        )
        trace = two_stage_select((two, one))
        self.assertEqual(trace.selected, one)
        self.assertIn("tie", trace.tiebreak_note)


if __name__ == "__main__":
    unittest.main()
