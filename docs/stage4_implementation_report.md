# 第四阶段实现报告：四层跨层冲突检测与协调实验

## 1. 验收结论

第四阶段已完成以下闭环：

1. aAgent、tAgent、nAgent 和 pAgent 从同一稳定任务状态生成不可变
   `LayerProposal`，Proposal 包含 read/write set、资源需求、预期 QoS 影响及受影响对象；
2. Proposal 生成不修改 `TaskSubnet`，实际配置只能由 `AgentController` 转换为
   `AuthorizedAction`；
3. 实现速率链、联合排队时延、端到端丢包、乘积可靠性、共享资源、路径—接入匹配
   和 write-set 冲突检查；
4. 实现 Proposed、Independent、Adjacent 和 no-verification 四种选择机制，四者共享
   场景、Proposal、安装后端、`TransactionExecutor` 和稳定态 Verifier；
5. 实现不接收 method 参数的独立穷举 Ground Truth；
6. 实现 Application-Capacity、Transport-Network、Network-Physical 三类主场景和
   Four-Layer Compound 时间序列；
7. 完成 30 seeds 正式实验、原始 JSONL/CSV、配对统计、95% CI、九组
   PDF/PNG/CSV 图和完整性审计；
8. Exp3 前置审计 12/12 通过，Exp2 结果审计 14/14 通过，全仓测试 143/143 通过。

本阶段结果统一标记为 `in_memory_transactional_control_plane_simulation`。它验证
控制面事务、跨层约束和方法机制，不是 netns、真实基站或 6G 数据面实测。

## 2. 第三阶段结果预审

新增 `scripts/audit_exp3_results.py`，读取 Exp3 的 runs、events、probe samples、
summary 和 failures，完成用户要求的 12 项检查。审计还生成：

- `results/exp3/processed/experiment_integrity_report.json`；
- `results/exp3/processed/pairwise_comparison.csv`。

配对表严格在相同 `(scenario, seed)` 下计算 Proposed 与 Full-Rebuild 的时延、规则、
Gateway 和未受影响业务中断差值。审计过程中发现旧聚合器把成功样本的
`rollback_latency_ms=0` 混入失败处理统计，现已将 rollback-only 指标只在实际
触发回滚的样本上聚合。重新聚合和绘图后审计结果为：

```text
PASS
12/12 checks passed
```

## 3. 四层分别观察什么、提出什么动作

四层状态位于 `src/core/cross_layer.py`，按业务边索引并挂接在唯一稳定
`TaskSubnet.cross_layer_runtime` 上。现有 Gateway 聚合状态继续服务于安装事务，
edge-scoped 跨层观测服务于本阶段联合约束，两者作用域明确，不互相覆盖。

### aAgent

观察任务阶段、需求速率、时延/丢包/可靠性要求、优先级和质量等级；可提出
`KEEP_QUALITY`、质量升降、应用速率升降和优先级变化。它不选择网络路径或接入资源。

### tAgent

观察 Session 的发送速率、拥塞窗口、重传、RTT、多路径和可靠传输模式；可提出
发送速率/拥塞窗口调整、多路径开关和可靠模式变化。

### nAgent

观察 Route 的可用带宽、利用率、队列、时延、丢包、可达性和候选路径；可提出
保持/切换路径、优先级调整和带宽预留/释放。

### pAgent

观察 access 的信号质量、可用容量、资源利用率、可靠性和在线状态；可提出
保持、分配、释放、重分配资源和切换接入。

所有 Agent 只执行 `observe() -> propose()`。本阶段实验路径不调用旧 Agent 的
`execute()` 或 `MetricProvider.apply_effect()`，因此生成候选时稳定状态和指标源均不变。

## 4. Proposal 为什么不会直接改变系统

`LayerProposal` 是 frozen dataclass。它声明：

- proposal/task/layer/action；
- target/read/write set；
- QoS gain、cost、confidence；
- 带宽、物理容量、时延和丢包预期；
- affected edge/session/route/Gateway；
- 参数候选值。

