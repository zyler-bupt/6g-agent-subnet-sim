"""Central experiment configuration for SANet core codebase."""

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
exp_name = 'exp_default'


@dataclass
class TrainConfig:
    # Model variant tag (kept for compatibility with legacy experiment scripts).
    model_type: str = 'shared_transformer'

    # Data and training schedule.
    data_root: str = str(PROJECT_ROOT / 'data' / 'train_band_n1')
    lambd: tuple = (1/3, 1/3, 1/3)
    val_ratio: float = 0.2
    dynamic_lambda: bool = True
    batch_size: int = 64
    input_dim: int = 3
    input_len: int = 15
    pred_len: int = 5
    stride: int = 1
    epochs: int = int(6e3)
    # Dynamic lambda update hyperparameters.
    lambda_gamma: float = 0.1
    lambda_rho: float = 0.1

    # Logging and optimizer.
    log_dir: str = str(PROJECT_ROOT / 'logs' / 'exp_log' / exp_name)
    learning_rate: float = 5e-4
    grad_clip: float = 1.0
    device: str = 'cuda'  # or 'cpu'

    # Backbone architecture.
    d_model: int = 128
    nhead: int = 8
    d_ff: int = 256
    num_layers: int = 6
    shared_layers: int = 1

    # Bandwidth-adaptive compression.
    use_bandwidth_adaptive_compression: bool = True
    bandwidth_adaptive_latent_dim: int = 64
    bandwidth_B: float = 32.0
    bandwidth_loss_weight: float = 0.1
    bandwidth_selection_mode: str = "none"
    random_selection_seed: int = 42

    # Runtime behavior.
    log_interval: int = 10
    random_split: bool = False


@dataclass
class InferenceConfig:
    # Paths.
    data_root: str = str(PROJECT_ROOT / 'logs' / 'exp_log' / exp_name / 'val_dataset.pt')
    scaler_path: str = str(PROJECT_ROOT / 'data' / 'train_band_n1')
    ckpt_dir: str = str(PROJECT_ROOT / 'logs' / 'exp_log' / exp_name / 'checkpoints' / 'model.pt')
    output_dir: str = str(PROJECT_ROOT / 'logs' / 'predictions' / exp_name)

    # Data windowing.
    batch_size: int = 64
    input_dim: int = 1
    input_len: int = 15
    pred_len: int = 5
    stride: int = 1
    epochs: int = 10
    # Dynamic lambda update hyperparameters (used by shared utilities).
    lambda_gamma: float = 0.1
    lambda_rho: float = 0.1

    # Optimizer-related fields kept for config compatibility.
    learning_rate: float = 5e-4
    grad_clip: float = 1.0
    device: str = 'cuda'  # or 'cpu'

    # Backbone architecture.
    d_model: int = 128
    nhead: int = 8
    d_ff: int = 256
    num_layers: int = 6
    shared_layers: int = 1

    # Bandwidth-adaptive compression.
    use_bandwidth_adaptive_compression: bool = True
    bandwidth_adaptive_latent_dim: int = 64
    bandwidth_B: float = 32.0
    bandwidth_selection_mode: str = "none"
    random_selection_seed: int = 42

    # Runtime behavior.
    log_interval: int = 10
