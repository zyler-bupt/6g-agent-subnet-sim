"""Utility helpers for dynamic task-weight update.

This is **independent** from LibMTL and provides a light-weight implementation
sufficient for SANet experiments.

The vector of task weights (lambda) is kept on the same device as the model.
It is updated as follows:

    λ ← Π_Δ [ λ − γ ( g¹ · (g²ᵀ λ) + ρ λ ) ]

where g¹, g² are task-gradient matrices with shape (task_num, P), where P is the
number of shared parameters flattened into one dimension.  Π_Δ is the Euclidean
projection onto the probability simplex (non-negative and sums to 1).

This file exposes two public functions:

    projection_simplex(y)     – project any 1-D tensor onto a simplex.
    update_lambda_modo(lambd, grads1, grads2, gamma, rho)
"""
from __future__ import annotations

import torch

def projection_simplex(y: torch.Tensor) -> torch.Tensor:
    """Project *y* onto the probability simplex Δ.

    Implementation follows the algorithm from:
        "Efficient Projections onto the l1-Ball for Learning in High Dimensions"
        – Duchi et al., ICML 2008.
    """
    if y.ndim != 1:
        raise ValueError("Input must be 1-D tensor")
    m = y.numel()
    sorted_y, _ = torch.sort(y, descending=True)
    tmpsum = 0.0
    tmax_f = (y.sum() - 1.0) / m
    for i in range(m - 1):
        tmpsum += sorted_y[i]
        tmax = (tmpsum - 1) / (i + 1.0)
        if tmax > sorted_y[i + 1]:
            tmax_f = tmax
            break
    return torch.clamp(y - tmax_f, min=0.0)

def update_lambda_modo(
    lambd: torch.Tensor,
    grads1: torch.Tensor,
    grads2: torch.Tensor,
    gamma: float = 0.1,
    rho: float = 0.1,
) -> torch.Tensor:
    """Update *lambd* according to MoDo projected gradient step.

    Implementation follows the algorithm from:
    "Three-way trade-off in multi-objective learning: Optimization, generalization and conflict-avoidance,"
    – Chen et al., NeurIPS 2023.

    Args
    ----
    lambd : (task_num,) current weight vector on simplex.
    grads1, grads2: matrices of shape (task_num, P) with task gradients from two
        independent stochastic passes.
    gamma, rho: learning-rate and regularisation hyper-parameters.
    """
    # Compute the matrix-vector product grads2^T @ lambd  -> shape (P,)
    v = torch.matmul(grads2.t(), lambd)
    # Then g1 @ v  -> shape (task_num,)
    step_direction = torch.matmul(grads1, v) + rho * lambd
    new_lambd = lambd - gamma * step_direction
    # Project back to simplex
    return projection_simplex(new_lambd)
