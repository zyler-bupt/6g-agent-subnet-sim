# 第三阶段实现报告：初始组网事务统一与动态删减 Agent 实验闭环

## 1. 验收结论

第三阶段已完成以下闭环：

1. 初始组网由原来的直接安装统一为 `version 0 -> 1` 的
   `stage -> staged verify -> activate -> stable verify` 事务；
2. `Proposed-Incremental`、`Full-Rebuild` 和 `Local-Only` 只负责产生
   `ReconfigurationPlan`，共同使用 `TransactionExecutor`；
3. 同一随机种子先生成一个不可变场景快照，三种方法共享任务、拓扑、部署、
   DAG、删除对象、验证器和安装后端；
4. 未受影响业务边在事务期间由 `ContinuousBusinessProbe` 周期探测，并在每个
   事务边界额外采样；
5. pAgent 物理绑定进入目标快照，成功时原子替换，失败时恢复旧稳定快照；
6. 已完成 30 个种子的全配置矩阵、原始日志、统计、95% 置信区间以及 9 组
   PDF/PNG/CSV 论文图；
7. 第二阶段产物重新生成后，`scripts/audit_stage2_results.py` 的 10 项检查全部
   通过；完整单元测试为 125/125 通过。

## 2. 第二阶段审计

新增 `scripts/audit_stage2_results.py`，读取用户指定的 5 个 Stage-2 文件并检查：

- 成功事件的时间戳、版本 1→2、staged 清空、残余规则和删除对象；
- 失败事件的全局 version 1 回滚、staged 清空和 stable 快照相等；
- 未受影响边仍存在且可达。

首次审计发现旧产物缺少“任务对象删除结果”和“未受影响边可达性”的证据字段，
并非事务代码静默失败。随后在 Stage-2 产物生成器中加入任务状态、实际 pAgent
资源及边可达性快照，重新生成产物后结果为：

```text
PASS
10/10 stage-2 artifact checks passed
```

## 3. 初始组网如何使用版本化事务

`AgentController.compile_task_subnet()` 只完成成员确认、四层绑定、Session/路径、
物理目标快照、四层可行性和规则编译，不修改任何 Gateway stable 状态。

`AgentController.build_task_subnet()` 随后创建 version 1 全量计划，并交给共享执行器：

```text
Task received (v0)
  -> mapping
  -> four-layer binding
  -> feasibility
  -> compile
  -> stage v1 on every involved gateway
  -> verify staged rules/sessions/version/QoS
  -> activate v1
  -> verify stable rules/sessions/version/QoS
  -> finalize rollback window
```

任一 stage、activate 或验证失败时，对全部事务参与网关执行 rollback。初始失败的
Gateway 保持 version 0、stable 为空、staged 为空，Controller 不登记稳定任务，
候选 `TaskSubnet` 状态为 `FAILED`。

新增完整绝对时间戳：`t_task_received`、`t_mapping_finished`、
`t_layer_binding_finished`、`t_feasibility_finished`、`t_compile_finished`、
`t_stage_started`、`t_stage_finished`、`t_staged_verify_finished`、
`t_activate_finished` 和 `t_stable_verify_finished`。

`formation_latency_ms` 从 task received 到 stable verification finished；旧字段
`controller_build_ms` 和 `networking_latency_ms` 保留为兼容别名，其明确口径是
`controller_compute_ms`，不再被当作完整组网时延。

## 4. 三种方法的真实代码差异

### Proposed-Incremental

`ImpactScopeAnalyzer` 从删除 Agent 的入边/出边继续追踪 Session、Route、t/n/p
支撑、物理绑定和路径上全部本地/中间/远端规则。目标状态继承未受影响对象，
`compute_rule_delta()` 只输出真实 additions、updates 和 deletions。

只有内容发生变化的 Gateway 计入 `affected_gateways`。为满足 task version 全局
一致性，其他仍参与任务的 Gateway 参加零差分 version barrier；这不会把其规则
计为变更，但可防止部分网关停留在旧版本。

### Full-Rebuild

从删除后的 `TaskSpec` 重新执行完整成员确认、四层绑定、Session 生成、路径选择和
规则编译。即使规则 ID 未变，所有存活目标规则也显式列为 updates 并向全部任务
Gateway 重新 stage，因此它不是固定单价或对 Proposed 乘系数得到的结果。

旧 stable 版本在提交前仍保持服务；Full-Rebuild 不先破坏性清空规则。这样三种
方法比较的是规划与安装范围，而不是由不同提交语义人为造成的停机差异。

