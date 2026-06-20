from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class DualAgentScaler:
    mean: np.ndarray
    std: np.ndarray

    @classmethod
    def fit(cls, values: np.ndarray) -> "DualAgentScaler":
        mean = values.mean(axis=0)
        std = values.std(axis=0)
        std = np.where(std < 1e-8, 1.0, std)
        return cls(mean=mean, std=std)

    def transform(self, values: np.ndarray) -> np.ndarray:
        return (values - self.mean) / self.std

    def inverse_transform(self, values: np.ndarray) -> np.ndarray:
        return values * self.std + self.mean

    def save(self, path: str | Path) -> None:
        np.savez(path, mean=self.mean, std=self.std)

    @classmethod
    def load(cls, path: str | Path) -> "DualAgentScaler":
        payload = np.load(path)
        return cls(mean=payload["mean"], std=payload["std"])


def load_dual_agent_series(data_root: str | Path) -> np.ndarray:
    """Return aligned [application demand, network bandwidth] observations."""

    root = Path(data_root)
    app_demand = np.load(root / "user_intent.npy").astype(np.float32).reshape(-1)
    network_bandwidth = np.load(root / "traffic.npy").astype(np.float32).reshape(-1)
    if len(app_demand) != len(network_bandwidth):
        raise ValueError("application and network series must have equal length")
    if len(app_demand) == 0:
        raise ValueError("dual-agent series must not be empty")
    return np.stack([app_demand, network_bandwidth], axis=-1)


def build_windows(
    values: np.ndarray,
    *,
    input_len: int = 15,
    pred_len: int = 5,
    stride: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    if values.ndim != 2 or values.shape[1] != 2:
        raise ValueError("values must have shape (time, 2)")
    if input_len <= 0 or pred_len <= 0 or stride <= 0:
        raise ValueError("window lengths and stride must be positive")
    last_start = len(values) - input_len - pred_len
    if last_start < 0:
        raise ValueError("series is shorter than input_len + pred_len")
    starts = range(0, last_start + 1, stride)
    past = np.stack([values[start : start + input_len] for start in starts])
    future = np.stack(
        [values[start + input_len : start + input_len + pred_len] for start in starts]
    )
    return past.astype(np.float32), future.astype(np.float32)
