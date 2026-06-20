from __future__ import annotations

import argparse
import json
from pathlib import Path

from semantic_controller.factory import build_semantic_controller_from_env


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the semantic controller smoke set")
    parser.add_argument(
        "--dataset",
        default="evaluation/semantic_smoke_testset.jsonl",
    )
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.dataset).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    controller = build_semantic_controller_from_env()
    trigger_correct = 0
    goal_correct = 0
    valid_plans = 0
    triggered_cases = 0
    for case in cases:
        result = controller.handle_user_input(case["input"])
        trigger_correct += result.trigger.triggered == case["should_trigger"]
        goal_correct += result.goal.goal_id.value == case["goal_id"]
        if case["should_trigger"]:
            triggered_cases += 1
            valid_plans += result.plan is not None

    total = len(cases)
    report = {
        "cases": total,
        "trigger_accuracy": trigger_correct / total if total else 0.0,
        "goal_accuracy": goal_correct / total if total else 0.0,
        "valid_plan_rate": valid_plans / triggered_cases if triggered_cases else 0.0,
        "mode": type(controller.goal_recognizer).__name__,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
