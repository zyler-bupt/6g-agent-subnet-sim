# Exp1 Transactional Formation Design

Date: 2026-08-27

## Decision

Exp1 remains an initial task-subnet formation experiment. It does not test
cross-layer proposal conflicts, load-factor search, or multi-layer action
arbitration; those remain exclusively in Exp2.

The existing `wcnc_final_v3` Exp1 netns artifact is retained as the immutable
nominal arm. A separately versioned arm named `exp1_transactional_v1` is added
and must never overwrite the nominal raw data. Its frozen config is
`configs/exp1_transactional_formation_v1.yaml`.

## Scientific questions

The nominal arm answers:

1. How does method-owned planning and deployment latency scale with the number
   of Agents?
2. How many deployment transactions and rules does each method require?

The transactional arm answers:

1. How long does each method take to reach the first verified-correct task
   subnet when deployment state is imperfect?
2. How much partial state, rollback work, and repeated control traffic is
   created before correctness is reached?

The transactional arm does not claim that CSPF or SFC has no validation. Each
baseline retains reasonable checks in its native information domain.

## Method boundaries

All methods receive the same Task DAG, Agent/Gateway mapping, observed
deployment state, deterministic fault event, netns backend, command executor,
and final ping/iperf3 verifier. No method reads the hidden fault schedule before
the fault is exposed through the common deployment API.

### Proposed

- Builds a Task-DAG-aware deployment graph.
- Groups independent commands into parallel prepare waves while respecting
  dependency order.
- Associates every prepared object with an observed configuration version.
- Commits a wave only after all members acknowledge prepare.
- On rejection, invalidates and retries only the rejected object's Task-DAG
  descendant closure plus directly shared gateway state.

This is configuration/transaction validation, not Exp2 cross-layer resource
optimization.

### CSPF-based Formation

- Prunes failed or insufficient-bandwidth links and enforces the path-delay
  constraint before deployment.
- Computes deterministic constrained paths with the frozen tie-break.
- Uses one transaction per flow. Independent flow transactions may execute in
  parallel; the implementation must not serialize them merely to weaken CSPF.
- On rejection, rolls back and recomputes the affected flow against the updated
  residual network state.
- Does not use Task-DAG dependency closure when choosing retry scope.

### Global SFC Embedding (Heuristic)

- Checks service-function availability, placement capacity, chain ordering,
  and network path constraints before deployment.
- Uses one transaction per source-to-sink chain. Independent chains may execute
  in parallel; hops within a chain preserve dependency order.
- On a rejected hop, rolls back and re-embeds the affected chain.
- Does not use the Proposed Task-DAG dependency closure.

## Common transaction API

Every method uses the same low-level API:

1. `prepare(transaction_id, expected_versions, commands)`
2. `commit(transaction_id)`
3. `abort(transaction_id)`
4. `verify_final(task_edges)`

Prepare checks only deployment invariants:

- target namespace/Gateway exists;
- expected configuration version matches;
- the command is syntactically and operationally admissible;
- the target returns an ACK before the common deadline;
- the transaction has not already been committed or aborted.

Prepare does not evaluate application/transport/network/physical proposal
combinations, shared QoS objectives, or an Exp2 oracle label.

Preparation writes route candidates into inactive task-specific policy tables
and records the corresponding activation rules without enabling them. Commit
activates the prepared rules; abort flushes the inactive tables. The audit
reads routes and rules back with `ip -j route` / `ip -j rule`, so prepared,
active, aborted, and rolled-back state is evidenced by the Linux data plane
rather than an in-memory flag.

Known prepare or command failure is handled before the expensive final data
plane verifier. Ping/iperf3 runs once a complete candidate has committed. A
candidate that commits but fails final verification is retained as a real
failure and may enter the method-owned rollback/replan path.

## Deterministic deployment-fault scenarios

Fault selection is derived only from `(scenario_class, num_agents, seed)` and
is identical across methods at the logical Task edge/Gateway level. The fault
schedule is hidden from method planning and recorded in raw provenance.

The frozen classes are:

1. `nominal`: no injected deployment fault; existing Exp1 data supplies this
   arm.
2. `stale_version`: one selected Gateway version advances after observation and
   before prepare.
3. `prepare_ack_timeout`: one selected Gateway withholds the first prepare ACK
   until the shared deadline, then behaves normally on the permitted retry.
4. `command_rejection`: one selected logical route command rejects its first
   installation attempt, then accepts the permitted retry.

These are control/deployment faults, not capacity conflicts. No sleep or
formula-derived latency is added to make a method slower. ACK timeouts use the
same real deadline and asynchronous control path for all methods. A pilot may
inspect runtime, exception coverage, and whether every class actually triggers;
it may not select parameters based on method ranking.

## State machine and timing

Each run follows:

