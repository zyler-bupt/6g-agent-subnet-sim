# Exp1 Transactional Formation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separately versioned, real-netns Exp1 transactional-robustness arm that measures method-owned formation and time to first verified-correct formation under deployment faults without importing Exp2 cross-layer conflict logic.

**Architecture:** A pure deterministic protocol module owns fault schedules, transaction state, and method retry scopes. A measured-netns runner adapts existing `ProcessNetnsTopology` plans to a common prepare/commit/abort backend, writes immutable attempt-level provenance, and invokes the existing common ping/iperf3 verifier only after a complete commit. Canonical aggregation, audit, plotting, and the remote launcher treat the existing nominal arm and new `exp1_transactional_v1` arm as separate hashed sources.

**Tech Stack:** Python 3.10, dataclasses, unittest, Linux network namespaces, iproute2 (`ip`, `tc`, `nsenter`, `unshare`), ping, iperf3, CSV/JSONL, PyYAML, matplotlib, Bash.

**Spec:** `docs/superpowers/specs/2026-08-27-exp1-transactional-formation-design.md`

## Global Constraints

- Preserve the existing 750-row nominal Exp1 raw artifact byte-for-byte.
- Exp1 transactional code must not import or call Exp2 oracle, gamma generation, proposal enumeration, or `evaluate_cross_layer_combination`.
- Methods are exactly `proposed`, `cspf`, and `global_sfc_embedding`.
- Agent counts are exactly `4, 8, 12, 16, 20`; formal seeds are `0:49`; pilot seeds are `9000:9019`.
- Transactional fault classes are exactly `stale_version`, `prepare_ack_timeout`, and `command_rejection`.
- All methods use the same logical fault schedule, netns backend, low-level transaction API, deadlines, and final ping/iperf3 verifier.
- CSPF may parallelize independent flows; Global SFC may parallelize independent chains while preserving hop order.
- No sleep or formula-derived method latency may be added. A prepare timeout must arise from the common asynchronous ACK deadline.
- Every failure, timeout, failed attempt, abort, rollback, and retry remains in raw provenance.
- The formal transactional grid contains exactly 2250 trial rows; the pilot contains exactly 900 trial rows.
- Formal artifacts use `results/paper/wcnc_final_v3/raw/exp1_transactional/` and never overwrite `raw/exp1/`.
- Existing user changes in `.gitignore`, `README.md`, untracked documents, and historical results are not modified or staged.

## File Structure

- Create `src/controller/formation_transactions.py`: pure transaction/fault state, method scope selection, deterministic fingerprints, no netns or Exp2 imports.
- Create `experiments/exp1_transactional_formation.py`: measured-netns runner and raw event writer; reuse existing topology and final verifier primitives.
- Create `configs/exp1_transactional_formation_v1.yaml`: frozen 2250-row formal protocol.
- Create `configs/exp1_transactional_formation_pilot_v1.yaml`: frozen 900-row pilot protocol.
- Create `tests/test_exp1_transactional_formation.py`: pure unit, runner-contract, and command-plan tests.
- Modify `experiments/exp1_netns_verified_formation.py`: expose transaction-safe staging/readback helpers without changing nominal execution behavior.
- Modify `experiments/paper_protocol.py`: register the arm ID, display labels, and exact method/fault sets.
- Modify `configs/wcnc_final_v3_raw_schema.json`: add required transactional trial properties.
- Modify `scripts/aggregate_wcnc_final_v3.py`: aggregate transactional metrics and paired differences.
- Modify `scripts/plot_wcnc_final_v3.py`: generate transactional time-to-correct and rollback/waste figures from aggregate CSV only.
- Modify `scripts/audit_wcnc_final_v3.py`: audit 2250-row grid, attempt provenance, source separation, hashes, and Exp2 isolation.
- Modify `scripts/run_wcnc_final_v3_remote.sh`: resumable pilot/formal arm plus safe continuation after a complete audited nominal Exp1 artifact.
- Modify `docs/wcnc_final_v3.md`: execution commands, result interpretation, and non-overlap with Exp2.

---

### Task 1: Deterministic fault and transaction protocol

