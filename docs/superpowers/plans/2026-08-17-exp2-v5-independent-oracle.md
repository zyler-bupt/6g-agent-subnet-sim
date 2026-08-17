# Exp2 v5 Independent Oracle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a registered Fig.2 experiment whose success verdict and
ground truth come from an independently encoded evaluator and whose raw results
are bound to the complete execution implementation.

**Architecture:** Keep the four production selectors and v4 scenario
distribution, but add a truth evaluator that imports no controller feasibility
or objective code. Inject it only into v5 ground truth and post-activation
verification. Freeze and commit every execution-critical source before a
method-free pilot and one-shot paired formal run.

**Tech Stack:** Python 3, standard-library `unittest`, YAML, CSV/JSONL,
Matplotlib only in the later publication assembly.

**Spec:**
`docs/superpowers/specs/2026-08-17-exp2-v5-independent-oracle-design.md`

## Global Constraints

- Do not edit, smooth, filter, or relabel any experimental result value.
- Do not choose formal seeds or gamma points from comparison-method outcomes.
- Keep v4 raw and processed directories immutable as rejected review evidence.
- V5 keeps the v4 scenario distribution and exact seed-to-environment mapping;
  it adds no outcome-shaping noise.
- Pilot is ground-truth-only and formal uses seeds 0..99 for all four methods.
- Gamma is the only controlled variable for a paired seed.
- All new shell, config, code, and text files use LF endings.
- Stage and commit only named v5 files; preserve unrelated dirty-tree changes.

---

### Task 1: Independently encoded truth evaluator

**Files:**
- Create: `src/simulation/demand_capacity_truth.py`
- Create: `tests/test_exp2_demand_capacity_v5.py`
- Read: `src/core/cross_layer.py`
- Read: `src/controller/feasibility.py`

**Interfaces:**
- Consumes: `CrossLayerTaskState`, `LayerProposal`, and immutable proposal
  parameter dictionaries.
- Produces:
  `evaluate_truth(state, proposals=(), check_write_sets=True) -> TruthEvaluation`
  and
  `solve_truth(state, proposals) -> GroundTruthResult`.

- [ ] **Step 1: Write RED independence and hand-derived feasibility tests**

  Add tests with literal two-choice, one-flow fixtures that assert capacity,
  QoS, route/access, shared-resource, staleness, and write-set failures. Add an
  import-boundary assertion using `unittest.mock.patch` so forcing
  `src.controller.feasibility.evaluate_cross_layer_combination` to return an
  invalid sentinel cannot alter `evaluate_truth` or `solve_truth`.

- [ ] **Step 2: Verify RED**

  Run:
  `.venv/bin/python -m unittest -v tests.test_exp2_demand_capacity_v5`

  Expected: import failure for the absent truth module.

- [ ] **Step 3: Implement independent projection and evaluation**

  Implement frozen `TruthEdgeMetrics` and `TruthEvaluation` data classes. Copy
  no production helper calls: apply proposal parameters by layer, derive
  throughput/latency/loss/reliability, enforce all documented constraints, and
  return deterministic unique violation strings.

- [ ] **Step 4: Implement independent 4096-way solver**

  Group proposals by `(edge_id, layer)`, enumerate `itertools.product`, evaluate
  each combination with `evaluate_truth`, and construct `GroundTruthResult`.
  The conflict reference is the all-KEEP combination. Select the best feasible
  IDs only for audit, using `(change_count, expected_cost, proposal_ids)`; no
  production controller objective is imported.

- [ ] **Step 5: Verify GREEN and legacy semantic agreement fixtures**

  Run the v5 test module and a small table of hand-derived fixtures through
  both evaluators. Agreement tests are characterization evidence only; the
  mutation test proves runtime independence.

---

### Task 2: Inject independent post-activation verification

**Files:**
- Modify: `src/controller/authorized_actions.py`
- Modify: `experiments/exp2_cross_layer_robustness.py`
- Modify: `tests/test_exp2_demand_capacity_v5.py`
- Test: `tests/test_stage4_cross_layer_conflict.py`
- Test: `tests/test_exp2_robustness.py`

**Interfaces:**
- Consumes: a callable with signature
  `evaluator(CrossLayerTaskState, Iterable[LayerProposal]) -> evaluation`.
- Produces: optional keyword
  `cross_layer_evaluator` on `AuthorizedActionExecutor` and optional keyword
  `post_evaluator` on `run_case`; both default to the existing production
  evaluator.

- [ ] **Step 1: Write RED injection test**

  Execute a minimal transaction with a sentinel evaluator that rejects the
  post-activation state. Assert rollback, failed transaction, and sentinel
  violation propagation. Separately assert a legacy call without the keyword
  retains current behavior.

- [ ] **Step 2: Verify RED**

  Run only the two new tests. Expected: unexpected-keyword failure.