```text
TASK_RECEIVED
  -> PLAN
  -> PREPARE
  -> COMMIT
  -> FINAL_VERIFY
  -> VERIFIED_CORRECT

PREPARE/COMMIT failure
  -> ABORT_OR_ROLLBACK
  -> UPDATE_OBSERVED_STATE
  -> METHOD_SCOPED_REPLAN
  -> PREPARE
```

`time_to_correct_formation` begins at `TASK_RECEIVED` and ends at the first
successful final verifier. It includes all planning, prepare, commit,
rollback, retry, and final verification time. A run that does not reach a
verified-correct state before the frozen deadline remains a timeout and is not
silently removed.

Method-owned latency ends at the successful commit and excludes only the
shared final verifier. It includes failed attempts and rollback work.

## Frozen grids

Pilot seeds: `9000:9019`.

Formal seeds: `0:49`, paired across all methods and scenario classes.

Agent counts: `4, 8, 12, 16, 20`.

Methods: `proposed`, `cspf`, `global_sfc_embedding`.

Transactional classes: `stale_version`, `prepare_ack_timeout`,
`command_rejection`. The existing nominal arm is reused by hash and execution
commit rather than rerun or overwritten.

The formal transactional grid contains exactly
`5 agent counts x 50 seeds x 3 methods x 3 classes = 2250` rows. The pilot
contains exactly `5 x 20 x 3 x 3 = 900` rows. Grid completeness is audited
before aggregation.

## Required raw schema

The transactional raw table records:

- `scenario_class`, `fault_schedule_fingerprint`, `logical_fault_target`;
- `observation_version_fingerprint`;
- `attempt_count`, `prepare_attempts`, `commit_attempts`;
- `rollback_count`, `rollback_scope_objects`, `wasted_rule_commands`;
- `partial_state_exposure_ms`;
- `planning_latency_ms`, `prepare_latency_ms`, `commit_latency_ms`;
- `rollback_replan_latency_ms`, `method_owned_formation_latency_ms`;
- `final_verification_latency_ms`, `time_to_correct_formation_ms`;
- `success`, `timeout`, `failure_stage`, and `failure_reason`;
- per-attempt event provenance and the common configuration hash.

All attempted rows, including failures and timeouts, remain immutable.

## Metrics and figures

Nominal main panel:

- Method-owned formation latency versus Agent count.
- Control messages and rules/deployment batches in an adjacent panel or table.

Transactional main panels:

- Time to First Verified-Correct Formation.
- Rollback Scope / Wasted Rule Commands.
- First-Attempt Commit Rate in a table unless it has meaningful separation.

The existing end-to-end verified nominal latency remains an auxiliary result
because the common verifier dominates its absolute value.

Continuous intervals use paired topology-seed cluster bootstrap. Proportion
metrics use Wilson 95% intervals with numerator and denominator. Paired
baseline-minus-Proposed differences are reported. Conditional latency is
always adjacent to unconditional success or timeout counts.

No expected ordering is forced. CSPF may be faster in small nominal cases. The
mechanism-level sanity expectation is that method-owned latency and rollback
work diverge as deployment faults expose different transaction/retry scopes.

## Isolation from Exp2

Exp1 transactional code must not import or call:

- Exp2 oracle/ground-truth classification;
- `evaluate_cross_layer_combination`;
- SANet-DW*, Weighted-Sum, or Independent selection;
- gamma demand generation;
- cross-layer proposal enumeration or hard feasibility search.

Tests enforce these boundaries.

## Data layout and provenance

Existing nominal artifacts remain under their current versioned Exp1 paths.
New artifacts use these exact non-overwriting paths:

```text
results/paper/wcnc_final_v3/
  raw/exp1_transactional/
  aggregated/exp1_transactional/
  pilot/exp1_transactional/
```

The final manifest records separate execution commits, config hashes, and raw
hashes for nominal and transactional arms. Figures may combine aggregated
statistics but every point must retain arm-specific raw provenance.

## Acceptance tests

Tests must prove:

1. The fault schedule is identical across paired methods and hidden during
   planning.
2. Exp1 transactional execution never calls Exp2 conflict/oracle code.
3. CSPF uses current residual capacity and may parallelize independent flows.
4. Global SFC validates placement/path constraints and may parallelize
   independent chains while preserving hop order.
5. Proposed retry scope follows Task-DAG descendants; baselines do not call
   that selector.
6. No commit occurs after a rejected prepare.
7. Abort removes prepared state; rollback removes committed partial state.
8. Failed attempts contribute to time, messages, rules, and rollback metrics.
9. Final ping/iperf3 is identical across methods.
10. Failures/timeouts remain in raw and in unconditional denominators.
11. Raw-to-aggregate and aggregate-to-figure outputs are reproducible.
12. Existing nominal raw hashes do not change.

## Remote execution

The remote launcher gains a resumable transactional pilot and formal step. It
must not block Exp2--Exp4 merely because the already complete nominal Exp1
runner returned nonzero for retained trial failures; it may continue only after
the exact 750-row nominal grid, configuration hash, and event audit pass.