### Local-Only

只向被删除 Agent 所在 Gateway 及路径上的直接相邻 Gateway 下发局部差分，不把
远端路径规则加入事务。共享 Verifier 会实际比较目标状态与所有任务 Gateway 的
staged/stable 规则、Session 和版本。因此，远端残余规则、Session 集不一致或版本
不一致会自然触发失败和全局回滚；代码没有预先把 Local-Only 标记为失败。

## 5. 共享事务执行器

`TransactionExecutor` 对三种计划执行完全相同的过程：

1. 向计划中的事务 Gateway stage 规则差分及本地 Session 快照；
2. 所有 stage ACK 成功后才进行 staged 验证；
3. 检查规则集合、规则内容、Session、版本、授权边、四层可行性及可选业务 QoS；
4. 统一 activate；
5. 替换 pAgent 物理资源目标快照；
6. stable 再验证；
7. 成功后 finalize，失败则恢复 Gateway 和物理资源的旧稳定快照。

Gateway 拒绝小于或等于 stable version 的 stage，activate 还要求精确匹配 staged
version，因此旧配置和延迟 ACK 不能覆盖新版本。

## 6. 公平场景生成

`ElasticScenarioGenerator` 使用局部 `random.Random(seed)`，生成：

- 无环 DAG（边始终从较小拓扑序指向较大拓扑序）；
- 可配置 Agent 数、边/Agent 比、Gateway 数和跨 Gateway 边比例；
- 线型多跳 Gateway 路径；
- 每个 Gateway 的 a/t/n/p Agent 部署；
- 可复现的 leaf、intermediate、fan-in 和 fan-out 删除对象；
- 至少删除 1 个 Agent 的比例删除事件。

每个快照计算 SHA-256 `scenario_fingerprint`。批量结果校验表明，同一
`(scenario, seed)` 下三种方法的 fingerprint 全部一致。不同方法不重新随机生成
任务或拓扑。

删除 intermediate Agent 时只移除原 DAG 中的关联边，不自动创建不存在的旁路边。

## 7. 时延与持续业务口径

成功运行的弹性时延为：

```text
event.occurred_at -> POST_ACTIVATE_VERIFY_FINISHED
```

失败运行不填写成功弹性时延，而单独记录：

```text
event.occurred_at -> ROLLBACK_FINISHED
```

所有时间来自 `perf_counter()` 的绝对单调时间戳。没有加入固定 sleep 放大结果；
探测器的可配置周期只用于观测，事务边界还会主动触发样本，使很快的内存事务同样
可审计。

探测器根据每条未受影响 Session 的完整 Gateway path 检查 active stable 规则，
记录 timestamp、reachable、latency、loss 和 throughput。中断定义为首次失败到
连续恢复健康的区间。

本阶段的共享两阶段提交在 stage 期间保留旧 stable 规则，完整 1890 次结果中三种
方法的未受影响边均未出现中断。这是当前内存事务语义的真实结果，不是在指标中
预设为 0；单元测试通过实际移除 stable 规则证明探测器可以发现中断。

## 8. pAgent 资源释放与回滚

编译阶段使用纯目标快照，不提前修改 pAgent。目标物理绑定包含 edge、pAgent、
Gateway、容量和版本。activate 后，执行器用目标绑定替换该任务的实际绑定；如果
容量检查、stable 验证或后续提交失败，则从事务前快照恢复全部旧绑定。

验证覆盖：

- 被删除 Agent 相关绑定在成功后全部释放；
- 未受影响绑定保持；
- rollback 后实际 pAgent 字典与事务前快照一致；
- 剩余任务继续经过物理容量与可靠性检查。

30-seed 批量结果中 Proposed 的 `residual_physical_resources` 总数为 0，每次成功
删除均至少释放 1 个实际物理绑定。

## 9. 批量实验与当前结果

默认配置对五类主变量逐一变化，共 21 个实验点；每点 30 个 seed、3 种方法，
合计 1890 次原始运行：

| 方法 | 成功 | 失败 | 成功运行平均弹性时延 | 成功运行平均规则变更比例 |
|---|---:|---:|---:|---:|
| Proposed | 630 | 0 | 36.212 ms | 0.1773 |
| Full-Rebuild | 630 | 0 | 41.499 ms | 1.4253 |
| Local-Only | 292 | 338 | 41.550 ms | 0.2339 |

Proposed 和 Full-Rebuild 的残余规则总数均为 0。Local-Only 有 179 次运行被检测到
远端删除规则残余；其余失败主要由 task version 或 Session 集不一致触发。失败
样本全部保存在 raw CSV，未删除异常值。