**Files:**
- Create: `src/controller/formation_transactions.py`
- Create: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Produces: `FaultClass`, `FormationFaultSchedule`, `TransactionPhase`, `TransactionAttempt`, `build_fault_schedule()`, `task_descendant_closure()`, and `retry_scope_for_method()`.
- Consumes: `FormationEdge`-compatible values with `edge_id`, `source_index`, and `target_index` attributes.

- [ ] **Step 1: Write failing deterministic-schedule and scope tests**

```python
class FormationTransactionProtocolTests(unittest.TestCase):
    def test_fault_schedule_is_paired_and_method_blind(self):
        left = build_fault_schedule("stale_version", 12, 7, edges)
        right = build_fault_schedule("stale_version", 12, 7, edges)
        self.assertEqual(left, right)
        self.assertNotIn("method", left.fingerprint_payload())

    def test_proposed_uses_descendants_but_baselines_do_not(self):
        self.assertEqual(
            retry_scope_for_method("proposed", "e-b", edges),
            frozenset({"e-b", "e-d"}),
        )
        self.assertEqual(
            retry_scope_for_method("cspf", "e-b", edges),
            frozenset({"e-b"}),
        )
        self.assertEqual(
            retry_scope_for_method("global_sfc_embedding", "e-b", edges),
            frozenset({"chain:e-a,e-b,e-d"}),
        )
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
python -m unittest tests.test_exp1_transactional_formation.FormationTransactionProtocolTests -v
```

Expected: import failure because `src.controller.formation_transactions` does not exist.

- [ ] **Step 3: Implement immutable protocol types and deterministic selection**

```python
class FaultClass(str, Enum):
    STALE_VERSION = "stale_version"
    PREPARE_ACK_TIMEOUT = "prepare_ack_timeout"
    COMMAND_REJECTION = "command_rejection"

@dataclass(frozen=True)
class FormationFaultSchedule:
    fault_class: FaultClass
    num_agents: int
    seed: int
    logical_edge_id: str
    gateway_index: int
    reject_attempt: int = 1

def build_fault_schedule(
    fault_class: str,
    num_agents: int,
    seed: int,
    edges: Sequence[FormationEdgeLike],
) -> FormationFaultSchedule:
    ordered = sorted(edges, key=lambda edge: edge.edge_id)
    rng = random.Random(f"exp1-transactional-v1:{fault_class}:{num_agents}:{seed}")
    edge = ordered[rng.randrange(len(ordered))]
    gateway_index = rng.randrange(4)
    return FormationFaultSchedule(FaultClass(fault_class), num_agents, seed, edge.edge_id, gateway_index)
```

Implement the DAG descendant traversal from edge endpoints. Implement method scopes without importing Proposed helpers into baseline branches.

- [ ] **Step 4: Run focused tests and confirm GREEN**

```bash
python -m unittest tests.test_exp1_transactional_formation.FormationTransactionProtocolTests -v
```

Expected: all protocol tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/controller/formation_transactions.py tests/test_exp1_transactional_formation.py
git commit -m "feat: define Exp1 transaction fault protocol"
```

### Task 2: Common prepare/commit/abort engine

**Files:**
- Modify: `src/controller/formation_transactions.py`
- Modify: `experiments/exp1_netns_verified_formation.py`
- Modify: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Consumes: `FormationDeploymentPlan`, `DeploymentBatch`, and an executor implementing `stage()`, `activate()`, `flush()`, and `readback()`.
- Produces: `FormationTransactionEngine.prepare()`, `.commit()`, `.abort()`, `.rollback()`, and `TransactionAttempt` audit records.

- [ ] **Step 1: Write failing state-machine tests with a recording executor**

```python
def test_rejected_prepare_cannot_commit(self):
    executor = RecordingExecutor(reject_prepare=True)
    engine = FormationTransactionEngine(executor, ack_timeout_ms=200)
    prepared = engine.prepare(transaction)
    self.assertFalse(prepared.accepted)
    with self.assertRaisesRegex(RuntimeError, "not prepared"):
        engine.commit(transaction.transaction_id)
    self.assertEqual(executor.activated, [])

