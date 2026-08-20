# Final Paper Experiment Suite Design

Date: 2026-08-20

## 1. Purpose and Scope

This design finalizes the four paper experiments for the existing 6G Agent
Task Subnet simulator. The implementation is a research experiment suite, not
a production distributed system. It prioritizes reuse of the Stage 2--5
control-plane simulator, minimal targeted adaptation, fast pilot feedback,
paired method comparison, reproducible raw data, and publication-ready vector
figures.

The four experiments answer one mechanism question each:

1. Exp.1 Formation: can a task subnet be built efficiently and reliably?
2. Exp.2 Coordination: why is explicit cross-layer coordination necessary?
3. Exp.3 Elasticity: can a business change be handled without full rebuild?
4. Exp.4 Resilience: can runtime failures be recovered with an appropriate
   repair scope?

The implementation order is Exp.1, Exp.3, Exp.4, then Exp.2. Each experiment
must pass pilot execution and sanity checking before work proceeds to the next
experiment. Paper mode runs only after all four pilots pass.

## 2. Scientific Fairness Contract

For a fixed `(experiment, point, topology_seed, event_id)`, the runner creates
one immutable scenario snapshot before any method runs. Every compared method
receives equivalent fresh instances derived from that snapshot. The snapshot
contains the topology, Task DAG, Agent placement, QoS, business change or
failure event, simulator latency primitives, verification cost, and all random
draws.

The raw output stores topology, QoS, scenario, and event fingerprints. The
audit rejects a comparison point if fingerprints differ across methods or if
the method set is incomplete. Method identity is never passed to scenario
generation, ground-truth labeling, primitive cost sampling, or the stable-state
verifier.

No method receives a different stress range. If pilot calibration changes a
scenario range, all methods for the affected experiment are rerun and the old
pilot comparison is removed from the authoritative output.

Plotting reads aggregated CSV only. It never edits, clamps, smooths, imputes,
or otherwise changes raw trial outcomes. Axis limits may change presentation,
but percentage-axis truncation must remain explicit in ticks and aggregation
data.

## 3. Architecture and Reuse

The suite uses a convergence approach rather than a simulator rewrite.

Existing experiment entry points are retained and adapted:

- `experiments/exp1_initial_formation.py`
- `experiments/exp2_cross_layer_conflict.py`
- `experiments/exp3_business_elasticity.py`
- `experiments/exp4_failure_reconfiguration.py`

Existing reusable mechanisms include:

- `AgentController` DAG compilation and four-layer supporting-Agent binding;
- Gateway stable/staged stores and versioned activation;
- `RuntimeEvent`, `ImpactScopeAnalyzer`, `RuleDelta`, and rollback;
- `TransactionExecutor` and the common staged/stable verifier;
- cross-layer proposals, feasibility evaluation, and exact ground truth;
- failure scenario generation and CSPF;
- current experiment metric and audit patterns.

A small shared paper-experiment layer centralizes only the cross-cutting
protocol:

- `pilot` and `paper` mode definitions;
- method IDs, labels, sources, adaptation flags, and plot styles;
- shared trial identity and fingerprint checks;
- the canonical raw row schema;
- seed-cluster bootstrap aggregation;
- result sanity checks and report generation.

The intended command surface is:

- `scripts/run_pilot.py`
- `scripts/run_paper.py`
- `scripts/aggregate_results.py`
- `scripts/sanity_check_results.py`
- `scripts/plot_paper_figures.py`

Core simulator files are changed only where an experiment requires a missing
mechanism. Existing public paths remain compatible unless a path is proven to
be an obsolete, incorrect experiment-only implementation.

## 4. Shared Scenario and Latency Model

The paper topology generator creates deterministic mesh-like Gateway graphs.
Link attributes are sampled once per topology seed and stored in the snapshot:

- delay: 5--30 ms;
- bandwidth: 50--200 Mbps;
- loss: 0--1%;
- jitter: 0--5 ms.

The common latency model exposes method-independent primitive operations such
as endpoint resolution, constrained path calculation, rule compilation,
Gateway stage, verification, activation, and stable verification. Primitive
costs are determined from the same topology and operation parameters for all
methods. A method's latency differs only because its algorithm performs a
different set of permitted operations or schedules shared operations with
different dependencies and concurrency. There are no per-method multipliers
or target-curve coefficients.

The raw trace records primitive counts and control messages so constant-output,
zero-work, and anomalous scheduling implementations can be detected.

## 5. Run Modes

### 5.1 Pilot

Pilot uses 5 topology seeds and 2 independent events per seed, producing about
10 paired trials per point. It is used for correctness, ordering, stress-range,
and anomaly checks. Pilot outputs are not paper results.

