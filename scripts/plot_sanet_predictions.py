from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


TASKS = {
    "csi": ("CSI amplitude", "Amplitude"),
    "traffic": ("Traffic / network series", "Mbps"),
    "intent": ("Intent / application series", "Mbps"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot SANet forecast outputs")
    parser.add_argument("--prediction-dir", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    prediction_dir = Path(args.prediction_dir)
    output_dir = Path(args.output_dir) if args.output_dir else prediction_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    _plot_forecast_examples(prediction_dir, output_dir / "forecast_examples.png")
    _plot_prediction_scatter(prediction_dir, output_dir / "prediction_scatter.png")
    print(f"saved={output_dir / 'forecast_examples.png'}")
    print(f"saved={output_dir / 'prediction_scatter.png'}")


def _load(root: Path, key: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    prediction = np.load(root / f"{key}_pred.npy")
    target = np.load(root / f"{key}_gt.npy")
    history = np.load(root / f"{key}_input.npy")
    return history, target, prediction


def _plot_forecast_examples(root: Path, output: Path) -> None:
    figure, axes = plt.subplots(3, 1, figsize=(11, 12), constrained_layout=True)
    for axis, (key, (title, unit)) in zip(axes, TASKS.items()):
        history, target, prediction = _load(root, key)
        sample_error = np.mean(np.abs(prediction - target), axis=1)
        sample_index = int(np.argsort(sample_error)[len(sample_error) // 2])
        past_x = np.arange(-history.shape[1] + 1, 1)
        future_x = np.arange(1, target.shape[1] + 1)
        baseline = np.repeat(history[sample_index, -1], target.shape[1])

        axis.plot(past_x, history[sample_index], color="#555555", marker="o", label="History")
        axis.plot(future_x, target[sample_index], color="#16855b", marker="o", linewidth=2.2, label="Ground truth")
        axis.plot(future_x, prediction[sample_index], color="#d64b3c", marker="s", linewidth=2.2, label="SANet")
        axis.plot(future_x, baseline, color="#3569c8", linestyle="--", linewidth=1.8, label="Last-value")
        axis.axvline(0.5, color="#999999", linestyle=":", linewidth=1)
        axis.set_title(f"{title} - median-error validation window #{sample_index}")
        axis.set_xlabel("Relative time step")
        axis.set_ylabel(unit)
        axis.grid(alpha=0.25)
        axis.legend(ncol=4, loc="best")
    figure.suptitle("SANet 10-epoch forecasts: 15 history steps to 5 future steps", fontsize=15)
    figure.savefig(output, dpi=180)
    plt.close(figure)


def _plot_prediction_scatter(root: Path, output: Path) -> None:
    figure, axes = plt.subplots(1, 3, figsize=(16, 5), constrained_layout=True)
    for axis, (key, (title, unit)) in zip(axes, TASKS.items()):
        history, target, prediction = _load(root, key)
        baseline = np.repeat(history[:, -1:], target.shape[1], axis=1)
        model_nmae = _nmae(prediction, target)
        baseline_nmae = _nmae(baseline, target)
        low = float(min(target.min(), prediction.min()))
        high = float(max(target.max(), prediction.max()))

        axis.scatter(target.ravel(), prediction.ravel(), s=12, alpha=0.32, color="#d64b3c", edgecolors="none")
        axis.plot([low, high], [low, high], color="#333333", linestyle="--", linewidth=1.2)
        axis.set_title(title)
        axis.set_xlabel(f"Ground truth ({unit})")
        axis.set_ylabel(f"SANet prediction ({unit})")
        axis.grid(alpha=0.2)
        axis.text(
            0.04,
            0.96,
            f"SANet NMAE: {model_nmae:.4f}\nLast-value NMAE: {baseline_nmae:.4f}",
            transform=axis.transAxes,
            va="top",
            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "#cccccc"},
        )
    figure.suptitle("Prediction agreement across all validation forecast points", fontsize=15)
    figure.savefig(output, dpi=180)
    plt.close(figure)


def _nmae(prediction: np.ndarray, target: np.ndarray) -> float:
    return float(np.mean(np.abs(prediction - target))) / (
        float(np.mean(np.abs(target))) + 1e-8
    )


if __name__ == "__main__":
    main()
