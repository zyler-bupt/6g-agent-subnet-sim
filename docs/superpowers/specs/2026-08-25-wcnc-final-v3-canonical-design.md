# WCNC Final v3 Canonical Experiment Design

## Purpose

`wcnc_final_v3` is the only publication experiment protocol. It replaces all
unversioned and earlier WCNC result pipelines without deleting their raw data.
Formal figures are derived only from immutable canonical raw trials through a
deterministic aggregate step.

## Canonical artifact root

All new artifacts live below `results/paper/wcnc_final_v3/`:

```text
raw/{exp1,exp2,exp3,exp4}/
aggregated/{exp1,exp2,exp3,exp4}/
figures/
pilot/
manifest.json
integrity_report.json
experiment_report.md
```

Old `results/raw/paper`, `results/exp3`, versioned WCNC directories, and demo
outputs remain audit-only and are never accepted as v3 inputs.

## Method registry

The frozen main-paper method sets are:

- Exp1: `proposed`, `cspf`, `global_sfc_embedding`.
- Exp2: `proposed`, `sanet_dw`, `weighted_sum`, `independent`.
- Exp3: `proposed`, `netren`, `local_only`, `full_rebuild`.
- Exp4 link: `proposed`, `cspf`, `full_rebuild`.
- Exp4 agent: `proposed`, `sfc_restoration`, `full_rebuild`.
- Exp4 capacity: `proposed`, `te_reopt`, `full_rebuild`.

Display labels are respectively Ours, CSPF-based Formation, Global SFC
Embedding (Heuristic), SANet-DW*, Weighted-Sum Coordination, Independent Layer
Optimization, NetRen*, Local-Only, Full Rebuild, CSPF Recovery, SFC
Restoration, and TE Re-optimization. `SANet-DW*` and `NetRen*` are adapted
implementations; the star and adaptation statement are mandatory in figures,
manifests, and reports.

## Fairness and provenance

Every paired method run consumes the same scenario fingerprint. All methods in
an experiment use the same installer, verifier, deadline, and raw schema. A
method may only read fields declared by its observation contract.

Exp2 has three explicit boundaries:

1. the offline oracle reads `true_state` and creates labels only;
2. online methods read a common `observed_state` and never receive oracle
   labels;
3. the common transaction verifier is a system safety gate and its rescue is
   reported separately from pre-verification method quality.

The demand model is `R_i = gamma * C_eff_i * epsilon_i`. Per-flow epsilon is
deterministic per seed, truncated to `[0.85, 1.15]`, normalized per scenario,
and unchanged across gamma. Online Proposed has `max_combinations` and
`coordination_timeout_ms` budgets.

NetRen* performs service-migration-driven network configuration resynthesis
from migrated flows and affected gateways. It must not call Ours' exact Task-DAG
dependency closure. It shares only the task-state adapter, installer, and
verifier.

## Experiment grids and metrics

Exp1 uses Agent counts `[4, 8, 12, 16, 20]`, pilot seeds `9000:9019`, and formal
seeds `0:49`. Its main methods share the real Linux netns/veth/netem,
ping/iperf3, timeout, and background-traffic backend. Main metrics are verified
formation latency, success rate, timeout rate, control messages, and rules
installed. Deterministic latency models are never mixed with measured netns
results.

Exp2 uses gamma `[0.8, 0.9, 1.0, 1.05, 1.1, 1.2, 1.3]`. Main metrics are
feasible-scenario QoS satisfaction and pre-verification correct decision.
Auxiliary metrics include post-verification safe rejection, rollback rate,
verification rescue rate, coordination latency, search timeout, and unsafe
proposal rate.

Exp3 uses affected dependency scope `[10, 20, 30, 40, 50]%`. Main metrics are
unconditional success, conditional verified latency, modification ratio,
unaffected-flow disturbance, and control messages. Conditional latency is
always labeled “Successful Runs Only”.

Exp4 is faceted by failure type. Link severity uses affected-flow ratio; Agent
severity uses dependency-closure ratio; capacity severity uses post-fault
capacity/requirement points `[1.1, 1.0, 0.9, 0.75, 0.6]`, plotted in increasing
difficulty. Feasible recovery and objectively-unrecoverable safe rejection use
different denominators. Inapplicable methods are absent/N/A, never zero.

## Statistics and figures

Rates use Wilson 95% intervals and retain numerator/denominator. Continuous
metrics use topology-seed cluster bootstrap; paper comparisons additionally
report same-seed paired-difference intervals. Failed and timed-out trials remain
in all unconditional metrics.

Figures read only canonical aggregate CSVs and emit PDF, PNG, SVG, and figure
source CSV. Plotting code contains no experiment values, smoothing, monotonic
enforcement, seed filtering, or demo fallback. Synthetic examples live only in
`examples/demo` with `mock_` or `synthetic_` names.

## Manifest and audit

The manifest records protocol ID, git commit, scoped source/config hashes,
method sets and labels, adaptation/reference text, seeds, environment,
dependency versions, observed/true schema versions, result mode, raw hashes,
aggregation rule, and plotting-script hash. The integrity audit rejects method
drift, pairing drift, source/config/data drift, missing failures/timeouts,
untraceable figure points, demo dependencies, and raw/aggregate/figure
reproduction failures.

Pilot selection may inspect only ground-truth class coverage, runtime, and
anomalies. It cannot inspect method ranking to select formal points. Formal
configuration is frozen after the pilot gate.
