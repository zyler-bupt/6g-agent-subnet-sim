# 第四阶段补充审计报告：跨层实验鲁棒性与非平凡性

## 1. 审计范围与结论

本补充阶段没有覆盖或修改 `results/exp2`。运行器在实验前后对该目录全部文件计算 SHA-256，并将清单保存为 `results/exp2_robustness/exp2_source_integrity.json`；前后哈希完全一致。

新增实验均标记为 `in_memory_transactional_control_plane_simulation`，属于事务逻辑和控制面仿真，不是 netns、真实无线协议栈或 6G 网络实测。

最终结果：

- 新增运行记录 2340 条，原 Exp2 分场景只读审计记录 2160 条；
- 同 seed 配对检验 804 项；
- runner error、协调超时、不安全提交和部分提交均为 0；
- 自动完整性审计 19/19 PASS；
- 完整测试 161/161 PASS，其中本阶段新增 18 个测试。

## 2. Proposed 的 100% 结果为何不是天然保证

原主实验共有 540 个唯一的 `scenario/pressure/seed` 样本：

| Ground Truth 分类 | 样本数 |
|---|---:|
| `NO_CONFLICT` | 254 |
| `RESOLVABLE_CONFLICT` | 286 |
| `UNRESOLVABLE_CONFLICT` | 0 |

原主实验的 Proposed QoS 满足率为 100%，首先是因为所有冲突都至少存在一个可行 Proposal 组合，原场景没有无解样本；其次，Proposed 对每层 3 个候选执行完整笛卡尔积搜索，每个单边场景检查 `3^4=81` 个组合，并从硬可行组合中按 Controller 策略选择动作。

该方法应准确描述为：

```text
exact feasible-combination search
```

它不是轻量启发式方法。

补充实验加入四种不可解决机制，每种 30 个 seed，共 120 个专用无解样本：

1. 应用需求大于所有物理容量候选；
2. 所有候选路径最低时延仍超过约束；
3. 最大端到端可靠性仍低于要求；
4. 共享资源总容量绝对不足。

这些场景的 Ground Truth 可行组合数均为 0。Proposed 的 QoS 满足率相应为 0%，但安全拒绝率为 100%，事务提交数为 0，稳定版本保持不变。因此 Proposed 不会因方法名称或 Ground Truth 标签而伪造成功。

## 3. Ground Truth 与 Proposed 的代码关系

| 项目 | Ground Truth | Proposed |
|---|---|---|
| 求解入口 | `GroundTruthSolver.solve` | `CrossLayerCoordinator._proposed` |
| 搜索方式 | 完整组合枚举 | 完整可行组合枚举 |
| 约束评估 | 公共系统约束函数 | 公共系统约束函数 |
| 目标函数 | QoS违约数、加权代价、改动数、资源量 | 瓶颈服务裕量、改动范围、执行开销、声明收益 |
| tie-break | proposal id 升序 | proposal id 降序 |
| 输入状态 | 独立深拷贝 | 方法自己的观测快照 |
| 读取 oracle 结果 | 不适用 | 否 |

二者共享论文定义的系统约束，但不共享求解函数、目标函数、tie-break、可变状态、缓存或求解结果。场景生成器将深拷贝交给 Ground Truth，Ground Truth 结果只保存标识和数值，不保存供 Proposed 使用的 Proposal 对象引用。

自动检查确认：

- Proposed 不读取 `ground_truth_best_combination`；
- Proposed 不读取冲突或可解决标签；
- 两个求解器运行前后输入摘要不变；
- 抽样 60 个案例中，Proposed 与 Ground Truth 最优组合仅 16.67% 完全相同。

原主实验中 Proposed 达到 100% 的直接原因是完整搜索找到了某个硬可行组合，不是复用 Ground Truth 的最优组合。完整报告见 `results/exp2_robustness/ground_truth_independence_report.json`。

## 4. 三类主场景的独立结果

以下数值来自原始 Exp2 的只读分场景审计，每类包含 180 个 seed/压力样本/方法。

