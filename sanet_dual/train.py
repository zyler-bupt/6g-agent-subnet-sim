from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.optim import AdamW
from torch.utils.data import DataLoader, TensorDataset, random_split

from sanet_dual.data import DualAgentScaler, build_windows, load_dual_agent_series
from sanet_dual.model import DualAgentSANet


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the two-agent SANet baseline")
    parser.add_argument(
        "--data-root",
        default="third_party/SANet/data/example_band_n1",
    )
    parser.add_argument("--output-dir", default="results/sanet_dual")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--input-len", type=int, default=15)
    parser.add_argument("--pred-len", type=int, default=5)
    parser.add_argument("--learning-rate", type=float, default=5e-4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--d-model", type=int, default=128)
    parser.add_argument("--nhead", type=int, default=8)
    parser.add_argument("--d-ff", type=int, default=256)
    parser.add_argument("--num-layers", type=int, default=6)
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = load_dual_agent_series(args.data_root)
    scaler = DualAgentScaler.fit(raw)
    normalized = scaler.transform(raw)
    past, future = build_windows(
        normalized,
        input_len=args.input_len,
        pred_len=args.pred_len,
    )
    dataset = TensorDataset(torch.from_numpy(past), torch.from_numpy(future))
    val_size = max(1, int(len(dataset) * 0.2))
    train_size = len(dataset) - val_size
    train_data, val_data = random_split(
        dataset,
        [train_size, val_size],
        generator=torch.Generator().manual_seed(args.seed),
    )
    train_loader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_data, batch_size=args.batch_size, shuffle=False)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DualAgentSANet(
        d_model=args.d_model,
        nhead=args.nhead,
        d_ff=args.d_ff,
        num_layers=args.num_layers,
    ).to(device)
    optimizer = AdamW(model.parameters(), lr=args.learning_rate)
    criterion = nn.MSELoss()

    for epoch in range(1, args.epochs + 1):
        train_loss = _run_epoch(model, train_loader, criterion, device, optimizer)
        val_loss = _run_epoch(model, val_loader, criterion, device)
        print(
            f"epoch={epoch} train_loss={train_loss:.6f} val_loss={val_loss:.6f}",
            flush=True,
        )

    scaler.save(output_dir / "scaler.npz")
    config = vars(args)
    (output_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    torch.save(
        {"model": model.state_dict(), "config": config},
        output_dir / "model.pt",
    )
    print(f"saved={output_dir / 'model.pt'}")


def _run_epoch(model, dataloader, criterion, device, optimizer=None) -> float:
    training = optimizer is not None
    model.train(training)
    total = 0.0
    batches = 0
    for past, future in dataloader:
        past = past.to(device)
        future = future.to(device)
        bos = torch.zeros((future.size(0), 1, 2), device=device, dtype=future.dtype)
        decoder_input = torch.cat([bos, future[:, :-1]], dim=1)
        if training:
            optimizer.zero_grad()
        with torch.set_grad_enabled(training):
            outputs = model(past, decoder_input)
            app_loss = criterion(outputs["application"], future[..., 0])
            network_loss = criterion(outputs["network"], future[..., 1])
            loss = 0.5 * app_loss + 0.5 * network_loss
            if training:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
        total += loss.item()
        batches += 1
    return total / max(batches, 1)


if __name__ == "__main__":
    main()