`collect_layer_proposals()` 只读取 `CrossLayerTaskState`。比较方法得到的是同一个
Proposal tuple，拒绝项和拒绝原因写入 `proposals.jsonl`。

只有 `AuthorizedActionExecutor.authorize()` 能生成 `authorized_by=AgentController`
的 `AuthorizedAction`。目标状态编译器会拒绝任务 ID 不匹配或授权者不是 Controller
的动作。授权动作随后被编译为 version+1 的 Session 速率、route/rule 内容和
pAgent 物理绑定目标快照，仍不会直接写 stable Gateway。

## 5. 如何识别动作组合冲突

`evaluate_cross_layer_combination()` 先按固定 a→t→n→p 顺序把 Proposal 投影到候选
状态，再统一计算：

```text
application rate <= transport rate <= network bandwidth <= physical capacity
```

并额外检查应用需求是否超过 network/physical 的实际瓶颈。

排队时延由当前网络背景利用率、队列占用和所选 transport 发送负载共同计算：

```text
effective utilization = background utilization
                      + 0.25 * send_rate / available_bandwidth
total latency = transport RTT + network latency + access latency + queue delay
```

端到端可靠性使用：

```text
R = R_transport * R_network * R_physical
```

丢包使用三个成功概率的乘积补集。共享资源需求包含受保护背景负载和目标业务需求，
不能仅验证单个 Proposal。Network-Physical 场景还检查所选 route 与 access binding
是否匹配。

两个非 KEEP Proposal 写同一对象且 `write_values` 不一致时产生 `WRITE_SET` 冲突。
统一冲突类型为 Application-Capacity、Transport-Network、Network-Physical、
Shared-Resource、Write-Set、QoS-Infeasible 和 Stale-State。

## 6. Proposed 与三个对比方法的真实代码差异

四种方法均由 `CrossLayerCoordinator.coordinate()` 接收同一个状态和 Proposal 池：

### Proposed-Four-Layer

对每个 edge/layer 选择一个 Proposal，第一版每个主场景为 `3^4=81` 个组合。每个组合
经过 write set、共享资源和端到端 QoS 全局检查。硬可行组合再按实际资源、改动范围、
执行开销和 Proposal 声明收益的确定性目标函数排序。

### Independent-Layer

每层只选择本层 utility 最高的 Proposal，Controller 合并后不调用联合可行性模型。
组合是否可行由激活后的共享稳定态 Verifier 判断，不在代码中预设失败。

### Adjacent-Layer

只执行 a↔t、t↔n、n↔p 三组局部检查。每组只关注其相邻边界的约束；后一个 pair
可以改变前一个 pair 的决定，合并后不做四层全局检查。因此它可以检测相邻冲突，
但仍可能遗漏共享资源或累积依赖冲突。

### w/o-Cross-Layer-Verification

枚举联合候选并根据 Agent 声明的 gain/cost 和交互改动开销排序，但不调用最终
可行性函数。它与 Independent 的实际选项不同，例如高压应用场景会保持物理资源，
但两者都可能在稳定态验证中暴露不可行配置。

方法名不会传入 feasibility、稳定态 Verifier 或 Ground Truth，代码中没有方法系数、
预设成功率或理想曲线。

## 7. Ground Truth 如何独立生成

`GroundTruthSolver.solve(state, proposals)` 没有 method 参数。它穷举小规模 Proposal
组合，记录：

- 每个组合的完整约束结果；
- feasible combination 集合；
- 按统一目标函数选出的 best feasible combination；
- 每个 Proposal 单独应用时的硬可行性；
- 各层局部最大收益组合是否冲突；
- 冲突是否存在至少一个可行解决组合。

四种方法执行前，场景生成器只计算一次 Ground Truth，并把同一对象与 SHA-256
fingerprint 写入场景快照。审计逐 scenario/seed 比较四种方法记录的状态、Proposal
和 Ground Truth，全部一致。