def test_abort_flushes_staged_state_and_readback_proves_it(self):
    engine.prepare(transaction)
    engine.abort(transaction.transaction_id)
    self.assertEqual(executor.readback(transaction.transaction_id), ())
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
python -m unittest tests.test_exp1_transactional_formation.FormationTransactionEngineTests -v
```

Expected: `FormationTransactionEngine` is missing.

- [ ] **Step 3: Implement the fail-closed engine**

```python
class FormationTransactionEngine:
    def prepare(self, transaction: FormationTransaction) -> TransactionAttempt:
        self._require_new(transaction.transaction_id)
        result = self.executor.stage(transaction, self.ack_timeout_ms)
        phase = TransactionPhase.PREPARED if result.accepted else TransactionPhase.REJECTED
        self._states[transaction.transaction_id] = phase
        return self._record(transaction, "prepare", result)

    def commit(self, transaction_id: str) -> TransactionAttempt:
        if self._states.get(transaction_id) is not TransactionPhase.PREPARED:
            raise RuntimeError(f"transaction {transaction_id} is not prepared")
        result = self.executor.activate(transaction_id)
        self._states[transaction_id] = TransactionPhase.COMMITTED
        return self._record_id(transaction_id, "commit", result)
```

Abort must flush inactive policy tables. Rollback must remove active task rules and restore the pre-attempt readback snapshot. Every transition records monotonic start/end timestamps, affected objects, commands attempted, and readback fingerprints.

- [ ] **Step 4: Add netns staging helpers without changing nominal behavior**

Add to `ProcessNetnsTopology`:

```python
def stage_transaction_commands(self, transaction_id: str, commands: Sequence[DeploymentCommand]) -> StageResult:
    return self._transaction_backend.stage(transaction_id, tuple(commands))

def activate_transaction(self, transaction_id: str) -> CommandResult:
    return self._transaction_backend.activate(transaction_id)

def abort_transaction(self, transaction_id: str) -> CommandResult:
    return self._transaction_backend.flush(transaction_id)

def read_transaction_state(self, transaction_id: str) -> tuple[dict[str, object], ...]:
    return self._transaction_backend.readback(transaction_id)
```

Initialize `_transaction_backend` as a `NetnsPolicyTableBackend(self)` in the
topology constructor. Use inactive task-specific route tables during stage and
`ip rule` activation during commit. Read back with
`ip -j route show table <table_id>` and `ip -j rule show`. Keep `run_one()` and
`install_deployment_plan()` unchanged for the nominal arm.

- [ ] **Step 5: Run engine and nominal regression tests**

```bash
python -m unittest \
  tests.test_exp1_transactional_formation.FormationTransactionEngineTests \
  tests.test_exp1_netns_canonical -v
```

Expected: transaction tests pass and every nominal Exp1 test remains green.

- [ ] **Step 6: Commit**

```bash
git add src/controller/formation_transactions.py experiments/exp1_netns_verified_formation.py tests/test_exp1_transactional_formation.py
git commit -m "feat: add common netns formation transactions"
```

### Task 3: Fair method adapters and retry behavior

**Files:**
- Modify: `src/controller/formation_transactions.py`
- Modify: `experiments/exp1_netns_verified_formation.py`
- Modify: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Consumes: canonical `FormationDeploymentPlan` and `FormationFaultSchedule`.
- Produces: `build_method_transactions(method_id, plan, edges)` and `build_retry_transactions(method_id, failed_object, plan, edges)`.

- [ ] **Step 1: Write failing fairness tests**

```python
def test_cspf_independent_flows_are_in_one_parallel_wave(self):
    waves = build_method_transactions("cspf", cspf_plan, edges)
    self.assertEqual(len(waves), 1)
    self.assertEqual({tx.scope_kind for tx in waves[0]}, {"flow"})

def test_sfc_parallelizes_chains_but_preserves_hop_order(self):
    waves = build_method_transactions("global_sfc_embedding", sfc_plan, edges)
    for chain_id in {tx.chain_id for wave in waves for tx in wave}:
        hops = [tx.hop_index for wave in waves for tx in wave if tx.chain_id == chain_id]
        self.assertEqual(hops, sorted(hops))

def test_baselines_never_call_task_descendant_closure(self):
    with patch("src.controller.formation_transactions.task_descendant_closure", side_effect=AssertionError):
        build_retry_transactions("cspf", "e-1", cspf_plan, edges)
        build_retry_transactions("global_sfc_embedding", "e-1", sfc_plan, edges)
