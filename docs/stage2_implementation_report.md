# 第二阶段实现报告：版本化事务与动态删除 Agent

## 1. 交付结论

本阶段在原有 `DAG -> Session -> GatewayRouteEntry -> Gateway install/ACK -> verify`
骨架上增量实现了统一四层任务状态、最小可用 pAgent、stable/staged 双规则库、
`RuleDelta`、`RuntimeEvent`、影响范围分析、单调时间戳日志和 `AGENT_REMOVE`
事务。没有替换现有 Controller、Gateway、E2E verifier 或测试床实现。

已实际贯通两条路径：

```text
成功：AGENT_REMOVE -> scope -> delta -> stage -> verify -> activate -> post-verify
失败：AGENT_REMOVE -> scope -> delta -> 部分 stage 失败 -> 全局 rollback
```

成功示例中，`agent-terminal-feedback` 被删除，任务由版本 1 提交到版本 2；
1 条业务边、1 个 Session、1 个 Route、2 个物理资源绑定和 2 条跨网关规则被删除，
其余 2 条业务边通过验证，残余规则为 0。故障示例中，`gw-cloud` 人为拒绝版本 2
的 stage，其他网关已产生的 staged 状态被统一清理，stable 版本仍为 1，回滚前后
stable 规则快照一致。

## 2. 修改文件

### 核心状态与事件

- `src/core/models.py`：扩展 `TaskSubnet` 为唯一运行真值，增加四层状态、版本、
  物理绑定、网关状态和规则稳定标识；`TaskSubnetState` 是兼容别名，不是第二份状态。
- `src/core/events.py`：新增 `RuntimeEvent`、`AGENT_REMOVE`、事件阶段和事件注入器。
- `src/core/rules.py`：新增 `RuleDelta` 和基于稳定 ID/内容的差分算法。
- `src/core/gateway.py`：增加 stable/staged 规则和 Session、版本检查、验证、激活、
  回滚及冲突检测。
- `src/core/__init__.py`：导出新模型与接口。

### 四层 Controller 路径

- `src/agents/phy_agent.py`：将 no-op pAgent 升级为最小物理状态与资源管理实现。
- `src/agents/__init__.py`：导出 `PhyAgent`，保留 `PhyAgentStub` 兼容别名。
- `src/sim/topology.py`：每个网关注册一个实际 pAgent，并提供初始容量与可靠性。
- `src/controller/feasibility.py`：新增应用、传输、网络、物理四层联合可行性检查。
- `src/controller/impact.py`：新增动态删除 Agent 的向下依赖追踪。
- `src/controller/transactions.py`：实现 Agent 删除的版本化事务编排和不变量验证。
- `src/controller/networking.py`：在原构建器中填充统一四层状态、物理绑定和版本 1
  规则，并增加统一 `handle_runtime_event()` 入口。
- `src/controller/risk.py`：启用非零物理层风险权重；物理风险按可靠性下界违约量计算。
- `src/controller/elastic.py`：旧 Session 调优路径保留业务边与 pAgent 绑定字段。
- `src/controller/__init__.py`：导出事务、影响范围和可行性接口。

### 日志、示例与兼容更新

- `src/metrics/collector.py`：新增独立的阶段二事件日志和 Agent 删除指标 schema。
- `src/metrics/__init__.py`：导出新 collector 和写出接口。
- `experiments/agent_removal_transaction.py`：成功提交与 stage 失败回滚的最小示例。
- `experiments/real_run.py`：真实目标 profile 更新为三个实际 pAgent ID。
- `src/sim/run_rescue_task.py`、`viz/trace.py`：去除“pAgent 占位”展示文本。
- `tests/test_stage2_agent_removal.py`：新增阶段二测试。
- `tests/test_async_agent_subnet.py`、`tests/test_networking_latency_measurement.py`：
  更新四个已不合理的旧断言。
- `README.md`：增加阶段二运行命令、能力和边界说明。

## 3. 旧接口保留情况

- 仍使用原 `TaskSubnet`；`TaskSubnetState = TaskSubnet`，避免两个真值来源。
- `app_agents`、`trans_agents`、`net_agents`、`phy_agents` 保留为兼容视图；
  新的字典状态是其来源。
