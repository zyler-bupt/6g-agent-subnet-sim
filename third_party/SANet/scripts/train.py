"""Training entrypoint for SANet multi-task learning."""

import argparse
import json
import os
import sys
from dataclasses import asdict

import numpy as np
import torch
import torch.nn as nn
from torch.optim import AdamW

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from tqdm import tqdm

from util.data_loader import MultiModalTimeSeriesDataset, collate_fn
from models.model_shared_transformer import MultiTaskTransformerShared
from configs.config import TrainConfig

from util.lambda_utils import update_lambda_modo
from util.utils import find_min_norm_element

try:
    from torch.utils.tensorboard import SummaryWriter
except ModuleNotFoundError:
    class SummaryWriter:  # type: ignore[override]
        """No-op fallback when tensorboard is unavailable."""

        def __init__(self, log_dir=None, *args, **kwargs):
            self.log_dir = log_dir if log_dir is not None else "./runs"

        def add_scalar(self, *args, **kwargs):
            return None

        def flush(self):
            return None

        def close(self):
            return None


def get_compression_regularization(model, cfg, device):
    """Get weighted bandwidth-adaptive compression regularization terms."""
    zero = torch.tensor(0.0, device=device)
    if not hasattr(model, 'get_aux_losses'):
        return zero, {'bandwidth_loss': zero, 'used_bandwidth': zero, 'mean_importance': zero}

    aux_losses = model.get_aux_losses() or {}
    bw_loss = aux_losses.get('bandwidth_loss', zero)
    weighted = getattr(cfg, 'bandwidth_loss_weight', 0.0) * bw_loss
    return weighted, {
        'bandwidth_loss': bw_loss,
        'used_bandwidth': aux_losses.get('used_bandwidth', zero),
        'mean_importance': aux_losses.get('mean_importance', zero),
    }


