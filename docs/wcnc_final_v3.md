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
