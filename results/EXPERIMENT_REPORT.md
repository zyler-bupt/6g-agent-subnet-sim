# Final Paper Experiment Report

> This report is generated from the checked paper-mode raw CSV and aggregate CSV. Pilot values are used only as implementation/stress checks.

论文图例说明：* denotes an adaptation of the corresponding published method to the common task-subnet simulation environment. We adapt the core algorithmic principle; we do not claim an exact reproduction of the original implementation.

正文采用 4 张组合图、共 9 个子图：Fig.1/2/3 各 2 个，Fig.4 为 3 个。Safe Rejection、gateway ratio、disturbance 等辅助指标保留在 CSV 与本报告中。

## Exp.1 — Initial Task Subnet Formation

### 场景实际实现

12 个 Gateway 构成 mesh-like topology；每个 seed 固定 topology、fork-join/modular DAG、Agent placement 和 QoS，并在四种方法间共享。Task size 为 8/12/16/20/24/28/32；churn 实验固定 24 Agents，使用 0/5/10/15/20/30%。Formation latency 覆盖 task received 到 stable verification finished 的完整事务周期。

### 方法实际实现

- A1-Agent-Embedded*：按 business edge 顺序执行 endpoint resolution、path、rule、deploy、verify、activate；不加入 LLM inference cost。
- CSPF：对每条 DAG business edge 执行 bandwidth/delay/connectivity constrained routing，并完成规则生成、部署、验证和稳定激活；不是只测路径计算。
- Proposed w/o Batch：与 Proposed 共享 DAG analysis、binding、path、verification 和 transaction semantics，唯一差异是 Gateway sequential stage/activation。
- Proposed：按 Gateway 分组并行 stage，随后 global verification、atomic activation 和 stable verification。

### Pilot / paper 数据范围

Pilot：130 paired instances / 520 method rows，5 seeds、每点约 10 trials。Paper：1110 paired instances / 4440 method rows，30 topology seeds；rate points 为 30×5=150 events。

### 主要趋势

Task size 8→32 时，Proposed mean latency 14.30→34.03 ms（32-Agent P95 36.37 ms）；w/o Batch 为 25.46→73.48 ms，CSPF 为 43.59→168.11 ms，A1-Agent-Embedded* 为 94.98→396.73 ms。所有曲线均随任务规模增长，A1 增长最快，batch/parallel deployment 的贡献清晰。30% churn 下 success 为 Proposed 94.0%、w/o Batch 62.7%、CSPF 6.0%、A1 0.0%。

## Exp.2 — Cross-Layer Conflict Resolution

### Conflict generator 与 feasible oracle

固定 20 business Agents、10 Gateways、30 DAG edges。对 round(conflict_density×|E_DAG|) 条业务边注入 Application↔Network、Transport↔Network、Network↔Physical 和 Cascaded Multi-Layer 四类冲突，目标混合为 25/20/20/35。每层提供 3 个全局候选 policy，共享 action space 为 3^4=81 combinations。独立 exact oracle 枚举这 81 个组合，只生成 ground-truth label，不接受 method 参数且 runtime 不计入方法。

Paper 中 oracle 判定 916/1050=87.2% instances 可解，落在 85–90% 目标内。FSR/QSR 仅以 solvable cases 为分母；SRR 仅以 infeasible cases 为分母，并在 aggregate CSV 保存 numerator/denominator。

### 方法实际实现

- Independent：四层分别以 worst-flow local objective 选择动作，组合后交给公共 verifier，不进行跨层 arbitration。
- Adjacent-Layer：仅顺序处理 a↔t、t↔n、n↔p pair，不能一次联合求解四层。
- SANet-DW*：依据当前各层 normalized violation 动态归一化 weights，对全部 81 个组合做 global weighted soft optimization；不在 selection 阶段调用 hard-feasibility filter。
- Proposed：显式构造 hard constraints、枚举 feasible combinations、执行 priority/cost arbitration，然后 verify/commit；无解时 safe rejection。

### Pilot / paper 数据范围与 high-conflict 表现

Pilot：70 paired instances / 280 rows。Paper：1050 paired instances / 4200 rows；每个 density point 150 instances。60% density 的 FSR/QSR 为 Proposed 100.00%、SANet-DW* 60.16%、Adjacent-Layer 0.00%、Independent 0.00%。40% 时 SANet-DW* 仍为 94.49%，说明其是有竞争力的 baseline；高 density 下显式 feasibility arbitration 的差异逐步显现。Proposed 在所有 intentionally infeasible cases 的 SRR 为 100%，其他三种方法为 0%，该辅助结果未占用正文子图。

## Exp.3 — Business-Change-Driven Elastic Reconfiguration

### Business change generation 与 affected scope

固定 24 Agents、12 Gateways、modular fork-join DAG。均衡生成 Agent Add、Agent Remove、DAG Edge Change、QoS Update；每次先生成真实 change，再计算 exact task dependency closure 和 affected_scope_ratio，最后放入 10/20/30/40/50% bucket，样本不足时继续采样，不反向修改结果。

### 方法实际实现

