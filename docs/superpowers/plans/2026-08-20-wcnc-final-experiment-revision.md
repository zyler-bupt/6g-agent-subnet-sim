# WCNC 2027 Final Experiment Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce physically meaningful, reproducible WCNC paper results and four final two-panel IEEE-style figures without replacing the existing simulator.

**Architecture:** Extend the canonical `PaperTrial` contract with explicit controller, path, Agent, and modification-scope metrics.  Keep each experiment's simulator logic isolated, then aggregate only raw fields and render the final figures from aggregate CSVs.  Pilot gates remain mandatory before paper execution.

**Tech Stack:** Python 3.10, asyncio discrete-event experiment code, NumPy cluster bootstrap, Matplotlib vector PDF/PNG, unittest.

**Spec:** `docs/superpowers/specs/2026-08-20-wcnc-final-experiment-revision-design.md`

## Global Constraints

- Never modify a plotted value in a plotting script.
- All methods in a paired trial share topology, task DAG, placement, QoS, event, and seed fingerprints.
- Retain NetKeeper* as the Exp.4 baseline and keep it network-only.
- Paper rate metrics use 30 topology seeds × 5 events and topology-clustered 95% CI.
- Do not modify the user's unrelated dirty files.
- Use `apply_patch` for source edits and TDD for behavior changes.

---

### Task 1: Freeze shared method, schema, aggregation, and style interfaces

**Files:**
- Modify: `experiments/paper_protocol.py`
- Modify: `src/metrics/paper.py`
- Modify: `scripts/aggregate_results.py`
- Modify: `scripts/paper_style.py`
- Modify: `tests/test_paper_protocol.py`
- Modify: `tests/test_paper_statistics.py`
- Modify: `tests/test_paper_plotting.py`

**Interfaces:**
- Produces: method id `srd` with label `SRD`; retained id `netkeeper` with label `NetKeeper*`.
- Produces `PaperTrial.controller_processing_latency_ms: float | None`.
- Produces `PaperTrial.affected_agent_count: int | None`.
- Produces `total_paths`, `changed_paths`, `total_agents`, `changed_agents`, and `modification_scope_ratio` nullable fields.
- Aggregates Exp.1 task-size rows into `controller_processing_latency_ms`, `formation_latency_ms`, and `success_rate_percent`.
- Aggregates Exp.3 on `affected_agent_count`.
- Aggregates Exp.4 successful `modification_scope_ratio_percent` while preserving success and capacity-stress metrics.

- [ ] **Step 1: Write failing interface tests**

```python
def test_wcnc_method_registry_and_palette():
    assert EXPERIMENT_METHODS["exp1"] == (
        "proposed", "proposed_without_batch", "cspf", "srd"
    )
    assert METHODS["srd"].label == "SRD"
    assert METHODS["netkeeper"].label == "NetKeeper*"
    assert METHOD_STYLES["proposed"].color == "#2ca25f"
    assert METHOD_STYLES["proposed"].marker == "D"

def test_new_trial_metrics_round_trip_and_aggregate(tmp_path):
    trials = [{
        "experiment": "exp1", "mode": "pilot", "series": "task_size",
        "method_id": "proposed", "method_label": "Proposed", "seed": 0,
        "task_size": 8, "formation_latency_ms": 24.0,
        "controller_processing_latency_ms": 3.0, "success": True,
    }]
    rows = aggregate_experiment(trials, tmp_path / "summary.csv", bootstrap_iterations=20)
    assert {row.metric for row in rows} == {
        "formation_latency_ms", "controller_processing_latency_ms",
        "success_rate_percent",
    }
    assert {row.x_name for row in rows} == {"task_size"}
```

- [ ] **Step 2: Run tests and observe failures caused by missing SRD/new fields/new aggregate axes**

Run: `python3 -m unittest -q tests.test_paper_protocol tests.test_paper_statistics tests.test_paper_plotting`

- [ ] **Step 3: Implement the minimal shared interfaces**

Use these exact style identities:

