# Exp2 v5 independent-oracle and provenance design

## Status and objective

This design supersedes the rejected `wcnc-final-gamma-v4` formal protocol.
The v4 raw data remain immutable review evidence and must not be relabeled or
used as v5 results.

V5 must answer one question: as task demand approaches and exceeds the
effective four-layer support capacity, how often do four coordination
strategies install a configuration that satisfies the task under an
independently implemented truth evaluator?

The main figure remains one panel with the four frozen method names. The
independent oracle is a judging mechanism, not a fifth plotted method.

## Scientific invariants

- `gamma = R_req / min(C_t, C_n, C_p)` uses the true initial capacities.
- Gamma is the only controlled variable. For a fixed seed, topology,
  capacities, proposal pool, shared trunk, route/access coupling, and every
  nuisance variable are identical across gamma.
- Every gamma uses exactly the same 100 formal seeds and four paired methods.
- Pilot selection uses only independently computed ground-truth classes and
  separate pilot seeds. It must never execute comparison methods.
- No CSV value is edited; no seed is selected from method outcomes; no result
  is smoothed; no random perturbation is added after execution.
- V5 retains the v4 generator distribution and exact seed-to-environment RNG
  mapping. The review-triggered revision changes evaluation independence and
  provenance, not the curve-generating scenarios.

## Architecture

### Independent truth evaluator

Add `src/simulation/demand_capacity_truth.py`. It may import immutable core
state/proposal data classes and the ground-truth result data class, but it must
not import or call `src.controller.feasibility`, controller selector helpers,
or controller objective helpers.

The module independently:

1. projects one proposal per `(flow, layer)` onto a copied truth state;
2. evaluates application, transport, network, physical, route/access, QoS,
   shared-resource, staleness, and write-set constraints;
3. enumerates all 4096 combinations for the truth oracle;
4. returns the ground-truth conflict class and feasible set; and
5. evaluates the selected post-activation state for the final success verdict.

The independent implementation may share equations documented by the
experiment, but it must not share production evaluation code. Tests must prove
that replacing or patching the controller predicate cannot change truth-oracle
or final-verifier results.

### Controller and execution data flow

The controller continues to coordinate from `snapshot.observed_state` using
the existing production feasibility implementation. The transaction projects
the selected proposals onto `snapshot.true_state`. The post-activation
verifier receives an injected evaluation callback; v5 injects the independent
truth evaluator, while all legacy experiments retain the current production
default.

The v5 generator uses the independent solver for `snapshot.ground_truth`.
No observation or actuation noise is added merely to make Ours fall below the
ceiling. If Ours matches the independently encoded feasibility ceiling, the
paper describes this as cross-implementation validation of exact search within
the simulator, not evidence of real-system optimality.

The generator exposes version `wcnc-final-gamma-v5` but samples physical
environment values from the frozen RNG namespace
`wcnc-final-gamma-environment-v4`. Thus the same `(gamma, seed)` has the same
topology, capacities, candidate resources, and nuisance state as the reviewed
v4 run; only the independent truth/evaluation path changes.

## Complete execution provenance

Before pilot or formal execution, all execution-critical v5 code must be
committed locally. The runner rejects execution if any registered critical
path differs from HEAD.

The raw execution manifest records:

- Git commit and scoped-clean result;
- SHA-256 for the config and every critical source file;
- Python implementation/version;
- installed distribution names/versions and a canonical dependency digest;
- methods, ordered gamma values, seeds, output path, and generator version.

Critical sources include the generator, independent truth evaluator,
controller selector and feasibility code, proposal/core data model,
authorized action/transaction path, shared `run_case`, and v5 runner. The
manifest is written before scenario or method execution. Each scenario and run
row carries its manifest digest.

The formal config binds the accepted pilot config, selection, execution
manifest, source manifest, and generator/truth source digests.

## Registered protocols and output isolation

- Generator version: `wcnc-final-gamma-v5`.
- Pilot config: `configs/exp2_demand_capacity_ratio_pilot_v5.yaml`.
- Formal config: `configs/exp2_demand_capacity_ratio_v5.yaml`.
- Pilot output:
  `results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5/`.
- Formal output: `results/exp2_demand_capacity_v5/`.
- Pilot seeds remain 9000..9009; formal seeds remain 0..99.
- The pilot candidate grid remains 0.8 through 1.4. The predeclared ordered
  prefix through the highest mixed-feasibility point remains the selection
  rule. “All ten pilot seeds were infeasible” must not be generalized into a
  population-wide claim.

V4 output paths are protected as prior results. V5 never overwrites them.

## Aggregation and statistical reporting

Aggregation must fail closed on missing, duplicate, unexpected, or unpaired
rows. Inference is emitted only for a complete paired Cartesian product.

In addition to the existing formula/environment/order checks, v5 aggregation
must:

- verify the complete source/dependency manifest;
- cross-link every run to exactly one decision and at least one event;
- verify run/decision method, scenario, satisfaction, and transaction fields;
- replay the independent oracle from each registered `(gamma, seed)` and
  compare class, feasible count, input/evaluation digest, and fingerprint;
- report unconditional task satisfaction and a report-only
  conditional-on-oracle-feasible rate; and
- retain Wilson 95% intervals for proportions and paired bootstrap plus exact
  McNemar tests. Holm correction is explicitly within gamma only.

The 93% to 98% v4 no-verification increase is historical and must not be
assumed for v5. If a similar paired transition occurs, report the paired
transition counts and test rather than infer improvement from marginal bands.

## Figure contract

Fig.2 remains:

- X: Demand-to-Capacity Ratio;
- Y: Task Satisfaction Rate (%), fixed 0--100%;
- methods in order: Ours, Adjacent-Layer Coordination (ALC), Layer-wise
  Independent, w/o Global Verification;
- line, marker, and low-alpha 95% confidence band using the frozen publication
  style mapping; and
- an annotation or caption statement giving the intrinsically infeasible ratio
  at the largest displayed gamma.

The independently evaluated ceiling and conditional rates are reported in the
experiment report/supplementary evidence, not added as main-figure series.

## Test and execution gates

1. RED tests demonstrate that the current shared evaluator and incomplete
   manifest violate the v5 contract.
2. Unit tests cover independent projection/evaluation and deliberate mutations
   of the production predicate.
3. Legacy cross-layer and robustness suites remain green through default
   evaluator injection.
4. A method-free registered pilot runs in the fresh v5 pilot directory.
5. The formal config is frozen from pilot evidence before any formal method
   outcome exists.
6. V5 execution code is committed and scoped-clean before the one-shot formal
   run.
7. Formal aggregation, replay audit, byte compilation, line-ending scan, and
   `git diff --check` pass before result review.
8. A fresh independent WCNC review must accept the v5 design/results before
   final plotting.
