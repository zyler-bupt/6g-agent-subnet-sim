# 第五阶段实现报告：故障与冲突驱动的弹性重配置

## 1. 结论与结果口径

本阶段已完成 `Conflict- and Failure-Driven Elastic Reconfiguration` 的内存事务
控制面实验闭环。主实验覆盖 3 类故障、每类 5 个等级、3 种方法和 30 个配对
随机种子，共 1350 次主运行；另有 180 次消融运行和 90 条复合场景方法级时间线。

主结果属于：

```text
in_memory_transactional_control_plane_simulation
```

它们不是 6G 无线数据面实测。实验时延来自实际 Python 规划、规则编译、序列化、
Gateway staged/ACK/activate、两次 Verifier 和健康窗口时间戳，不读取旧 `CostModel`，
也没有为了放大差异加入固定 sleep。

30-seed 主结果摘要如下。恢复时延包含统一的 150 ms 故障检测和 3 个健康窗口；
括号内为纯修复阶段均值。

| 故障类型 | 方法 | 恢复率 | 恢复时延 ms | 规则变更比例 | 网关影响比例 |
|---|---|---:|---:|---:|---:|
| Agent Failure | Proposed | 100% | 275.79 (125.79) | 0.306 | 0.923 |
| Agent Failure | Full-Rebuild | 100% | 280.76 (130.76) | 1.031 | 1.000 |
| Agent Failure | Network-Only | 20% | 277.01（仅成功样本） | 0.061 | 0.913 |
| Link Failure/Degradation | Proposed | 100% | 274.55 (124.55) | 0.220 | 0.972 |
| Link Failure/Degradation | Full-Rebuild | 100% | 281.18 (131.18) | 1.068 | 1.000 |
| Link Failure/Degradation | Network-Only | 100% | 274.81 (124.81) | 0.220 | 0.972 |
| Physical Capacity Drop | Proposed | 100% | 276.22 (126.22) | 0.271 | 0.967 |
| Physical Capacity Drop | Full-Rebuild | 100% | 279.96 (129.96) | 1.000 | 1.000 |
| Physical Capacity Drop | Network-Only | 40% | 274.24（仅成功样本） | 0.000 | 0.967 |

Proposed 相比 Full-Rebuild 的平均规则变更比例在 Agent、链路和物理故障中分别
减少约 70.3%、79.4% 和 72.9%。恢复总时延分别减少约 1.8%、2.4% 和 1.3%；
差异较小是因为三个方法共享 150 ms 检测口径与 100 ms 稳定健康窗口，且内存
Gateway 的事务操作很快。

所有主方法的未受影响业务中断均为 0。这个结果不是硬编码：stable/staged 双库
在提交前保持旧稳定规则，当前内存 activate 也没有模拟数据面切换空窗。因此本阶段
不能据此宣称 Full-Rebuild 一定造成更高真实数据面 collateral interruption；该问题
需要可执行的 netns shadow table/atomic swap 后端或真实网关验证。

## 2. 三类故障如何注入

### Agent Failure

`Gateway.fail_agent()` 在 `fault_effective_at` 将 Agent 卡状态变为 offline。主扫描包含：

- application Agent，同网关备用；
- application Agent，异网关备用；
- tAgent 故障；
- nAgent 故障；
- pAgent 故障。

业务 Agent 替换会保持已授权业务边语义和 edge ID，只将故障端点替换为预注册备用
Agent；异网关替换会重新编译关联 Session、路径、物理绑定和远端规则。支撑 Agent
故障通过同层、同能力备用 Agent 重绑定。若场景设置 `backup_available=false`，
Proposed 返回 `UNRESOLVABLE`，不提交虚假恢复；该路径有单元测试覆盖。

### Link Degradation / Link Failure

场景为每个跨网关目标提供两条候选路径。主路径上的链路容量按
`1.0, 0.8, 0.6, 0.4, 0.0` 缩放；0 表示不可达。故障链路会传播到所有包含该链路的
Route、Session、业务边、规则和网关。需要切换时，受影响 endpoint pair 分别计算
不包含故障链路的路径，而不是只修复最初选中的一条业务边。

### Physical Capacity Drop

pAgent 的总容量按 `1.0, 0.8, 0.6, 0.4, 0.2` 缩放，同时保持网络路径逻辑可达。
真实容量低于该 pAgent 当前聚合预留时，Proposed/Full-Rebuild 授权
`SWITCH_ACCESS`，排除退化接入并将相关物理绑定迁移到预注册备用 pAgent；
Network-Only 不能修改物理接入，因此只能在剩余容量仍足够的 1.0/0.8 两档成功。

## 3. 故障时间与公平检测

