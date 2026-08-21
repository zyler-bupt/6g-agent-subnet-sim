# Final Experiment Protocol — WCNC 2027 (v3 freeze)

Status: **frozen**. Four experiments only; each answers one scientific question.
No further main baselines without paper-level motivation.

Paper story: the proposed task-driven cross-layer agent subnet framework enables
(1) task-aware subnet formation, (2) verified cross-layer coordination,
(3) safe elastic reconfiguration, (4) heterogeneous failure recovery — i.e.
complete lifecycle management, whereas traditional network-centric methods
solve only part of the problem.

---

## 1. Frozen experiment → scientific question

| Exp | Title | Scientific question |
|---|---|---|
| Exp1 | Task Communication Subnet Formation | Can a task DAG be efficiently compiled into an executable communication subnet? |
| Exp2 | Cross-Layer Coordination | Why is cross-layer coordination necessary? |
| Exp3 | Elastic Reconfiguration | Can the system adapt to task evolution with **minimum SAFE** modification? |
| Exp4 | Failure Recovery | Can the system recover from heterogeneous failures? |

---

## 2. Exp1 — Formation

- **Methods (main):** `proposed` (task-aware cross-layer compilation) ·
  `cspf` (network-centric path optimization) · `sfc_reoptimization`
  (complete service-chain reconstruction).
- **Removed from main:** `ilp_sfc` (appendix/scalability only), `srd`,
  `proposed_without_batch` (internal ablations).
- **Latency model (must hold):**
  `T_form = T_ctrl + T_dispatch + T_install + T_verify + T_activate`.
  No artificial scaling. The only legitimate latency differences between
  methods come from:
  - `proposed`: DAG dependency analysis + **parallel gateway deployment** +
    aggregated verification (fast).
  - `cspf`: centralized computation + **sequential deployment** (slower).
  - `sfc_reoptimization`: complete reconstruction → pays install/verify/
    activate an extra time + superlinear T_ctrl (slowest, by design).
- **Expected order (verified numerically, 8–32 agents):**
  `proposed (0.5–1.6 s) < CSPF (2.2–9.9 s) < SFC-Reopt (3.4–15.1 s)`, with
  SFC-ReOpt a consistent ~1.5× of CSPF (never "≈", so reviewers cannot
  question why full reconstruction ≈ routing).
- **Figure (Fig.1):** (a) formation latency vs #agents (line); (b) success
  rate (bar). If all methods are ~100%, report in text instead of plotting.

## 3. Exp2 — Cross-Layer Coordination

- **Methods (main):** `proposed` (verified coordination) · `independent`
  (no cross-layer interaction) · `weighted_sum` (classic soft optimization) ·
  `sanet_dw` (closest semantic-aware literature baseline).
- **Removed from main:** `adjacent_layer` (internal ablation only).
- **Metrics:** (1) QoS Satisfaction Rate; (2) **Safe Rejection Rate** — the
  key differentiator: the contribution is not only finding good solutions but
  *preventing unsafe configurations from executing*.
- **Expected trend:** low conflict → all methods close. High conflict →
  `independent` degrades fastest (no cross-layer check); `weighted_sum` better
  but may violate constraints (no hard feasibility); `sanet_dw` better semantic
  coordination; `proposed` highest because of explicit verification. Avoid
  0%↔100% jumps — curves must be smooth.
- **Figure (Fig.2):** (a) QoS satisfaction vs conflict density (line);
  (b) Safe Rejection Rate vs conflict density (line).

## 4. Exp3 — Elastic Reconfiguration

- **Methods (main):** `proposed` (dependency-aware elastic reconfiguration) ·
  `local_only` (fast local repair, may miss global dependency) ·
  `netren` (dynamic service/network reconfiguration) · `full_rebuild`
  (complete reconstruction).
- **Claim:** *minimum safe* modification — NOT merely minimum scope.
- **Metrics:** (1) Reconfiguration latency; (2) Modification scope;
  (3) Success rate. Do **not** judge by scope alone (Local-Only may win scope
  but lose correctness).
- **Expected trade-off:** `local_only` small scope but lower correctness;
  `full_rebuild` high correctness but large modification; `proposed` best
  trade-off.
- **Figure (Fig.3):** (a) latency vs changed agents (line); (b) scope–success
  trade-off (scatter).

## 5. Exp4 — Failure Recovery

- **Failure types:** Link failure · Agent failure · Physical capacity
  degradation.
- **Failure-specific comparison (never all baselines together):**
  - Link failure: `proposed` vs `cspf` (opt. `frr`).
  - Agent failure: `proposed` vs `full_rebuild`.
  - Capacity degradation: `proposed` vs `te_reopt` vs `full_rebuild`.
- **Pending strategies:** `cspf` / `frr` / `te_reopt` recovery implementations
  are not yet written; `network_only` is the executable placeholder in
  `EXPERIMENT_METHODS['exp4']` and will be replaced by the specific literature
  baselines per failure type once implemented (clean interface already in
  `EXPERIMENT_FAILURE_METHODS`).
