from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch

from sanet_dual.data import DualAgentScaler, build_windows, load_dual_agent_series
from sanet_dual.model import DualAgentSANet


@torch.no_grad()
def main() -> None:
    parser = argparse.ArgumentParser(description="Run two-agent SANet inference")
    parser.add_argument(
        "--data-root",
        default="third_party/SANet/data/example_band_n1",
    )
    parser.add_argument("--model-dir", default="results/sanet_dual")
    parser.add_argument("--input-len", type=int, default=15)
    parser.add_argument("--pred-len", type=int, default=5)
    args = parser.parse_args()

    model_dir = Path(args.model_dir)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    checkpoint = torch.load(model_dir / "model.pt", map_location=device)
    config = checkpoint.get("config", {})
    model = DualAgentSANet(
        d_model=int(config.get("d_model", 128)),
        nhead=int(config.get("nhead", 8)),
        d_ff=int(config.get("d_ff", 256)),
        num_layers=int(config.get("num_layers", 6)),
    ).to(device)
    model.load_state_dict(checkpoint["model"])
    model.eval()

    scaler = DualAgentScaler.load(model_dir / "scaler.npz")
    raw = load_dual_agent_series(args.data_root)
    past, future = build_windows(
        scaler.transform(raw),
        input_len=args.input_len,
        pred_len=args.pred_len,
    )
    past_tensor = torch.from_numpy(past).to(device)
    generated = torch.zeros(
        (len(past_tensor), args.pred_len + 1, 2),
        dtype=past_tensor.dtype,
        device=device,
    )
    for step in range(args.pred_len):
        outputs = model(past_tensor, generated[:, :-1])
        generated[:, step + 1, 0] = outputs["application"][:, step]
        generated[:, step + 1, 1] = outputs["network"][:, step]

    predictions = scaler.inverse_transform(generated[:, 1:].cpu().numpy())
    targets = scaler.inverse_transform(future)
    np.save(model_dir / "predictions.npy", predictions)
    np.save(model_dir / "targets.npy", targets)
    for index, name in enumerate(("application", "network")):
        denominator = np.mean(np.abs(targets[..., index])) + 1e-8
        nmae = np.mean(np.abs(predictions[..., index] - targets[..., index])) / denominator
        print(f"{name}_nmae={nmae:.6f}")


if __name__ == "__main__":
    main()