```python
"proposed": MethodStyle("Proposed", "#2ca25f", "-", "D")
"cspf": MethodStyle("CSPF", "#e74c3c", "--", "s")
"srd": MethodStyle("SRD", "#f39c12", ":", "^")
"proposed_without_batch": MethodStyle("Proposed w/o Batch", "#7f7f7f", "--", "o")
```

Set axis labels to 10 pt, ticks to 9 pt, legend to 8.5 pt, line width to 1.8, marker size to 5.5, and CI alpha to 0.15.

- [ ] **Step 4: Re-run the shared tests**

Run: `python3 -m unittest -q tests.test_paper_protocol tests.test_paper_statistics tests.test_paper_plotting`

---

### Task 2: Implement Exp.1 physical control-plane formation timing

**Files:**
- Modify: `src/controller/formation_strategies.py`
- Modify: `experiments/exp1_initial_formation.py`
- Modify: `tests/test_paper_exp1.py`

**Interfaces:**
- Consumes: `PaperTrial.controller_processing_latency_ms` and method id `srd` from Task 1.
- Produces: `FormationOutcome.controller_processing_latency_ms`.
- Produces: deterministic per-Gateway RTT in `[5, 20]` ms and processing in `[1, 5]` ms as shared primitive costs.
- Produces: task-size trials using `rate_events_per_seed` and a common background churn probability.

- [ ] **Step 1: Write failing physical-timing tests**

```python
async def test_t_ctrl_excludes_gateway_communication_and_t_form_includes_it():
    outcome = await run_formation_method(snapshot, "proposed")
    assert outcome.controller_processing_latency_ms > 0
    assert outcome.formation_latency_ms > outcome.controller_processing_latency_ms
    assert any(op.kind == "gateway_dispatch_install_ack" for op in outcome.trace.operations)

async def test_srd_accumulates_shared_gateway_operations():
    proposed = await run_formation_method(snapshot, "proposed")
    srd = await run_formation_method(snapshot, "srd")
    assert proposed.trace.primitive_cost_fingerprint == srd.trace.primitive_cost_fingerprint
    assert srd.formation_latency_ms > proposed.formation_latency_ms
```

- [ ] **Step 2: Run Exp.1 tests and observe the expected missing-field/method failures**

Run: `python3 -m unittest -q tests.test_paper_exp1`

- [ ] **Step 3: Implement scheduled controller/Gateway operations**

Generate RTT and Gateway processing from `stable_fingerprint({snapshot event, gateway})`; never use method id.  Dispatch/install/ACK and verification/report/ACK operations include those costs once per actual scheduled exchange.  Proposed parallelizes Gateway batches; Proposed w/o Batch serializes the same batches; CSPF and SRD complete the entire edge-by-edge procedure.

Compute `T_ctrl` from controller-only critical work:

```python
controller_kinds = {
    "dag_analysis", "supporting_agent_binding", "endpoint_resolution",
    "path_compute", "rule_generate"
}
```

Do not derive `T_ctrl` by subtracting an arbitrary percentage from `T_form`.

- [ ] **Step 4: Run Exp.1 tests**

Run: `python3 -m unittest -q tests.test_paper_exp1`

---

### Task 3: Re-index Exp.3 by exact affected Agent count and record object changes

**Files:**
- Modify: `src/controller/business_reconfiguration.py`
- Modify: `experiments/exp3_business_elasticity.py`
- Modify: `tests/test_paper_exp3.py`

**Interfaces:**
- Consumes: new nullable `PaperTrial` object-change fields from Task 1.
- Produces: `BusinessReconfigurationOutcome.total_paths`, `changed_paths`, `total_agents`, `changed_agents`, and `modification_scope_ratio`.
- Produces: trial `affected_agent_count = len(snapshot.closure.agent_ids)` and series `affected_agents`.

- [ ] **Step 1: Write failing tests for paired affected-Agent counts and real object diffs**

```python
async def test_affected_agent_count_is_shared_and_matches_exact_closure():
    sample = sample_affected_scope_bucket(10, seed=0, event_id=0, schedule_index=0)
    rows = await run_exp3("pilot", root, seeds=(0,), buckets=(10,))
    assert {row.affected_agent_count for row in rows} == {
        len(sample.closure.agent_ids)
    }

async def test_full_rebuild_changes_more_objects_than_proposed():
    assert full.changed_rules >= proposed.changed_rules
    assert full.modification_scope_ratio >= proposed.modification_scope_ratio
```