- Local-Only：仅 changed object、one-hop DAG neighbors 和直接 Gateway；不做 closure 或 escalation。
- NetRen*：从 changed requirements 推导 changed network flows，执行 configuration resynthesis、consistency repair、deployment 和 verification；不读取 task dependency closure。
- Full-Rebuild：重新编译 current DAG、全量生成/部署 rules 并验证。
- Proposed：计算 task/supporting-Agent closure 和 affected Gateways/flows，仅 stage RuleDelta；不可行时按 Tier-1→2→3 扩围。

### Pilot / paper 数据范围与 trade-off

Pilot：50 paired instances / 200 rows。Paper：750 paired instances / 3000 rows，每 bucket 150 events。Affected scope 10→50% 时，Proposed latency 8.41→11.04 ms，rule change 10.35→52.65%，success 始终 100%。Local-Only successful-case latency 6.89→9.39 ms、rule change 7.78→42.71%，但 success 从 28.00% 降到 5.33%。NetRen* success 为 100%，但 50% scope rule change 95.56%；Full-Rebuild 约 101.64%。结果体现 near Full-Rebuild correctness + near minimal necessary modification cost，而不是强行让 Proposed latency 最低。

## Exp.4 — Failure-Driven Elastic Recovery

### 三类 failure 与方法实际实现

Agent Failure 选择 active business Agent 并提供 1–3 compatible replacements；Link Failure 选择正在承载 task traffic 的 link 且大多数有 alternate route；Capacity Degradation 以 10/20/30/40/50% reduction 注入有限 spare capacity。Recovery latency 从 controller 收到 RuntimeEvent 后到 stable verification，不是 physical failure detection latency。

- CSPF：仅使用 topology/link state/bandwidth/delay 做 network rerouting，不能替换 business Agent。
- NetKeeper*：依据 runtime anomaly、traffic state 和 network policy 做 autonomous network configuration/resource adjustment；不使用 Task DAG closure 或 business-Agent replacement reasoning。
- Full-Rebuild：故障后重编译完整 members、DAG edges、supporting bindings 和 Gateway rules。
- Proposed：direct impact→Task DAG closure→cross-layer analysis→Tier-1/2/3 repair→RuleDelta→verify/activate。

### Pilot / paper 数据范围与 failure-specific 表现

Pilot：80 paired instances / 320 rows。Paper：1200 paired instances / 4800 rows，每 point 150 faults。Agent Failure success：Proposed 98.00%、Full-Rebuild 98.00%、CSPF/NetKeeper* 0%；Proposed mean successful latency 7.01 ms，rule change 28.08%，Full-Rebuild 分别 16.34 ms 和 116.34%。Link Failure 中 CSPF 3.95 ms，快于 Proposed 4.63 ms，符合 routing-only baseline 的能力边界。30% capacity degradation 下 success：Proposed 100.00%、NetKeeper* 100.00%、CSPF 32.00%、Full-Rebuild 100.00%。50% stress 下 Proposed/Full-Rebuild 保持 100%，NetKeeper*/CSPF 均为 32.00%。

## Final Audit Answers

1. 哪些结果符合预期机制？Exp.1 显示 batching/parallel deployment 的可扩展性；Exp.2 显示 weighted soft coordination 在高 conflict 下仍有竞争力，但 hard-feasibility arbitration 更稳定；Exp.3 显示 Proposed 的规则修改随 exact scope 增长且保持 Full-Rebuild 级 correctness；Exp.4 显示 failure-specific capability boundary，特别是 CSPF 的 Link Failure 优势和 Proposed 的 Agent Failure/capacity-stress 优势。
2. 哪些结果不完全符合直觉？NetRen* 在 Exp.3 全部成功，比“medium-high”预期更强，但付出了显著更大的 rule scope；Agent Failure 中 Proposed/Full-Rebuild 为 98% 而非 100%，说明 replacement/transaction 条件仍会自然失败；Local-Only 即使在 10% bucket 也只有 28% success，表明 one-hop scope 对部分 change type 已不足。
3. 是否存在 baseline implementation anomaly？没有发现 baseline implementation anomaly：四组 paper sanity 均为 0 error，paired method/fingerprint 完整，raw 可重聚合；ALWAYS_SUCCESS warnings 仅对应 NetRen*/Full-Rebuild 的设计能力，不是常量输出 bug。
4. 是否存在全部 100% / 全失败问题？低压力点出现所有方法 100% 是合理的；没有高压力点所有方法同时 100%，也没有任何点所有方法同时失败。个别方法的 100%（NetRen*/Full-Rebuild、Proposed solvable FSR）或 0%（network-only Agent Failure）均有明确机制解释。
5. 是否需要调整 stress range？当前不需要。四组均在低压力保持接近，并在目标机制区间产生分离；sanity 无 scenario-too-easy/too-difficult error。若论文篇幅允许，可把 SRR、gateway ratio 和 disturbance 放 appendix，而不是新增正文图。
6. raw CSV 在哪里？`results/raw/paper/exp1/trials.csv`、`exp2/trials.csv`、`exp3/trials.csv`、`exp4/trials.csv`；pilot 对应位于 `results/raw/pilot/`。
7. final PDF figures 在哪里？`results/paper_figures/Fig1_Formation.pdf`、`Fig2_Cross_Layer_Coordination.pdf`、`Fig3_Business_Elasticity.pdf`、`Fig4_Failure_Recovery.pdf`；同目录提供 PNG 预览。