```

- [ ] **Step 2: Run fairness tests and confirm RED**

```bash
python -m unittest tests.test_exp1_transactional_formation.MethodTransactionAdapterTests -v
```

Expected: adapter functions are missing.

- [ ] **Step 3: Implement method-specific transaction grouping**

```python
def build_method_transactions(method_id, plan, edges):
    if method_id == "proposed":
        return _dag_dependency_waves(plan, edges)
    if method_id == "cspf":
        return (_parallel_flow_transactions(plan),)
    if method_id == "global_sfc_embedding":
        return _parallel_chain_hop_waves(plan)
    raise ValueError(f"unsupported method: {method_id}")
```

CSPF planning must update residual capacity after each deterministic path reservation even when resulting transactions execute in parallel. Global SFC planning must reject missing service placement, insufficient placement capacity, invalid hop order, or infeasible path before returning transactions.

- [ ] **Step 4: Run adapter and canonical planner tests**

```bash
python -m unittest \
  tests.test_exp1_transactional_formation.MethodTransactionAdapterTests \
  tests.test_exp1_netns_canonical.Exp1DeploymentPlanTests -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/controller/formation_transactions.py experiments/exp1_netns_verified_formation.py tests/test_exp1_transactional_formation.py
git commit -m "feat: add fair Exp1 transaction adapters"
```

### Task 4: Frozen configs and measured transactional runner

**Files:**
- Create: `configs/exp1_transactional_formation_v1.yaml`
- Create: `configs/exp1_transactional_formation_pilot_v1.yaml`
- Create: `experiments/exp1_transactional_formation.py`
- Modify: `experiments/paper_protocol.py`
- Modify: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Consumes: `ProcessNetnsTopology`, method plans/adapters, transaction engine, and fault schedule.
- Produces: `run_transactional_trial()`, `run_transactional_experiment()`, `TransactionalFormationRun`, `raw/runs.csv`, `raw/attempts.jsonl`, and `raw/measurement_scope.json`.

- [ ] **Step 1: Write failing frozen-protocol and runner-contract tests**

```python
def test_formal_grid_is_exactly_2250_trials(self):
    config = load_transactional_config(Path("configs/exp1_transactional_formation_v1.yaml"))
    schedule = build_transactional_schedule(config)
    self.assertEqual(len(schedule), 2250)

def test_fault_is_hidden_until_common_prepare_api(self):
    runner = RecordingTransactionalRunner()
    runner.run_one(method="proposed", fault_class="command_rejection", seed=0, num_agents=4)
    self.assertNotIn("fault_schedule", runner.planner_inputs)
    self.assertEqual(runner.executor_seen_fault_attempt, 1)

def test_final_verifier_signature_is_identical_for_all_methods(self):
    self.assertEqual({row.verifier_fingerprint for row in paired_rows}, {paired_rows[0].verifier_fingerprint})
```

- [ ] **Step 2: Run runner tests and confirm RED**

```bash
python -m unittest tests.test_exp1_transactional_formation.TransactionalRunnerContractTests -v
```

Expected: config and runner module are missing.

- [ ] **Step 3: Add exact formal and pilot configs**

Formal config must freeze:

```yaml
experiment:
  arm_id: exp1_transactional_v1
  phase: formal
  frozen: true
methods: [proposed, cspf, global_sfc_embedding]
scenario_classes: [stale_version, prepare_ack_timeout, command_rejection]
simulation:
  seeds: "0:49"
  task_sizes: [4, 8, 12, 16, 20]
transaction:
  max_attempts: 2
  prepare_ack_timeout_ms: 200
verification:
  inherit_from: configs/exp1_netns_verified_formation_v3.yaml
```

Pilot differs only by phase, seeds `9000:9019`, and output provenance. It does not loosen deadlines or verification.

- [ ] **Step 4: Implement the trial state machine and raw writer**

```python
@dataclass(frozen=True)
class TransactionalFormationRun:
    protocol_id: str
    arm_id: str
    scenario_class: str
    seed: int
    num_agents: int
    method_id: str
    fault_schedule_fingerprint: str
    attempt_count: int
    rollback_count: int
    method_owned_formation_latency_ms: float | None
    time_to_correct_formation_ms: float | None
    success: bool
    timeout: bool
    failure_stage: str
    failure_reason: str
