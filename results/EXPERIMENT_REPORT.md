# WCNC 2027 Final Experiment Report

> 本报告由 paper-mode trial-level raw CSV、cluster-bootstrap aggregate CSV 与 sanity 输出自动生成；pilot 仅用于逻辑和 stress 检查，不作为论文结果。

图例说明：* denotes an adaptation of the corresponding published method to the common task-subnet simulation environment. We adapt the core algorithmic principle; we do not claim an exact reproduction of the original implementation.

正文最终保留 4 张双栏组合图、共 8 个子图。原始成功率、P95、changed paths/agents、gateway ratio、disturbance 和 safe rejection 等辅助结果仍保存在 CSV。

## Exp.1 — Task Communication Subnet Formation

### 场景与时延口径

12 个 Gateway 构成平均 degree 3–4 的 mesh-like topology；每个 seed 固定 fork-join/modular DAG、Agent placement、QoS、拓扑和随机事件，并在全部方法间共享。Task size 为 8/12/16/20/24/28/32。Pilot 中 5% 背景 churn 会令小规模 SRD 过早失效、1% 又使全部曲线为 100%，因此按统一 stress-calibration 规则取中间的 2%，每次均重跑所有方法；paper 使用该同一 2% 轻量背景 churn。

- Controller Processing Latency (`T_ctrl`)：DAG parsing/dependency analysis、Agent mapping、path compilation 和 rule generation 的 controller critical path。
- End-to-End Formation Latency (`T_form`)：`T_ctrl + dispatch + install + verify + activate + post-activation stable report/verification`。Controller–Gateway RTT 为 5–20 ms，Gateway processing 为 1–5 ms，并显式计入 serialization；所有方法使用完全相同、由 paired event fingerprint 决定的控制面成本。

### 方法实际实现

- SRD：Sequential Rule Deployment；逐 business edge 执行 resolve→compile→deploy→verify→activate，Gateway 操作顺序累计。
- CSPF：对每条 business edge 做 bandwidth/delay/connectivity constrained routing，并包含规则生成、部署、验证和激活，不是只测路径计算。
- Proposed w/o Batch：与 Proposed 使用同一 DAG analysis、binding、routing、verification 和 transaction semantics，只把 Gateway stage/verify/activate 改为顺序执行；作为消融保留在 raw CSV，不占正文 Fig.1 图例。
- Proposed：DAG-aware compilation、Gateway batching、并行 Gateway deployment、全局验证、原子激活和 stable verification。

### Pilot / paper 数据范围与主要趋势

Pilot：130 paired instances / 520 method rows。Paper：1950 paired instances / 7800 method rows；paper mode 使用 30 topology seeds，rate point 每 seed 5 个独立事件。

Proposed T_ctrl: x=8→32 时 mean 6.58→22.39ms。
Proposed T_form: x=8→32 时 mean 93.45→119.30ms，高端点 P95=129.28ms。
CSPF T_form: x=8→32 时 mean 746.97→2889.84ms。
SRD T_form: x=8→32 时 mean 1380.79→5820.53ms。
Proposed formation success: x=8→32 时 mean 100.00→99.33%。
Fig.1(a) 使用明确标注的 logarithmic y-axis，以同时呈现约 10^2–10^4 ms 的真实端到端值；raw/aggregate 数值未缩放。

## Exp.2 — Cross-Layer Coordination

### Conflict generator 与 exact feasible oracle

固定 20 business Agents、10 Gateways、约 30 条 DAG edges；按 conflict density 注入 Application↔Network、Transport↔Network、Network↔Physical 与 Cascaded Multi-Layer 冲突（25/20/20/35）。所有方法共享相同的每层候选 action space。Exact oracle 枚举 3^4=81 个 action combinations，仅生成 ground-truth feasible label，不计入任何方法 runtime。

Paper oracle 判定 916/1050 = 87.2% instances 可解。FSR/QSR 只以 solvable instances 为分母；SRR 只以 intentionally infeasible instances 为分母。

### 方法实际实现

- Independent-Layer：四层独立选择 local objective 最优动作，组合后才进入公共 verifier。
- Adjacent-Layer：只允许 a↔t、t↔n、n↔p 的相邻层协调。
- SANet-DW*：根据 normalized layer violation 动态更新权重，对相同 81 个组合做全局 weighted soft optimization；选择阶段不使用 Proposed 的 hard-feasibility filter。
- Proposed：构造全局 hard constraints，执行 conflict detection、feasible search、priority arbitration、verification 与 commit/reject。