def train_one_epoch(model, dataloader, criterion, optimizer, device, epoch, cfg, writer=None):
    model.train()
    pbar = tqdm(enumerate(dataloader), total=len(dataloader))
    # Initialize lambda only once and reuse it across epochs.
    task_keys = ['csi', 'traffic', 'intent']
    if not hasattr(train_one_epoch, 'lambd'):
        train_one_epoch.lambd = torch.tensor([1/3, 1/3, 1/3], device=device)

    # Accumulators for epoch-level summaries.
    epoch_scalar_loss = 0.0
    epoch_task_scalar_loss = 0.0
    epoch_grad_norm = 0.0
    epoch_task_losses = {k: 0.0 for k in task_keys}
    epoch_conflict_loss = 0.0
    epoch_bw_loss = 0.0
    epoch_used_bw = 0.0
    batch_count = 0

    # Parameter ids for shared backbone layers.
    shared_param_ids = {id(p) for n, p in model.named_parameters() if 'shared_transformer' in n}

    def grad_vec(params_filter=None):
        parts = []
        for p in model.parameters():
            if params_filter is not None and id(p) not in params_filter:
                continue
            if p.grad is None:
                parts.append(torch.zeros_like(p.data).view(-1))
            else:
                parts.append(p.grad.detach().view(-1))
        return torch.cat(parts)

    for i, ((past_seq, future_seq), targets) in pbar:
        past_seq = past_seq.to(device)
        future_seq = future_seq.to(device)
        # Teacher-forcing decoder input: prepend BOS and shift right.
        B, T_pred, F = future_seq.shape
        bos = torch.zeros((B, 1, F), device=device, dtype=future_seq.dtype)
        dec_in = torch.cat([bos, future_seq[:, :-1, :]], dim=1)
        targets = {k: v.to(device) for k, v in targets.items()}

        # First backward pass to collect gradients for lambda update.
        global_step = (epoch - 1) * len(dataloader) + i
        optimizer.zero_grad()
        outputs = model(past_seq, dec_in)
        losses = [criterion(outputs[k], targets[k]) for k in task_keys]
        compression_reg, compression_stats = get_compression_regularization(model, cfg, device)
        # Weighted task loss with current lambda.
        task_scalar_loss = torch.dot(train_one_epoch.lambd, torch.stack(losses))
        scalar_loss = task_scalar_loss + compression_reg
        scalar_loss.backward(retain_graph=True)
        # Collect per-task gradients for MoDo-style lambda update.
        def grad_to_vec(params_filter=None) -> torch.Tensor:
            parts = []
            for p in model.parameters():
                if params_filter is not None and id(p) not in params_filter:
                    continue
                if p.grad is None:
                    parts.append(torch.zeros_like(p.data).view(-1))
                else:
                    parts.append(p.grad.detach().view(-1))
            return torch.cat(parts)

        grads_task = []
        grads_shared_task = []
        model.zero_grad()
        for loss in losses:
            optimizer.zero_grad()
            loss.backward(retain_graph=True)
            grads_task.append(grad_to_vec())
            grads_shared_task.append(grad_to_vec(shared_param_ids))
            model.zero_grad()
        grads_task = torch.stack(grads_task)  # (task_num, P)
        grads_shared_task = torch.stack(grads_shared_task)  # (task_num, P_shared)
        # Reuse same minibatch gradients for g1 and g2.
        grads1_tensor = grads_task
        grads2_tensor = grads_task.detach()

        # Save gradient diagnostics periodically.
        if epoch % 10 == 0:
            save_dir = cfg.log_dir + '/gradients'
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, f"gradients_epoch{epoch}.npz")
            
            # Raw gradients of the three tasks on shared parameters.
            original_grads = grads_shared_task.cpu().numpy()
            
            # Approximate post-conflict resolution direction.
            with torch.no_grad():
                # Gradient using current lambda weighting.
                weighted_grad = torch.zeros_like(grads_shared_task[0])
                for i, task_grad in enumerate(grads_shared_task):
                    weighted_grad += train_one_epoch.lambd[i] * task_grad
                
                # Gradient using minimum-norm convex combination.
                sol_vec = find_min_norm_element(grads_shared_task)  # shape: (3,)
                conflict_resolved_grad = (sol_vec.unsqueeze(1) * grads_shared_task).sum(dim=0)  # (P_shared,)
                conflict_resolved_grad = conflict_resolved_grad.cpu().numpy()
            
            np.savez(
                save_path,
                grads_shared=original_grads,
                grads_conflict_resolved=conflict_resolved_grad,
                grads_weighted=weighted_grad.cpu().numpy(),
                losses=[loss.item() for loss in losses],
                lambda_weights=train_one_epoch.lambd.cpu().numpy(),
            )
        
        # Update lambda if dynamic re-weighting is enabled.
        if cfg.dynamic_lambda:
            train_one_epoch.lambd = update_lambda_modo(
                train_one_epoch.lambd,
                grads1_tensor,
                grads2_tensor,
                gamma=cfg.lambda_gamma,
                rho=cfg.lambda_rho,
            ).detach()

        # Recompute loss with updated lambda and apply optimizer step.
        optimizer.zero_grad()
        outputs = model(past_seq, dec_in)  
        losses = [criterion(outputs[k], targets[k]) for k in task_keys]
        compression_reg, compression_stats = get_compression_regularization(model, cfg, device)
        task_scalar_loss = torch.dot(train_one_epoch.lambd, torch.stack(losses))
        scalar_loss = task_scalar_loss + compression_reg

        # Conflict loss (for monitoring only).
        with torch.no_grad():
            sol_vec = find_min_norm_element(grads_shared_task)
            conflict_loss_val = torch.dot((train_one_epoch.lambd - sol_vec.to(device)), torch.stack(losses)).item()
        epoch_conflict_loss += conflict_loss_val
        if writer is not None:
            writer.add_scalar("Train/conflict_loss_batch", conflict_loss_val, global_step)

        scalar_loss.backward()

        # Gradient norm before clipping.
        grad_norm = torch.norm(torch.stack([
            p.grad.detach().norm(2) for p in model.parameters() if p.grad is not None
        ]), 2)

        # Gradient clipping.
        torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.grad_clip)
        optimizer.step()

        # Batch-to-epoch aggregation.
        epoch_scalar_loss += scalar_loss.item()
        epoch_task_scalar_loss += task_scalar_loss.item()
        epoch_grad_norm += grad_norm.item()
        epoch_bw_loss += compression_stats['bandwidth_loss'].item()
        epoch_used_bw += compression_stats['used_bandwidth'].item()
        for idx, key in enumerate(task_keys):
            epoch_task_losses[key] += losses[idx].item()
        batch_count += 1

        if writer is not None:
            writer.add_scalar(
                "BandwidthAdaptive/bandwidth_loss_batch",
                compression_stats['bandwidth_loss'].item(),
                global_step,
            )
            writer.add_scalar(
                "BandwidthAdaptive/used_bandwidth_batch",
                compression_stats['used_bandwidth'].item(),
                global_step,
            )
            writer.add_scalar(
                "BandwidthAdaptive/mean_importance_batch",
                compression_stats['mean_importance'].item(),
                global_step,
            )

        if i % cfg.log_interval == 0:
            pbar.set_description(
                f"Epoch {epoch} TaskLoss {task_scalar_loss.item():.4f} Total {scalar_loss.item():.4f}"
            )

    # Epoch-level TensorBoard logging.
    if writer is not None and batch_count > 0:
        avg_scalar_loss = epoch_scalar_loss / batch_count
        avg_task_scalar_loss = epoch_task_scalar_loss / batch_count
        avg_grad_norm = epoch_grad_norm / batch_count
        avg_conflict_loss = epoch_conflict_loss / batch_count if batch_count else 0.0
        avg_bw_loss = epoch_bw_loss / batch_count if batch_count else 0.0
        avg_used_bw = epoch_used_bw / batch_count if batch_count else 0.0
        writer.add_scalar("Train/epoch_loss", avg_scalar_loss, epoch)
        writer.add_scalar("Train/epoch_task_loss", avg_task_scalar_loss, epoch)
        writer.add_scalar("Train/epoch_grad_norm", avg_grad_norm, epoch)
        writer.add_scalar("Train/conflict_loss", avg_conflict_loss, epoch)
        writer.add_scalar("BandwidthAdaptive/bandwidth_loss_epoch", avg_bw_loss, epoch)
        writer.add_scalar("BandwidthAdaptive/used_bandwidth_epoch", avg_used_bw, epoch)

        for key, total_loss in epoch_task_losses.items():
            writer.add_scalar(f"Train/{key}_epoch_loss", total_loss / batch_count, epoch)

        for idx, key in enumerate(task_keys):
            writer.add_scalar(f"Lambda/{key}", train_one_epoch.lambd[idx].item(), epoch)

