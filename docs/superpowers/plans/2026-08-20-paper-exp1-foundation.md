# Paper Experiment Foundation and Exp.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared paper-experiment protocol and complete the paired four-method Exp.1 pilot with raw CSV, cluster-bootstrap aggregation, sanity checks, and Fig.1 PDF/PNG outputs.

**Architecture:** Add a small protocol/metrics layer around the existing simulator, then implement a deterministic formation scenario and method scheduler that uses common primitive costs and the existing transactional verifier. Existing Stage 2--5 APIs stay intact; final results use new canonical output paths.

**Tech Stack:** Python 3, asyncio, dataclasses, PyYAML, NumPy, Matplotlib, unittest.

**Spec:** `docs/superpowers/specs/2026-08-20-paper-experiments-finalization-design.md`

## Global Constraints

- Reuse the existing simulator and transaction/verifier path; do not build a production subsystem.
- All methods at one trial point share topology, DAG, placement, QoS, churn trace, primitive costs, and seeds.
- Pilot is exactly 5 topology seeds and 2 events per seed.
- Exp.1 methods are `proposed`, `proposed_without_batch`, `cspf`, and `a1_agent_embedded`.
- No method-specific latency multiplier, success probability, or result post-processing is permitted.
- Authoritative pilot output is under `results/raw/pilot/exp1` and `results/aggregated/pilot/exp1`.
- Tests precede behavior changes; existing Stage 2--5 regression tests must remain green.

---

### Task 1: Canonical Protocol, Method Registry, and Raw Schema

**Files:**
- Create: `experiments/paper_protocol.py`
- Create: `src/metrics/paper.py`
- Create: `configs/paper_experiments.yaml`
- Test: `tests/test_paper_protocol.py`

**Interfaces:**
- Produces: `RunMode`, `ModeSpec`, `METHODS`, `MethodMetadata`, `PaperTrial`, `write_paper_trials()`, `stable_fingerprint()`.
- Consumes: standard-library dataclasses, hashing, CSV, JSON, and PyYAML config values.

- [ ] **Step 1: Write failing protocol and schema tests**

```python
class PaperProtocolTests(unittest.TestCase):
    def test_pilot_and_paper_mode_counts(self):
        self.assertEqual(mode_spec("pilot").topology_seeds, 5)
        self.assertEqual(mode_spec("pilot").events_per_seed, 2)
        self.assertEqual(mode_spec("paper").topology_seeds, 30)
        self.assertEqual(mode_spec("paper").rate_events_per_seed, 5)

    def test_adapted_labels_and_sources_are_canonical(self):
        self.assertEqual(METHODS["a1_agent_embedded"].label, "A1-Agent-Embedded*")
        self.assertEqual(METHODS["a1_agent_embedded"].source, "A1 Agent")
        self.assertTrue(METHODS["a1_agent_embedded"].adapted)

    def test_paper_trial_contains_required_columns(self):
        required = {"experiment", "seed", "event_id", "method_id", "success",
                    "formation_latency_ms", "rollback_count", "stale_state_detected"}
        self.assertTrue(required.issubset(PaperTrial.__dataclass_fields__))
```

- [ ] **Step 2: Run the tests and confirm missing-module failure**

Run: `python3 -m unittest tests.test_paper_protocol -v`

Expected: FAIL because `experiments.paper_protocol` and `src.metrics.paper` do not exist.

- [ ] **Step 3: Implement the minimal shared interfaces**

```python
class RunMode(str, Enum):
    PILOT = "pilot"
    PAPER = "paper"

@dataclass(frozen=True)
class ModeSpec:
    topology_seeds: int
    events_per_seed: int
    rate_events_per_seed: int

def mode_spec(mode: str | RunMode) -> ModeSpec:
    return {
        RunMode.PILOT: ModeSpec(5, 2, 2),
        RunMode.PAPER: ModeSpec(30, 1, 5),
    }[RunMode(mode)]
```

Define the complete method registry from the spec and a frozen `PaperTrial`
dataclass with every canonical raw field. Optional experiment-specific values
use `str | None`, `float | None`, or `bool | None`; they are serialized as empty
CSV cells, not fabricated zeros. Include `task_received_at`,
`event_occurred_at`, and `stable_verify_finished_at` as timing provenance so
reported intervals can be independently recomputed. `write_paper_trials()`
always writes the canonical column order.

- [ ] **Step 4: Add the complete shared YAML protocol**

