# Canonical WCNC experiment pipeline (`wcnc_final_v3`)

The frozen protocol is `configs/wcnc_final_v3.yaml`. Formal artifacts live only under `results/paper/wcnc_final_v3/`; historical result directories are never read by this pipeline.

## Result modes

- Exp1 nominal and `exp1_transactional_v1`: `measured_netns`; all three
  methods use the same Linux netns/veth/tc setup and final ping/iperf3 verifier.
- Exp2–Exp4: `transactional_simulation`; latency is simulation/control-plane time and is never described as physical testbed latency.

## Baselines

- Exp1: `proposed`, `cspf`, `global_sfc_embedding`.
- Exp2: `proposed`, `sanet_dw`, `weighted_sum`, `independent`.
- Exp3: `proposed`, `netren`, `local_only`, `full_rebuild`.
- Exp4 link: `proposed`, `cspf`, `full_rebuild`; Agent: `proposed`, `sfc_restoration`, `full_rebuild`; capacity: `proposed`, `te_reopt`, `full_rebuild`.

`SANet-DW*` and `NetRen*` are adapted baselines. The star must remain in every figure and the manuscript must not call either a full reproduction.

Exp1 uses method-owned deployment plans rather than a shared route installer:

- `proposed`: exact Task-DAG edge host routes in one parallel batch;
- `cspf`: deterministic constrained per-flow paths installed in sequential flow batches;
- `global_sfc_embedding`: a deterministic source-to-sink chain cover installed in sequential chain batches.

All three plans operate on the same Agent/Gateway mapping and finish with the same ping/iperf3 verifier. No sleep or formula-derived latency is added.

The outer namespace gate enumerates interfaces through the current netns
netlink view (`ip -j link show`), not the host-visible sysfs mount. Global SFC
installs a connected `/30` route in each per-Agent policy table and activates
each chain hop with a deterministic destination-specific rule before shared
verification.

The v3 verifier uses a two-second TCP iperf3 measurement window at the
unchanged 0.5 Mbps requirement. This reduces one-second slow-start/reporting
instability under netem loss and background traffic. Reported verified
latencies remain the measured values; no fixed duration is subtracted.
Because the verifier window is part of `T_form`, absolute v3 latency values are
not directly comparable with historical v2 runs that used a one-second window.

`Control Messages` counts controller deployment transactions (one sequential
batch is one transaction); `Rules Installed` counts the netlink route/rule
commands actually executed. Planning work units and every batch/command are
retained in the raw event log for audit.

## Exp1 analysis cohort and figures

The immutable Exp1 raw table retains every attempted method run.  A failure in
the `PREPARATION` stage is classified as infrastructure invalid because it
occurs before the method-owned formation work (for example, failure to start
the shared background-traffic fixture).  If any method has such a failure,
all three methods for that `(scenario_fingerprint, seed)` pair remain in raw
data but are excluded from paired method analysis.  The normalized rows record
this explicitly through `infrastructure_valid`,
`paired_analysis_eligible`, and `paired_exclusion_reason`.

Failures after preparation, including ping/iperf3 data-plane verification
failures, remain in the paired cohort and count against unconditional success.
The aggregate table also reports the paired-scenario retention rate and the
preparation-failure rate, so attrition cannot be hidden.  This paired
infrastructure rule is a documented post-run analysis amendment; it does not
alter, delete, or overwrite raw measurements.

Normalization and final integrity audit both require the exact frozen grid:
five task sizes, seeds 0--49, and the three canonical methods (750 rows total).
Validation is keyed by `(num_agents, seed, method_id)`, so deleting an entire
triplet or replacing one method with a duplicate cannot be hidden by a repeated
scenario fingerprint.

The Exp1 main result consists of two measured latency views:

- `Conditional Verified Latency (Successful Runs Only)` includes the common
  ping/iperf3 verification window and must be shown next to unconditional
  success counts in the table/report;