仿真先改变真实故障状态，再创建 RuntimeEvent。`RuntimeEvent.occurred_at` 等于
`fault_effective_at`，不是故障注入函数返回后的时间。每个场景保存连续监控样本，
默认每 50 ms 一次，连续 3 次异常后：

```text
failure_detected_at = fault_effective_at + 150 ms
```

同一 scenario/seed 下三种方法共享完全相同的故障生效时刻、监控样本和检测时刻。
规划和事务使用映射到该共同检测时刻的单调模拟时钟，同时以墙钟测量实际处理耗时。

```text
T_recovery = service_recovered_at - fault_effective_at
T_repair   = service_recovered_at - failure_detected_at
```

`service_recovered_at` 只在新版本已激活、版本一致、staged 为空、受影响边可达、QoS
满足、无残余规则且连续 3 个健康窗口后记录。

## 4. 三种方法的真实代码差异

### Proposed-Cross-Layer-Elastic

故障依赖闭包由 `ImpactScopeAnalyzer.analyze_failure()` 计算。四层均生成只读 Proposal，
Controller 根据故障类型选择必要的应用端点替换、传输重绑定、路径切换和物理接入
切换，执行故障真值下的提交前可行性验证，然后只编译真实 RuleDelta。所有任务网关
参加零差分版本屏障以保持版本一致，但 `affected_gateways` 只统计内容或依赖实际变化
的网关。

### Full-Rebuild

Full-Rebuild 使用相同四层恢复目标，但重新执行完整成员确认、支撑映射、全部 Session、
全部 Route 和全部 Rule 编译。所有仍存在的规则都作为 update 重新安装，而不是把
完整重建伪装成零差分。它与 Proposed 使用同一 `TransactionExecutor`、安装器和
Verifier。

### Reactive-Network-Only

该方法只接收 network Proposal，只能选择 `KEEP_ROUTE`、`SWITCH_ROUTE` 或带宽预留。
它在全部链路故障/退化中恢复成功，也能处理 nAgent 故障，说明纯网络异常下传统
重路由已经足够。它不能替换业务 Agent、重绑定 tAgent、迁移 pAgent 或降低应用/
传输负载，因此 Agent Failure 总体恢复率为 20%，Physical Capacity Drop 为 40%。
其 210 个主实验失败均以
`UNRESOLVABLE_BY_NETWORK_ONLY:cross-layer action required` 安全保留，没有伪造成功。

## 5. 四层协调开销与修改范围

Proposed 的协调阶段均值为：

- Agent Failure：4.37 ms；Network-Only 为 1.00 ms；
- Link Failure/Degradation：4.13 ms；Network-Only 为 4.31 ms；
- Physical Capacity Drop：4.82 ms；Network-Only 为 1.63 ms。

这里的协调阶段包含可执行目标编译；链路故障下 Network-Only 同样需要重算所有受
故障链路影响的 endpoint pair，因此与 Proposed 接近。控制消息与字节量来自实际
stage/validate/activate/ACK 序列化结果。

## 6. 验证、回滚和消融

所有正常方法在 staged 和 stable 两个阶段使用故障感知 Verifier。任一网关 stage、
staged 验证、activate、物理绑定切换或 stable 验证失败，统一回滚到旧版本。

消融场景对 `no_scope` 与 `no_verification_rollback` 注入相同的 post-observation 状态
漂移：

- `no_scope` 的事务 Verifier 检测到过期候选，触发全局 rollback，前后稳定快照一致；
- `no_verification_rollback` 使用结构验证提交 stale 候选，随后外部统一 Verifier
  记录 QoS 失败，但不回滚，因而不会被错误计为恢复成功。

这项漂移写入场景指纹，不依赖方法名生成不同故障。

## 7. pAgent 的作用

pAgent 不再是 no-op。它上报总/可用容量、可靠性和资源利用率；每条 Session 具有
端点物理绑定。Physical Capacity Drop 后，聚合绑定需求与真实容量一起进入统一
Verifier。授权 `SWITCH_ACCESS` 后，目标版本生成新的物理绑定快照；activate 时才
替换真实绑定。stage 失败不会修改旧绑定，activate 后失败则恢复旧快照。

## 8. 持续业务探测

`ContinuousBusinessProbe` 在事务开始、stage 完成、staged verify、activate、stable
verify 和提交/回滚边界采样未受影响边，并可按配置周期后台采样。受影响边另外保存
fault、detection、事务阶段和连续健康窗口样本。服务中断只由这些 probe 时间戳计算，
不使用固定 CostModel 单价。

## 9. 数据、统计、图和自动审计

原始文件位于 `results/exp4/raw/`：