- [ ] **Step 3: Add evaluator dependency injection**

  Store the callable in `PostActivationCrossLayerVerifier`; invoke it after
  activation and in the fallback post-evaluation path. Thread the optional
  keyword through `AuthorizedActionExecutor.execute` and `run_case` without
  changing legacy defaults.

- [ ] **Step 4: Verify GREEN and compatibility**

  Run:
  `.venv/bin/python -m unittest -v tests.test_exp2_demand_capacity_v5 tests.test_stage4_cross_layer_conflict tests.test_exp2_robustness`

  Expected: all tests pass and legacy result modes remain unchanged.

---

### Task 3: V5 generator and registered protocol

**Files:**
- Modify: `src/simulation/demand_capacity_ratio.py`
- Modify: `experiments/exp2_demand_capacity_ratio.py`
- Create: `configs/exp2_demand_capacity_ratio_pilot_v5.yaml`
- Create: `configs/exp2_demand_capacity_ratio_v5.yaml`
- Modify: `tests/test_exp2_demand_capacity_v5.py`

**Interfaces:**
- Consumes: `solve_truth` and `evaluate_truth` from Task 1.
- Produces: generator version `wcnc-final-gamma-v5`, fresh v5 pilot/formal
  paths, and formal `run_case(..., post_evaluator=evaluate_truth)` calls.

- [ ] **Step 1: Write RED v5 protocol tests**

  Assert new output isolation, exact pilot/formal seeds, forbidden pilot
  methods, generator version, 4096 truth combinations, true-state oracle use,
  paired nuisance invariance, and rejection of v4 output descendants.

- [ ] **Step 2: Verify RED**

  Run the v5 protocol tests. Expected: v4 version/path and shared oracle cause
  failures.

- [ ] **Step 3: Switch generator truth and runner verification**

  Preserve every v4 sampling range and use the explicit frozen RNG namespace
  `wcnc-final-gamma-environment-v4` so seed-to-environment values stay exact.
  Use `solve_truth(deepcopy(state), deepcopy(proposals))`; pass `evaluate_truth`
  to formal `run_case`. Rename protocol/result identifiers to v5 without
  changing frozen paper method names.

- [ ] **Step 4: Add v5 configs and strict validation**

  Pilot ratios are 0.8..1.4 and seeds 9000..9009 with no methods. Formal seeds
  are 0..99; its ratio list must exactly match the accepted v5 pilot selection.
  Both output paths are fresh and protect every v1--v4 result directory.

- [ ] **Step 5: Verify GREEN**

  Run v5 tests, shared Fig.2 tests, and byte compilation. Do not execute a
  method-output pilot.

---

### Task 4: Complete source and environment provenance

**Files:**
- Modify: `experiments/exp2_demand_capacity_ratio.py`
- Modify: `scripts/aggregate_exp2_demand_capacity.py`
- Modify: `tests/test_exp2_demand_capacity_v5.py`

**Interfaces:**
- Produces:
  `build_execution_source_manifest(config_path) -> dict`,
  `validate_execution_source_manifest(manifest) -> None`, and raw
  `execution_source_manifest.json` plus its SHA-256 in the execution manifest.

- [ ] **Step 1: Write RED provenance tests**

  Assert the manifest contains Git commit, scoped-clean flag, Python version,
  canonical installed-distribution digest, and SHA-256 entries for generator,
  truth evaluator, selector, production feasibility, proposal/core models,
  authorized action/transaction path, shared run-case module, and v5 runner.
  Mutating a copied critical file path or digest must fail before output rows.

- [ ] **Step 2: Verify RED**

  Run the provenance tests. Expected: missing manifest builder/fields.

- [ ] **Step 3: Implement fail-closed source manifest**

  Resolve repository-relative paths, hash bytes in sorted path order, record
  `git rev-parse HEAD`, and use scoped `git diff --quiet HEAD -- <paths>` plus
  `git diff --cached --quiet HEAD -- <paths>`. Canonicalize installed
  distributions as sorted lowercase `name==version` strings and hash the JSON
  list. Write manifests before pilot/formal scenario generation.

- [ ] **Step 4: Bind pilot and formal protocols**

  Pilot selection records source-manifest SHA-256. Formal config records and
  validates the accepted pilot config, pilot selection, pilot execution/source
  manifests, and current critical-source digest.

- [ ] **Step 5: Verify GREEN**

  Run provenance tests and a temporary-directory protocol smoke. Confirm a
  critical-source mutation fails closed and leaves no run CSV.

---

### Task 5: Aggregation replay and cross-artifact audit

**Files:**
- Modify: `scripts/aggregate_exp2_demand_capacity.py`
- Modify: `tests/test_exp2_demand_capacity_v5.py`

**Interfaces:**
- Consumes: raw `runs.csv`, `scenarios.jsonl`, `decisions.jsonl`,
  `events.jsonl`, configuration, execution manifest, and source manifest.