```

Measure every duration from `time.perf_counter()`. Invoke the common final verifier only after a complete commit. Write the terminal trial row even when an exception or timeout occurs; write every transition to `attempts.jsonl` before the terminal row is considered complete.

- [ ] **Step 5: Run runner-contract tests**

```bash
python -m unittest tests.test_exp1_transactional_formation.TransactionalRunnerContractTests -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add configs/exp1_transactional_formation_v1.yaml configs/exp1_transactional_formation_pilot_v1.yaml experiments/exp1_transactional_formation.py experiments/paper_protocol.py tests/test_exp1_transactional_formation.py
git commit -m "feat: add measured transactional Exp1 runner"
```

### Task 5: Schema, normalization, aggregation, and paired statistics

**Files:**
- Create: `scripts/normalize_wcnc_final_v3_exp1_transactional.py`
- Modify: `configs/wcnc_final_v3_raw_schema.json`
- Modify: `scripts/aggregate_wcnc_final_v3.py`
- Modify: `tests/test_wcnc_final_v3_pipeline.py`
- Modify: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Consumes: measured `runs.csv` and `attempts.jsonl` from Task 4.
- Produces: canonical `raw/exp1_transactional/trials.csv`, `aggregated/exp1_transactional/metrics.csv`, and `paired_differences.csv`.

- [ ] **Step 1: Write failing normalization and aggregation tests**

```python
def test_transactional_normalizer_requires_exact_2250_grid(self):
    with self.assertRaisesRegex(ValueError, "2250"):
        normalize_transactional(incomplete_runs, target, formal_config)

def test_failed_attempt_time_and_waste_are_not_dropped(self):
    rows = aggregate_transactional(raw_fixture)
    metric = find(rows, "method_owned_formation_latency_ms", "cspf")
    self.assertEqual(metric["denominator"], 2)
    self.assertGreater(find(rows, "wasted_rule_commands", "cspf")["estimate"], 0)

def test_time_to_correct_is_conditional_and_adjacent_to_success(self):
    self.assertEqual(find(rows, "success_rate", "proposed")["denominator"], 2)
    self.assertEqual(find(rows, "time_to_correct_formation_ms", "proposed")["numerator"], 1)
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
python -m unittest \
  tests.test_exp1_transactional_formation.TransactionalAggregationTests \
  tests.test_wcnc_final_v3_pipeline -v
```

Expected: normalizer/metrics are missing.

- [ ] **Step 3: Add schema fields and fail-closed normalization**

Require the fields listed in the spec, `arm_id == exp1_transactional_v1`, measured-netns mode, exact config hash, exact method/fault/size/seed grid, unique trial IDs, paired fault fingerprints, and at least one terminal attempt event per trial. Preserve every source measurement field.

- [ ] **Step 4: Aggregate required metrics and paired differences**

Add:

```python
TRANSACTIONAL_CONTINUOUS = (
    ("method_owned_formation_latency_ms", False),
    ("time_to_correct_formation_ms", True),
    ("rollback_scope_objects", False),
    ("wasted_rule_commands", False),
    ("partial_state_exposure_ms", False),
)
```

Use Wilson intervals for success, timeout, and first-attempt commit rate. Use topology-seed cluster bootstrap for continuous metrics and paired baseline-minus-Proposed differences. Pair by `(scenario_class, num_agents, seed, fault_schedule_fingerprint)`.

- [ ] **Step 5: Run pipeline tests and confirm GREEN**

```bash
MPLCONFIGDIR=/tmp/wcnc-v3-mpl python -m unittest \
  tests.test_exp1_transactional_formation.TransactionalAggregationTests \
  tests.test_wcnc_final_v3_pipeline -v