- `Route-installation Latency` isolates the method-owned deployment stage and
  is not formula-derived.

Because verified latency is dominated by the shared data-plane verification
window, it is used as the end-to-end scaling result rather than evidence of a
pointwise speedup.  Paired baseline-minus-Ours confidence intervals are the
primary method comparison.  Success is retained in the aggregate table with
Wilson numerator/denominator intervals, but Exp1 does not create a standalone
success-rate main figure when the methods are near saturation.

## Exp1 transactional arm

`exp1_transactional_v1` is a separate robustness arm, not a replacement for
the nominal Exp1 artifact. It injects deterministic deployment faults
(`stale_version`, `prepare_ack_timeout`, and `command_rejection`) at the common
transaction executor after method planning. These are configuration-version,
ACK, and command-installation faults. They are not Exp2 cross-layer capacity or
proposal conflicts, and this runner does not use the Exp2 oracle.

The primary latency values have distinct meanings:

- `Method-owned formation latency` includes planning, prepare, commit,
  rollback, and method-scoped retry, but excludes the common final verifier;
- `Time to correct formation` ends only at the first verified-correct subnet
  and therefore includes that verifier;
- failed and timeout trials remain in unconditional success denominators;
- time-to-correct is conditional on verified success and is interpreted next
  to the unconditional counts in the aggregate table.

The four transactional figures show measured method-owned latency,
time-to-correct, rollback scope, and wasted rule commands. There is no
standalone transactional success plot unless a future frozen protocol makes
it scientifically informative. Plotting reads aggregated CSV only and neither
smooths values nor enforces a trend.

## Pilot and smoke commands

```bash
.venv/bin/python scripts/pilot_wcnc_final_v3.py
.venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 9000:9019
```

Pilot selection may inspect only oracle class coverage, runtime, and exceptions.

Run the six-trial transactional Linux smoke (one class, one size, two seeds,
three methods) with:

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

Run the frozen 900-row pilot with no grid overrides:

```bash
sudo -E env \
  -u WCNC_EXP1_INSIDE_USERNS \
  -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/pilot/exp1_transactional \
  --require-complete-grid
```

## Formal run

On the Linux netns host:

```bash
scripts/run_wcnc_final_v3_remote.sh
```

The script first runs a `4 agents × 2 seeds × 3 methods` netns smoke. It stops before the formal run if any smoke trial fails. When unprivileged user namespaces cannot configure veth/netlink, it requests `sudo` and still creates a fresh outer network namespace automatically.

To run the smoke manually, explicitly remove any inherited internal sentinel:

```bash
sudo env \
  -u WCNC_EXP1_INSIDE_USERNS \
  -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_netns_verified_formation \
  --config configs/exp1_netns_verified_formation_pilot_v3.yaml \
  --output-dir results/paper/wcnc_final_v3/pilot/exp1_netns_smoke_v3 \
  --seeds 9000:9001 --task-sizes 4 \
  --methods proposed,cspf,global_sfc_embedding \
  --require-all-success
```

The runner rejects a manually forged namespace sentinel. Formal runs return non-zero if any trial fails or the expected grid is incomplete.

The launcher runs, in order: nominal smoke and formal validation,
transactional smoke, the 900-row transactional pilot, the 2250-row
transactional formal arm, Exp2--Exp4, aggregation, plotting, and the final
manifest audit. The exact transactional formal command is:

```bash
sudo -E env \
  -u WCNC_EXP1_INSIDE_USERNS \
  -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/exp1_transactional_staging_v1 \
  --require-complete-grid
```

The formal methods, fault classes, sizes, and seeds are read from the frozen
config; formal CLI grid overrides are forbidden. Normalization publishes into
separate immutable directories:

```text
raw/exp1/trials.csv
raw/exp1/execution_commit.txt
raw/exp1_transactional/trials.csv
raw/exp1_transactional/attempts.jsonl
raw/exp1_transactional/normalization_manifest.json
raw/exp1_transactional/execution_commit.txt
```

