"""Core mathematical helpers used by SANet models and training."""

import math

import torch
import torch.nn as nn


class PositionalEncoding(nn.Module):
    """Sinusoidal positional encoding for sequence models."""

    def __init__(self, d_model: int, max_len: int = 10000):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # (1, max_len, d_model)
        self.register_buffer("pe", pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Add positional encoding to tensor shaped as (batch, seq_len, d_model)."""
        seq_len = x.size(1)
        x = x + self.pe[:, :seq_len]
        return x


def compute_amplitude(csi: torch.Tensor) -> torch.Tensor:
    """Compute amplitude from complex CSI where last dim is (real, imag)."""
    return torch.linalg.norm(csi, dim=-1)


def projection2simplex(y):
    """Project a vector onto the probability simplex."""
    m = len(y)
    sorted_y = torch.sort(y, descending=True)[0]
    tmpsum = 0.0
    tmax_f = (torch.sum(y) - 1.0) / m
    for i in range(m - 1):
        tmpsum += sorted_y[i]
        tmax = (tmpsum - 1) / (i + 1.0)
        if tmax > sorted_y[i + 1]:
            tmax_f = tmax
            break
    return torch.max(y - tmax_f, torch.zeros(m).to(y.device))


def find_min_norm_element(grads):
    """Find a convex combination of gradients with minimum norm."""

    def _min_norm_element_from2(v1v1, v1v2, v2v2):
        if v1v2 >= v1v1:
            gamma = 0.999
            cost = v1v1
            return gamma, cost
        if v1v2 >= v2v2:
            gamma = 0.001
            cost = v2v2
            return gamma, cost
        gamma = -1.0 * ((v1v2 - v2v2) / (v1v1 + v2v2 - 2 * v1v2))
        cost = v2v2 + gamma * (v1v2 - v2v2)
        return gamma, cost

    def _min_norm_2d(grad_mat):
        dmin = 1e8
        for i in range(grad_mat.size()[0]):
            for j in range(i + 1, grad_mat.size()[0]):
                c, d = _min_norm_element_from2(grad_mat[i, i], grad_mat[i, j], grad_mat[j, j])
                if d < dmin:
                    dmin = d
                    sol = [(i, j), c, d]
        return sol

    def _next_point(cur_val, grad, n):
        proj_grad = grad - (torch.sum(grad) / n)
        tm1 = -1.0 * cur_val[proj_grad < 0] / proj_grad[proj_grad < 0]
        tm2 = (1.0 - cur_val[proj_grad > 0]) / (proj_grad[proj_grad > 0])

        _ = torch.sum(tm1 < 1e-7) + torch.sum(tm2 < 1e-7)
        t = torch.ones(1).to(grad.device)
        if (tm1 > 1e-7).sum() > 0:
            t = torch.min(tm1[tm1 > 1e-7])
        if (tm2 > 1e-7).sum() > 0:
            t = torch.min(t, torch.min(tm2[tm2 > 1e-7]))

        next_point = proj_grad * t + cur_val
        next_point = projection2simplex(next_point)
        return next_point

    MAX_ITER = 250
    STOP_CRIT = 1e-5

    grad_mat = grads.mm(grads.t())
    init_sol = _min_norm_2d(grad_mat)

    n = grads.size()[0]
    sol_vec = torch.zeros(n).to(grads.device)
    sol_vec[init_sol[0][0]] = init_sol[1]
    sol_vec[init_sol[0][1]] = 1 - init_sol[1]

    # For two tasks, the closed-form initialization is already optimal.
    if n < 3:
        return sol_vec

    iter_count = 0
    while iter_count < MAX_ITER:
        grad_dir = -1.0 * torch.matmul(grad_mat, sol_vec)
        new_point = _next_point(sol_vec, grad_dir, n)

        v1v1 = torch.sum(sol_vec.unsqueeze(1).repeat(1, n) * sol_vec.unsqueeze(0).repeat(n, 1) * grad_mat)
        v1v2 = torch.sum(sol_vec.unsqueeze(1).repeat(1, n) * new_point.unsqueeze(0).repeat(n, 1) * grad_mat)
        v2v2 = torch.sum(new_point.unsqueeze(1).repeat(1, n) * new_point.unsqueeze(0).repeat(n, 1) * grad_mat)

        nc, _ = _min_norm_element_from2(v1v1, v1v2, v2v2)
        new_sol_vec = nc * sol_vec + (1 - nc) * new_point
        change = new_sol_vec - sol_vec
        if torch.sum(torch.abs(change)) < STOP_CRIT:
            return sol_vec
        sol_vec = new_sol_vec
        iter_count += 1

    return sol_vec