```

- [ ] **Step 6: Commit**

```bash
git add scripts/normalize_wcnc_final_v3_exp1_transactional.py configs/wcnc_final_v3_raw_schema.json scripts/aggregate_wcnc_final_v3.py tests/test_wcnc_final_v3_pipeline.py tests/test_exp1_transactional_formation.py
git commit -m "feat: aggregate transactional Exp1 results"
```

### Task 6: Publication figures and integrity audit

**Files:**
- Modify: `scripts/plot_wcnc_final_v3.py`
- Modify: `scripts/audit_wcnc_final_v3.py`
- Modify: `tests/test_wcnc_final_v3_pipeline.py`

**Interfaces:**
- Consumes: Task 5 aggregate CSV and raw/attempt hashes.
- Produces: PDF/PNG/SVG figures, manifest arm entries, and explicit audit checks.

- [ ] **Step 1: Write failing plot and audit tests**

```python
def test_transactional_plot_reads_only_aggregate_csv(self):
    run_plotter(root, experiment="exp1_transactional")
    self.assertTrue((root / "figures/exp1_transactional_time_to_correct_formation_ms.pdf").is_file())
    self.assertFalse((root / "figures/exp1_transactional_success_rate.pdf").exists())

def test_audit_rejects_fault_fingerprint_mismatch_and_missing_attempts(self):
    report = audit(root, manifest)
    self.assertIn("paired fault schedule drift", report["errors"])
    self.assertIn("missing transactional attempt provenance", report["errors"])

def test_manifest_keeps_nominal_and_transactional_execution_commits(self):
    self.assertEqual(set(manifest["exp1_arms"]), {"nominal", "exp1_transactional_v1"})
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
MPLCONFIGDIR=/tmp/wcnc-v3-mpl python -m unittest tests.test_wcnc_final_v3_pipeline -v
```

- [ ] **Step 3: Add figures without smoothing or generated values**

Generate:

```text
exp1_transactional_method_owned_formation_latency_ms.{pdf,png,svg}
exp1_transactional_time_to_correct_formation_ms.{pdf,png,svg}
exp1_transactional_rollback_scope_objects.{pdf,png,svg}
exp1_transactional_wasted_rule_commands.{pdf,png,svg}
```

Facet or line-style by `scenario_class`; keep method color/marker mappings consistent with nominal Exp1. Do not generate a standalone success curve unless frozen formal results show meaningful separation; retain success in aggregate/table output.

- [ ] **Step 4: Add audit and manifest arm provenance**

Audit exact grid, unique trial/attempt IDs, same fault fingerprint across methods, hidden-schedule evidence, final verifier fingerprint equality, raw-to-aggregate provenance, nominal raw hash immutability, and separate execution/config/source hashes for both arms.

- [ ] **Step 5: Run plot/audit repeatability tests**

```bash
MPLCONFIGDIR=/tmp/wcnc-v3-mpl python -m unittest tests.test_wcnc_final_v3_pipeline -v
```

Run the plotter twice on a fixture and assert identical PDF/SVG hashes.

- [ ] **Step 6: Commit**

```bash
git add scripts/plot_wcnc_final_v3.py scripts/audit_wcnc_final_v3.py tests/test_wcnc_final_v3_pipeline.py
git commit -m "feat: audit and plot transactional Exp1"
```

### Task 7: Remote orchestration and Exp2--Exp4 continuation

**Files:**
- Modify: `scripts/run_wcnc_final_v3_remote.sh`
- Modify: `docs/wcnc_final_v3.md`
- Modify: `tests/test_exp1_transactional_formation.py`

**Interfaces:**
- Consumes: all runners, configs, normalizers, audit, aggregate, and plotter.
- Produces: resumable smoke/pilot/formal commands and complete logs/markers.

- [ ] **Step 1: Write failing launcher-contract tests**

```python
def test_launcher_runs_transactional_arm_before_exp2(self):
    script = Path("scripts/run_wcnc_final_v3_remote.sh").read_text()
    self.assertLess(script.index("exp1_transactional_formal"), script.index("run_step exp2"))

def test_complete_nominal_grid_can_continue_despite_trial_failures(self):
    result = validate_completed_nominal_artifacts(nominal_fixture)
    self.assertTrue(result.complete)
    self.assertEqual(result.raw_rows, 750)
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
python -m unittest tests.test_exp1_transactional_formation.RemoteLauncherContractTests -v
```

- [ ] **Step 3: Add audited nominal continuation and transactional steps**

The launcher must capture the nominal runner status, then continue only when a dedicated read-only completeness/event audit proves the exact 750-row grid and config hash. It must never convert an incomplete or unaudited run to success.

Add resumable commands for:

```bash
sudo -E env -u WCNC_EXP1_INSIDE_USERNS -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/pilot/exp1_transactional \
  --seeds 9000:9019

