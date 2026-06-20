# SANet Dual-Agent Baseline

This adaptation excludes the physical-layer CSI task and leaves the upstream
snapshot in `third_party/SANet` unchanged.

Data mapping:

- `user_intent.npy` -> aAgent application demand
- `traffic.npy` -> nAgent reachable network bandwidth
- `CSI.npy` -> excluded from this phase

The model keeps a private embedding for each task and reuses one Transformer
encoder-decoder backbone across both tasks. Training uses the mean of the two
normalized MSE losses. Dynamic task weighting and feature compression belong to
the later SANet mechanism-reproduction phase.

Install PyTorch for the target CPU/CUDA environment, then run from the repository
root:

```bash
python3 -m sanet_dual.train --epochs 20
python3 -m sanet_dual.inference
```

CPU smoke test with a smaller backbone:

```bash
python3 -m sanet_dual.train --epochs 1 --d-model 32 --nhead 4 --d-ff 64 --num-layers 1
python3 -m sanet_dual.inference
```
