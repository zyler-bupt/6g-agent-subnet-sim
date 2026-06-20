"""Shared-transformer model with bandwidth-adaptive compression."""

import math
from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn

from util.utils import PositionalEncoding


class SwishFFN(nn.Module):
    def __init__(self, d_model: int, d_ff: int, dropout: float = 0.1, num_layers: int = 1):
        super().__init__()
        layers = []
        layers.extend([
            nn.Linear(d_model, d_ff),
            nn.SiLU(),
            nn.Dropout(dropout),
        ])
        for _ in range(num_layers - 1):
            layers.extend([
                nn.Linear(d_ff, d_ff),
                nn.SiLU(),
                nn.Dropout(dropout),
            ])
        layers.extend([
            nn.Linear(d_ff, d_model),
            nn.Dropout(dropout),
        ])
        self.seq = nn.Sequential(*layers)

    def forward(self, x):
        return self.seq(x)


def build_transformer_block(d_model: int, nhead: int, d_ff: int, dropout: float = 0.1):
    return nn.TransformerEncoderLayer(
        d_model=d_model,
        nhead=nhead,
        dim_feedforward=d_ff,
        dropout=dropout,
        activation="gelu",
        batch_first=True,
    )


class SharedEmbedding(nn.Module):
    def __init__(
        self,
        input_dim: int,
        d_model: int,
        max_len: int = 10000,
        d_ff: int = None,
        dropout: float = 0.1,
        shared_layers: int = 1,
    ):
        super().__init__()
        self.linear = nn.Linear(input_dim, d_model)
        self.ffn = SwishFFN(d_model, d_ff or d_model * 4, dropout, shared_layers)
        self.pos_enc = PositionalEncoding(d_model, max_len=max_len)

    def forward(self, x):  # x: (B, T, input_dim)
        h = self.linear(x)
        h = self.ffn(h)
        h = self.pos_enc(h)
        return h


class BandwidthAdaptiveCompressionModule(nn.Module):
    """Bandwidth-adaptive compression with learned importance + top-k gating."""

    def __init__(
        self,
        d_model: int,
        latent_dim: int,
        bandwidth_B: float,
        dropout: float = 0.1,
        use_hard_topk: bool = True,
        straight_through_topk: bool = True,
    ) -> None:
        super().__init__()
        self.bandwidth_B = float(bandwidth_B)
        self.latent_dim = int(latent_dim)
        self.use_hard_topk = bool(use_hard_topk)
        self.straight_through_topk = bool(straight_through_topk)

        self.compressor = nn.Sequential(
            nn.Conv1d(d_model, latent_dim, kernel_size=1, bias=False),
            nn.BatchNorm1d(latent_dim),
            nn.GELU(),
            nn.Dropout(dropout),
        )

        self.importance_filter = nn.Sequential(
            nn.Conv1d(d_model, latent_dim, kernel_size=1),
            nn.BatchNorm1d(latent_dim),
            nn.Sigmoid(),
        )
        self._init_importance_filter()

        self.decompressor = nn.Sequential(
            nn.Conv1d(latent_dim, d_model, kernel_size=1, bias=False),
            nn.BatchNorm1d(d_model),
            nn.GELU(),
        )

    def encode(self, embedding: torch.Tensor) -> torch.Tensor:
        x = embedding.transpose(1, 2)  # (B, D, T)
        compressed = self.compressor(x)  # (B, latent_dim, T)
        return compressed.transpose(1, 2)  # (B, T, latent_dim)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        return self.decompressor(latent.transpose(1, 2)).transpose(1, 2)  # (B, T, D)

    @staticmethod
    def _hard_topk_mask(scores: torch.Tensor, keep_k: int) -> torch.Tensor:
        if keep_k <= 0:
            return torch.zeros_like(scores)
        if keep_k >= scores.size(-1):
            return torch.ones_like(scores)
        topk_idx = torch.topk(scores, k=keep_k, dim=-1, largest=True, sorted=False).indices
        mask = torch.zeros_like(scores)
        mask.scatter_(-1, topk_idx, 1.0)
        return mask

    def _init_importance_filter(self) -> None:
        bn = self.importance_filter[1]
        if not isinstance(bn, nn.BatchNorm1d):
            return

        p_init = self.bandwidth_B / max(float(self.latent_dim), 1.0)
        p_init = min(max(p_init, 0.01), 0.99)
        logit_p = math.log(p_init / (1.0 - p_init))
        with torch.no_grad():
            bn.weight.fill_(0.1)
            bn.bias.fill_(logit_p)

    def forward(self, embedding: torch.Tensor) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        x = embedding.transpose(1, 2)  # (B, D, T)
        latent = self.compressor(x).transpose(1, 2)  # (B, T, latent_dim)

        importance_soft = self.importance_filter(x).transpose(1, 2)  # (B, T, latent_dim)
        if self.use_hard_topk:
            keep_k = int(round(self.bandwidth_B))
            keep_k = max(0, min(keep_k, self.latent_dim))
            hard_mask = self._hard_topk_mask(importance_soft, keep_k)
            if self.training and self.straight_through_topk:
                importance = hard_mask + importance_soft - importance_soft.detach()
            else:
                importance = hard_mask
            used_bandwidth = hard_mask.sum(dim=-1).mean()
        else:
            importance = importance_soft
            used_bandwidth = importance.sum(dim=-1).mean()

        latent_weighted = latent * importance
        reconstructed = self.decode(latent_weighted)

        # Soft overflow penalty for bandwidth budget.
        over_bandwidth = torch.relu(used_bandwidth - self.bandwidth_B)
        scaled_over = over_bandwidth / max(self.bandwidth_B, 1.0)
        scaled_over = torch.clamp(scaled_over, max=10.0)
        bandwidth_loss = torch.expm1(scaled_over)

        aux_losses = {
            "bandwidth_loss": bandwidth_loss,
            "used_bandwidth": used_bandwidth,
            "mean_importance": importance_soft.mean(),
        }
        return reconstructed, aux_losses