| 场景 | 方法 | QoS满足率 | 不可行率 | 冲突检测率 | 可解决冲突解决率 | 回滚率 |
|---|---|---:|---:|---:|---:|---:|
| Application-Capacity | Proposed | 100.00% | 0.00% | 100.00% | 100.00% | 0.00% |
|  | Independent | 50.00% | 50.00% | 0.00% | 0.00% | 50.00% |
|  | Adjacent | 83.33% | 16.67% | 100.00% | 66.67% | 16.67% |
|  | w/o Verification | 50.00% | 50.00% | 0.00% | 0.00% | 50.00% |
| Transport-Network | Proposed | 100.00% | 0.00% | 100.00% | 100.00% | 0.00% |
|  | Independent | 50.00% | 50.00% | 0.00% | 0.00% | 50.00% |
|  | Adjacent | 100.00% | 0.00% | 100.00% | 100.00% | 0.00% |
|  | w/o Verification | 50.00% | 50.00% | 0.00% | 0.00% | 50.00% |
| Network-Physical | Proposed | 100.00% | 0.00% | 100.00% | 100.00% | 0.00% |
|  | Independent | 41.11% | 58.89% | 0.00% | 0.00% | 58.89% |
|  | Adjacent | 100.00% | 0.00% | 100.00% | 100.00% | 0.00% |
|  | w/o Verification | 41.11% | 58.89% | 0.00% | 0.00% | 58.89% |

分压力、seed 和事务时延记录位于 `raw/main_scenario_audit.csv` 和 `processed/scenario_specific_summary.csv`，并非只报告三个场景的混合均值。

## 5. Independent 与 w/o Verification 的真实差异

Independent 对每层分别选择本层效用最高 Proposal，不建立联合评分，也不执行全局预检查。

`w/o Verification` 枚举四层联合组合，根据 Proposal 声明收益、成本和动作交互进行联合排序，但跳过最终硬约束检查，直接交给公共事务执行器和执行后 Verifier。

在主实验、不可解决实验和陈旧状态实验的 810 对同 seed 样本中：

- Proposal 集合完全相同率为 0%；
- 动作集合完全相同率为 0%；
- 两者都没有调用 Proposed 的硬可行性过滤。

它们在部分总体指标上相同，是因为不同动作最终触发了相同瓶颈，并被同一个执行后 Verifier 拒绝和回滚，而不是因为共用结果函数。差异示例见 `results/exp2_robustness/independent_vs_noverification_report.json`。

## 6. 状态噪声

六个观测量使用相互独立、裁剪到合法区间的乘性高斯噪声。所有方法获得相同带噪观测和 Proposal 池，Ground Truth 始终对真实状态与同一 Proposal 池求解。

三个场景合并后，每点包含 90 个配对样本：

| 噪声标准差 | Proposed QoS | Proposed不可行率 | Adjacent QoS | Adjacent不可行率 |
|---:|---:|---:|---:|---:|
| 0% | 100.00% | 0.00% | 100.00% | 0.00% |
| 5% | 100.00% | 0.00% | 90.00% | 10.00% |
| 10% | 100.00% | 0.00% | 85.56% | 14.44% |
| 20% | 98.89% | 1.11% | 87.78% | 12.22% |

20% 噪声下 Proposed 有 1 次动作在真实状态上不可行，被统一执行后 Verifier 检出并成功回滚。这说明当前协调具备较强但非完美的噪声鲁棒性，也证明结果没有被写死为 100%。本组采样的误报率和漏报率均为 0，但 QoS 失败仍可能来自带噪状态下对可行组合的错误选择。

## 7. 状态陈旧

Proposal 在版本 1 生成；陈旧时间大于 0 时网络和物理状态变化，执行观测版本变为 2，Proposal 仍保留 `observed_version=1` 和原 read set。

- 0 ms：Proposed 与 Adjacent QoS 均为 100%；
- 20/50/100/200 ms：Proposed 的陈旧检测率和拒绝率均为 100%，不启动事务、不发生回滚；
- 相同陈旧点下 Adjacent 继续执行，随后 100% 被统一 Verifier 检出并回滚；
- 所有失败均无部分提交，稳定版本保持不变。

当前只实现拒绝明显过期 Proposal，尚未自动重新观测并生成 Proposal。因此陈旧时间大于 0 时 Proposed 的 QoS 恢复率为 0；这是安全性优先而非完整恢复闭环。

## 8. 缺失层级信息

缺失 `aAgent`、`tAgent`、`nAgent` 或 `pAgent` 观测时：

