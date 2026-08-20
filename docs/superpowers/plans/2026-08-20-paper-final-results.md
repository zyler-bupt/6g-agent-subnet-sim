# Final Paper Results and Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate paper mode on all four passing pilots, run the complete formal grids, regenerate every aggregate and figure from raw data, write `EXPERIMENT_REPORT.md`, clean obsolete artifacts, and publish the verified deliverable.

**Architecture:** A single paper runner validates pilot manifests, invokes each experiment with the frozen shared config, aggregates by topology-seed clusters, runs all audits, and produces the final report from machine-readable summaries. No paper value is copied from pilot output.

**Tech Stack:** Python 3, existing experiment runners, NumPy, Matplotlib, CSV/JSON/Markdown, unittest, Git.

**Spec:** `docs/superpowers/specs/2026-08-20-paper-experiments-finalization-design.md`

## Global Constraints

- Paper mode must not begin unless Exp.1, Exp.3, Exp.4, and Exp.2 pilots have no sanity errors.
- Continuous metrics use at least 30 topology seeds; rate metrics use 30 seeds times 5 events.
- All 12 named figures are regenerated from `results/raw/paper` through aggregated CSV.
- Final reporting must describe observed results, including mechanisms that do not match expectations.
- Only confirmed experiment artifacts are staged; unrelated office files, archives, legacy results, and user changes remain untouched.

---

### Task 1: Pilot Manifests and Gated Paper Runner

**Files:**
- Create: `scripts/run_paper.py`
- Modify: `scripts/run_pilot.py`
- Test: `tests/test_paper_runner.py`

**Interfaces:**
- Produces: per-experiment `pilot_manifest.json`, `validate_all_pilots()`, and `run_paper()`.
- Consumes: raw hashes, config hash, sanity JSON, method sets, and experiment runners.

- [ ] **Step 1: Write failing gate tests**

```python
def test_paper_runner_refuses_missing_or_error_pilot(self):
    with self.assertRaisesRegex(RuntimeError, "pilot gate failed"):
        validate_all_pilots(root_with_exp3_error_manifest)

def test_paper_runner_requires_exact_method_sets_and_config_hash(self):
    manifest = valid_manifest()
    manifest["methods"] = ["proposed"]
    with self.assertRaisesRegex(RuntimeError, "method set"):
        validate_pilot_manifest(manifest, "exp1", current_config_hash)
```

- [ ] **Step 2: Run runner tests and confirm failure**

Run: `python3 -m unittest tests.test_paper_runner -v`

Expected: FAIL because paper gating is missing.

- [ ] **Step 3: Implement immutable pilot manifests**

Store experiment, mode, config SHA-256, raw SHA-256, aggregate SHA-256, sanity
SHA-256, method IDs, seeds, events, error count, warning explanations, and
pilot completion timestamp. `run_paper.py` validates all four manifests before
creating any paper output directory.

- [ ] **Step 4: Implement canonical paper execution order**

Call `run_exp1`, `run_exp3`, `run_exp4`, and `run_exp2` with paper mode and the
frozen config. Use 30 topology seeds and five events where a rate is reported.
After each experiment, aggregate and sanity-check before continuing. Stop on
the first error without claiming paper completion.

- [ ] **Step 5: Run gate tests**

Run: `python3 -m unittest tests.test_paper_runner -v`

Expected: PASS.

- [ ] **Step 6: Commit paper runner**

```bash
git add -- scripts/run_paper.py scripts/run_pilot.py tests/test_paper_runner.py
git commit -m "Gate formal runs on audited pilots"
```

### Task 2: Paper Execution, Reaggregation, and Figure Reproduction

**Files:**
- Generate: `results/raw/paper/exp1/trials.csv`
- Generate: `results/raw/paper/exp2/trials.csv`
- Generate: `results/raw/paper/exp3/trials.csv`
- Generate: `results/raw/paper/exp4/trials.csv`
- Generate: `results/aggregated/paper/`
- Regenerate: `results/paper_figures/`
- Test: `tests/test_paper_results_integrity.py`

**Interfaces:**
- Produces: complete paper raw/aggregate/figure artifact set and integrity hashes.
- Consumes: frozen configuration and all passing pilot manifests.

- [ ] **Step 1: Add an integrity test that recomputes aggregates from raw data**

```python
def test_checked_in_aggregates_reproduce_from_raw(self):
    with tempfile.TemporaryDirectory() as directory:
        aggregate_all(Path("results/raw/paper"), Path(directory))
        self.assertEqual(tree_hash(Path(directory)),
                         tree_hash(Path("results/aggregated/paper")))

def test_all_required_vector_figures_exist(self):
    for stem in REQUIRED_FIGURE_STEMS:
        self.assertTrue((FIGURE_DIR / f"{stem}.pdf").read_bytes().startswith(b"%PDF"))
```

- [ ] **Step 2: Run the integrity test and confirm failure before paper output exists**

Run: `python3 -m unittest tests.test_paper_results_integrity -v`

Expected: FAIL on missing paper artifacts.

- [ ] **Step 3: Run the complete paper experiment suite**

Run: `python3 scripts/run_paper.py`

