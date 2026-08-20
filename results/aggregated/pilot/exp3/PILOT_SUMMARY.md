# Exp.3 Pilot Summary

> Pilot only — these values are for implementation and stress-range checks, not paper results.

- Raw trials: 200
- Topology seeds: 5
- Sanity errors: 0
- Sanity warnings: 2

## Method Ranking and Trade-off

| Method | Mean Successful Latency (ms) | P95 Latency (ms) | Rule Change Ratio (%) | Success Rate (%) | Unaffected Disturbance (%) |
|---|---:|---:|---:|---:|---:|
| Proposed | 9.869 | 11.295 | 31.7 | 100.0 | 0.0 |
| NetRen* | 13.536 | 14.910 | 82.0 | 100.0 | 90.6 |
| Full-Rebuild | 16.856 | 18.191 | 100.0 | 100.0 | 100.0 |
| Local-Only | 6.911 | 7.359 | 24.2 | 4.0 | 0.0 |

## Sanity Findings

- [WARNING] `ALWAYS_SUCCESS`: full_rebuild succeeds for every sampled affected_agents trial
- [WARNING] `ALWAYS_SUCCESS`: netren succeeds for every sampled affected_agents trial

## Outputs

- Raw CSV: `results/raw/pilot/exp3/trials.csv`
- Aggregate CSV: `results/aggregated/pilot/exp3/summary.csv`
- Figure: `results/paper_figures_final/Fig3_Elasticity.{pdf,png,csv}`
