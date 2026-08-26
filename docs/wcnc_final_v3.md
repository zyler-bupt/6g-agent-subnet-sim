# Canonical WCNC experiment pipeline (`wcnc_final_v3`)

The frozen protocol is `configs/wcnc_final_v3.yaml`. Formal artifacts live only under `results/paper/wcnc_final_v3/`; historical result directories are never read by this pipeline.

## Result modes

- Exp1: `measured_netns`; all three methods use the same Linux netns/veth/tc setup and the same ping/iperf3 verifier.
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

## Pilot

```bash
.venv/bin/python scripts/pilot_wcnc_final_v3.py
.venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 9000:9019
```

Pilot selection may inspect only oracle class coverage, runtime, and exceptions.

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

Individual transactional experiments can be resumed with:

```bash
.venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 0:99
.venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp3 --seeds 0:99
.venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp4 --seeds 0:99
```

Aggregation, integrity audit, and figures:

```bash
.venv/bin/python scripts/aggregate_wcnc_final_v3.py
.venv/bin/python scripts/audit_wcnc_final_v3.py --write-manifest
MPLCONFIGDIR=/tmp/wcnc-v3-mpl .venv/bin/python scripts/plot_wcnc_final_v3.py
```

The plotter accepts only aggregated rows tagged `wcnc_final_v3` and rejects paths containing `demo` or `synthetic`.