Expected: all four experiments finish, each point has 30 topology clusters,
rate points have 150 event instances, and no sanity errors are reported.

- [ ] **Step 4: Independently regenerate aggregates and figures**

Run: `python3 scripts/aggregate_results.py --input-root results/raw/paper --output-root results/aggregated/paper --all`

Run: `python3 scripts/plot_paper_figures.py --input-root results/aggregated/paper --output-dir results/paper_figures --all`

Expected: byte-stable CSV values for the same environment/config and all 12 PDF/PNG pairs.

- [ ] **Step 5: Run integrity tests**

Run: `python3 -m unittest tests.test_paper_results_integrity -v`

Expected: PASS.

### Task 3: Experiment Report and Final Scientific Audit

**Files:**
- Create: `scripts/write_experiment_report.py`
- Create: `results/EXPERIMENT_REPORT.md`
- Create: `results/aggregated/paper/integrity_report.json`
- Test: `tests/test_paper_report.py`

**Interfaces:**
- Produces: report sections for Exp.1--4 and final seven-question summary.
- Consumes: frozen config, method metadata, pilot manifests, paper aggregates, sanity outputs, and raw locations.

- [ ] **Step 1: Write failing report-content tests**

```python
def test_report_contains_required_methods_and_final_questions(self):
    report = build_experiment_report(report_inputs)
    for text in ("A1-Agent-Embedded*", "SANet-DW*", "NetRen*", "NetKeeper*",
                 "baseline implementation anomaly", "raw CSV", "final PDF figures"):
        self.assertIn(text, report)
```

- [ ] **Step 2: Run report test and confirm failure**

Run: `python3 -m unittest tests.test_paper_report -v`

Expected: FAIL because report generation is missing.

- [ ] **Step 3: Generate evidence-backed report sections**

For each experiment, read actual config and aggregate values, describe the
implemented scenario/method adaptations, state pilot/paper sample counts, and
summarize observed mean/success/P95 trends. The final section answers: expected
mechanisms, unexpected results, baseline anomalies, all-success/all-failure,
stress adjustment need, raw CSV location, and PDF location. Never insert an
expected trend when the aggregate shows otherwise.

- [ ] **Step 4: Generate and validate the report**

Run: `python3 scripts/write_experiment_report.py --config configs/paper_experiments.yaml --pilot-root results/aggregated/pilot --paper-root results/aggregated/paper --output results/EXPERIMENT_REPORT.md`

Run: `python3 -m unittest tests.test_paper_report -v`

Expected: PASS.

- [ ] **Step 5: Commit paper data, figures, and report**

```bash
git add -- results/raw/paper results/aggregated/paper results/paper_figures results/EXPERIMENT_REPORT.md scripts/write_experiment_report.py tests/test_paper_results_integrity.py tests/test_paper_report.py
git commit -m "Generate final reproducible paper results"
```

### Task 4: Cleanup, Full Verification, Review, and GitHub Publication

**Files:**
- Remove: root zero-byte files named `--csv-output`, `--install-mode`, `--iperf-seconds`, `--json-output`, `--ping-count`, `--ping-interval-s`, `--rounds`, `--scenario`, `--semantic-mode`, `--sudo`, `--verify-mode`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Produces: a clean documented final entry point and published branch.
- Consumes: all completed paper artifacts.

- [ ] **Step 1: Verify every cleanup target is an untracked zero-byte regular file**

Run: `find . -maxdepth 1 -type f -size 0 -name '--*' -printf '%f\n' | sort`

Expected: exactly the eleven approved accidental command-option filenames.

- [ ] **Step 2: Remove only the verified accidental files and document canonical commands**

Delete the exact eleven files after the read-only check. Update `.gitignore` to
exclude caches, legacy bulk result directories, archives, and office documents
without ignoring `results/raw/{pilot,paper}`, `results/aggregated/{pilot,paper}`,
`results/paper_figures`, or `results/EXPERIMENT_REPORT.md`. Update README with
pilot, paper, aggregate, sanity, plotting, and report commands.

- [ ] **Step 3: Run the full verification suite**

Run: `python3 -m unittest discover -v`

Run: `python3 scripts/sanity_check_results.py --input-root results/raw/paper --all --output results/aggregated/paper/sanity.json`

Run: `python3 -m unittest tests.test_paper_results_integrity tests.test_paper_report -v`

Expected: all tests pass, sanity has zero errors, raw-to-aggregate reproduction passes, and all PDFs are vector files.

- [ ] **Step 4: Inspect the exact final Git scope**

Run: `git status --short`

Run: `git diff --check`

Run: `git diff --stat origin/codex/semantic-controller...HEAD`

Expected: only approved experiment implementation, configuration, tests,
reasonable raw/aggregate artifacts, figures, report, README, and cleanup changes.

- [ ] **Step 5: Commit cleanup and final documentation**

```bash
git add -- .gitignore README.md
git commit -m "Document and clean final paper experiment workflow"
```

- [ ] **Step 6: Perform completion review and push**

Use the `verification-before-completion` and `requesting-code-review` skills,
address any findings, rerun affected verification, then execute:

```bash
git push origin codex/semantic-controller
```

Expected: GitHub branch contains every verified phase commit and no unrelated uncommitted artifact is published.
