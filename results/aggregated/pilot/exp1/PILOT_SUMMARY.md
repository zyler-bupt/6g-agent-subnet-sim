# Exp.1 Pilot Summary

> Pilot only — these values are for implementation and stress-range checks, not paper results.

- Raw trials: 520
- Topology seeds: 5
- Sanity errors: 0
- Sanity warnings: 0

## Method Ranking

| Latency Rank | Method | Mean Formation Latency (ms) | P95 Formation Latency (ms) | Churn Success Rate (%) |
|---:|---|---:|---:|---:|
| 1 | Proposed | 24.583 | 35.229 | 100.0 |
| 2 | Proposed w/o Batch | 51.625 | 77.240 | 85.0 |
| 3 | CSPF | 106.246 | 168.527 | 65.0 |
| 4 | A1-Agent-Embedded* | 247.578 | 410.952 | 36.7 |

## Sanity Findings

- No findings.

## Outputs

- Raw CSV: `results/raw/pilot/exp1/trials.csv`
- Aggregate CSV: `results/aggregated/pilot/exp1/summary.csv`
- Figure: `results/paper_figures/Fig1_Formation.{pdf,png}`