- [ ] **Step 2: Run Exp.3 tests and observe missing-field/series failures**

Run: `python3 -m unittest -q tests.test_paper_exp3`

- [ ] **Step 3: Implement comparisons against the stable subnet**

Paths are compared by business-edge id and Gateway path.  Agents are compared
across application, transport, network, and physical state mappings; an Agent
id counts as changed if it is added, removed, or has a different state/binding.
Use the exact formula from the design for modification scope.

- [ ] **Step 4: Run Exp.3 tests**

Run: `python3 -m unittest -q tests.test_paper_exp3`

---

### Task 4: Add Exp.4 path/Agent modification metrics while retaining NetKeeper*

**Files:**
- Modify: `src/controller/paper_failure_recovery.py`
- Modify: `experiments/exp4_failure.py`
- Modify: `tests/test_paper_exp4.py`

**Interfaces:**
- Consumes: new nullable `PaperTrial` object-change fields from Task 1.
- Produces: `PaperFailureOutcome.total_paths`, `changed_paths`, `total_agents`, `changed_agents`, and `modification_scope_ratio`.
- Retains: `NetKeeperNetworkStrategy.method == "netkeeper"` and its network-only input/action boundary.

- [ ] **Step 1: Write failing tests for physical modification scope**

```python
async def test_agent_recovery_records_changed_agent_and_path_objects():
    proposed = await run_paper_failure_method(snapshot, "proposed")
    assert proposed.success
    assert proposed.changed_agents >= 1
    assert proposed.changed_paths >= 1
    expected = (
        proposed.changed_rules + proposed.changed_paths + proposed.changed_agents
    ) / (proposed.total_rules + proposed.total_paths + proposed.total_agents)
    assert proposed.modification_scope_ratio == expected

async def test_netkeeper_remains_network_only():
    outcome = await run_paper_failure_method(capacity_snapshot, "netkeeper")
    assert outcome.selected_layers <= {"network"}
```

- [ ] **Step 2: Run Exp.4 tests and observe missing metric failures**

Run: `python3 -m unittest -q tests.test_paper_exp4`

- [ ] **Step 3: Implement stable-to-target comparisons**

Use the actual executable plan target.  Rejected methods report zero changed
objects in raw data and no successful latency; aggregation excludes their
modification scope from successful bars.  Do not modify NetKeeper* planning.

- [ ] **Step 4: Run Exp.4 tests**

Run: `python3 -m unittest -q tests.test_paper_exp4`

---

### Task 5: Render the final four figures and per-figure CSVs

**Files:**
- Modify: `scripts/plot_final_paper_figures.py`
- Modify: `scripts/run_pilot.py`
- Modify: `scripts/run_paper.py`
- Modify: `tests/test_paper_plotting.py`
- Modify: `tests/test_paper_runner.py`
- Modify: `tests/test_paper_results_integrity.py`

**Interfaces:**
- Consumes: canonical aggregate rows from Tasks 1–4.
- Produces: the twelve exact files in `results/paper_figures_final/`.

- [ ] **Step 1: Write failing artifact and panel-selection tests**

```python
def test_final_figure_set_has_four_two_panel_vector_figures_and_csvs():
    assert expected_stems == {
        "Fig1_Formation", "Fig2_CrossLayer", "Fig3_Elasticity", "Fig4_Recovery"
    }
    for stem in expected_stems:
        assert (output_dir / f"{stem}.pdf").read_bytes().startswith(b"%PDF")
        assert (output_dir / f"{stem}.png").read_bytes().startswith(b"\x89PNG")
        assert list(csv.DictReader((output_dir / f"{stem}.csv").open()))
```

- [ ] **Step 2: Run plotting/runner tests and observe old filename/panel failures**

Run: `python3 -m unittest -q tests.test_paper_plotting tests.test_paper_runner tests.test_paper_results_integrity`

- [ ] **Step 3: Implement the final selection without result transformation**