- 对应层从方法可见 Proposal 池中完全移除；
- 不使用真实值填补缺失观测；
- 观测状态使用显式保守上界或下界；
- Ground Truth 仅在独立 oracle 路径中使用完整真实状态。

Proposed 在四种缺失情况下均安全拒绝，事务执行数和不可行执行数均为 0。Adjacent 使用其余相邻信息继续规划，四种情况下均被统一 Verifier 判为不可行并成功回滚。两者 QoS 恢复率均为 0，说明本阶段实现的是安全降级，不是缺失观测下的服务恢复。

## 9. Proposal 规模与开销

每层候选数从 1 增加到 5。单个监控边的全局组合数分别为 1、16、81、256 和 625。

| 每层Proposal数 | Proposed评估数 | Proposed时延(ms) | 峰值内存(MB) | Adjacent候选对评估数 | Adjacent时延(ms) |
|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 0.479 | 0.007 | 3 | 0.747 |
| 2 | 16 | 2.873 | 0.026 | 12 | 1.211 |
| 3 | 81 | 13.145 | 0.046 | 27 | 2.019 |
| 4 | 256 | 41.389 | 0.110 | 48 | 3.113 |
| 5 | 625 | 100.925 | 0.238 | 75 | 4.506 |

Proposed 的增长与完整枚举一致，Adjacent 只计算三个相邻层候选对，约为 `3n^2`，不构造全局组合。原始数据通过 `combination_evaluation_mode` 区分两类评估。1000 ms 安全超时下无样本超时；超时路径已有单元测试，发生时会保留为失败记录。

当前结果支持单个受监控边、每层至多 5 个候选的小规模精确搜索。多个业务边联合时，空间会按所有边和层的候选数连乘，需要支配剪枝、分解、分支定界或有界启发式搜索。

## 10. 配对统计

所有核心比较均按相同 scenario、因子和 seed 配对：

- 二元指标采用 exact McNemar 检验；
- 连续指标采用 Wilcoxon signed-rank 正态近似；
- 分别报告 discordant-pair difference 和 rank-biserial correlation；
- 差值同时报告均值、中位数与 95% 置信区间。

例如，20% 噪声的 Transport-Network 场景中，Proposed 相比 Adjacent 的 QoS 满足率配对差为 0.333，McNemar `p=0.001953`；每层 5 个 Proposal 时，Proposed 的协调时延明显高于 Adjacent，Wilcoxon `p<2e-6`。完整 804 项结果位于 `processed/pairwise_tests.csv`。

## 11. 新增接口和运行方式

重点文件：

- `src/simulation/conflict_robustness.py`：不可解决、噪声、陈旧、缺失层和规模场景；
- `src/controller/cross_layer_coordinator.py`：独立 Proposed 策略目标、缺失层安全拒绝；
- `src/controller/ground_truth.py`：独立 oracle 和部分 Proposal 池接口；
- `src/metrics/exp2_robustness.py`：补充实验原始 schema；
- `experiments/exp2_cross_layer_robustness.py`：批量入口和旧结果哈希保护；
- `scripts/aggregate_exp2_robustness.py`：聚合与配对统计；
- `scripts/plot_exp2_robustness.py`：9 组图及 CSV；
- `scripts/audit_exp2_robustness.py`：19 项数据与代码审计；
- `tests/test_exp2_robustness.py`：18 个新增测试。

```bash
python3 -m experiments.exp2_cross_layer_robustness \
  --config configs/exp2_cross_layer_robustness.yaml \
  --seeds 0:29 \
  --output-dir results/exp2_robustness
python3 -m scripts.aggregate_exp2_robustness --results-dir results/exp2_robustness
python3 -m scripts.plot_exp2_robustness --results-dir results/exp2_robustness
python3 -m scripts.audit_exp2_robustness --results-dir results/exp2_robustness
```

## 12. 当前限制

1. 每个鲁棒性案例只联合一个受监控业务边，多边联合规模尚未验证。
2. 物理容量、可靠性、排队和观测噪声均为抽象模型。
3. 陈旧或缺失观测采取安全拒绝，没有自动 Proposal 再生成和恢复。
4. Ground Truth 与 Proposed 均为指数级枚举，只适用于小候选池。
5. Wilcoxon 使用正态近似且不依赖 SciPy，样本量为 30。
6. 结果属于内存事务控制面仿真，不代表数据面或真实无线环境性能。