Every completed pilot produces raw rows, aggregated metrics, method rankings,
means, success rates, P95 latency, sanity results, and provisional figures.

### 5.2 Paper

Continuous metrics use at least 30 topology seeds. Rate metrics use 5
independent events for each of 30 topology seeds, giving 150 event instances
per point. When a trial produces both continuous and rate metrics, all five
events may be retained, while confidence intervals continue to cluster by
topology seed.

Continuous latency aggregation stores mean, P50, P95, and a 95% cluster
bootstrap interval. Rate metrics also use topology-seed cluster bootstrap so
multiple events from one topology are not treated as independent topologies.

## 6. Method Registry

The canonical main-figure registry is:

| Method ID | Label | Source | Adapted |
|---|---|---|---|
| `proposed` | Proposed | This work | false |
| `proposed_without_batch` | Proposed w/o Batch | This work | false |
| `cspf` | CSPF | CSPF | false |
| `a1_agent_embedded` | A1-Agent-Embedded* | A1 Agent | true |
| `sanet_dw` | SANet-DW* | SANet | true |
| `adjacent_layer` | Adjacent-Layer | Adjacent-layer coordination | false |
| `independent` | Independent | Independent layer optimization | false |
| `netren` | NetRen* | NetRen | true |
| `local_only` | Local-Only | Local repair | false |
| `full_rebuild` | Full-Rebuild | Global reconstruction | false |
| `netkeeper` | NetKeeper* | NetKeeper | true |

Paper text and captions state: "* denotes an adaptation of the corresponding
published method to the common task-subnet simulation environment." Adapted
methods are described as adaptations of core algorithmic principles, never as
exact reproductions of original implementations.

## 7. Exp.1: Initial Task Subnet Formation

### 7.1 Scenario

Exp.1 uses 12 Gateways with average degree 3--4 and task sizes
`[8, 12, 16, 20, 24, 28, 32]`. The deterministic modular fork-join generator
creates forks, merge nodes, average out-degree near 1.5--2.0, and a target
60--70% cross-Gateway business-edge ratio. It does not generate pure chains.

Latency trials sweep task size. Churn trials fix task size at 24 and sweep
`[0, 5, 10, 15, 20, 30]%`. Churn represents transient bandwidth, latency, and
queue fluctuation during formation, not link or Agent failure.

### 7.2 Methods

- Proposed performs DAG dependency analysis, supporting-Agent binding,
  cross-layer rule compilation, Gateway grouping, parallel stage, global
  verification, atomic activation, and stable verification.
- Proposed w/o Batch uses identical DAG analysis, binding, paths, rules,
  verification, and transaction semantics. Its only difference is sequential
  Gateway stage and activation.
- CSPF processes every business edge through constrained path computation,
  rule generation, deployment, and verification. Constraints include
  connectivity, bandwidth, and delay. The measured interval ends only after a
  stable executable subnet exists.
- A1-Agent-Embedded* processes each required communication operation
  sequentially through endpoint resolution, path calculation, configuration,
  deployment, verification, and activation. LLM inference is excluded because
  the baseline represents sequential agent-driven network procedure execution.

Formation success requires all business edges reachable, every required rule
installed, QoS verified, no partial activation, and stable verification passed.
Formation latency is measured from task receipt to stable verification finish.

## 8. Exp.3: Business-Change Elastic Reconfiguration

### 8.1 Scenario and Sampling

Exp.3 uses 24 business Agents, 12 Gateways, and approximately 36 edges in a
modular fork-join DAG. Events balance Agent Add, Agent Remove, DAG Edge Change,
and QoS Update.

The generator first creates a valid change, then computes its exact dependency
closure, then calculates affected-scope ratio, and finally places the event in
one of `[10, 20, 30, 40, 50]%` buckets. It never mutates a measured closure to
fill a bucket. Insufficient buckets trigger additional method-independent event
sampling.

### 8.2 Methods

- Local-Only updates the changed object, one-hop DAG neighbors, and the directly
  attached Gateway. It has no closure computation or scope escalation.
- NetRen* derives changed required network flows, resynthesizes the relevant
  network configuration, repairs consistency, deploys, and verifies. It does
  not use task dependency closure, supporting-Agent dependency closure, or
  elastic scope escalation.
- Full-Rebuild discards the current task-subnet configuration and recompiles,
  redeploys, and verifies the complete current DAG.
- Proposed starts from changed objects, computes task and supporting-Agent
  closure, derives affected flows and Gateways, stages the RuleDelta, verifies,
  and activates. An infeasible Tier-1 scope escalates to Tier-2 and then Tier-3;
  it does not begin with a full rebuild.

Success requires a valid new DAG, satisfied QoS, obsolete-rule removal, no stale
rules, and stable verification. Unaffected flows are defined using the
method-independent ground-truth dependency closure.

