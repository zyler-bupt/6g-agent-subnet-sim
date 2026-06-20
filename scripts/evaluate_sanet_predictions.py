from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare SANet predictions with a last-value baseline"
    )
    parser.add_argument("--prediction-dir", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    prediction_dir = Path(args.prediction_dir)
    report = {
        key: _evaluate_task(prediction_dir, key)
        for key in ("csi", "traffic", "intent")
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


def _evaluate_task(prediction_dir: Path, key: str) -> dict:
    prediction = np.load(prediction_dir / f"{key}_pred.npy")
    target = np.load(prediction_dir / f"{key}_gt.npy")
    history = np.load(prediction_dir / f"{key}_input.npy")
    if prediction.shape != target.shape:
        raise ValueError(f"{key}: prediction and target shapes differ")
    if history.ndim != 2 or target.ndim != 2:
        raise ValueError(f"{key}: expected two-dimensional arrays")

    last_value = np.repeat(history[:, -1:], target.shape[1], axis=1)
    model_metrics = _metrics(prediction, target)
    baseline_metrics = _metrics(last_value, target)
    improvement = (
        baseline_metrics["nmae"] - model_metrics["nmae"]
    ) / max(baseline_metrics["nmae"], 1e-12)
    return {
        "samples": int(target.shape[0]),
        "horizon": int(target.shape[1]),
        "model": model_metrics,
        "last_value": baseline_metrics,
        "relative_nmae_improvement": improvement,
    }


def _metrics(prediction: np.ndarray, target: np.ndarray) -> dict[str, float]:
    error = prediction - target
    mae = float(np.mean(np.abs(error)))
    denominator = float(np.mean(np.abs(target))) + 1e-8
    return {
        "mae": mae,
        "nmae": mae / denominator,
        "rmse": float(np.sqrt(np.mean(error * error))),
    }


if __name__ == "__main__":
    main()