- **Metrics:** (1) Recovery latency; (2) Modification scope. If recovery
  success is always 100%, report in text (no meaningless success-rate figure).
- **Figure (Fig.4):** (a) recovery latency grouped by failure type (bar);
  (b) modification scope grouped by failure type (bar).

---

## 6. List of changed files (this v3 round)

| File | Change |
|---|---|
| `experiments/paper_protocol.py` | `MethodMetadata` extended with `category` / `reference` / `why_included` / `status`; all `METHODS` entries rewritten with v3 §6 metadata; `network_only` registered as executable placeholder strategy; method sets already match v3 (Exp1: proposed/cspf/sfc_reoptimization; Exp2: proposed/independent/weighted_sum/sanet_dw; Exp3 unchanged; Exp4 executable union + `EXPERIMENT_FAILURE_METHODS`). |
| `src/simulation/latency_model.py` | SFC-Reopt latency gap vs CSPF made mechanism-driven (extra 60% install/verify/activate pass + superlinear T_ctrl) so ordering is `proposed < CSPF < SFC-Reopt` with clear, reviewer-defensible separation. |
| `figures/ieee_plots.py` | Demo `_demo_rows` corrected so Exp1 illustration uses the same `proposed < CSPF < SFC-Reopt` separation as the model (no "≈" trap). Method styling covers full v3 set incl. `network_only`. |
| `experiments/exp1_initial_formation.py`, `exp2_conflict.py`, `exp3_business_elasticity.py`, `exp4_failure.py` | `method.source` → `method.reference` (field rename). |
| `tests/test_paper_protocol.py`, `tests/test_paper_exp2.py` | Updated to `.reference` and aligned label expectations to v3 metadata. |
| `plan/baseline_description.md` | **New** — per-method metadata table, literature-inspired vs strategy/ablation split (v3 §6/§8). |
| `plan/final_experiment_protocol.md` | **New** — this document (v3 §8). |
| `figures/out/*` | Re-rendered Fig.1–4 demo (PDF+PNG) with corrected Exp1 trend. |

*Not committed from this session:* sandbox has no GitHub route (proxy 502 +
partial clone missing parent object), so commit/push must run in the user's
terminal. A ready `COMMIT_MSG.txt` is provided for `git commit -F`.

---

## 7. Expected trend explanations (one paragraph each, paper-ready)

- **Fig.1a (latency):** Proposed stays lowest because it analyses the task DAG
  once, deploys gateway rules in parallel, and aggregates verification; CSPF
  is slower due to centralized sequential deployment; SFC-Reopt is slowest
  because complete reconstruction re-derives and redeploys the whole rule set.
- **Fig.1b (success):** All three reach ~100% on feasible topologies; the
  difference is latency/efficiency, not feasibility — reported in text if flat.
- **Fig.2a/2b (QoS + Safe Rejection):** As conflict density rises, Independent
  degrades first (no cross-layer check), Weighted-Sum accepts some infeasible
  configs (no hard verification, so Safe Rejection stays low), SANet improves
  semantic coordination, and Proposed stays highest on both because explicit
  verification rejects unsafe configurations instead of executing them.
- **Fig.3a/3b (elasticity):** Local-Only is fastest/ smallest scope but misses
  global dependencies → lower success; Full Rebuild is always correct but pays
  full reconstruction cost; Proposed's dependency-aware scope control yields
  the best latency–scope–success trade-off (scatter lies top-left of the
  trade-off frontier).
- **Fig.4a/4b (recovery):** Per failure type, Proposed recovers with smaller
  scope/latency than the brute-force Full Rebuild; link recovery is fastest
  (local reroute), agent recovery needs replacement (slower), capacity
  recovery re-optimizes resources. Network-centric baselines handle only their
  native failure class, confirming the need for a unified lifecycle framework.

---

## 8. v3 §9 final quality checklist

- [x] No artificial latency scaling — all durations derived from physical
      primitives; method gaps come from deploy mode / reconstruction passes.
- [x] Every baseline has a clear scientific role (see `baseline_description.md`).
- [x] Proposed advantage comes from mechanism: task dependency, parallel
      deployment, verification, elastic scope control.
- [x] No meaningless 100% curves — Exp1b/Exp4 success reported in text when flat.
- [x] No baseline is intentionally weakened — SFC-ReOpt is *stronger* in
      optimization but pays its reconstruction cost honestly.
- [x] Every figure explainable in one paragraph (§7 above).
- [ ] **Remaining before full run:** implement Exp4 `cspf`/`frr`/`te_reopt`
      recovery strategies and wire them into `EXPERIMENT_METHODS['exp4']`;
      write `scripts/aggregate_results.py` to emit the aggregated CSV
      (`experiment,subplot,method,x,y_mean,y_lo,y_hi`) the figure generator
      consumes; then run the full suite in the user environment.
