"""Data loading utilities for multi-modal SANet training and inference."""

import os
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset


class MultiModalTimeSeriesDataset(Dataset):
    """Dataset that yields aligned CSI, traffic, and user-intent sequences."""

    def __init__(self, data_root: str, input_len: int = 32, pred_len: int = 32, stride: int = 1):
        """
        Args:
            data_root: Directory containing `CSI.npy`, `traffic.npy`, and optional `user_intent.npy`.
            input_len: Length of the history window.
            pred_len: Length of the future prediction window.
            stride: Sliding-window step size.
        """
        self.input_len = input_len
        self.pred_len = pred_len

        # CSI is expected as complex channels (real, imag) and converted to amplitude.
        self.csi = np.load(os.path.join(data_root, "CSI.npy")).astype(np.float32)  # (T, 2)
        self.csi_amp = np.linalg.norm(self.csi, axis=-1, keepdims=True)  # (T, 1)

        self.traffic = np.load(os.path.join(data_root, "traffic.npy")).astype(np.float32).reshape(-1, 1)  # (T, 1)

        user_intent_path = os.path.join(data_root, "user_intent.npy")
        if os.path.isfile(user_intent_path):
            self.user_intent = np.load(user_intent_path).astype(np.float32).reshape(-1, 1)
        else:
            # Keep interface stable if user intent is unavailable.
            self.user_intent = np.zeros_like(self.traffic)

        assert self.csi_amp.shape[0] == self.traffic.shape[0] == self.user_intent.shape[0], (
            "Mismatched time dimensions across modalities"
        )

        # Normalize all modalities with a shared scaler.
        from util.normalizer import StandardScaler

        scaler_path = os.path.join(data_root, "scaler.npz")
        concat = np.concatenate([self.csi_amp, self.traffic, self.user_intent], axis=1)
        if os.path.isfile(scaler_path):
            scaler = StandardScaler.load(scaler_path)
        else:
            scaler = StandardScaler.from_data(concat)
            scaler.save(Path(scaler_path))

        # Expose normalization stats for downstream tools.
        self.mean = scaler.mean
        self.std = scaler.std

        normalized_data = scaler.transform(concat)
        self.csi_amp = normalized_data[:, 0].reshape(-1, 1)
        self.traffic = normalized_data[:, 1].reshape(-1, 1)
        self.user_intent = normalized_data[:, 2].reshape(-1, 1)

        self.T = self.csi_amp.shape[0]
        # Need one history window and one future window for each training sample.
        self.indices = list(range(0, self.T - (self.input_len + self.pred_len) + 1, stride))

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        s = self.indices[idx]
        mid = s + self.input_len
        end = mid + self.pred_len

        # Build history window.
        amp_in = torch.from_numpy(self.csi_amp[s:mid])  # (input_len, 1)
        traf_in = torch.from_numpy(self.traffic[s:mid])
        int_in = torch.from_numpy(self.user_intent[s:mid])

        # Build prediction targets.
        amp_tgt = torch.from_numpy(self.csi_amp[mid:end]).squeeze(-1)  # (pred_len,)
        traf_tgt = torch.from_numpy(self.traffic[mid:end]).squeeze(-1)
        int_tgt = torch.from_numpy(self.user_intent[mid:end]).squeeze(-1)

        input_seq = torch.cat([amp_in, traf_in, int_in], dim=-1)  # (input_len, 3)
        targets = {
            "csi": amp_tgt,
            "traffic": traf_tgt,
            "intent": int_tgt,
        }
        teacher_forcing_future = torch.cat(
            [amp_tgt.unsqueeze(-1), traf_tgt.unsqueeze(-1), int_tgt.unsqueeze(-1)],
            dim=-1,
        )
        return (input_seq, teacher_forcing_future), targets


def collate_fn(batch):
    """Collate function that keeps both history and future windows."""
    pf_pairs, targets_list = zip(*batch)
    past_seqs, future_seqs = zip(*pf_pairs)
    past_seqs = torch.stack(past_seqs)  # (B, T_in, F)
    future_seqs = torch.stack(future_seqs)  # (B, T_out, F)

    targets = {}
    for key in targets_list[0].keys():
        targets[key] = torch.stack([t[key] for t in targets_list])  # (B, T_out)
    return (past_seqs, future_seqs), targets


def get_dataloader(data_root: str, batch_size: int = 32, shuffle: bool = True, **kwargs):
    """Create a dataloader from either saved dataset (`.pt`) or raw `.npy` files."""
    if data_root.endswith(".pt"):
        dataset = torch.load(data_root, weights_only=False)
    else:
        dataset = MultiModalTimeSeriesDataset(data_root, **kwargs)
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, collate_fn=collate_fn)