The two `execution_commit.txt` files identify the commit that actually ran
each measured arm; they need not be equal. An existing nominal staging tree
may be reused only when the read-only validator proves the exact 750-row grid,
paired fingerprints, frozen configuration hash, and causally complete event
provenance (identity, stage timestamps, counts, and terminal outcome), and its
original execution commit is present. An incomplete nominal attempt is never
overwritten: it is retained and the launcher starts the next numbered tree in
`exp1_netns_staging_v3_attempts/`, recording the validated selection in
`environment/exp1_nominal_selection.json`. This confined receipt stores the
relative staging path plus the runs, events, scope, configuration, and
execution-commit hashes. The selected path must be the canonical staging tree
or an `attempt-NNN` child, and the 40-hex execution commit must resolve as a
Git commit. The receipt is re-audited before normalization. A nonzero nominal
runner exit caused by retained trial failures is not itself masked: the
launcher continues only after this artifact audit and successful
normalization. Missing, duplicated, fabricated, or hash-drifted artifacts fail
closed. Transactional command failures are never converted to success.

Step markers bind the repository commit, complete protocol-file signature,
exact command signature, and current artifact hash. Therefore a changed
command or artifact cannot be skipped by an old marker. Canonical raw files
are copied only when absent; an existing different file is never overwritten.

At the time this orchestration was implemented, canonical formal raw for
Exp2--Exp4 was still missing. Those experiments remain explicit post-Exp1
steps; figures and the final PASS audit cannot complete until their formal raw
artifacts exist.

The Exp1 transactional runner appends only an exact schedule prefix. The
launcher writes `execution_commit.txt` before the first trial. `--resume` is
accepted only when it equals current `HEAD` and existing rows/events are a
causally valid, unmodified prefix of the same frozen grid. A complete artifact
is validation-only; a missing/skipped row, changed commit, or event tampering
fails closed. A manual resume repeats the original command with `--resume`:

After initialization and after every complete trial, the runner atomically
checkpoints both byte lengths and SHA-256 hashes in `measurement_scope.json`.
On resume, a shorter or changed authenticated prefix fails closed. Bytes after
the last checkpoint (for example, events written before their terminal CSV
row, or both files written before the scope replacement) are copied with hash
metadata into `recovery/quarantine-NNNN/` and only then atomically truncated
back to the authenticated prefix. The in-flight trial is rerun; committed
trials are not discarded.

```bash
sudo -E env -u WCNC_EXP1_INSIDE_USERNS -u WCNC_EXP1_PARENT_NETNS_INODE \
  .venv/bin/python -m experiments.exp1_transactional_formation \
  --config configs/exp1_transactional_formation_v1.yaml \
  --output-dir results/paper/wcnc_final_v3/exp1_transactional_staging_v1 \
  --require-complete-grid --resume
```

Exp2--Exp4 run under
`simulation_staging/<experiment>-<commit>-<protocol-signature>/`, never in
canonical raw. Before immutable whole-tree publication, the launcher verifies
the exact producer CSV schema, metric type/range domains, finite values, exact
0--99 seed grid, applicable method set, paired fingerprint, and an execution
commit equal to the current resolvable Git commit. Extra provenance files
already in canonical raw remain; any same-name byte difference aborts before
any staged file is copied. Publication is restartable: compatible files may
remain after interruption, but `publication_complete.json` is written
atomically only after every source artifact has been copied and hashed.

Aggregation, figures, and final integrity audit (in this order):

```bash
.venv/bin/python scripts/aggregate_wcnc_final_v3.py
MPLCONFIGDIR=/tmp/wcnc-v3-mpl .venv/bin/python scripts/plot_wcnc_final_v3.py
.venv/bin/python scripts/audit_wcnc_final_v3.py --write-manifest
```

The plotter accepts only aggregated rows tagged `wcnc_final_v3` and rejects paths containing `demo` or `synthetic`.