```yaml
mode:
  pilot: {topology_seeds: 5, events_per_seed: 2}
  paper: {topology_seeds: 30, continuous_events_per_seed: 1, rate_events_per_seed: 5}
exp1:
  task_sizes: [8, 12, 16, 20, 24, 28, 32]
  churn_percent: [0, 5, 10, 15, 20, 30]
  churn_task_size: 24
  num_gateways: 12
  average_degree: [3, 4]
```

- [ ] **Step 5: Run protocol tests**

Run: `python3 -m unittest tests.test_paper_protocol -v`

Expected: PASS.

- [ ] **Step 6: Commit the shared protocol**

```bash
git add -- experiments/paper_protocol.py src/metrics/paper.py configs/paper_experiments.yaml tests/test_paper_protocol.py
git commit -m "Add canonical paper experiment protocol"
```

### Task 2: Deterministic Mesh Topology and Modular Fork-Join DAG

**Files:**
- Create: `src/simulation/paper_scenarios.py`
- Test: `tests/test_paper_scenarios.py`

**Interfaces:**
- Consumes: `ScenarioConfig`, `ScenarioSnapshot`, and core `TaskSpec`/`BusinessEdge` models.
- Produces: `PaperTopology`, `FormationScenarioSnapshot`, `generate_mesh_topology(seed, num_gateways=12)`, `generate_modular_task(task_size, topology, seed)`.

- [ ] **Step 1: Write failing deterministic scenario tests**

```python
class PaperFormationScenarioTests(unittest.TestCase):
    def test_mesh_has_required_attributes_and_degree(self):
        topology = generate_mesh_topology(7, num_gateways=12)
        degrees = topology.degrees()
        self.assertTrue(all(2 <= value <= 5 for value in degrees.values()))
        self.assertGreaterEqual(sum(degrees.values()) / 12.0, 3.0)
        self.assertLessEqual(sum(degrees.values()) / 12.0, 4.0)
        self.assertTrue(all(5.0 <= link.delay_ms <= 30.0 for link in topology.links))
        self.assertTrue(all(50.0 <= link.bandwidth_mbps <= 200.0 for link in topology.links))

    def test_modular_task_has_forks_merges_and_cross_gateway_edges(self):
        first = generate_formation_snapshot(task_size=24, seed=11, event_id=0)
        second = generate_formation_snapshot(task_size=24, seed=11, event_id=0)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(first.has_fork)
        self.assertTrue(first.has_merge)
        self.assertGreaterEqual(first.cross_gateway_edge_ratio, 0.60)
        self.assertLessEqual(first.cross_gateway_edge_ratio, 0.70)
```

- [ ] **Step 2: Run the scenario tests and confirm failure**

Run: `python3 -m unittest tests.test_paper_scenarios -v`

Expected: FAIL because the paper scenario module is absent.

- [ ] **Step 3: Implement topology generation**

Create a connected 12-node ring first, then add seed-shuffled nonduplicate
chords until total degree is within the configured 3--4 average range. Sample
link delay, bandwidth, loss, and jitter once from a local `random.Random(seed)`
and include every sampled field in the topology fingerprint.

- [ ] **Step 4: Implement modular fork-join generation**

Partition topologically ordered Agents into modules. Within each module create
a split, two or three parallel branch paths, and a merge; connect adjacent
modules through their merge/split. Add only forward edges until average
out-degree lies in 1.5--2.0. Assign Agents to Gateways with a deterministic
search that targets 60--70% cross-Gateway edges without changing the DAG.

- [ ] **Step 5: Run scenario and legacy generator tests**

Run: `python3 -m unittest tests.test_paper_scenarios tests.test_stage3_business_elasticity -v`

Expected: PASS.

- [ ] **Step 6: Commit the scenario generator**

```bash
git add -- src/simulation/paper_scenarios.py tests/test_paper_scenarios.py
git commit -m "Add paired paper topology and modular DAG generator"
```

### Task 3: Common Formation Primitive Model and Four Strategies

**Files:**
- Create: `src/controller/formation_strategies.py`
- Modify: `experiments/exp1_initial_formation.py`
- Test: `tests/test_paper_exp1.py`

**Interfaces:**
- Consumes: `FormationScenarioSnapshot`, existing `AgentController.compile_task_subnet()`, `TransactionExecutor`, and common verifier.
- Produces: `FormationTrace`, `FormationOutcome`, `run_formation_method(snapshot, method_id)`.

- [ ] **Step 1: Write failing strategy-boundary tests**