def evaluate(model, dataloader, criterion, device):
    model.eval()
    total_loss = 0.0
    task_keys = ['csi', 'traffic', 'intent']
    with torch.no_grad():
        for (past_seq, future_seq), targets in dataloader:
            past_seq = past_seq.to(device)
            future_seq = future_seq.to(device)
            # Teacher-forcing decoder input: prepend BOS and shift right.
            B_eval, T_pred_eval, F_eval = future_seq.shape
            bos_eval = torch.zeros((B_eval, 1, F_eval), device=device, dtype=future_seq.dtype)
            dec_in = torch.cat([bos_eval, future_seq[:, :-1, :]], dim=1)
            targets = {k: v.to(device) for k, v in targets.items()}
            outputs = model(past_seq, dec_in)
            losses = [criterion(outputs[k], targets[k]) for k in task_keys]
            scalar_loss = torch.dot(train_one_epoch.lambd, torch.stack(losses))
            total_loss += scalar_loss.item()
    return total_loss / len(dataloader)

def main():
    parser = argparse.ArgumentParser(description="Train SANet with optional checkpoint resume")
    parser.add_argument("--resume", type=str, default=None, help="Path to checkpoint to resume from")
    parser.add_argument("--epochs", type=int, default=None, help="Override config epochs")
    parser.add_argument("--log_dir", type=str, default=None, help="Override config log_dir")
    parser.add_argument("--learning_rate", type=float, default=None, help="Override config learning_rate")
    parser.add_argument("--bandwidth_B", type=float, default=None, help="Override bandwidth budget B")
    parser.add_argument(
        "--bandwidth_loss_weight",
        type=float,
        default=None,
        help="Override bandwidth overflow loss weight",
    )
    parser.add_argument(
        "--bandwidth_adaptive_latent_dim",
        type=int,
        default=None,
        help="Override latent dimension for bandwidth-adaptive compression",
    )
    parser.add_argument(
        "--bandwidth_selection_mode",
        type=str,
        choices=["none", "random", "magnitude"],
        default=None,
        help="Dim selection mode when adaptive compression is disabled",
    )
    parser.add_argument(
        "--random_selection_seed",
        type=int,
        default=None,
        help="Seed used by random dim selection baseline",
    )
    parser.add_argument(
        "--use_bandwidth_adaptive_compression",
        type=int,
        choices=[0, 1],
        default=None,
        help="Override bandwidth-adaptive compression switch (1:on, 0:off)",
    )
    args = parser.parse_args()
    cfg = TrainConfig()

    if args.epochs is not None:
        cfg.epochs = args.epochs
    if args.log_dir is not None:
        cfg.log_dir = args.log_dir
    if args.learning_rate is not None:
        cfg.learning_rate = args.learning_rate
    if args.bandwidth_B is not None:
        cfg.bandwidth_B = args.bandwidth_B
    if args.bandwidth_loss_weight is not None:
        cfg.bandwidth_loss_weight = args.bandwidth_loss_weight
    if args.bandwidth_adaptive_latent_dim is not None:
        cfg.bandwidth_adaptive_latent_dim = args.bandwidth_adaptive_latent_dim
    if args.bandwidth_selection_mode is not None:
        cfg.bandwidth_selection_mode = args.bandwidth_selection_mode
    if args.random_selection_seed is not None:
        cfg.random_selection_seed = args.random_selection_seed
    if args.use_bandwidth_adaptive_compression is not None:
        cfg.use_bandwidth_adaptive_compression = bool(args.use_bandwidth_adaptive_compression)

    device = torch.device(cfg.device if torch.cuda.is_available() else 'cpu')

    # Build dataset and random train/validation split.
    full_dataset = MultiModalTimeSeriesDataset(cfg.data_root, input_len=cfg.input_len, pred_len=cfg.pred_len, stride=cfg.stride)
    val_size = int(len(full_dataset) * cfg.val_ratio)
    train_size = len(full_dataset) - val_size
    train_ds, val_ds = torch.utils.data.random_split(full_dataset, [train_size, val_size], generator=torch.Generator().manual_seed(42))
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=cfg.batch_size, shuffle=True, collate_fn=collate_fn)
    val_loader = torch.utils.data.DataLoader(val_ds, batch_size=cfg.batch_size, shuffle=False, collate_fn=collate_fn)
    
    # Save validation subset for reproducible offline inference.
    if not os.path.exists(cfg.log_dir):
        os.makedirs(cfg.log_dir, exist_ok=True)
    torch.save(val_ds, os.path.join(cfg.log_dir, 'val_dataset.pt'))

    model = MultiTaskTransformerShared(
        input_dim=cfg.input_dim,
        d_model=cfg.d_model,
        nhead=cfg.nhead,
        d_ff=cfg.d_ff,
        num_layers=cfg.num_layers,
        shared_layers=cfg.shared_layers,
        use_bandwidth_adaptive_compression=getattr(cfg, 'use_bandwidth_adaptive_compression', True),
        bandwidth_adaptive_latent_dim=getattr(cfg, 'bandwidth_adaptive_latent_dim', None),
        bandwidth_B=getattr(cfg, 'bandwidth_B', None),
        bandwidth_selection_mode=getattr(cfg, 'bandwidth_selection_mode', "none"),
        random_selection_seed=getattr(cfg, 'random_selection_seed', 42),
    ).to(device)
    criterion = nn.MSELoss()
    optimizer = AdamW(model.parameters(), lr=cfg.learning_rate)

    start_epoch = 1
    if args.resume is not None and os.path.exists(args.resume):
        ckpt = torch.load(args.resume, map_location=device)
        if isinstance(ckpt, dict) and 'model' in ckpt:
            model.load_state_dict(ckpt['model'])
            if 'optimizer' in ckpt:
                optimizer.load_state_dict(ckpt['optimizer'])
            start_epoch = ckpt.get('epoch', start_epoch)
            print(f"Resumed training from epoch {start_epoch}.")
        else:
            model.load_state_dict(ckpt)
            print("Loaded model weights from checkpoint (optimizer state not found).")

    writer = SummaryWriter(log_dir=cfg.log_dir if hasattr(cfg, 'log_dir') else './runs')

    # Persist resolved training configuration.
    config_path = os.path.join(writer.log_dir, 'train_config.json')
    with open(config_path, 'w') as f:
        json.dump(asdict(cfg), f, indent=2)

    for epoch in range(start_epoch, cfg.epochs + 1):
        train_one_epoch(model, train_loader, criterion, optimizer, device, epoch, cfg, writer)
        val_loss = evaluate(model, val_loader, criterion, device)
        writer.add_scalar("Val/total_loss", val_loss, epoch)
        print(f"Epoch {epoch} Validation Loss: {val_loss:.4f}")
        writer.flush()

        # Save periodic checkpoints.
        if epoch % 20 == 0:
            ckpt_dir = os.path.join(cfg.log_dir, 'checkpoints')
            if not os.path.exists(ckpt_dir):
                os.makedirs(ckpt_dir)
            ckpt_path = os.path.join(ckpt_dir, f"model_epoch{epoch}.pt")
            torch.save({
                'model': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch + 1,
            }, ckpt_path)
            link_path = os.path.join(ckpt_dir, 'model.pt')
            if os.path.exists(link_path):
                os.unlink(link_path)
            os.symlink(ckpt_path, link_path)

            print(f"Saved checkpoint: {ckpt_path}")

    # Save final checkpoint.
    ckpt_dir = os.path.join(cfg.log_dir, "checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)
    final_ckpt_path = os.path.join(ckpt_dir, "model_final.pt")
    torch.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "epoch": cfg.epochs + 1,
        },
        final_ckpt_path,
    )
    link_path = os.path.join(ckpt_dir, "model.pt")
    if os.path.exists(link_path):
        os.unlink(link_path)
    os.symlink(final_ckpt_path, link_path)

    print(f"Training complete. Final model saved to {final_ckpt_path}")
    writer.close()


if __name__ == '__main__':
    main()
