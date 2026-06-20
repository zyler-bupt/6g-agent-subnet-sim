"""Feature-wise standardization utility used across SANet pipelines."""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
import torch

ArrayLike = Union[np.ndarray, torch.Tensor]


class StandardScaler:
    """Store feature-wise mean/std and apply reversible normalization."""

    def __init__(self, mean: np.ndarray, std: np.ndarray):
        self.mean = mean.astype(np.float32)
        self.std = std.astype(np.float32)

    @staticmethod
    def from_data(arr: np.ndarray) -> "StandardScaler":
        """Build scaler statistics from data shaped as (..., F)."""
        mean = arr.mean(axis=0)
        std = arr.std(axis=0)
        return StandardScaler(mean, std)

    def transform(self, x: ArrayLike) -> ArrayLike:
        """Normalize full feature vectors with broadcasting support."""
        if isinstance(x, np.ndarray):
            return (x - self.mean) / (self.std + 1e-8)
        if torch.is_tensor(x):
            device = x.device
            mean = torch.from_numpy(self.mean).to(device)
            std = torch.from_numpy(self.std).to(device)
            return (x - mean) / (std + 1e-8)
        raise TypeError("Unsupported type for transform")

    def inverse_transform(self, x: ArrayLike) -> ArrayLike:
        """Inverse transform for tensors/arrays shaped as (..., F)."""
        if isinstance(x, np.ndarray):
            return x * (self.std + 1e-8) + self.mean
        if torch.is_tensor(x):
            device = x.device
            mean = torch.from_numpy(self.mean).to(device)
            std = torch.from_numpy(self.std).to(device)
            return x * (std + 1e-8) + mean
        raise TypeError("Unsupported type for inverse_transform")

    def inverse_feature(self, x: ArrayLike, idx: int):
        """Inverse transform a single feature channel by index."""
        if isinstance(x, np.ndarray):
            return x * (self.std[idx] + 1e-8) + self.mean[idx]
        if torch.is_tensor(x):
            return x * (self.std[idx] + 1e-8) + self.mean[idx]
        raise TypeError("Unsupported type")

    def save(self, path: Path):
        """Persist scaler statistics as `.npz`."""
        np.savez(path, mean=self.mean, std=self.std)

    @staticmethod
    def load(path: Path) -> "StandardScaler":
        """Load scaler statistics from `.npz`."""
        data = np.load(path)
        return StandardScaler(data["mean"], data["std"])
