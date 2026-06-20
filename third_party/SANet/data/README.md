# Data Directory

This project expects each dataset split directory to contain:

- `CSI.npy`
- `traffic.npy`
- `user_intent.npy`
- `scaler.npz` (optional; auto-generated if missing)

Example layout:

```text
data/
  train_band_n1/
    CSI.npy
    traffic.npy
    user_intent.npy
    scaler.npz
```

Notes:

- `CSI.npy` should be shaped like `(T, 2)` for I/Q channels.
- `traffic.npy` and `user_intent.npy` should be time-aligned with CSI.
- If `user_intent.npy` is missing, loader will fallback to zeros.