sudo -E env -u WCNC_EXP1_INSIDE_USERNS -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/exp1_transactional_staging_v1 \
  --seeds 0:49
```

Then normalize transactional raw, run Exp2, Exp3, Exp4, aggregate, audit, and plot. Step markers include git commit, protocol signature, artifact hash, and exact command signature.

- [ ] **Step 4: Document commands and interpretation**

Document that Exp2--Exp4 formal raw is currently missing, that nominal and transactional Exp1 use separate execution commits, and that deployment faults are not Exp2 cross-layer conflicts.

- [ ] **Step 5: Validate shell and launcher tests**

```bash
bash -n scripts/run_wcnc_final_v3_remote.sh
python -m unittest tests.test_exp1_transactional_formation.RemoteLauncherContractTests -v
```

- [ ] **Step 6: Commit**

```bash
git add scripts/run_wcnc_final_v3_remote.sh docs/wcnc_final_v3.md tests/test_exp1_transactional_formation.py
git commit -m "feat: orchestrate transactional Exp1 formal run"
```

### Task 8: Verification, Linux smoke, pilot, and handoff

**Files:**
- Modify: `results/paper/wcnc_final_v3/pilot/ground_truth_distribution.md` only through the versioned pilot reporter if results are intentionally retained outside Git.
- Modify: `results/paper/wcnc_final_v3/experiment_report.md` only through the report generator.

**Interfaces:**
- Consumes: complete implementation from Tasks 1--7.
- Produces: verified code, pilot report, smoke evidence, remote formal command, and a reviewable commit series.

- [ ] **Step 1: Run compilation and focused tests**

```bash
python -m py_compile \
  src/controller/formation_transactions.py \
  experiments/exp1_transactional_formation.py \
  experiments/exp1_netns_verified_formation.py \
  scripts/normalize_wcnc_final_v3_exp1_transactional.py \
  scripts/aggregate_wcnc_final_v3.py \
  scripts/audit_wcnc_final_v3.py \
  scripts/plot_wcnc_final_v3.py
bash -n scripts/run_wcnc_final_v3_remote.sh
MPLCONFIGDIR=/tmp/wcnc-v3-mpl python -m unittest \
  tests.test_exp1_transactional_formation \
  tests.test_exp1_netns_canonical \
  tests.test_wcnc_final_v3_pipeline -v
```

- [ ] **Step 2: Run broader protocol regression**

```bash
MPLCONFIGDIR=/tmp/wcnc-v3-mpl python -m unittest \
  tests.test_paper_results_integrity \
  tests.test_wcnc_final_experiments \
  tests.test_paper_exp1 \
  tests.test_exp1_netns_canonical \
  tests.test_wcnc_final_v3_pipeline \
  tests.test_exp1_transactional_formation
```

- [ ] **Step 3: Run a six-trial Linux smoke**

Use one fault class, one size, two seeds, and all methods:

```bash
sudo -E env \
  -u WCNC_EXP1_INSIDE_USERNS \
  -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/pilot/exp1_transactional_smoke \
  --seeds 9000:9001 \
  --task-sizes 4 \
  --scenario-classes command_rejection \
  --methods proposed,cspf,global_sfc_embedding \
  --require-complete-grid
```

Acceptance: 6 terminal rows, 6 paired fault fingerprints, a failed first command attempt in every row, no silent success override, common verifier fingerprints equal within each seed, and no residual task rules after cleanup.

- [ ] **Step 4: Run the full 900-row pilot**

```bash
sudo -E env \
  -u WCNC_EXP1_INSIDE_USERNS \
  -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/pilot/exp1_transactional \
  --seeds 9000:9019
```

Inspect only class trigger coverage, runtime, exceptions, residual state, and completeness. Do not inspect method ranking to tune the frozen formal config.

- [ ] **Step 5: Request code review and address Critical/Important findings**

Review the complete diff against the spec, with particular attention to baseline fairness, actual rather than modeled timing, Exp2 isolation, failure retention, and nominal hash preservation.

- [ ] **Step 6: Run final verification and commit any review fixes**

Repeat Steps 1--4 after fixes. Record exact test counts, smoke rows, pilot trigger distribution, nominal raw SHA-256, and the remaining formal command in the handoff.