```python
class FormationStrategyTests(unittest.IsolatedAsyncioTestCase):
    async def test_proposed_and_no_batch_share_paths_rules_and_verifier(self):
        snapshot = generate_formation_snapshot(20, 4, 0)
        proposed = await run_formation_method(snapshot, "proposed")
        no_batch = await run_formation_method(snapshot, "proposed_without_batch")
        self.assertEqual(proposed.path_fingerprint, no_batch.path_fingerprint)
        self.assertEqual(proposed.rule_fingerprint, no_batch.rule_fingerprint)
        self.assertEqual(proposed.verifier_fingerprint, no_batch.verifier_fingerprint)
        self.assertEqual(proposed.trace.stage_mode, "parallel_gateway_batch")
        self.assertEqual(no_batch.trace.stage_mode, "sequential_gateway")

    async def test_cspf_and_a1_measure_complete_stable_formation(self):
        snapshot = generate_formation_snapshot(16, 8, 1)
        for method in ("cspf", "a1_agent_embedded"):
            outcome = await run_formation_method(snapshot, method)
            self.assertGreater(outcome.formation_latency_ms, 0.0)
            self.assertTrue(outcome.stable_verification_attempted)
            self.assertEqual(outcome.required_edge_count, outcome.processed_edge_count)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python3 -m unittest tests.test_paper_exp1.FormationStrategyTests -v`

Expected: FAIL because formation strategies are missing.

- [ ] **Step 3: Implement shared primitive cost sampling**

```python
@dataclass(frozen=True)
class FormationPrimitiveCosts:
    endpoint_resolution_ms: Mapping[str, float]
    path_computation_ms: Mapping[str, float]
    rule_compile_ms: Mapping[str, float]
    gateway_stage_ms: Mapping[str, float]
    gateway_verify_ms: Mapping[str, float]
    gateway_activate_ms: Mapping[str, float]
    stable_verify_ms: float
```

Generate these costs in the scenario snapshot from topology/link attributes.
Every method reads the same mapping. Total time is the makespan of each method's
operation dependency graph: sum for sequential operations, maximum for parallel
Gateway batches, and shared fixed graph-analysis/binding operations where the
method performs them.

- [ ] **Step 4: Implement the four method schedules**

Proposed uses one DAG compile and parallel Gateway groups. The no-batch variant
uses the same candidate subnet but sums Gateway stage/activate operations.
CSPF performs constrained path calculation and its rule/deploy/verify sequence
for every business edge. A1 additionally resolves endpoints and completes the
full network procedure before starting the next communication operation.

All methods invoke the same final stable-state verifier. Churn is a pre-sampled
time-indexed state trace; a method observes it according to its operation
schedule. The verifier decides reachability, installed rules, QoS, partial
activation, and stable-state validity.

- [ ] **Step 5: Run strategy tests and formation regressions**

Run: `python3 -m unittest tests.test_paper_exp1 tests.test_stage3_business_elasticity.InitialBuildTransactionTests -v`

Expected: PASS.

- [ ] **Step 6: Commit formation methods**

```bash
git add -- src/controller/formation_strategies.py experiments/exp1_initial_formation.py tests/test_paper_exp1.py
git commit -m "Implement paired formation strategies"
```

### Task 4: Exp.1 Runner and Canonical Raw Output

**Files:**
- Modify: `experiments/exp1_initial_formation.py`
- Modify: `src/metrics/paper.py`
- Test: `tests/test_paper_exp1.py`

**Interfaces:**
- Consumes: `mode_spec()`, `generate_formation_snapshot()`, `run_formation_method()`, `PaperTrial`.
- Produces: `run_exp1(mode, output_root, seeds=None) -> list[PaperTrial]` and `results/raw/<mode>/exp1/trials.csv`.

- [ ] **Step 1: Add failing paired-grid and raw-field tests**

```python
async def test_pilot_has_complete_paired_method_grid(self):
    with tempfile.TemporaryDirectory() as directory:
        rows = await run_exp1("pilot", Path(directory), task_sizes=(8,), churn_points=(0,))
    self.assertEqual(len(rows), 5 * 2 * 4 * 2)  # latency and churn series
    by_trial = defaultdict(set)
    for row in rows:
        by_trial[(row.trial_id, row.scenario_fingerprint)].add(row.method_id)
    self.assertTrue(all(methods == set(EXP1_METHODS) for methods in by_trial.values()))
```

- [ ] **Step 2: Run the new runner test and confirm failure**

Run: `python3 -m unittest tests.test_paper_exp1.PaperExp1RunnerTests -v`

Expected: FAIL because `run_exp1` is absent.

- [ ] **Step 3: Implement deterministic run ordering and mapping**

Use task-size series `[8,12,16,20,24,28,32]` and churn series
`[0,5,10,15,20,30]`. Pilot runs two events for every topology seed. Every
method is run from a fresh instantiation of the same immutable snapshot. Map
the outcome to all canonical raw columns, including method source/adaptation,
fingerprints, rule/Gateway/flow counts, rollback, stale state, and control
messages.