- Produces: summary/class/paired/conditional CSVs and a fail-closed integrity
  JSON containing replay, decision-link, event-link, and provenance findings.

- [ ] **Step 1: Write RED artifact-link tests**

  Create a complete literal four-method fixture, then independently remove or
  corrupt one decision, event, satisfaction flag, method, fingerprint, source
  digest, and oracle digest. Each mutation must return integrity FAIL without
  crashing or emitting paired inference.

- [ ] **Step 2: Write RED replay test**

  Generate one registered v5 scenario and corrupt its stored truth class or
  feasible count. Assert replay catches both changes.

- [ ] **Step 3: Implement cross-link and replay audits**

  Index runs/decisions by `run_id`, require one decision and one-or-more events,
  compare shared fields, validate the source manifest, regenerate each unique
  `(gamma, seed)` under the registered config, and compare scenario fingerprint,
  truth class, feasible count, oracle input hash, and evaluation hash.

- [ ] **Step 4: Add conditional descriptive output**

  Write `conditional_feasible_summary.csv` with successful/feasible samples
  and Wilson interval. Keep unconditional task satisfaction as the only main
  Fig.2 metric. State that Holm families are per gamma.

- [ ] **Step 5: Verify GREEN and malformed-input behavior**

  Run all aggregation tests, including incomplete Cartesian fixtures. Expected:
  clean fixture PASS; every mutation FAIL with a named diagnostic and no crash.

---

### Task 6: Freeze code, run pilot, and freeze formal configuration

**Files:**
- Update: `.superpowers/sdd/wcnc-v2-plan/progress.md`
- Update: `.superpowers/sdd/wcnc-v2-plan/task-2-report.md`
- Generate: `results/wcnc_pilot_v2/exp2_demand_capacity_ratio_v5/`
- Finalize: `configs/exp2_demand_capacity_ratio_v5.yaml`

**Interfaces:**
- Consumes: Tasks 1--5 with all focused/legacy tests green.
- Produces: immutable implementation commit, registered oracle-only pilot, and
  a formal YAML whose range/hash bindings are fixed before method outcomes.

- [ ] **Step 1: Run pre-freeze verification**

  Run v5, shared Fig.2, legacy cross-layer, and robustness suites; byte-compile
  all touched Python files; scan LF endings; run `git diff --check`.

- [ ] **Step 2: Commit only v5 implementation paths**

  Inspect `git diff` for every named file, stage explicit paths only, and
  create a local commit titled `Implement independent Fig.2 v5 protocol`.
  Confirm all execution-critical paths are clean against that commit.

- [ ] **Step 3: Run the registered method-free pilot once**

  Execute the v5 pilot config into its fresh directory. Verify zero method
  rows, exact seed/grid completeness, source manifest, 4096 oracle counts, and
  class-only retention evidence.

- [ ] **Step 4: Freeze formal range and bindings**

  Copy only the retained ordered gamma prefix into the formal YAML and add the
  accepted pilot config/selection/execution/source hashes. Do not inspect any
  method outcome. Commit the frozen config and report evidence.

---

### Task 7: One-shot formal run, audit, and independent result review

**Files:**
- Generate: `results/exp2_demand_capacity_v5/`
- Update: `.superpowers/sdd/wcnc-v2-plan/progress.md`
- Update: `.superpowers/sdd/wcnc-v2-plan/task-2-report.md`

**Interfaces:**
- Consumes: frozen committed v5 code/config and accepted pilot bindings.
- Produces: 100 paired scenarios per retained gamma, processed statistics,
  strict replay audit, and reviewer verdict.

- [ ] **Step 1: Run formal exactly once**

  Execute the registered v5 formal command. Preserve failures and all
  infeasible scenarios. Do not rerun individual seeds or edit raw files.

- [ ] **Step 2: Aggregate and replay audit**

  Require exact Cartesian completeness, balanced method order, full source
  binding, independent oracle replay, and run/decision/event linkage. Record
  raw and processed SHA-256 values.

- [ ] **Step 3: Interpret without curve shaping**

  Report unconditional counts, truth classes, conditional feasible rates,
  paired transitions/tests, and any nonmonotonic point. Freeze the highest
  displayed gamma from the method-free pilot, even if formal curves are less
  visually convenient.

- [ ] **Step 4: Request independent WCNC review**

  Give a fresh read-only reviewer the design, code, manifests, raw/processed
  evidence, and exact claim. A Critical or Important scientific finding blocks
  final plotting; valid findings trigger a new version rather than raw edits.

- [ ] **Step 5: Checkpoint handoff**

  If accepted, mark Task 2 complete and prepare the processed Fig.2 CSV for
  final publication assembly. Commit code/report changes with explicit paths;
  do not stage ignored raw artifacts or unrelated workspace files.