- Fig.1: `formation_latency_ms` and `success_rate_percent` over task size; main methods `srd`, `cspf`, `proposed`.
- Fig.2: QoS line over conflict density; FSR/SRR grouped bars at 50% conflict density with separate denominators.
- Fig.3: successful reconfiguration latency and changed-rule ratio over affected Agent count. Formal points require at least 10 independent topology clusters; sparse exact-count observations remain in raw/aggregate CSVs.
- Fig.4: successful latency and successful modification-scope grouped bars by the three fixed failure types.

Each `.csv` is the exact aggregate subset used by its two panels and includes a `panel` column.

- [ ] **Step 4: Run plotting and runner tests**

Run: `python3 -m unittest -q tests.test_paper_plotting tests.test_paper_runner tests.test_paper_results_integrity`

---

### Task 6: Pilot, paper execution, report, integrity verification, commit, and push

**Files:**
- Modify: `scripts/sanity_check_results.py`
- Modify: `scripts/write_experiment_report.py`
- Modify: `results/EXPERIMENT_REPORT.md`
- Replace from execution: `results/raw/pilot/**`, `results/aggregated/pilot/**`
- Replace from execution: `results/raw/paper/**`, `results/aggregated/paper/**`
- Create from execution: `results/paper_figures_final/**`
- Modify: `tests/test_paper_report.py`

**Interfaces:**
- Consumes: completed experiment runners and final plotters.
- Produces: audited pilot manifests, paper manifests, report, integrity report, and final archive.

- [ ] **Step 1: Write failing report/integrity assertions for the revised metric definitions and paths**

Run: `python3 -m unittest -q tests.test_paper_report tests.test_paper_results_integrity`

- [ ] **Step 2: Run every pilot from the revised common configuration**

Run four audited pilot invocations, stopping before paper mode if any one reports a sanity error:

`python3 scripts/run_pilot.py --experiments exp1 --replace-pilot`

`python3 scripts/run_pilot.py --experiments exp2 --replace-pilot`

`python3 scripts/run_pilot.py --experiments exp3 --replace-pilot`

`python3 scripts/run_pilot.py --experiments exp4 --replace-pilot`

Inspect sanity output.  If the common Exp.1 background churn is uniformly too easy or too hard, change only that shared probability and rerun all Exp.1 pilot methods.

- [ ] **Step 3: Run paper mode only after all pilot gates pass**

Run: `python3 scripts/run_paper.py --experiments exp1 exp3 exp4 exp2 --replace-paper`

- [ ] **Step 4: Generate the final report and archive**

Run: `python3 scripts/write_experiment_report.py`

Create `paper_results_final.tar.gz` containing the report, final figures,
paper aggregates, and paper raw CSVs.

- [ ] **Step 5: Run fresh complete verification**

Run: `python3 -m unittest -q`

Run: `git diff --check`

Inspect all four PNGs and verify each PDF contains vector drawing content.

- [ ] **Step 6: Commit only revision-owned files and push**

```bash
git add docs/superpowers scripts/paper_style.py scripts/aggregate_results.py \
  scripts/plot_final_paper_figures.py scripts/run_pilot.py scripts/run_paper.py \
  scripts/sanity_check_results.py scripts/write_experiment_report.py \
  experiments/paper_protocol.py experiments/exp1_initial_formation.py \
  experiments/exp3_business_elasticity.py experiments/exp4_failure.py \
  src/metrics/paper.py src/controller/formation_strategies.py \
  src/controller/business_reconfiguration.py src/controller/paper_failure_recovery.py \
  tests/test_paper_protocol.py tests/test_paper_statistics.py tests/test_paper_plotting.py \
  tests/test_paper_exp1.py tests/test_paper_exp3.py tests/test_paper_exp4.py \
  tests/test_paper_runner.py tests/test_paper_results_integrity.py \
  tests/test_paper_report.py results/EXPERIMENT_REPORT.md \
  results/raw/pilot results/aggregated/pilot results/raw/paper \
  results/aggregated/paper results/paper_figures_final
git commit -m "Revise WCNC paper experiments and final figures"
git push
```
