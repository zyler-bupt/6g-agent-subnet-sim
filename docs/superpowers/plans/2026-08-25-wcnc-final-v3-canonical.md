# WCNC Final v3 Canonical Experiments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, validate, pilot, and package one reproducible `wcnc_final_v3` Exp1–Exp4 experiment pipeline whose figures are derived only from canonical raw trials.

**Architecture:** Extend the existing paper experiment modules behind one frozen protocol and one canonical trial schema. Preserve experiment-specific runners, but route them through a versioned orchestrator, aggregation library, manifest/audit layer, and data-only plotter.

**Tech Stack:** Python 3, asyncio, unittest, NumPy, matplotlib, Linux netns/iproute2, ping, iperf3.

**Spec:** `docs/superpowers/specs/2026-08-25-wcnc-final-v3-canonical-design.md`

## Global Constraints

- Protocol ID is exactly `wcnc_final_v3`.
- Old raw data is never deleted or overwritten.
- Pilot seeds are `9000:9019`; Exp1 formal seeds are `0:49`; Exp2–Exp4 formal seeds are `0:99` unless a frozen reduced topology count is explicitly reported.
- Formal plotting reads canonical aggregate CSV only and never demo data.
- SANet-DW* and NetRen* remain adapted baselines with explicit provenance.
- Production behavior changes follow test-first red/green cycles.

---

### Task 1: Frozen protocol and canonical schema

**Files:**
- Create: `configs/wcnc_final_v3.yaml`
- Modify: `experiments/paper_protocol.py`
- Modify: `src/metrics/paper.py`
- Test: `tests/test_wcnc_final_v3_protocol.py`

**Interfaces:**
- Produces: `PROTOCOL_ID`, v3 grids, method sets, display metadata, result modes, and canonical trial provenance/Exp2 pre-verification fields.

- [ ] Write tests for exact method sets, labels, grids, failure-specific methods, protocol/config fingerprint, and nullable canonical fields.
- [ ] Run tests and confirm failures identify missing v3 protocol/schema.
- [ ] Add frozen configuration and registry/schema implementation.
- [ ] Run protocol/schema tests and existing paper protocol tests.

### Task 2: Exp2 truth/observation/verifier separation and search budgets

**Files:**
- Modify: `src/simulation/demand_capacity_ratio.py`
- Modify: `src/controller/paper_cross_layer_coordination.py`
- Modify: `experiments/exp2_demand_capacity_ratio.py`
- Test: `tests/test_wcnc_final_v3_exp2.py`

**Interfaces:**
- Produces: deterministic per-seed epsilon profile, shared observation fingerprint, independent oracle fingerprint, budgeted online selection, and verifier-before/after metrics.

- [ ] Write failing tests for epsilon invariance across gamma, absence of the gamma=1 class cliff, equal observations, oracle isolation, noise effects, budget exhaustion, SANet soft selection, and verifier rescue metrics.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement the smallest observation/oracle boundary and budgeted coordinators.
- [ ] Run focused and legacy Exp2 tests.

### Task 3: Exp1 shared measured backend and baseline semantics

**Files:**
- Modify: `experiments/exp1_netns_verified_formation.py`
- Modify: `src/controller/formation_strategies.py`
- Test: `tests/test_wcnc_final_v3_exp1.py`

**Interfaces:**
- Produces: paired method runs for `proposed`, `cspf`, and `global_sfc_embedding` using the same topology, impairment, probe, timeout, and measured clock backend.

- [ ] Write failing backend-contract tests for method pairing, identical scenario/probe inputs, deterministic CSPF/SFC tie-breaks, common verification, and timeout preservation.
- [ ] Run tests and confirm failures.
- [ ] Implement CSPF-based formation and heuristic global SFC embedding adapters over the shared backend.
- [ ] Run focused Exp1 and networking tests; skip only privilege-dependent live netns cases with an explicit reason.

### Task 4: Exp3 NetRen adaptation and Exp4 failure-specific baselines

**Files:**
- Modify: `src/controller/business_reconfiguration.py`
- Modify: `src/controller/paper_failure_recovery.py`
- Modify: `experiments/exp3_business_elasticity.py`
- Modify: `experiments/exp4_failure.py`
- Test: `tests/test_wcnc_final_v3_exp3_exp4.py`

**Interfaces:**
- Produces: NetRen migration-flow resynthesis provenance and failure-specific method/scenario grids with N/A represented by absence.

- [ ] Write failing tests that forbid NetRen exact closure access, require resynthesis provenance, enforce failure-specific method sets, check severity semantics, and reject zero-valued N/A rows.
- [ ] Run tests and confirm failures.
- [ ] Implement NetRen adapted resynthesis and align SFC Restoration/TE Re-optimization/CSPF Recovery contracts.
- [ ] Run focused and legacy Exp3/Exp4 tests.

### Task 5: Canonical orchestration, aggregation, manifest, and audit

**Files:**
- Create: `scripts/run_wcnc_final_v3.py`
- Create: `scripts/aggregate_wcnc_final_v3.py`
- Create: `scripts/audit_wcnc_final_v3.py`
- Modify: `scripts/paper_statistics.py`
- Test: `tests/test_wcnc_final_v3_pipeline.py`

**Interfaces:**
- Produces: versioned raw/aggregate layout, Wilson rate intervals, cluster intervals, paired-difference intervals, manifest hashes, provenance joins, and integrity report.

- [ ] Write failing tests for immutable paths, rate denominators, paired differences, timeout retention, hash drift, raw-to-aggregate reproduction, and provenance completeness.
- [ ] Run tests and confirm failures.
- [ ] Implement the orchestrator, aggregators, manifest writer, and audit checks.
- [ ] Run pipeline/statistics/integrity tests.

### Task 6: Publication plotting and remote runner

**Files:**
- Create: `scripts/plot_wcnc_final_v3.py`
- Create: `scripts/run_wcnc_final_v3_remote.sh`
- Test: `tests/test_wcnc_final_v3_plotting.py`

**Interfaces:**
- Consumes: only `results/paper/wcnc_final_v3/aggregated/*/summary.csv`.
- Produces: PDF/PNG/SVG plus figure-source CSV and a resumable remote formal workflow.

- [ ] Write failing tests for aggregate-only inputs, no demo references, labels/stars, N/A handling, successful-run latency labeling, deterministic outputs, and remote script stages.
- [ ] Run tests and confirm failures.
- [ ] Implement data-only figures using the installed academic plot style as a visual reference, without copying synthetic values.
- [ ] Implement dependency checks, environment capture, resume markers, per-experiment commands, aggregation, audit, and plotting in the remote script.
- [ ] Run plotting and remote-script tests.

### Task 7: Smoke, pilot, report, and final verification

**Files:**
- Generate: `results/paper/wcnc_final_v3/pilot/**`
- Generate: `results/paper/wcnc_final_v3/experiment_report.md`
- Generate: `results/paper/wcnc_final_v3/integrity_report.json`

**Interfaces:**
- Produces: local smoke/pilot evidence and exact remote formal commands without claiming unavailable netns/formal results.

- [ ] Run the full relevant unittest suite.
- [ ] Run deterministic smoke trials for Exp2–Exp4 and nonprivileged Exp1 contract tests.
- [ ] Run the 9000:9019 pilot where runtime permits; record ground-truth distributions only for the formal gate.
- [ ] Run the v3 audit and reproduce aggregate/figure artifacts.
- [ ] Review repository diff, verify user changes remain, and report local versus remote completion precisely.
