# SANet

## Repository Layout

```text
SANet/
├── configs/     # Training and inference defaults
├── data/        # Dataset format docs and local experiment data
├── models/      # Model definitions
├── scripts/     # Train/inference entrypoints
└── util/        # Data loading, normalization, and optimization helpers
```

## Requirements

- Python 3.9+
- CUDA is optional but recommended for training

Install PyTorch first using the command recommended for your platform on the official PyTorch site, then install the remaining dependencies:

```bash
pip install -r requirements.txt
```

## Quick Start

Train from the repository root:

```bash
python scripts/train.py
```

Example:

```bash
python scripts/train.py \
  --epochs 200 \
  --log_dir logs/exp_log/exp_my_run \
  --bandwidth_B 64 \
  --bandwidth_loss_weight 0.5 \
  --use_bandwidth_adaptive_compression 1
```

Run inference with:

```bash
python scripts/inference.py
```

Main defaults are defined in `configs/config.py`.

## Data Format

Each dataset directory should contain:

- `CSI.npy`: shape `(T, 2)` for I/Q channels
- `traffic.npy`: shape `(T,)` or `(T, 1)`
- `user_intent.npy`: shape `(T,)` or `(T, 1)`, optional
- `scaler.npz`: optional, auto-generated if missing

Example layout:

```text
data/
  train_band_n1/
    CSI.npy
    traffic.npy
    user_intent.npy
    scaler.npz
```

More details are in [data/README.md](data/README.md).

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

## Citation

If you use SANet in your research, please cite the following paper:

```bibtex
@article{xiao2026sanet,
  title   = {{SANet}: A Semantic-aware Agentic {AI} Networking Framework for Cross-layer Optimization in {6G}},
  author  = {Xiao, Yong and Li, Xubo and Zhou, Haoran and Li, Yingyu and Gao, Yayu and Shi, Guangming and Zhang, Ping and Krunz, Marwan},
  journal = {IEEE Transactions on Mobile Computing},
  year    = {2026},
  note    = {Early Access}
}
```