- [ ] **Step 4: Run Exp.1 tests**

Run: `python3 -m unittest tests.test_paper_protocol tests.test_paper_scenarios tests.test_paper_exp1 -v`

Expected: PASS.

- [ ] **Step 5: Commit the runner**

```bash
git add -- experiments/exp1_initial_formation.py src/metrics/paper.py tests/test_paper_exp1.py
git commit -m "Add canonical Exp1 runner and raw rows"
```

### Task 5: Cluster Bootstrap, Aggregation, and Sanity Checks

**Files:**
- Create: `scripts/paper_statistics.py`
- Create: `scripts/aggregate_results.py`
- Create: `scripts/sanity_check_results.py`
- Test: `tests/test_paper_statistics.py`
- Test: `tests/test_paper_sanity.py`

**Interfaces:**
- Produces: `cluster_bootstrap_interval(rows, value, cluster, statistic, iterations, seed)`, `aggregate_experiment()`, `SanityFinding`, `check_results()`.
- Consumes: canonical `PaperTrial` CSV.

- [ ] **Step 1: Write failing bootstrap and sanity tests**

```python
def test_cluster_bootstrap_resamples_seeds_not_events(self):
    rows = [{"seed": 0, "v": 0.0}, {"seed": 0, "v": 100.0},
            {"seed": 1, "v": 50.0}, {"seed": 1, "v": 50.0}]
    interval = cluster_bootstrap_interval(rows, "v", "seed", np.mean, 2000, 9)
    self.assertEqual(interval.cluster_count, 2)
    self.assertLessEqual(interval.lower, interval.estimate)
    self.assertGreaterEqual(interval.upper, interval.estimate)

def test_zero_latency_and_broken_pairing_are_errors(self):
    findings = check_results(rows_with_zero_latency_and_missing_method())
    self.assertIn("ERROR", {item.level for item in findings})
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python3 -m unittest tests.test_paper_statistics tests.test_paper_sanity -v`

Expected: FAIL because statistics and sanity modules are missing.

- [ ] **Step 3: Implement deterministic seed-cluster bootstrap**

Group raw rows by topology seed, sample seed IDs with replacement using
`numpy.random.default_rng(seed)`, concatenate all events in selected clusters,
and recompute the statistic. Return estimate, lower 2.5th percentile, upper
97.5th percentile, cluster count, and sample count. Aggregation writes mean,
P50, P95, interval bounds, counts, and applicable rates without excluding
failures from rate denominators.

- [ ] **Step 4: Implement schema, fairness, and trend findings**

Emit errors for missing columns, mismatched fingerprints, incomplete method
sets, impossible ratios, zero latency, and constant-output implementation
anomalies. Emit warnings for all methods at 100%, all below 20%, or Exp.1
latency that does not generally rise with task size. Do not flag Local-Only or
CSPF merely for lower latency in their natural domains.

- [ ] **Step 5: Run statistics and sanity tests**

Run: `python3 -m unittest tests.test_paper_statistics tests.test_paper_sanity -v`

Expected: PASS.

- [ ] **Step 6: Commit aggregation and sanity checking**

```bash
git add -- scripts/paper_statistics.py scripts/aggregate_results.py scripts/sanity_check_results.py tests/test_paper_statistics.py tests/test_paper_sanity.py
git commit -m "Add clustered aggregation and experiment sanity checks"
```

### Task 6: Shared Plot Style, Fig.1, and Pilot Orchestration

**Files:**
- Modify: `scripts/paper_style.py`
- Create: `scripts/plot_paper_figures.py`
- Create: `scripts/run_pilot.py`
- Test: `tests/test_paper_plotting.py`
- Test: `tests/test_paper_runner.py`

**Interfaces:**
- Consumes: aggregated Exp.1 CSV and canonical YAML.
- Produces: Fig.1a/Fig.1b PDF and PNG, pilot summary Markdown/JSON, phase exit status.

- [ ] **Step 1: Write failing style and file tests**

```python
def test_method_style_registry_has_canonical_cross_figure_entries(self):
    self.assertEqual(METHOD_STYLES["proposed"].label, "Proposed")
    self.assertEqual(METHOD_STYLES["cspf"].marker, "s")
    self.assertNotEqual(METHOD_STYLES["proposed"].linestyle,
                        METHOD_STYLES["proposed_without_batch"].linestyle)

def test_exp1_plot_writes_vector_pdf_and_png(self):
    paths = plot_exp1(fixture_summary_path, output_dir)
    self.assertTrue((output_dir / "Fig1a_Formation_Latency.pdf").exists())
    self.assertTrue((output_dir / "Fig1b_Formation_Success.png").exists())
    self.assertTrue((output_dir / "Fig1a_Formation_Latency.pdf").read_bytes().startswith(b"%PDF"))
```

