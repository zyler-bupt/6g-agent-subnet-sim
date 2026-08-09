# WCNC paper result summary

> Scope: all four figures are regenerated from raw experiment records. Exp1 is an in-memory transactionally verified control-plane simulation; it does not contain ping or iPerf3 and is therefore reported in milliseconds.

## Initial formation

| Agents | T_form (ms) | T_ctrl (ms) | Verification share |
|---|---|---|---|
| 10 | 14.99 | 2.39 | 14.8% |
| 20 | 22.47 | 4.04 | 16.4% |
| 40 | 45.43 | 8.66 | 18.9% |
| 60 | 70.33 | 13.80 | 21.5% |
| 80 | 98.35 | 19.53 | 24.2% |

## QoS satisfaction at the highest scanned conflict level

| Conflict | Highest x | Proposed | Adjacent | Independent | w/o Verification |
|---|---|---|---|---|---|
| Application–Capacity | 1.4 | 100.0% | 0.0% | 0.0% | 0.0% |
| Transport–Network | 99 | 100.0% | 100.0% | 0.0% | 0.0% |
| Network–Physical | 50 | 100.0% | 100.0% | 0.0% | 0.0% |

## Robustness and coordination scale

- At 20% observation noise: Proposed 98.9% QoS satisfaction; Adjacent-Layer 87.8%.
- At 5 proposals/layer (625 raw combinations): Proposed exact search 100.93 ms; Adjacent-Layer 4.51 ms.

## Business-change elasticity

| Agents | Proposed (ms) | Full-Rebuild (ms) | Latency reduction |
|---|---|---|---|
| 10 | 18.02 | 19.01 | 5.2% |
| 20 | 26.15 | 30.45 | 14.1% |
| 40 | 53.68 | 60.82 | 11.7% |
| 60 | 84.21 | 102.33 | 17.7% |
| 80 | 112.58 | 135.06 | 16.6% |

| Removed | Proposed rule ratio | Full rule ratio | Rule reduction | Local-Only failure |
|---|---|---|---|---|
| 5% | 0.040 | 1.221 | 96.8% | 100.0% |
| 10% | 0.135 | 1.450 | 90.7% | 60.0% |
| 20% | 0.305 | 1.425 | 78.6% | 13.3% |
| 40% | 0.621 | 1.275 | 51.3% | 0.0% |

## Failure-driven recovery

| Failure | Proposed | Full-Rebuild | Network-Only | Rule reduction | Repair ms (P/F) |
|---|---|---|---|---|---|
| Agent Failure | 100.0% | 100.0% | 20.0% | 70.3% | 125.79 / 130.76 |
| Link Failure | 100.0% | 100.0% | 100.0% | 79.4% | 124.55 / 131.18 |
| Physical Capacity Drop | 100.0% | 100.0% | 40.0% | 72.9% | 126.22 / 129.96 |

The common 150 ms detection interval is excluded from the repair breakdown. Verification includes post-activation stable-health windows so each stack closes to the measured post-detection repair latency.