这些数值是当前机器上的控制面内存模拟观测值，不应直接外推为 6G 数据面实测性能。

## 10. 输出文件与图数据来源

原始输出：

- `results/exp3/raw/runs.csv`：1890 条主记录；
- `results/exp3/raw/events.jsonl`：逐阶段绝对时间戳；
- `results/exp3/raw/probe_samples.csv`：逐边持续探测样本；
- `results/exp3/raw/scenario_snapshots.json`：每个实验点/seed 的公平场景证据。

聚合输出：

- `results/exp3/processed/by_seed.csv`：不删样本的 seed 明细；
- `results/exp3/processed/summary.csv`：mean、median、sample std、95% CI、P95、
  success rate、rollback rate 和 residual-rule rate；
- `results/exp3/processed/failures.csv`：全部失败运行。

图片和对应 CSV 均位于 `results/exp3/figures/`：

| 图片 | CSV 来源/筛选 |
|---|---|
| `fig_elastic_latency_vs_agent_count` | summary 中 `agent_count` + elastic latency |
| `fig_elastic_latency_vs_removal_ratio` | `removal_ratio` + elastic latency |
| `fig_elastic_latency_vs_gateway_count` | `gateway_count` + elastic latency |
| `fig_changed_rules_ratio` | `removal_ratio` + changed-rules ratio |
| `fig_affected_gateways` | `removal_ratio` + affected gateways |
| `fig_unaffected_service_interruption` | `removal_ratio` + probe interruption |
| `fig_agent_removal_position` | `removal_position` + elastic latency |
| `fig_elastic_latency_breakdown` | 默认 20-Agent 点的实际阶段时间戳差 |
| `fig_success_and_residual_rule_rate` | `removal_ratio` 的 success/residual rates |

绘图脚本只读取聚合 CSV，不包含目标曲线、方法系数或趋势修正。

## 11. 修改和新增文件

第三阶段核心新增：

- `scripts/audit_stage2_results.py`
- `src/controller/reconfiguration.py`
- `src/controller/strategies.py`
- `src/controller/transaction_executor.py`
- `src/e2e/transactional_installers.py`
- `src/simulation/continuous_probe.py`
- `src/simulation/scenario_generator.py`
- `src/metrics/exp3.py`
- `experiments/exp3_business_elasticity.py`
- `configs/exp3_business_elasticity.yaml`
- `scripts/aggregate_exp3.py`
- `scripts/plot_exp3.py`
- `tests/test_stage3_business_elasticity.py`

核心修改：

- `src/controller/networking.py`：纯编译入口、初始事务、完整时间戳及事务化 rebuild；
- `src/controller/transactions.py`：Stage-2 删除入口改为共享策略/执行器的兼容封装；
- `src/core/events.py`：TASK_BUILD 与比例删除事件；
- `src/core/models.py`：完整 formation 指标；
- `src/controller/impact.py`：多 Agent 删除影响追踪；
- `src/core/gateway.py`：公开 stable/staged Session 读取接口；
- `README.md`、`.gitignore`：运行说明和交付结果范围。

## 12. 测试报告

```text
阶段二开始前旧测试：85
阶段二新增测试：26
第三阶段新增测试：14
总测试：125
失败：0
```

第三阶段没有修改既有测试。此前阶段二修改了两个旧测试：

1. pAgent no-op/三层预测测试改为四层实际状态、预测、绑定与非直接执行候选动作，
   因为 no-op 假设已与四层模型要求冲突；
2. 旧 `networked` 字符串断言改为统一稳定状态 `stable`，`NETWORKED` 仍保留为
   枚举兼容别名。

## 13. netns 与当前局限

`InMemoryTransactionalInstaller` 已完整运行本阶段批量实验。
`NetnsTransactionalInstaller` 与其共享接口，但当前显式抛出 `NotImplementedError`，
因为现有 netns 安装器是立即修改路由，尚无可证明安全的 shadow table 和原子切换。

因此：

> 当前批量结果属于事务逻辑与控制面模拟，不得描述为真实网络实测结果。

尚未实现：netns 两阶段提交、小规模 netns 删除验证、Agent 增加/替换、链路和
Gateway 故障、多路径切换、完整四层冲突实验、复杂无线协议和大规模并发任务。
另外，内存 Gateway 的 activate 很快且旧 stable 始终在线，因此不能据当前 0 ms
未受影响中断推断真实设备更新也一定无中断；后续应由 netns/硬件后端测量。
