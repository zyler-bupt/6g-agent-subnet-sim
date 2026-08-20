# WCNC 2027 Final Experiment Revision Design

## Objective

Revise the four existing paper experiments without replacing the simulator or
post-processing results.  The revision gives every plotted metric a defensible
communication-system meaning, keeps paired inputs identical across methods,
and reduces the main paper presentation to four double-column figures with two
panels each.

## Global experimental contract

- Every method in a paired trial receives the same topology, task DAG, Agent
  placement, QoS, event, and random seed.
- Trial CSVs remain the source of truth.  Aggregation and plotting may only
  select and summarize trial fields; they may not rescale or edit outcomes.
- Paper mode uses 30 topology seeds and five independent events for rate
  metrics.  Confidence intervals are clustered by topology seed.
- Continuous latency summaries include mean, P50, P95, and 95% CI.  Rate
  summaries use percentages and 95% cluster-bootstrap CI.
- NetKeeper* remains the Exp.4 baseline name.  It is described precisely as a
  NetKeeper-inspired network-configuration recovery adaptation and is not
  granted business-Agent replacement or task-dependency reasoning.

## Exp.1 — Task Communication Subnet Formation

The experiment records two distinct latency fields.  Controller processing
latency, `T_ctrl`, contains DAG parsing/analysis, supporting-Agent mapping,
path compilation, and rule generation.  End-to-end formation latency,
`T_form`, is the discrete-event completion time through controller-to-Gateway
dispatch, Gateway processing, rule installation, acknowledgement,
verification reporting, activation, and stable verification.

The control-plane model deterministically samples a 5–20 ms controller–Gateway
RTT and 1–5 ms Gateway processing time per Gateway and paired event.  The same
primitive costs are shared by every method.  Sequential deployment accumulates
Gateway operations; Proposed batches by Gateway and executes Gateway stages in
parallel.  The model is scheduled operation by operation and never multiplies
measured latency by a presentation factor.

The existing sequential edge-by-edge implementation becomes SRD (Sequential
Rule Deployment).  CSPF still performs complete path computation, rule
generation, deployment, and verification.  Proposed w/o Batch remains in raw
data as an ablation but the main figure contains SRD, CSPF, and Proposed.

Fig.1(a) plots end-to-end formation latency against the number of Agents with
95% CI shading.  Fig.1(b) plots formation success against the same Agent count.
A fixed, light background state-change probability is shared within every
paired trial so larger DAGs and slower sequential transactions naturally have
more exposure; the pilot may adjust this single common probability if every
method is 100% or every method fails, after which all methods are rerun.

## Exp.2 — Cross-Layer Coordination

The action space, exact feasibility oracle, and four methods remain unchanged:
Independent, Adjacent-Layer, SANet-DW*, and Proposed.  The oracle is used only
for ground-truth labeling and is excluded from runtime.

Fig.2(a) plots QoS satisfaction on solvable instances against conflict density.
Fig.2(b) is a grouped bar chart at the declared high-conflict operating point;
one bar reports feasible-solution rate over solvable instances and the other
reports safe-rejection rate over infeasible instances.  Their denominators are
kept separate.  No method-selection outcome is altered for the bar chart.

## Exp.3 — Business-Driven Elastic Reconfiguration

The existing balanced business changes and exact dependency closure remain.
For a physically meaningful horizontal variable, each event records the number
of business Agents in the exact dependency closure whose communication state
must change.  This is named `affected_agent_count` and is identical for all
methods in a paired event.

The methods remain Local-Only, NetRen*, Full-Rebuild, and Proposed.  Fig.3(a)
plots successful reconfiguration latency against affected Agent count with 95%
CI shading.  Fig.3(b) plots changed-rule ratio against affected Agent count.
Success rate, changed-Gateway ratio, and disturbance remain in raw/aggregate
data and the report so Local-Only's low cost is not confused with correctness.

## Exp.4 — Failure Recovery

The methods remain Proposed, NetKeeper*, CSPF, and Full-Rebuild.  NetKeeper* is
network-configuration recovery driven by anomaly, traffic, and policy state;
CSPF is constrained network-only recovery.  Therefore their recovery latency
is N/A for business-Agent failures they cannot repair, rather than zero.

Every trial records changed rules, changed paths, and changed Agents.  The main
modification-scope value is

`(changed_rules + changed_paths + changed_agents) /
 (total_rules + total_paths + total_agents)`.

Counts are derived by comparing stable and target subnet objects.  Failed or
inapplicable methods do not contribute a successful modification-scope bar.
Fig.4(a) shows successful recovery latency by Agent failure, link failure, and
30% physical-capacity reduction.  Fig.4(b) shows successful modification scope
for the same failure types.  Recovery success and the complete 10–50% capacity
stress series stay in aggregate CSV and the report; if success is mostly 100%,
it is reported in text instead of consuming a panel.

## Figure and artifact contract

The final output directory is `results/paper_figures_final/` and contains:

- `Fig1_Formation.pdf`, `.png`, `.csv`
- `Fig2_CrossLayer.pdf`, `.png`, `.csv`
- `Fig3_Elasticity.pdf`, `.png`, `.csv`
- `Fig4_Recovery.pdf`, `.png`, `.csv`

Each figure is 7.0 by 2.5 inches with two panels, white background, horizontal
dashed grid only, vector PDF, and 300-dpi PNG.  Axis labels are 10 pt, ticks
9 pt, legends 8–9 pt, lines 1.8 pt, and markers 5–6 pt.  Proposed is always
green `#2ca25f` with a diamond.  Baseline identities use consistent red,
orange, and blue styles; the ablation is gray.  CI shading is used only for
latency/scalability curves with alpha 0.15 and never on bar charts.

Pilot outputs are regenerated before paper outputs.  Formal execution is
allowed only after pilot sanity checks report no errors.  The final report
documents actual metric definitions, baseline capability boundaries, warnings,
raw paths, and figure paths without claiming exact reproduction of adapted
published systems.
