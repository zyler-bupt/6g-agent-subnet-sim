# Baseline Description — WCNC 2027 Final Protocol (v3)

Every method used in the frozen experiment protocol carries explicit metadata
(`experiments/paper_protocol.py` → `MethodMetadata`: `category`, `reference`,
`why_included`, `status`). This document is the human-readable version.

Classification rule (v3 §6):
- **literature-inspired** — inspired by a published work or a classic, named
  algorithm. Presented as a *comparison baseline*.
- **strategy** — a repair / reconstruction *strategy* (NOT a published
  algorithm). Presented as a comparison point, but never framed as a
  literature contribution.
- **proposed** — the method under evaluation.
- **internal-ablation** — internal ablation study; never in main figures.
- **status**: `main` (frozen main figures) · `appendix` (scalability /
  supplementary) · `ablation` (internal only) · `pending` (named in the
  frozen protocol, recovery strategy not yet implemented).

---

## A. Proposed method

| method_id | label | category | reference | why included |
|---|---|---|---|---|
| `proposed` | Proposed | proposed | This work (task-driven cross-layer agent subnet) | Task-aware cross-layer subnet compilation with verified execution and parallel gateway deployment. The method under evaluation. |

---

## B. Literature-inspired baselines (main figures)

| method_id | label | reference | why included | experiment |
|---|---|---|---|---|
| `cspf` | CSPF | Constraint-based Shortest Path First (RFC 2702 / MPLS-TE) | Network-centric constrained path computation. Represents the traditional routing-oriented solution. | Exp1 |
| `sfc_reoptimization` | SFC Re-opt | SFC Re-optimization (service-chain re-embedding) | Complete service-chain reconstruction: recomputes path, rule set and deployment order on every change. Exp1 baseline for full recompute. | Exp1 |
| `weighted_sum` | Weighted-Sum | Weighted-sum multi-objective optimization | Classic soft cross-layer optimization baseline for Exp2: maximizes a weighted utility without hard feasibility verification. | Exp2 |
| `sanet_dw` | SANet* | SANet (semantic-aware agent network), adapted | Closest existing semantic-aware agent coordination work. Literature baseline for Exp2 cross-layer coordination. | Exp2 |
| `netren` | NetRen* | NetRen (dynamic service/network reconfiguration), adapted | Represents dynamic service/network reconfiguration. Literature baseline for Exp3 elastic reconfiguration. | Exp3 |

## C. Strategy baselines (main figures)

| method_id | label | reference | why included | experiment |
|---|---|---|---|---|
| `independent` | Independent | Independent layer optimization (oracle baseline) | Each layer optimized independently with no cross-layer interaction. Shows why coordination is necessary. | Exp2 |
| `local_only` | Local-Only | Local-only update (repair strategy) | Fast local repair that modifies only directly affected components. May miss global dependencies. | Exp3 |
| `full_rebuild` | Full Rebuild | Full rebuild (global reconstruction) | Recomputes the whole subnet from scratch. High correctness but expensive. | Exp3, Exp4 |
| `network_only` | Network-Only | Network-only local recovery (executable placeholder) | Currently the executable proxy for link/capacity recovery baselines (CSPF/FRR/TE-Reopt) until their recovery strategies are implemented; lives only in `EXPERIMENT_METHODS['exp4']`, will be replaced by the specific literature baselines per failure type. | Exp4 |

---

## D. Pending recovery strategies (named in frozen protocol, not yet implemented)

These belong to the frozen protocol but their recovery strategies are not
implemented in the runner. The runner currently executes `EXPERIMENT_METHODS['exp4']`
(the executable union) and the plotting layer filters per failure type using
`EXPERIMENT_FAILURE_METHODS['exp4']`. Until implemented, `network_only` is the
executable placeholder.

| method_id | label | reference | why included | failure type |
|---|---|---|---|---|
| `frr` | FRR | IP/MPLS Fast Reroute | Local protection baseline for link-failure recovery. | Link |
| `te_reopt` | TE-Reopt | Traffic Engineering Re-optimization | TE re-optimization baseline for physical-capacity-degradation recovery. | Capacity |

---

## E. Appendix / internal-ablation methods (NOT in main figures)

| method_id | label | category | reference | why included | status |
|---|---|---|---|---|---|
| `ilp_sfc` | ILP-SFC | literature-inspired | ILP VNF/SFC embedding | Global optimization reference for a scalability analysis. Does not model task-agent semantic dependency, so appendix only. | appendix |
| `sfc_reconfiguration` | SFC-Reconfig* | literature-inspired | SFC Reconfiguration, adapted | Service-chain reconfiguration baseline for an appendix/alternative elasticity comparison. | appendix |
| `netkeeper` | NetKeeper* | literature-inspired | NetKeeper, adapted | Network-keeper style baseline for an alternative recovery comparison. | appendix |
| `sfc_restoration` | SFC-Restore | literature-inspired | SFC Restoration | Service-chain restoration baseline for an appendix/alternative recovery comparison. | appendix |
| `proposed_without_batch` | Proposed w/o Batch | internal-ablation | This work (ablation) | Ablation: removes parallel batch deployment to isolate its contribution. | ablation |
| `srd` | SRD | internal-ablation | Sequential Rule Deployment (internal) | Ablation of the deployment strategy. Not a literature baseline. | ablation |
| `adjacent_layer` | Adjacent-Layer | internal-ablation | Adjacent-layer coordination (internal) | Internal ablation only: weaker than global coordination, not a strong literature baseline. | ablation |
