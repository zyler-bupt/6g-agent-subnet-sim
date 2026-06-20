from __future__ import annotations

import math

import torch
import torch.nn as nn


class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = 10000) -> None:
        super().__init__()
        encoding = torch.zeros(max_len, d_model)
        position = torch.arange(max_len, dtype=torch.float32).unsqueeze(1)
        divisor = torch.exp(
            torch.arange(0, d_model, 2, dtype=torch.float32)
            * (-math.log(10000.0) / d_model)
        )
        encoding[:, 0::2] = torch.sin(position * divisor)
        encoding[:, 1::2] = torch.cos(position * divisor)
        self.register_buffer("encoding", encoding.unsqueeze(0))

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return values + self.encoding[:, : values.size(1)]


class PrivateEmbedding(nn.Module):
    def __init__(self, d_model: int, d_ff: int, dropout: float) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(1, d_model),
            nn.Linear(d_model, d_ff),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
        )
        self.position = PositionalEncoding(d_model)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return self.position(self.layers(values))


class SharedForecastBackbone(nn.Module):
    def __init__(
        self,
        d_model: int,
        nhead: int,
        d_ff: int,
        num_layers: int,
        dropout: float,
    ) -> None:
        super().__init__()
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=d_ff,
            dropout=dropout,
            activation="gelu",
            batch_first=True,
        )
        decoder_layer = nn.TransformerDecoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=d_ff,
            dropout=dropout,
            activation="gelu",
            batch_first=True,
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.decoder = nn.TransformerDecoder(decoder_layer, num_layers=num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.output = nn.Linear(d_model, 1)

    def forward(self, past: torch.Tensor, future: torch.Tensor) -> torch.Tensor:
        memory = self.encoder(past)
        mask = torch.triu(
            torch.full(
                (future.size(1), future.size(1)),
                float("-inf"),
                device=future.device,
            ),
            diagonal=1,
        )
        hidden = self.decoder(future, memory, tgt_mask=mask)
        return self.output(self.norm(hidden)).squeeze(-1)


class DualAgentSANet(nn.Module):
    """aAgent/nAgent model with private embeddings and a shared Transformer."""

    def __init__(
        self,
        d_model: int = 128,
        nhead: int = 8,
        d_ff: int = 256,
        num_layers: int = 6,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        self.application_embedding = PrivateEmbedding(d_model, d_ff, dropout)
        self.network_embedding = PrivateEmbedding(d_model, d_ff, dropout)
        self.shared_backbone = SharedForecastBackbone(
            d_model=d_model,
            nhead=nhead,
            d_ff=d_ff,
            num_layers=num_layers,
            dropout=dropout,
        )

    def forward(
        self,
        past: torch.Tensor,
        decoder_input: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        app_past = self.application_embedding(past[..., 0:1])
        app_future = self.application_embedding(decoder_input[..., 0:1])
        network_past = self.network_embedding(past[..., 1:2])
        network_future = self.network_embedding(decoder_input[..., 1:2])
        return {
            "application": self.shared_backbone(app_past, app_future),
            "network": self.shared_backbone(network_past, network_future),
        }