## 8. pAgent 如何真实参与

pAgent 不是权重为 0 的占位符：

1. `physical.available_capacity_mbps` 直接进入速率链和 Application-Capacity 约束；
2. `physical.reliability` 进入乘积可靠性和组合丢包；
3. `access_id` 与 nAgent 的 route access requirement 联合检查；
4. pAgent Proposal 可以改变容量、可靠性、signal quality、access latency 和 access ID；
5. 授权执行时，目标应用速率会编译为实际 `PhysicalResourceBinding` 预留；
6. 成功事务提交新物理快照，失败事务恢复旧 stable 物理绑定。

因此 Network-Physical 高压场景的不可行性由实际 pAgent 容量下降触发，Proposed
必须联合选择稳定 route 与 `SWITCH_ACCESS` 才能提交。

## 9. 冲突解决失败如何回滚

四种方法都调用同一个 `AuthorizedActionExecutor -> TransactionExecutor`：

```text
authorize
  -> compile version+1 target and RuleDelta
  -> stage every task Gateway
  -> staged structural/business verification
  -> activate
  -> replace physical bindings
  -> stable business + cross-layer verification
  -> commit or global rollback
```

`PostActivationCrossLayerVerifier` 在 staged 阶段保留共同的规则/Session/业务连通性
检查，在 stable 阶段加入完整跨层约束。这使 no-verification 确实跳过执行前联合检查，
而执行结果仍由与其他方法相同的 Verifier 判断。稳定验证失败时，Gateway stable
rules/session/version 和 pAgent 绑定一起恢复，staged 区清空。

正式主实验中的 602 条不可行执行全部触发 rollback 且 rollback 成功；加上 compound
和层级消融后，`failures.csv` 共保留 811 条失败记录，没有 runner error。

## 10. 场景与正式运行规模

三类主场景各 6 个压力等级、4 种方法、30 seeds：

```text
3 * 6 * 4 * 30 = 2160 runs
```

Compound 场景为 5 个连续时间点，每个 seed/method 形成一条时间序列主记录：

```text
30 seeds * 4 methods = 120 runs, 600 timeline samples
```

层级消融为 5 种方法 × 30 seeds，共 150 条。正式 `runs.csv` 总计 2430 条，
`pairwise_comparison.csv` 含 1710 条同 scenario/seed 配对记录。

主实验汇总如下：

| 方法 | 主运行 | QoS 满足率 | 不可行配置率 | 检测率 | 误报率 | 可解冲突解决率 | 平均协调时延 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Proposed | 540 | 1.0000 | 0.0000 | 1.0000 | 0.0000 | 1.0000 | 5.288 ms |
| Independent | 540 | 0.4704 | 0.5296 | 0.0000 | 0.0000 | 0.0000 | 0.035 ms |
| Adjacent | 540 | 0.9444 | 0.0556 | 1.0000 | 0.0000 | 0.8951 | 0.472 ms |
| w/o Verification | 540 | 0.4704 | 0.5296 | 0.0000 | 0.0000 | 0.0000 | 0.556 ms |

286/540 个 method-independent 场景样本存在冲突且均有可行解。Adjacent 能发现这些
相邻局部症状，但有 30 条最强共享资源案例在 pair 结果合并后仍不可行，所以其检测率
高于最终解决率。Proposed 的额外协调时延来自真实 81 组合约束计算。

这些观测值依赖当前机器和 Python 内存后端，不应外推为实际 6G 网络时延。

## 11. 输出文件与每张图的数据来源

原始输出位于 `results/exp2/raw/`：

- `runs.csv`：2430 条主记录；
- `events.jsonl`：Proposal、协调、stage、verify、activate/rollback 的绝对单调时间戳；
- `proposals.jsonl`：每个候选、是否选择、局部可行性和拒绝原因；
- `conflicts.jsonl`：独立 Ground Truth 和策略检测记录；
- `scenarios.jsonl`：去重后的方法无关场景快照；
- `compound_timeline.csv`：600 个复合场景时序样本。