class LatentCompressionModule(nn.Module):
    """Deterministic latent bottleneck used by random/magnitude baselines."""

    def __init__(self, d_model: int, latent_dim: int, dropout: float = 0.1) -> None:
        super().__init__()
        self.latent_dim = int(latent_dim)
        self.compressor = nn.Sequential(
            nn.Conv1d(d_model, latent_dim, kernel_size=1, bias=False),
            nn.BatchNorm1d(latent_dim),
            nn.GELU(),
            nn.Dropout(dropout),
        )
        self.decompressor = nn.Sequential(
            nn.Conv1d(latent_dim, d_model, kernel_size=1, bias=False),
            nn.BatchNorm1d(d_model),
            nn.GELU(),
        )

    def encode(self, embedding: torch.Tensor) -> torch.Tensor:
        x = embedding.transpose(1, 2)
        compressed = self.compressor(x)
        return compressed.transpose(1, 2)

    def decode(self, latent: torch.Tensor) -> torch.Tensor:
        return self.decompressor(latent.transpose(1, 2)).transpose(1, 2)


def _generate_subsequent_mask(sz: int, device):
    """Square subsequent mask for autoregressive attention."""
    return torch.triu(torch.full((sz, sz), float("-inf"), device=device), diagonal=1)


class SharedTransformerModel(nn.Module):
    """Single Transformer model shared across all modalities."""

    def __init__(self, d_model: int, nhead: int, num_layers: int, d_ff: int, dropout: float = 0.1):
        super().__init__()
        encoder_layer = nn.TransformerEncoderLayer(
            d_model, nhead, d_ff, dropout, batch_first=True, activation="gelu"
        )
        decoder_layer = nn.TransformerDecoderLayer(
            d_model, nhead, d_ff, dropout, batch_first=True, activation="gelu"
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers)
        self.decoder = nn.TransformerDecoder(decoder_layer, num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.proj = nn.Linear(d_model, 1)

    def forward(self, enc_emb: torch.Tensor, dec_emb: torch.Tensor) -> torch.Tensor:
        memory = self.encoder(enc_emb)
        tgt_mask = _generate_subsequent_mask(dec_emb.size(1), dec_emb.device)
        h = self.decoder(dec_emb, memory, tgt_mask=tgt_mask)
        h = self.norm(h)
        return self.proj(h).squeeze(-1)  # (B, T)


class MultiTaskTransformerShared(nn.Module):
    def __init__(
        self,
        input_dim: int = 3,
        d_model: int = 128,
        nhead: int = 4,
        d_ff: int = 256,
        num_layers: int = 3,
        shared_layers: int = 1,
        dropout: float = 0.1,
        use_bandwidth_adaptive_compression: bool = True,
        bandwidth_adaptive_latent_dim: Optional[int] = None,
        bandwidth_B: Optional[float] = None,
        use_hard_topk: bool = True,
        bandwidth_selection_mode: str = "none",
        random_selection_seed: int = 42,
    ):
        super().__init__()
        self.csi_embed = SharedEmbedding(1, d_model, shared_layers=shared_layers)
        self.traffic_embed = SharedEmbedding(1, d_model, shared_layers=shared_layers)
        self.intent_embed = SharedEmbedding(1, d_model, shared_layers=shared_layers)

        self.use_bandwidth_adaptive_compression = bool(use_bandwidth_adaptive_compression)
        if bandwidth_adaptive_latent_dim is None:
            bandwidth_adaptive_latent_dim = max(1, d_model // 2)
        if bandwidth_B is None:
            bandwidth_B = float(bandwidth_adaptive_latent_dim) / 2.0

        self.bandwidth_B = float(bandwidth_B)
        self.bandwidth_adaptive_latent_dim = int(bandwidth_adaptive_latent_dim)
        self.use_hard_topk = bool(use_hard_topk)
        self.bandwidth_selection_mode = str(bandwidth_selection_mode).lower()
        self.random_selection_seed = int(random_selection_seed)

        self._valid_selection_modes = {"none", "random", "magnitude"}
        if self.bandwidth_selection_mode not in self._valid_selection_modes:
            raise ValueError(
                f"Invalid bandwidth_selection_mode={self.bandwidth_selection_mode}. "
                f"Expected one of {sorted(self._valid_selection_modes)}."
            )

        if self.use_bandwidth_adaptive_compression:
            self.csi_bandwidth_compressor = BandwidthAdaptiveCompressionModule(
                d_model,
                self.bandwidth_adaptive_latent_dim,
                self.bandwidth_B,
                dropout,
                use_hard_topk=self.use_hard_topk,
            )
            self.traffic_bandwidth_compressor = BandwidthAdaptiveCompressionModule(
                d_model,
                self.bandwidth_adaptive_latent_dim,
                self.bandwidth_B,
                dropout,
                use_hard_topk=self.use_hard_topk,
            )
            self.intent_bandwidth_compressor = BandwidthAdaptiveCompressionModule(
                d_model,
                self.bandwidth_adaptive_latent_dim,
                self.bandwidth_B,
                dropout,
                use_hard_topk=self.use_hard_topk,
            )
        elif self.bandwidth_selection_mode != "none":
            self.csi_latent = LatentCompressionModule(d_model, self.bandwidth_adaptive_latent_dim, dropout)
            self.traffic_latent = LatentCompressionModule(d_model, self.bandwidth_adaptive_latent_dim, dropout)
            self.intent_latent = LatentCompressionModule(d_model, self.bandwidth_adaptive_latent_dim, dropout)

        self.shared_transformer = SharedTransformerModel(d_model, nhead, num_layers, d_ff, dropout)
        self._aux_losses: Dict[str, torch.Tensor] = {}

    def get_aux_losses(self) -> Dict[str, torch.Tensor]:
        return self._aux_losses

    def _select_dims_without_adaptive_compression(
        self, embedding: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if self.bandwidth_selection_mode == "none":
            full_bw = embedding.new_tensor(float(embedding.size(-1)))
            return embedding, full_bw

        bsz, seq_len, dim = embedding.shape
        keep_k = int(round(self.bandwidth_B))
        keep_k = max(0, min(keep_k, dim))
        if keep_k >= dim:
            full_bw = embedding.new_tensor(float(dim))
            return embedding, full_bw
        if keep_k == 0:
            return torch.zeros_like(embedding), embedding.new_tensor(0.0)

        if self.bandwidth_selection_mode == "magnitude":
            scores = embedding.abs()
        else:
            if self.training:
                scores = torch.rand_like(embedding)
            else:
                b_idx = torch.arange(bsz, device=embedding.device, dtype=embedding.dtype).view(bsz, 1, 1)
                t_idx = torch.arange(seq_len, device=embedding.device, dtype=embedding.dtype).view(1, seq_len, 1)
                d_idx = torch.arange(dim, device=embedding.device, dtype=embedding.dtype).view(1, 1, dim)
                raw = torch.sin(
                    (b_idx + 1.0) * 17.0
                    + (t_idx + 1.0) * 23.0
                    + (d_idx + 1.0) * 31.0
                    + float(self.random_selection_seed)
                ) * 43758.5453
                scores = torch.frac(raw).abs()

        topk_idx = torch.topk(scores, k=keep_k, dim=-1, largest=True, sorted=False).indices
        mask = torch.zeros_like(embedding)
        mask.scatter_(-1, topk_idx, 1.0)
        masked = embedding * mask
        used_bw = embedding.new_tensor(float(keep_k))
        return masked, used_bw

    def forward(self, past_seq: torch.Tensor, future_seq: torch.Tensor) -> Dict[str, torch.Tensor]:
        amp_past = past_seq[..., 0:1]
        traf_past = past_seq[..., 1:2]
        intent_past = past_seq[..., 2:3]
        amp_future = future_seq[..., 0:1]
        traf_future = future_seq[..., 1:2]
        intent_future = future_seq[..., 2:3]

        csi_enc_emb = self.csi_embed(amp_past)
        csi_dec_emb = self.csi_embed(amp_future)
        intent_enc_emb = self.intent_embed(intent_past)
        intent_dec_emb = self.intent_embed(intent_future)
        traf_enc_emb = self.traffic_embed(traf_past)
        traf_dec_emb = self.traffic_embed(traf_future)

        aux_terms = []
        if self.use_bandwidth_adaptive_compression:
            csi_enc_emb, aux = self.csi_bandwidth_compressor(csi_enc_emb)
            aux_terms.append(aux)
            csi_dec_emb, aux = self.csi_bandwidth_compressor(csi_dec_emb)
            aux_terms.append(aux)

            intent_enc_emb, aux = self.intent_bandwidth_compressor(intent_enc_emb)
            aux_terms.append(aux)
            intent_dec_emb, aux = self.intent_bandwidth_compressor(intent_dec_emb)
            aux_terms.append(aux)

            traf_enc_emb, aux = self.traffic_bandwidth_compressor(traf_enc_emb)
            aux_terms.append(aux)
            traf_dec_emb, aux = self.traffic_bandwidth_compressor(traf_dec_emb)
            aux_terms.append(aux)
        elif self.bandwidth_selection_mode != "none":
            csi_enc_latent = self.csi_latent.encode(csi_enc_emb)
            csi_dec_latent = self.csi_latent.encode(csi_dec_emb)
            intent_enc_latent = self.intent_latent.encode(intent_enc_emb)
            intent_dec_latent = self.intent_latent.encode(intent_dec_emb)
            traf_enc_latent = self.traffic_latent.encode(traf_enc_emb)
            traf_dec_latent = self.traffic_latent.encode(traf_dec_emb)
            latent_dim = float(csi_enc_latent.size(-1))

            csi_enc_latent, csi_enc_bw = self._select_dims_without_adaptive_compression(csi_enc_latent)
            csi_dec_latent, csi_dec_bw = self._select_dims_without_adaptive_compression(csi_dec_latent)
            intent_enc_latent, intent_enc_bw = self._select_dims_without_adaptive_compression(intent_enc_latent)
            intent_dec_latent, intent_dec_bw = self._select_dims_without_adaptive_compression(intent_dec_latent)
            traf_enc_latent, traf_enc_bw = self._select_dims_without_adaptive_compression(traf_enc_latent)
            traf_dec_latent, traf_dec_bw = self._select_dims_without_adaptive_compression(traf_dec_latent)

            csi_enc_emb = self.csi_latent.decode(csi_enc_latent)
            csi_dec_emb = self.csi_latent.decode(csi_dec_latent)
            intent_enc_emb = self.intent_latent.decode(intent_enc_latent)
            intent_dec_emb = self.intent_latent.decode(intent_dec_latent)
            traf_enc_emb = self.traffic_latent.decode(traf_enc_latent)
            traf_dec_emb = self.traffic_latent.decode(traf_dec_latent)

            avg_used_bw = torch.stack(
                [csi_enc_bw, csi_dec_bw, intent_enc_bw, intent_dec_bw, traf_enc_bw, traf_dec_bw]
            ).mean()
            mean_importance = avg_used_bw / latent_dim
            aux_terms.append(
                {
                    "bandwidth_loss": past_seq.new_tensor(0.0),
                    "used_bandwidth": avg_used_bw,
                    "mean_importance": mean_importance,
                }
            )

        if aux_terms:
            self._aux_losses = {
                "bandwidth_loss": torch.stack([x["bandwidth_loss"] for x in aux_terms]).mean(),
                "used_bandwidth": torch.stack([x["used_bandwidth"] for x in aux_terms]).mean(),
                "mean_importance": torch.stack([x["mean_importance"] for x in aux_terms]).mean(),
            }
        else:
            zero = past_seq.new_tensor(0.0)
            self._aux_losses = {
                "bandwidth_loss": zero,
                "used_bandwidth": zero,
                "mean_importance": zero,
            }

        preds = {
            "csi": self.shared_transformer(csi_enc_emb, csi_dec_emb),
            "intent": self.shared_transformer(intent_enc_emb, intent_dec_emb),
            "traffic": self.shared_transformer(traf_enc_emb, traf_dec_emb),
        }
        return preds