- [ ] **Step 2: Run plotting tests and confirm failure**

Run: `python3 -m unittest tests.test_paper_plotting tests.test_paper_runner -v`

Expected: FAIL because the unified plotting and pilot runners are absent.

- [ ] **Step 3: Implement the canonical style registry**

Use Times New Roman with Liberation Serif and DejaVu Serif fallbacks, 9 pt axis
labels, 8 pt ticks, 8 pt legend, line width 1.7, marker size 4.5, confidence
alpha 0.15, white axes, and horizontal dashed grid only. Use a single method
registry for every later figure.

- [ ] **Step 4: Implement Fig.1 and pilot orchestration**

Fig.1a reads latency mean/lower/upper rows by task size. Fig.1b reads formation
success rows by churn percentage. Both use 0--100% for rates unless an explicit
axis range remains clear. `run_pilot.py --experiments exp1` runs Exp.1, writes
canonical raw data, aggregates, checks sanity, plots, and writes
`results/aggregated/pilot/exp1/PILOT_SUMMARY.md`; it exits nonzero on errors.

- [ ] **Step 5: Run all Phase 1 tests**

Run: `python3 -m unittest tests.test_paper_protocol tests.test_paper_scenarios tests.test_paper_exp1 tests.test_paper_statistics tests.test_paper_sanity tests.test_paper_plotting tests.test_paper_runner -v`

Expected: PASS.

- [ ] **Step 6: Commit plotting and pilot entry point**

```bash
git add -- scripts/paper_style.py scripts/plot_paper_figures.py scripts/run_pilot.py tests/test_paper_plotting.py tests/test_paper_runner.py
git commit -m "Add Exp1 pilot aggregation and paper plots"
```

### Task 7: Execute, Audit, and Publish Exp.1 Pilot

**Files:**
- Generate: `results/raw/pilot/exp1/trials.csv`
- Generate: `results/aggregated/pilot/exp1/summary.csv`
- Generate: `results/aggregated/pilot/exp1/PILOT_SUMMARY.md`
- Generate: `results/paper_figures/Fig1a_Formation_Latency.{pdf,png}`
- Generate: `results/paper_figures/Fig1b_Formation_Success.{pdf,png}`

**Interfaces:**
- Consumes: the complete Phase 1 implementation.
- Produces: an audited pilot decision that either passes or identifies one shared scenario recalibration.

- [ ] **Step 1: Run the Exp.1 pilot from an empty authoritative Exp.1 pilot directory**

Run: `python3 scripts/run_pilot.py --experiments exp1`

Expected: 5 seeds, 2 events, complete four-method pairing, raw/aggregate/figure outputs, and no sanity errors.

- [ ] **Step 2: Inspect the generated ranking and trend report**

Run: `python3 scripts/sanity_check_results.py --input results/raw/pilot/exp1/trials.csv --experiment exp1 --output results/aggregated/pilot/exp1/sanity.json`

Expected: latency generally rises with task size; no method has constant or zero latency. Any all-success warning at zero/low churn is acceptable only if higher churn differentiates methods.

- [ ] **Step 3: If stress calibration is required, change only shared config and rerun every method**

Edit only `configs/paper_experiments.yaml` scenario ranges, remove the old
authoritative Exp.1 pilot output through the runner's `--replace-pilot` guarded
option, and rerun Step 1. Never rerun a single method.

- [ ] **Step 4: Run Phase 1 and existing regression verification**

Run: `python3 -m unittest tests.test_paper_protocol tests.test_paper_scenarios tests.test_paper_exp1 tests.test_paper_statistics tests.test_paper_sanity tests.test_paper_plotting tests.test_paper_runner tests.test_stage3_business_elasticity tests.test_stage4_cross_layer_conflict tests.test_stage5_failure_reconfiguration -v`

Expected: PASS.

- [ ] **Step 5: Commit and push the audited Exp.1 pilot**

```bash
git add -- configs/paper_experiments.yaml results/raw/pilot/exp1/trials.csv results/aggregated/pilot/exp1 results/paper_figures/Fig1a_Formation_Latency.pdf results/paper_figures/Fig1a_Formation_Latency.png results/paper_figures/Fig1b_Formation_Success.pdf results/paper_figures/Fig1b_Formation_Success.png
git commit -m "Run and audit final Exp1 pilot"
git push origin codex/semantic-controller
```
