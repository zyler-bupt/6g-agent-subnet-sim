"""Realistic control-plane + deployment + verification latency model (Exp.1).

This module is the single source of truth for the Exp.1 formation latency.
It is intentionally *pure* (no project imports) so it can be unit-tested and
audited in isolation.

Design rules (must hold for the paper to be defensible)
------------------------------------------------------
1. Every duration is derived from physical-ish primitives
   (serialization, propagation/RTT, gateway processing, state
   synchronization, data-plane probing). We do **not** multiply or inflate
   numbers by hand.
2. All methods observe the *same* primitives for a given topology
   (primitives are seeded from the topology fingerprint). The only
   legitimate sources of latency differences between methods are:
     - the deployment strategy (parallel gateways vs. one-by-one), and
     - an up-front optimization cost (ILP / full re-optimization).
3. The main metric is decomposed exactly as the paper requires:

       T_form = T_ctrl + T_dispatch + T_install + T_verify + T_activate

   where T_ctrl is controller-side algorithmic work (ms scale, shows
   algorithm efficiency) and the remaining four phases are control-plane /
   data-plane round trips that land in the hundreds-of-ms-to-seconds range.
4. Verification is modeled as a real pipeline:
       install -> gateway state report -> controller validation
       -> data-plane probing (ping/iperf3) -> stable -> activation
   Activation happens *after* successful verification.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Mapping, Sequence

# --- Physical-ish primitive ranges (milliseconds) -------------------------
# Chosen to match the experiment guide:
#   message serialization ............ 1-5 ms
#   propagation / RTT ................ 5-20 ms
#   gateway processing ............... 5-20 ms
#   state synchronization (report+validate+ack) ... 2-8 ms
#   data-plane probing (ping/iperf3 per edge) ... 5-30 ms
SERIALIZATION_MS = (1.0, 5.0)
PROPAGATION_RTT_MS = (5.0, 20.0)
GATEWAY_PROCESSING_MS = (5.0, 20.0)
STATE_SYNC_MS = (2.0, 8.0)
DATA_PLANE_PROBE_MS = (5.0, 30.0)

# Deterministic per-gateway / per-edge primitive resolution.
# Same topology fingerprint -> identical primitives for *every* method.


def _seeded_rng(seed_str: str) -> random.Random:
    digest = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def topology_primitives(
    topology_seed_str: str,
    gateway_ids: Sequence[str],
    edge_ids: Sequence[str],
    path_lengths: Mapping[str, int],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Return (gateways, edges) primitive dictionaries, deterministic per topology."""
    rng = _seeded_rng(topology_seed_str)
    gateways: dict[str, dict[str, float]] = {}
    for gateway_id in gateway_ids:
        gateways[gateway_id] = {
            "rtt": rng.uniform(*PROPAGATION_RTT_MS),
            "proc": rng.uniform(*GATEWAY_PROCESSING_MS),
            "ser": rng.uniform(*SERIALIZATION_MS),
            "sync": rng.uniform(*STATE_SYNC_MS),
        }
    edges: dict[str, dict[str, float]] = {}
    for edge_id in edge_ids:
        edges[edge_id] = {
            "probe": rng.uniform(*DATA_PLANE_PROBE_MS),
            # controller-side rule generation (part of T_ctrl)
            "rule_gen": 0.20 + 0.05 * max(1, path_lengths.get(edge_id, 1)),
            "path_len": float(max(1, path_lengths.get(edge_id, 1))),
        }
    return gateways, edges


@dataclass(frozen=True)
class FormationLatencyBreakdown:
    method_id: str
    t_ctrl_ms: float
    t_dispatch_ms: float
    t_install_ms: float
    t_verify_ms: float
    t_activate_ms: float
    t_form_ms: float
    deploy_mode: str
    notes: str = ""

    @property
    def phases(self) -> dict[str, float]:
        return {
            "T_ctrl": self.t_ctrl_ms,
            "T_dispatch": self.t_dispatch_ms,
            "T_install": self.t_install_ms,
            "T_verify": self.t_verify_ms,
            "T_activate": self.t_activate_ms,
        }


