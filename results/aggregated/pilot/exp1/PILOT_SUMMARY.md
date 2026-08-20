# Exp.1 Pilot Summary

> Pilot only — these values are for implementation and stress-range checks, not paper results.

- Raw trials: 520
- Topology seeds: 5
- Sanity errors: 0
- Sanity warnings: 1

## Method Ranking

| Latency Rank | Method | Mean Formation Latency (ms) | P95 Formation Latency (ms) | Task-Size Success Rate (%) |
|---:|---|---:|---:|---:|
| 1 | Proposed | 108.765 | 125.879 | 100.0 |
| 2 | Proposed w/o Batch | 472.958 | 691.673 | 100.0 |
| 3 | CSPF | 1808.025 | 2977.340 | 98.6 |
| 4 | SRD | 3653.304 | 6478.973 | 97.1 |

## Sanity Findings

- [WARNING] `ALWAYS_SUCCESS`: proposed_without_batch succeeds for every sampled task_size trial

## Outputs

- Raw CSV: `results/raw/pilot/exp1/trials.csv`
- Aggregate CSV: `results/aggregated/pilot/exp1/summary.csv`
- Figure: `results/paper_figures_final/Fig1_Formation.{pdf,png,csv}`