- `Gateway.route_table` 保留为 active stable 规则视图；`install_subnet()`、
  `apply_update()` 和 `installed_route_table()` 仍可用。
- `AgentController.build_task_subnet()`、既有 DAG/Session/路径/规则编译方法和 E2E
  verifier 均保留。
- `TaskState.NETWORKED` 保留为 `STABLE` 的枚举兼容别名；对外状态值统一为 `stable`。
- `PhyAgentStub` 名称保留为 `PhyAgent` 的兼容别名，但行为不再是 no-op。
- 旧 `networking_latency_ms` 和 `service_interruption_ms` 兼容字段保持原含义。

## 4. pAgent 如何参与四层可行性

每个任务 Session 绑定源、目的接入域的 pAgent。Controller 在初始编译时按业务边
速率建立 `PhysicalResourceBinding`，并把 `PhysicalAgentState` 和绑定写入同一个
`TaskSubnet`。联合检查依次确认：

1. 应用端点存在且在线；
2. 对应传输 Session 和 tAgent 可用；
3. nAgent 路径可达且容量满足；
4. 每个绑定 pAgent 在线、可靠性不低于边/任务要求；
5. 物理预留不低于业务边速率，pAgent 聚合预留不超过总容量。

pAgent 可生成 `KEEP_RESOURCE`、`ALLOCATE_RESOURCE`、`RELEASE_RESOURCE` 和
`SWITCH_ACCESS` 候选动作；`execute()` 不直接改变绑定，只有 Controller 在已验证
流程中调用分配/释放接口。风险项 `lambda_h` 非零，且只计算预测可靠性低于任务
硬下界的违约量，正常可靠性不会产生伪风险。

## 5. 动态删除如何追踪远端依赖

`ImpactScopeAnalyzer` 首先计算删除 Agent 的所有入边和出边，然后按稳定标识追踪：

```text
BusinessEdge
  -> Session / tAgent
  -> Route / nAgent / gateway path
  -> source + destination pAgent bindings
  -> source、transit、destination Gateway rules
```

`affected_agents` 包括被删除 aAgent 及相关 t/n/p 支撑 Agent；
`affected_gateways` 来自所有命中的转发规则和物理绑定，而不是只取 aAgent 所在网关。
候选状态同时删除关联 Session、路径/监控支撑、物理绑定、规则和监控边 ID。规则表本身
是 allow-list，因此删除所有相关 allow 规则也清除了本地和远端白名单效果。

## 6. RuleDelta 计算

规则以 `rule_id` 建立旧、新索引，不使用对象地址。内容比较至少覆盖：

```text
task_id, rule_id, gateway_id, src_agent, dst_agent,
next_hop, route_id, priority, allowed
```

同时比较数据面动作、匹配字段、QoS、t/n/p 支撑信息。只有新状态存在的是 addition，
只有旧状态存在的是 deletion，同一 `rule_id` 但内容变化的是 update。配置版本不参与
内容差异判断，因此未改变规则可以继承到新 stable 配置而不重新下发；Gateway 的
task-level stable version 仍统一递增。

## 7. stage、activate 与 rollback

1. `stage_delta()` 从当前 stable task slice 复制候选，只在 staged 中应用 additions、
   updates 和 deletions，并保存 rollback 快照。
2. 所有任务相关网关进入同一 staged version；只有规则受影响网关携带规则差分，其他
   网关参加版本屏障，不重写未变规则。
3. `validate_staged()` 校验 task/gateway/version、动作目标、重复 ID 和冲突流匹配。
4. Controller 校验有效边规则完整、删除对象无规则/Session/物理绑定、未受影响边可达、
   非授权流无 allow 规则以及四层联合可行性。
5. 全部成功后并行 `activate()`；随后再次检查 stable version、staged 清空、业务可达性
   和残余规则，最后 `finalize()` 丢弃 rollback 快照。
6. 任一 stage、验证或 activate 失败时，对全部事务网关执行 rollback。即使已有网关完成
   activate，也从备份恢复上一 stable 版本，避免部分激活。