- `runs.csv`：1530 条运行记录（1350 主实验 + 180 消融）；
- `events.jsonl`：故障、监控、范围、Proposal 和事务日志；真实回滚案例额外保留完整
  前后快照，成功事务不重复序列化同一规则库；
- `proposals.jsonl`：候选、选择、授权和拒绝原因；
- `scenarios.jsonl`：不可变场景、故障与监控样本；
- `probe_samples.csv`：受影响及未受影响业务采样；
- `timeline.csv`：30 seed × 3 方法的复合时间序列。

`scripts/aggregate_exp4.py` 输出 mean、median、std、95% CI、P95、成功率、回滚率和
同 seed 配对统计。连续指标使用 Wilcoxon signed-rank 正态近似，成功/失败使用精确
McNemar 检验，并报告 effect size。

`scripts/plot_exp4.py` 生成 10 张图；每张同时包含 PDF、PNG 和 CSV。绘图脚本只读取
聚合/时间序列数据，不平滑或修正结果。

`scripts/audit_exp4_results.py` 的 17 项完整性检查全部 PASS，包括 1350 主样本计数、
30 seeds、方法配对、公平故障时间、共享执行器、Network-Only 动作边界、版本、
staged、residual、QoS、失败保留、配对统计和 CostModel 隔离。

## 10. 测试结果

第五阶段新增 `tests/test_stage5_failure_reconfiguration.py`，覆盖用户要求的故障时间、
公平检测、三类影响范围、三种方法、链路切换、物理跨层必要性、QoS 恢复、持续探测、
回滚、残余规则、无备用安全拒绝和验证/回滚消融。

```text
旧测试通过：161
新增测试：18
总测试：179
失败：0
修改旧测试：0
```

`AgentController.compile_task_subnet()` 只增加了两个可选参数
`gateway_path_overrides` 与 `excluded_support_agents`，旧调用语义保持不变。

## 11. netns 真实 Linux 验证

已实现 `testbed/stage5_netns_link_failure.py`。脚本真实创建：

```text
primary: s5-gw1 -> s5-gw2 -> s5-gw4
backup : s5-gw1 -> s5-gw3 -> s5-gw4
```

并用 `ip link set` 关闭主链路、用 `ip route replace` 切换双向路由，以 ping 连续健康
窗口和 iperf3 验证恢复。它记录真实 `time.monotonic()` 墙钟、命令日志、路由前后和
吞吐，但明确只称为 mechanism validation，不宣称原子事务。

本环境 UID=1008，缺少 CAP_NET_ADMIN/CAP_SYS_ADMIN；`ip netns`/`tc` 预检失败，
受控 `sudo -n` 尝试也因需要密码而失败。因此本次没有伪造 netns 实测记录。
阻塞证据保存在 `results/exp4/netns/preflight_report.json`。有权限的机器可运行：

```bash
sudo python3 testbed/stage5_netns_link_failure.py --output-dir results/exp4/netns
```

## 12. 修改文件与保留接口

核心新增文件：

- `src/core/failures.py`；
- `src/simulation/failure_scenario_generator.py`；
- `src/controller/failure_recovery.py`；
- `src/e2e/failure_verifier.py`；
- `src/metrics/exp4.py`；
- `experiments/exp4_failure_reconfiguration.py`；
- `scripts/aggregate_exp4.py`、`scripts/plot_exp4.py`、`scripts/audit_exp4_results.py`；
- `configs/exp4_failure_reconfiguration.yaml`；
- `testbed/stage5_netns_link_failure.py`；
- `tests/test_stage5_failure_reconfiguration.py`。

扩展文件为 `src/core/events.py`、`src/controller/impact.py` 和
`src/controller/networking.py`。已有 DAG→Session→规则、Gateway stable/staged 双库、
RuleDelta、TransactionExecutor、ContinuousBusinessProbe、pAgent 绑定和全部旧实验接口
均保留。

## 13. 当前限制

- pAgent 是容量/可靠性/接入绑定抽象，不含真实调制编码、RB 调度、干扰和移动信道；
- 拓扑是可控的全连接网关抽象，路径选择不是实际 6G 路由协议；
- 监控样本和检测时间是确定性的逻辑仿真，虽然方法间公平，但不代表真实探针抖动；
- 内存 Gateway 的 activate 很快且未制造数据面切换空窗，因此 collateral impact 为 0；
- 批量实验未在 netns 运行，netns 后端也没有 shadow-table/atomic-swap；
- 第一版故障恢复使用确定性受限策略，不是大规模多任务全局优化；
- 尚未覆盖网关故障主实验、无线基站调度、移动性和多任务共享物理资源恢复。