## 9. Exp.4: Failure-Driven Elastic Recovery

### 9.1 Scenario

Exp.4 reuses the Exp.3 base topology: 24 business Agents, 12 Gateways, and
approximately 36 business edges.

- Agent Failure selects an active business Agent and creates 1--3 compatible
  replacements at differing Gateway locations and network conditions.
- Link Failure selects an active task-traffic link. Most instances contain at
  least one feasible alternate path.
- Capacity Degradation reduces capacity by 30% in failure-type comparison
  panels and sweeps `[10, 20, 30, 40, 50]%` in the stress panel. Spare capacity
  is limited but not universally absent.

`RuntimeEvent` is the controller notification boundary. Reported latency is
therefore reconfiguration/recovery latency after event notification, not
physical failure-detection latency.

### 9.2 Methods

- CSPF uses network topology, link state, available bandwidth, and delay. It may
  reroute but cannot replace a business Agent, modify the Task DAG, alter
  application behavior, or perform cross-layer supporting-Agent coordination.
- NetKeeper* adapts autonomous network configuration update: it consumes the
  runtime anomaly, traffic state, and network policy to perform traffic-aware
  rerouting and network resource adjustment. It has no Task DAG closure,
  business-Agent replacement reasoning, or cross-layer elastic-scope expansion.
- Full-Rebuild recompiles all members, DAG edges, supporting bindings, and
  Gateway rules before verification and activation.
- Proposed localizes the failed object, computes Task DAG and cross-layer
  impact, tries Tier-1 local/parameter repair, Tier-2 path or replacement repair,
  and Tier-3 larger task-subnet recompilation, then applies and verifies the
  RuleDelta.

Link-failure results are allowed to show CSPF equal to or faster than Proposed.
Agent failure should expose CSPF's capability boundary naturally. Increasing
capacity severity should reveal when network-only adjustment is insufficient.

## 10. Exp.2: Cross-Layer Conflict Resolution

### 10.1 Scenario and Oracle

Exp.2 uses 20 business Agents, 10 Gateways, and approximately 28--32 DAG edges.
Every initial state is valid, stable, and QoS-satisfied. Conflict density sweeps
`[0, 10, 20, 30, 40, 50, 60]%` and is the fraction of business edges receiving
conflicts.

Conflict types are sampled approximately as application-network 25%,
transport-network 20%, network-physical 20%, and cascaded multi-layer 35%.
Resource and QoS parameters are generated first; the offline exact feasibility
oracle then labels the shared candidate action space. The oracle is used only
for ground truth and is excluded from method runtime. The pilot target is
85--90% solvable and 10--15% infeasible cases across the experiment, achieved
through shared parameter-range calibration rather than direct label assignment.

The common action space remains small enough for exact enumeration where
possible. If the final bounded representation exceeds practical enumeration,
the oracle may use an exact ILP/SMT formulation without changing method inputs.

### 10.2 Methods

- Independent lets application, transport, network, and physical layers select
  their local objectives independently and combines the actions without
  arbitration.
- Adjacent-Layer coordinates only application-transport,
  transport-network, and network-physical pairs; it cannot jointly solve all
  four layers.
- SANet-DW* dynamically updates layer weights from objective violations and
  performs competitive global weighted optimization over the common action
  space. It does not receive Proposed's explicit hard-feasibility arbitration,
  task-level hard-constraint checking, or transaction-level pre-verification.
- Proposed collects proposals and states, constructs global hard constraints,
  identifies conflicts, searches feasible combinations, performs
  priority-aware arbitration, verifies, and commits or rolls back.

Feasible Solution Rate and QoS Satisfaction Rate use only ground-truth solvable
cases. Safe Rejection Rate uses only ground-truth infeasible cases.

## 11. Canonical Raw Schema

Every trial contains at least:

```text
experiment, mode, trial_id, seed, event_id, method_id, method_label,
method_source, adapted, topology_fingerprint, scenario_fingerprint,
qos_fingerprint, event_fingerprint, task_size, num_dag_edges, num_gateways,
state_churn_probability, conflict_density, conflict_type,
ground_truth_feasible, business_change_type, affected_scope_ratio,
failure_type, failure_severity, formation_latency_ms,
resolution_latency_ms, reconfiguration_latency_ms, recovery_latency_ms,
success, qos_satisfied, safe_rejection, total_rules, changed_rules,
rule_change_ratio, total_gateways, changed_gateways, gateway_change_ratio,
total_flows, unaffected_flows, disturbed_unaffected_flows,
unaffected_disturbance_ratio, control_messages, rollback_count,
stale_state_detected
```

Unused experiment-specific fields remain empty rather than receiving fabricated
zeros. Derived ratios are recomputed during audit from their numerator and
denominator where applicable.