## 8. 如何防止旧版本覆盖新版本

- stage 要求候选版本严格高于该 task 的 stable version。
- 同一 task 已有其他 staged version 时拒绝新的 stage。
- 本次 additions/updates 中的 rule version 必须与 staged version 一致；未改变规则按
  原 rule version 继承，由 task-level stable version 表示其当前生效配置版本。
- activate 必须精确匹配当前 staged version，且高于 stable version。
- 延迟 activate ACK 对已清理或已升级的 stage 返回拒绝。
- 旧 rollback 不得清除更新版本的 staged 状态；提交 `finalize()` 后，迟到 rollback
  也不能恢复已关闭的旧快照。

## 9. 时间戳与未受影响业务

事件注入器先调用单调时钟记录 `occurred_at`，再把事件交给 Controller。
`MetricsCollector` 的第一条记录直接使用该值，后续记录拒绝倒序时间戳。

```text
elastic_latency_ms = (ACTIVATE_FINISHED - EVENT_OCCURRED) * 1000
rollback_latency_ms = (ROLLBACK_FINISHED - EVENT_OCCURRED) * 1000
```

最小示例中两条未受影响边在 staged 阶段继续使用 stable 规则，提交前和提交后均通过
synthetic business verifier，记录的 interrupted edge 数与逻辑中断时间均为 0。
这证明当前内存仿真没有观察到中断，不等价于真实 netns/远程网关上的零中断结论。

## 10. 测试结果与旧测试修改理由

开发前完整测试为 85 项通过。阶段二新增 26 项，完整套件共 111 项。

```text
旧测试通过数量：85
新增测试数量：26
失败测试数量：0
完整测试数量：111
```

修改的四个旧测试及理由：

1. `test_rescue_subnet_maps_business_edges_to_trans_and_net` 改为四层映射断言：
   pAgent 已按需求进入任务状态，旧的空集合断言失效。
2. `test_agent_loop_produces_three_layer_predictions` 改为四层预测断言：
   pAgent 现在上报可靠性与可用容量预测。
3. `test_physical_stub_is_noop_and_lambda_h_defaults_zero` 改为候选动作不直接修改状态：
   no-op 与零物理权重正是本阶段要求移除的行为。
4. `test_measurement_reports_networking_latency_samples` 的状态值从 `networked` 改为
   `stable`：版本化状态机明确使用 STABLE 作为已提交状态。

运行命令：

```bash
python3 -m unittest discover -s tests -v
```

## 11. 示例与原始产物

运行：

```bash
python3 -m experiments.agent_removal_transaction \
  --output-dir results/stage2 \
  --seed 7
```

产生：

- `agent_remove_success_events.jsonl`
- `agent_remove_success_metrics.json`
- `agent_remove_success_rule_snapshots.json`
- `agent_remove_stage_failure_events.jsonl`
- `agent_remove_stage_failure_metrics.json`
- `agent_remove_stage_failure_rule_snapshots.json`
- `agent_removal_metrics.csv`
- `run_summary.json`

这些数据由事件流程直接生成，未写死实验趋势或时延。

## 12. 当前简化与未实现事项

- 本阶段只支持 `AGENT_REMOVE`；未实现 Agent 增加/替换、边变化、链路/网关故障。
- 初始组网仍保留既有直接 install/ACK 骨架并建立版本 1；本阶段没有把四组论文实验一次性
  改成同一事务入口。
- pAgent 是容量/可靠性/预留抽象，不包含无线调度、资源块、调制编码或真实 PHY 驱动。
- 动态事务目前运行在进程内 Gateway；尚无持久化 WAL、进程崩溃恢复、分布式共识或并发
  事务锁，真实 netns installer 也尚未接入两阶段提交。
- allow 规则承担白名单语义，尚未维护独立 ACL 数据库。
- 状态监控项以 monitored edge/support binding 表示，尚未实现独立订阅服务。
- 未受影响业务中断来自本阶段 synthetic verifier 的事件观测，没有持续真实流量窗口。
- 未实现批量随机种子、论文 baseline、聚合统计和最终论文绘图。