def _avg(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# Per-phase physical composition of a single gateway/edge round-trip.
#   dispatch : controller -> gateway message      (serialization + propagation)
#   install  : gateway applies rule + ACK         (processing + serialization)
#   verify   : report + validation + data-plane probe (rtt + proc + ser + probe)
#   activate : commit + ACK                        (propagation + light processing)
PHASE_WEIGHTS = {
    "dispatch": {"rtt": 1.0, "proc": 0.0, "ser": 1.0},
    "install": {"rtt": 0.0, "proc": 1.0, "ser": 1.0},
    "verify": {"rtt": 1.0, "proc": 1.0, "ser": 1.0},
    "activate": {"rtt": 1.0, "proc": 0.5, "ser": 1.0},
}


def formation_latency_breakdown(
    *,
    method_id: str,
    num_agents: int,
    num_edges: int,
    gateway_ids: Sequence[str],
    edge_ids: Sequence[str],
    edges_on_gateway: Mapping[str, Sequence[str]],
    path_lengths: Mapping[str, int],
    topology_seed_str: str,
) -> FormationLatencyBreakdown:
    """Compute the five-phase formation latency for one method on one topology.

    `edges_on_gateway` maps each gateway id to the list of business-edge ids
    whose path traverses it. `path_lengths` maps each edge id to its hop count.
    `topology_seed_str` must be identical across methods for a fair comparison.
    """
    gateways, edges = topology_primitives(
        topology_seed_str, gateway_ids, edge_ids, path_lengths
    )

    # --- T_ctrl: controller processing (algorithmic, ms scale) ------------
    dag_analysis = 0.50 + 0.08 * num_agents + 0.06 * num_edges
    rule_generation = sum(item["rule_gen"] for item in edges.values())
    t_ctrl = dag_analysis + rule_generation
    if method_id == "ilp_sfc":
        # Global ILP/SFC embedding: super-linear up-front optimization cost
        # that explodes with task size (the method's known weakness).
        t_ctrl += 2.0 + 2.0 * num_edges + 0.12 * num_edges * num_edges
    elif method_id == "sfc_reoptimization":
        # Full service-chain re-computation on every change.
        t_ctrl += 1.0 + 1.0 * num_edges + 0.05 * num_edges * num_edges

    # --- Deployment phases ------------------------------------------------
    # Proposed deploys rules in parallel across independent gateways; the
    # baselines (srd/cspf/ilp_sfc/sfc_reoptimization) deploy edge-by-edge.
    deploy_mode = "parallel" if method_id == "proposed" else "sequential"

    avg_rtt = _avg([g["rtt"] for g in gateways.values()]) or 12.0
    avg_proc = _avg([g["proc"] for g in gateways.values()]) or 12.0
    avg_ser = _avg([g["ser"] for g in gateways.values()]) or 3.0

    def _round_unit(weights: dict[str, float], rtt: float, proc: float, ser: float) -> float:
        return weights["rtt"] * rtt + weights["proc"] * proc + weights["ser"] * ser

    def _parallel_phase(phase: str, extra_per_gateway: Mapping[str, float]) -> float:
        weights = PHASE_WEIGHTS[phase]
        best = 0.0
        for gateway_id, elist in edges_on_gateway.items():
            g = gateways[gateway_id]
            workload = sum(max(1, path_lengths.get(e, 1)) for e in elist)
            cost = workload * _round_unit(weights, g["rtt"], g["proc"], g["ser"])
            cost += extra_per_gateway.get(gateway_id, 0.0)
            best = max(best, cost)
        return best

    def _sequential_phase(phase: str, extra_per_edge: Mapping[str, float]) -> float:
        weights = PHASE_WEIGHTS[phase]
        total = 0.0
        for e in edge_ids:
            hops = max(1, path_lengths.get(e, 1))
            total += hops * _round_unit(weights, avg_rtt, avg_proc, avg_ser)
            total += extra_per_edge.get(e, 0.0)
        return total

    probe_per_gateway = {
        gateway_id: sum(edges[e]["probe"] for e in elist)
        for gateway_id, elist in edges_on_gateway.items()
    }
    probe_per_edge = {e: edges[e]["probe"] for e in edge_ids}

    if deploy_mode == "parallel":
        t_dispatch = _parallel_phase("dispatch", {})
        t_install = _parallel_phase("install", {})
        t_verify = _parallel_phase("verify", probe_per_gateway)
        t_activate = _parallel_phase("activate", {})
    else:
        t_dispatch = _sequential_phase("dispatch", {})
        t_install = _sequential_phase("install", {})
        t_verify = _sequential_phase("verify", probe_per_edge)
        t_activate = _sequential_phase("activate", {})

    t_form = t_ctrl + t_dispatch + t_install + t_verify + t_activate
    return FormationLatencyBreakdown(
        method_id=method_id,
        t_ctrl_ms=t_ctrl,
        t_dispatch_ms=t_dispatch,
        t_install_ms=t_install,
        t_verify_ms=t_verify,
        t_activate_ms=t_activate,
        t_form_ms=t_form,
        deploy_mode=deploy_mode,
        notes=(
            "parallel gateway deployment"
            if deploy_mode == "parallel"
            else "sequential edge-by-edge deployment"
        ),
    )