### Pilot / paper 数据范围与 high-conflict 表现

Pilot：70 paired instances / 280 method rows。Paper：1050 paired instances / 4200 method rows；paper mode 使用 30 topology seeds，rate point 每 seed 5 个独立事件。

Proposed high-conflict QoS: 100.00%（x=60）。
SANet-DW* high-conflict QoS: 60.16%（x=60）。
Adjacent-Layer high-conflict QoS: 0.00%（x=60）。
Independent-Layer high-conflict QoS: 0.00%（x=60）。
Proposed high-conflict FSR: 100.00%（x=60）。
SANet-DW* high-conflict FSR: 60.16%（x=60）。
Adjacent-Layer high-conflict FSR: 0.00%（x=60）。
Independent-Layer high-conflict FSR: 0.00%（x=60）。
Safe rejection 与 successful resolution 在 Fig.2(b) 中作为两个条件指标分别显示，未混用 denominator。50% conflict 下四种方法的 SRR 均为 100%：公共 transaction verifier 都能阻止 infeasible commit；Proposed 的优势来自 selection-time feasible search，而不是绕过公共安全检查。

## Exp.3 — Business-Driven Elastic Reconfiguration

### Business change 与 affected scope

固定 24 Agents、12 Gateways、modular fork-join DAG；均衡生成 Agent Add、Agent Remove、DAG Edge Change 和 QoS Update。事件先真实生成，再计算 exact dependency closure 和 affected-scope bucket，不按结果反向修改事件。正文横轴使用 Number of Affected Agents，即 exact closure 中 Agent id 的数量，避免用抽象 pressure level。

### 方法实际实现

- Local-Only：只修改 changed object、one-hop DAG neighbors 与直接 Gateway；不做完整 closure 或 scope escalation。
- NetRen*：按 changed service/network flows 做 configuration resynthesis、consistency repair、deployment 和 verification，不读取 task dependency closure。
- Full-Rebuild：重新编译 current DAG 并全量重部署 rules、paths 和 Agent bindings。
- Proposed：从 task/supporting-Agent dependency closure 形成 RuleDelta，仅 stage 必要 Gateway scope，并由公共 verifier 决定 commit/rollback。

### Pilot / paper 数据范围与 latency/scope trade-off

Pilot：50 paired instances / 200 method rows。Paper：750 paired instances / 3000 method rows；paper mode 使用 30 topology seeds，rate point 每 seed 5 个独立事件。

Local-Only latency: x=3→5 时 mean 6.83→7.03ms。
Proposed latency: x=3→15 时 mean 8.15→10.94ms，高端点 P95=11.83ms。
NetRen* latency: x=3→15 时 mean 11.83→14.09ms。
Full-Rebuild latency: x=3→15 时 mean 16.79→16.72ms。
Local-Only success: x=3→15 时 mean 47.62→7.41%。
Proposed success: x=3→15 时 mean 100.00→100.00%。
NetRen* success: x=3→15 时 mean 100.00→100.00%。
Full-Rebuild success: x=3→15 时 mean 100.00→100.00%。
Local-Only changed-rule ratio: x=3→15 时 mean 4.74→41.83%。
Proposed changed-rule ratio: x=3→15 时 mean 6.21→50.63%。
NetRen* changed-rule ratio: x=3→15 时 mean 52.27→94.45%。
Full-Rebuild changed-rule ratio: x=3→15 时 mean 100.00→100.00%。
正确性仍保存在 success/QoS raw 与 aggregate CSV：本实验的论文主张是接近 Full-Rebuild correctness，同时避免其全量 modification scope；并不要求 Proposed 比 Local-Only 更快。
Exact affected-Agent counts are observed after closure generation. Formal curves/report trends require at least 10 independent topology clusters per point; sparse tail points remain unchanged in raw/aggregate CSVs. Local-Only latency is conditional on successful stable verification, so its formal latency curve stops when that support threshold is no longer met.

## Exp.4 — Failure Recovery

### Failure generator 与方法实际实现

Agent Failure 为 active business Agent 提供 1–3 个 compatible replacements；Link Failure 选择承载 task traffic 的 active link，并让大多数 case 存在 alternate route；Physical Capacity Reduction 使用 10/20/30/40/50% reduction 和有限 spare capacity。Recovery latency 从 controller 收到 RuntimeEvent 后开始，不包含 physical failure detection latency。

