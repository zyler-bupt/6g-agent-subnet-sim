# Exp.2 Pilot Summary

> Pilot only — these values are for implementation and stress-range checks, not paper results.

- Raw trials: 280
- Topology seeds: 5
- Oracle-solvable instances: 62 / 70 (88.6%)
- Sanity errors: 0
- Sanity warnings: 0

## Conditional Coordination Results

| Method | Feasible Solution Rate (%) | QoS Satisfaction Rate (%) | Safe Rejection Rate (%) | P95 Resolution Latency (ms) |
|---|---:|---:|---:|---:|
| Proposed | 100.0 | 100.0 | 100.000 | 4.168 |
| SANet-DW* | 93.5 | 93.5 | 100.000 | 4.204 |
| Adjacent-Layer | 79.0 | 79.0 | 100.000 | 3.510 |
| Independent | 58.1 | 58.1 | 100.000 | 3.312 |

## Sanity Findings

- No findings.

## Outputs

- Raw CSV: `results/raw/pilot/exp2/trials.csv`
- Aggregate CSV: `results/aggregated/pilot/exp2/summary.csv`
- Figure: `results/paper_figures_final/Fig2_CrossLayer.{pdf,png,csv}`
- Panel (b) reports feasible resolution and safe rejection with separate denominators.
