"""Inference entrypoint for SANet multi-task models."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import numpy as np
import torch
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from configs.config import InferenceConfig
from models.model_shared_transformer import MultiTaskTransformerShared
from util.data_loader import get_dataloader


@torch.no_grad()
def run_inference(cfg: InferenceConfig, if_autoregressive: bool = True) -> None:
    """Run model inference and save predictions/targets/inputs as `.npy` files."""
    ckpt_path = Path(cfg.ckpt_dir)
    output_dir = Path(cfg.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    device = torch.device(cfg.device if torch.cuda.is_available() else "cpu")

    dataloader = get_dataloader(
        cfg.data_root,
        batch_size=cfg.batch_size,
        shuffle=False,
        input_len=cfg.input_len,
        pred_len=cfg.pred_len,
        stride=cfg.stride,
    )

    model = MultiTaskTransformerShared(
        input_dim=cfg.input_dim,
        d_model=cfg.d_model,
        nhead=cfg.nhead,
        d_ff=cfg.d_ff,
        num_layers=cfg.num_layers,
        shared_layers=cfg.shared_layers,
        use_bandwidth_adaptive_compression=getattr(cfg, "use_bandwidth_adaptive_compression", True),
        bandwidth_adaptive_latent_dim=getattr(cfg, "bandwidth_adaptive_latent_dim", None),
        bandwidth_B=getattr(cfg, "bandwidth_B", None),
        bandwidth_selection_mode=getattr(cfg, "bandwidth_selection_mode", "none"),
        random_selection_seed=getattr(cfg, "random_selection_seed", 42),
    ).to(device)
    state_dict = torch.load(ckpt_path, map_location=device)
    model.load_state_dict(state_dict["model"])
    model.eval()

    from util.normalizer import StandardScaler

    scaler = StandardScaler.load(Path(cfg.scaler_path) / "scaler.npz")
    idx_map = {"csi": 0, "traffic": 1, "intent": 2}

    preds: Dict[str, list[np.ndarray]] = {"csi": [], "traffic": [], "intent": []}
    targets: Dict[str, list[np.ndarray]] = {"csi": [], "traffic": [], "intent": []}
    inputs: Dict[str, list[np.ndarray]] = {"csi": [], "traffic": [], "intent": []}

    pred_len = cfg.pred_len
    feat_dim = len(idx_map)

    for _ in range(1):
        for (past_seq, future_seq), _targets in tqdm(dataloader, desc="Infer"):
            past_seq = past_seq.to(device)
            future_seq = future_seq.to(device)
            bsz, _, _ = past_seq.shape

            if if_autoregressive:
                # Decoder buffer contains BOS + generated predictions.
                future_seq = torch.zeros((bsz, pred_len + 1, feat_dim), dtype=past_seq.dtype, device=device)
                step_preds = {k: [] for k in idx_map}

                for t in range(pred_len):
                    dec_in = future_seq[:, :-1]
                    outputs = model(past_seq, dec_in)
                    for key, idx in idx_map.items():
                        step = outputs[key][:, t]
                        step_preds[key].append(step.cpu())
                        future_seq[:, t + 1, idx] = step

                for key in idx_map:
                    arr = torch.stack(step_preds[key], dim=1)
                    arr = scaler.inverse_feature(arr, idx_map[key])
                    preds[key].append(arr.numpy())
                    targets[key].append(scaler.inverse_feature(_targets[key], idx_map[key]))
                    inputs[key].append(
                        scaler.inverse_feature(past_seq[:, :, idx_map[key]], idx_map[key]).cpu().numpy()
                    )
            else:
                outputs = model(past_seq, future_seq)
                for key in idx_map:
                    arr = scaler.inverse_feature(outputs[key], idx_map[key])
                    preds[key].append(arr.cpu().numpy())
                    targets[key].append(scaler.inverse_feature(_targets[key], idx_map[key]))
                    inputs[key].append(
                        scaler.inverse_feature(past_seq[:, :, idx_map[key]], idx_map[key]).cpu().numpy()
                    )

    for key, chunks in preds.items():
        arr = np.concatenate(chunks, axis=0)
        np.save(output_dir / f"{key}_pred.npy", arr)
        np.save(output_dir / f"{key}_gt.npy", np.concatenate(targets[key], axis=0))
        np.save(output_dir / f"{key}_input.npy", np.concatenate(inputs[key], axis=0))
        print(f"Saved {key} predictions to {(output_dir / f'{key}_pred.npy').resolve()}")


def main() -> None:
    cfg = InferenceConfig()
    run_inference(cfg, if_autoregressive=True)


if __name__ == "__main__":
    main()