处理输出位于 `results/exp2/processed/`：

- `summary.csv`：mean、median、sample std、95% CI、P95 和各类比例；
- `by_seed.csv`：全部 seed 明细；
- `failures.csv`：811 条原始失败，不删除异常样本；
- `pairwise_comparison.csv`：相同 scenario/seed 的 Proposed 配对差值；
- `experiment_integrity_report.json`：14/14 PASS。

每张图的 PDF、PNG 和输入 CSV 位于 `results/exp2/figures/`：

| 图片 | CSV 数据来源 |
|---|---|
| `fig_qos_satisfaction_vs_conflict_pressure` | summary 的主场景 QoS rate 与 95% CI |
| `fig_infeasible_rate_vs_conflict_pressure` | summary 的 infeasible rate 与 95% CI |
| `fig_conflict_detection_rate` | Ground Truth 条件下的 TP/(TP+FN) |
| `fig_conflict_resolution_rate` | resolvable Ground Truth 条件下的解决率 |
| `fig_false_alarm_rate` | 非冲突样本上的 FP/(FP+TN) |
| `fig_coordination_latency` | 原始协调起止时间的 mean ± 95% CI |
| `fig_coordination_overhead` | 实际事务 control bytes 的 mean ± 95% CI |
| `fig_layer_ablation` | application-capacity ρ=1.2 的五方法 QoS rate |
| `fig_compound_scenario_timeline` | timeline 中需求、发送、网络/物理容量、QoS 和检测时刻 |

绘图代码不平滑、修正或重写数据。

## 12. 修改和新增文件

第四阶段核心新增：

- `src/core/cross_layer.py`
- `src/agents/layer_proposals.py`
- `src/controller/conflicts.py`
- `src/controller/feasibility.py`
- `src/controller/ground_truth.py`
- `src/controller/cross_layer_coordinator.py`
- `src/controller/authorized_actions.py`
- `src/simulation/conflict_scenario_generator.py`
- `src/metrics/exp2.py`
- `experiments/exp2_cross_layer_conflict.py`
- `configs/exp2_cross_layer_conflict.yaml`
- `scripts/audit_exp3_results.py`
- `scripts/aggregate_exp2.py`
- `scripts/plot_exp2.py`
- `scripts/audit_exp2_results.py`
- `tests/test_stage4_cross_layer_conflict.py`

兼容性修改：

- `TaskSubnet` 增加 edge-scoped cross-layer runtime 和授权动作字段；
- `RuntimeEventType` 增加 `CROSS_LAYER_CONFLICT`；
- core/controller/agents/simulation/metrics 包导出新增接口；
- `to_jsonable()` 支持 frozenset；
- Exp3 rollback-only 聚合口径修复。

未修改前三阶段旧测试。测试统计为旧测试 125、新增测试 18、总计 143、失败 0。

## 13. 当前仍有的简化和局限

1. 每个主场景聚焦一条 monitored business edge；共享资源使用可审计的保护背景负载，
   尚未扫描多个同时产生 Proposal 的业务边组合；
2. 组合求解使用小候选池确定性穷举，规模扩大后需要 branch-and-bound、MILP 或启发式；
3. 排队、丢包、可靠性和无线容量是抽象模型，不模拟调制编码、RB 调度或真实协议栈；
4. Proposal profile 由可复现的场景状态生成，不包含在线学习或 LLM；
5. Synthetic business verifier 被配置为非瓶颈，用于隔离规则/连通性验证；论文不得把
   当前结果描述为真实流量吞吐实测；
6. Netns 两阶段提交仍未实现，本阶段没有 netns 结果；
7. 未实现链路/网关故障、Agent 新增/替换和完整多路径恢复，这些留待第五阶段；
8. Proposed 在本组设计中 100% 成功是因为 Ground Truth 表明全部注入冲突均可解，
   不代表任意外部状态下一定存在可行配置。