- CSPF / network-only recovery：仅基于 topology、link state、bandwidth 与 delay reroute，不能替换 business Agent。
- NetKeeper*：保留该名称；依据 runtime anomaly、traffic state 与 network policy 更新网络配置/资源，但不使用 Task DAG closure、business-Agent replacement reasoning 或跨层弹性扩围。
- Full-Rebuild：故障后重新编译完整 members、DAG edges、supporting bindings、paths 与 Gateway rules。
- Proposed：direct impact→Task DAG closure→cross-layer analysis→Tier-1/2/3 repair→RuleDelta→verification→activate。

### Pilot / paper 数据范围与 failure-specific 表现

Pilot：80 paired instances / 320 method rows。Paper：1200 paired instances / 4800 method rows；paper mode 使用 30 topology seeds，rate point 每 seed 5 个独立事件。

Agent Failure — Proposed: success=98.00%, latency=7.01ms, scope=19.96%；NetKeeper*: success=0.00%, latency=N/A, scope=N/A；CSPF: success=0.00%, latency=N/A, scope=N/A；Full-Rebuild: success=98.00%, latency=16.34ms, scope=100.00%。
Link Failure — Proposed: success=99.33%, latency=4.63ms, scope=5.55%；NetKeeper*: success=99.33%, latency=4.31ms, scope=5.44%；CSPF: success=99.33%, latency=3.95ms, scope=5.44%；Full-Rebuild: success=99.33%, latency=15.24ms, scope=100.00%。
Capacity Reduction (30%) — Proposed: success=100.00%, latency=6.09ms, scope=11.99%；NetKeeper*: success=100.00%, latency=4.28ms, scope=5.02%；CSPF: success=32.00%, latency=5.25ms, scope=12.49%；Full-Rebuild: success=100.00%, latency=15.21ms, scope=100.00%。
Fig.4(b) 的 Modification Scope 统一为 `(changed rules + changed paths + changed agents)/(stable/target union rule objects + path objects + agent objects)`；失败且未提交的方法显示 N/A，而不是伪造 0% 成功修改。Success/QoS recovery 与 10–50% capacity stress 曲线保留在 raw/aggregate CSV 和正文讨论中。

## Final Audit Answers

1. 哪些结果符合预期机制？ Exp.1 分离 T_ctrl/T_form 并显示 Gateway batching/parallel deployment；Exp.2 区分 soft weighted coordination 与 hard feasibility；Exp.3 展示 correctness–scope trade-off；Exp.4 展示 Link/Agent/physical-capacity 三类 failure 的能力边界。具体数值见各节自动提取结果。
2. 哪些结果不符合？ 三点需要在正文如实解释：8-Agent 时 SRD 已为 1380.79 ms，而 Proposed 为 93.45 ms，小规模 latency gap 仍较大，这是 SRD 累计每个 Gateway RTT 的物理语义，不是数值缩放；60% conflict 时 Adjacent/Independent FSR 分别为 0.00%/0.00%，但 SANet-DW* 仍有竞争力；15 affected Agents（满足 cluster-support 门限的最高点）时 Local-Only success 为 7.41%，说明 one-hop baseline 在大 closure 下确实不足。以上均保留在 raw CSV，没有为了满足预设排序而修改。
3. 是否存在 baseline implementation anomaly？ 未发现；paper sanity error count=0。现有 warnings 仅为 Exp.3 NetRen*/Full-Rebuild 与 Exp.4 Full-Rebuild 的 always-success，符合其 resynthesis/full-rebuild 能力边界，未伴随 constant latency、zero latency 或 incomplete pair。
4. 是否存在全部 100% / 全失败问题？ 不存在所有方法整条曲线 100% 或所有方法同时失败；低 stress 的局部 100% 保留。
5. 是否需要调整 stress range？ 当前 sanity 未要求调整；可进入论文报告。
6. raw CSV 在哪里？`results/raw/paper/exp1/trials.csv`、`exp2/trials.csv`、`exp3/trials.csv`、`exp4/trials.csv`；pilot 位于 `results/raw/pilot/`。
7. final PDF figures 在哪里？`results/paper_figures_final/`，包含 Fig1_Formation、Fig2_CrossLayer、Fig3_Elasticity、Fig4_Recovery 的 PDF、PNG 与 figure-source CSV。