## 12. Aggregation and Sanity Checks

Aggregation produces per-point trial counts, topology-cluster counts, mean,
P50, P95, cluster-bootstrap lower and upper 95% bounds, success rate, QoS rate,
and safe-rejection rate as applicable. It also emits per-seed intermediate
statistics for independent audit.

Sanity checks report `ERROR`, `WARNING`, or `PASS`:

1. warn if every method is 100% successful;
2. warn if every method is below 20% success;
3. check that Exp.1 formation latency generally increases with task size;
4. check that Exp.2 FSR does not anomalously improve with conflict density;
5. check that Exp.3 Proposed changed rules generally increase with affected
   scope;
6. check that Exp.4 network-only recovery does not improve anomalously with
   capacity reduction;
7. error on constant output, zero latency, always-success, or always-failure
   patterns that indicate implementation defects.

Fingerprint mismatches, incomplete paired method sets, schema violations,
invalid metric denominators, impossible ratios, and missing outputs are errors.
Scientifically plausible outcomes such as lower Local-Only latency, faster CSPF
link repair, or high Full-Rebuild success are not errors.

A pilot passes only with no errors. Warnings must either be explained as a
plausible mechanism outcome in the pilot report or resolved through a shared
scenario recalibration and complete rerun.

## 13. Figures and Style

The authoritative figure directory is `results/paper_figures/`. Each figure is
written as vector PDF and PNG preview:

- `Fig1a_Formation_Latency`
- `Fig1b_Formation_Success`
- `Fig2a_Feasible_Solution_Rate`
- `Fig2b_QoS_Satisfaction`
- `Fig2c_Safe_Rejection`
- `Fig3a_Reconfiguration_Latency`
- `Fig3b_Rule_Change_Ratio`
- `Fig3c_Reconfiguration_Success`
- `Fig4a_Recovery_Latency`
- `Fig4b_Recovery_Success`
- `Fig4c_Rule_Change_Ratio`
- `Fig4d_Capacity_Stress`

The shared Matplotlib style uses Times New Roman or a metrically compatible
Times fallback, approximately 8.5--9 pt axis labels, 8 pt ticks, 7.5--8 pt
legends, 1.5--1.8 line width, 4--5 marker size, light 0.12--0.18 confidence
bands, white background, and horizontal dashed grids only. Single-column
figures target 3.45 by 2.45 inches. Multi-panel layouts target approximately
7.1 by 2.25 inches.

Method color, marker, and line style live in one registry and remain stable
across figures. Proposed is dark blue/circle/solid; the no-batch ablation is a
lighter dashed blue; CSPF is green/square; adapted SANet, NetRen, and NetKeeper
methods share the registered orange/diamond family; Independent is gray/X;
Local-Only is purple/triangle-down; Full-Rebuild is dark red/X.

## 14. Verification Strategy

Tests are written before each behavior change and cover:

- deterministic modular DAG and mesh topology generation;
- shared scenario and event fingerprints across methods;
- method capability boundaries and forbidden inputs;
- formation batching as the only Proposed/no-batch difference;
- business-change closure and bucket placement;
- failure replacement, alternate path, and capacity semantics;
- exact oracle independence and conditional metric denominators;
- canonical raw schema and cluster bootstrap behavior;
- sanity warnings/errors and global plot-style consistency;
- vector PDF and required filename production.

Existing Stage 2--5 regression tests remain green. Each phase runs its targeted
tests, pilot, aggregation, sanity checks, and figure smoke tests before its
commit is considered complete. Before final completion, the full test suite,
all paper audits, raw-to-aggregate reproduction, and figure existence checks
must pass.

## 15. Output, Reporting, and Git Hygiene

Authoritative outputs are:

```text
results/raw/pilot/
results/raw/paper/
results/aggregated/pilot/
results/aggregated/paper/
results/paper_figures/
results/EXPERIMENT_REPORT.md
```

`EXPERIMENT_REPORT.md` documents the actual scenario, method adaptations,
pilot and paper ranges, observed trends, deviations from expected mechanisms,
baseline anomalies, all-success/all-failure findings, stress-range decisions,
and exact locations of raw CSV and final figures.

Clearly accidental zero-byte command-option files are removed. Historical
result directories remain non-authoritative and excluded from final runners
and Git publication. An obsolete experiment path is deleted only after its
replacement is verified and repository references show it is unused; otherwise
it remains marked or treated as legacy.

Each phase is committed independently after verification and pushed to the
current `codex/semantic-controller` branch. Git publication includes source,
configuration, tests, the final report, aggregate CSV, paper figures, and raw
CSV files that are reasonably sized. It excludes unrelated office documents,
archives, legacy bulk output, caches, and transient logs.

