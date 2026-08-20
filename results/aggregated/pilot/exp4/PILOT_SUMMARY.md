# Exp.4 Pilot Summary

> Pilot only — these values are for implementation and stress-range checks, not paper results.

- Raw trials: 320
- Topology seeds: 5
- Sanity errors: 0
- Sanity warnings: 2

## Failure Type Results

| Failure Type | Method | Mean Successful Latency (ms) | Success Rate (%) | Rule Change Ratio (%) |
|---|---|---:|---:|---:|
| agent_failure | Proposed | 7.219 | 100.0 | 27.7 |
| agent_failure | NetKeeper* | N/A | 0.0 | 0.0 |
| agent_failure | CSPF | N/A | 0.0 | 0.0 |
| agent_failure | Full-Rebuild | 17.133 | 100.0 | 115.8 |
| link_failure | Proposed | 4.909 | 100.0 | 9.0 |
| link_failure | NetKeeper* | 4.602 | 100.0 | 9.7 |
| link_failure | CSPF | 4.242 | 100.0 | 9.7 |
| link_failure | Full-Rebuild | 15.948 | 100.0 | 101.6 |
| capacity_degradation | Proposed | 6.111 | 100.0 | 17.7 |
| capacity_degradation | NetKeeper* | 4.580 | 100.0 | 9.2 |
| capacity_degradation | CSPF | 5.189 | 50.0 | 8.3 |
| capacity_degradation | Full-Rebuild | 15.923 | 100.0 | 101.3 |

## Capacity Stress

- Proposed: 10%=100.0%, 20%=100.0%, 30%=100.0%, 40%=100.0%, 50%=100.0%
- NetKeeper*: 10%=100.0%, 20%=100.0%, 30%=100.0%, 40%=90.0%, 50%=50.0%
- CSPF: 10%=100.0%, 20%=100.0%, 30%=50.0%, 40%=50.0%, 50%=50.0%
- Full-Rebuild: 10%=100.0%, 20%=100.0%, 30%=100.0%, 40%=100.0%, 50%=100.0%

## Sanity Findings

- [WARNING] `ALWAYS_SUCCESS`: full_rebuild succeeds for every sampled capacity_stress trial
- [WARNING] `ALWAYS_SUCCESS`: full_rebuild succeeds for every sampled failure_type trial

## Outputs

- Raw CSV: `results/raw/pilot/exp4/trials.csv`
- Aggregate CSV: `results/aggregated/pilot/exp4/summary.csv`
- Figure: `results/paper_figures/Fig4_Failure_Recovery.{pdf,png}`
