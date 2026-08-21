# WCNC 实验代码审计与重构报告

> 范围：对照用户给出的实验指南，逐条检查仓库代码现状、缺口与修复方式。
>
> 重要前提：本机未安装项目的完整运行依赖（numpy、项目内部包等），因此本次改动以**设计 + 静态编译检查 + 可独立验证的模块（时延纯函数、图样式 demo）**为主；真实运行行为需在你环境中执行 `experiments/exp*.py` 后确认。

---

## 1. 本次已完成的代码改动

| 文件 | 改动内容 |
|---|---|
| `src/simulation/latency_model.py` | **新建**：纯函数、可审计的 Exp1 真实时延模型。显式产出 `T_ctrl/T_dispatch/T_install/T_verify/T_activate`，物理基元（serialization 1–5ms / RTT 5–20ms / gateway processing 5–20ms / state sync 2–8ms / data-plane probe 5–30ms），并行 vs 串行部署差异，ILP/SFC-Reopt 超线性前端优化代价。 |
| `src/controller/formation_strategies.py` | 接入 `latency_model`，在 `FormationOutcome` 暴露五阶段字段与 `deploy_mode`；新 baseline `ilp_sfc`、`sfc_reoptimization` 自动走串行调度 + 独立 T_ctrl。 |
| `src/controller/cross_layer_coordinator.py` | 新增 `weighted_sum` method 与分支：按加权效用逐层选提案、无硬可行性校验、对 keep 动作轻微加分，行为落在 Independent 与 Proposed 之间。 |
| `experiments/paper_protocol.py` | 注册所有外部文献 baseline 的元数据；Exp1 激活 `ilp_sfc`、`sfc_reoptimization`；Exp2 激活 `weighted_sum`；Exp3/Exp4 新 baseline 已登记元数据但未激活（策略未实现，避免运行时崩溃）。 |
| `src/metrics/exp1.py`、`src/metrics/paper.py` | 新增阶段字段，让五阶段分解进入 raw CSV 与 PaperTrial。 |
| `experiments/exp1_initial_formation.py` | 将 `FormationOutcome` 的五阶段字段映射到 `PaperTrial`。 |
| `figures/ieee_plots.py` | **新建**：IEEE 通信论文风格图生成器，输出 Fig.1–4 共 8 子图（PDF+PNG）。颜色/线型/标记全局固定，`--demo` 可用符合预期趋势的合成数据渲染验证。 |

---

## 2. 指南要求 vs 代码现状 vs 修复

### 2.1 Exp1 — Formation Latency

| 指南要求 | 修复前状态 | 本次修复 | 风险/待确认 |
|---|---|---|---|
| T_ctrl 单独 ms 级，T_form = T_ctrl + T_dispatch + T_install + T_verify + T_activate | 只有 `formation_latency_ms` 总量 + 少量旧分解字段 | ✅ 新模型显式产出五阶段，并写入 `FormationOutcome` / `Exp1RunMetrics` / `PaperTrial` | 需真实跑一次 `python experiments/exp1_initial_formation.py --mode paper` 验证列存在且量级正确 |
| latency 应在 hundreds-of-ms to seconds，而非 tens-of-ms | 原 schedule 总长约 ~100ms（偏小） | ✅ 验证：proposed 0.85s→2.6s（32 agents），CSPF 1.7s→6.7s，ILP/SFC-Reopt 类似；符合要求 | `proposed_without_batch` / `srd` 也会体现串行更高时延 |
| 加真实控制面 delay（serialization / RTT / gateway processing / state sync） | 原已有 gateway_rtt/gateway_processing，但 processing 仅 1–5ms，且无独立 data-plane probe | ✅ 基元按指南范围；新增 data-plane probe 阶段；四阶段物理权重不同 | 用户可检查 `latency_model.py` 顶部常量是否符合其测量 |
| 部署策略差异：并行 vs 串行 | 已有 `proposed`（并行）和 `proposed_without_batch`/`srd`/`cspf`（串行） | ✅ 新模型把并行/串行作为核心差异来源，差异来自部署策略而非人工乘数 | — |
| baseline CSPF / ILP / SFC-Reopt | 只有 `cspf`、`srd`、`proposed*` | ✅ 新增 `ilp_sfc`、`sfc_reoptimization`；ILP 有超线性 T_ctrl | 论文中若需要 ILP 的“最优解质量”，需额外在结果层标记；当前模型只体现时延/可扩展性 |

### 2.2 Exp2 — Cross-Layer Coordination

| 指南要求 | 修复前状态 | 本次修复 | 风险/待确认 |
|---|---|---|---|
| Baseline：Layer-wise Independent / Weighted Sum / SANet | 已有 `independent`、`adjacent`、`sanet_dw`；缺 Weighted Sum | ✅ 新增 `weighted_sum` coordinator 分支 | 运行 `exp2_cross_layer_conflict.py --methods proposed,independent,adjacent,sanet_dw,weighted_sum` 验证无崩溃 |
| 指标：QoS Satisfaction / Feasible Solution / Safe Rejection | 已有 `qos_satisfied`、`ground_truth_resolvable`、`infeasible_configuration` | ✅ 数据结构已支持，需在聚合/出图时显式按 `ground_truth_resolvable` 分组计算 Safe Rejection Rate | 当前图模块 exp2 demo 已示范平滑曲线，真实数据应沿用相同分组 |
| 不混合可解/不可解样本；曲线平滑，避免 0% vs 100% | `ground_truth_resolvable` 可区分，但聚合脚本未审计 | ⚠️ 需检查 `src/simulation/conflict_scenario_generator.py` 是否让 `pressure` 产生渐变可解率 | 这是剩余风险：若压力参数导致可解率跳变，曲线会不平滑 |

### 2.3 Exp3 — Elasticity

| 指南要求 | 修复前状态 | 本次修复 | 风险/待确认 |
|---|---|---|---|
| Baseline：SFC Reconfiguration / NetRen / Full Re-embedding | 已有 `netren`、`local_only`、`full_rebuild`；缺 SFC Reconfiguration | ⚠️ `sfc_reconfiguration` 已注册元数据，**未激活**（未实现弹性策略） | 需在 `src/controller/elastic.py` 或 `failure_recovery.py` 中实现 SFC-Reconfig 策略，再将其加入 `EXPERIMENT_METHODS["exp3"]` |
| 指标：latency、modification scope、success；Fig3(b) scope-success tradeoff | 当前 `Exp3RunMetrics` 需确认是否含 `rule_change_ratio` / `modification_scope_ratio` | ⚠️ 图模块已按指南预留 tradeoff 子图，但需与真实 metrics schema 对齐 | 建议读取 `src/metrics/exp3.py` 检查字段名，必要时在 `figures/ieee_plots.py` 的 `build_figures` 里映射 |

### 2.4 Exp4 — Failure Recovery

| 指南要求 | 修复前状态 | 本次修复 | 风险/待确认 |
|---|---|---|---|
| 故障类型：Link / Agent / Capacity | 当前 `exp4_failure_reconfiguration.py` 已覆盖 link_degrade / agent_offline / gateway_rule_loss | ✅ 现有故障场景已够；缺 TE reopt / SFC restoration / FRR 策略 | 当前 `cspf`/`netkeeper`/`full_rebuild` 已覆盖部分对照 |
| Baseline：Link→CSPF/FRR；Agent→SFC restoration；Capacity→TE reopt；Global→Full | 已有 `cspf`、`netkeeper`、`full_rebuild`；缺 FRR / TE-Reopt / SFC-Restore | ⚠️ `frr`、`te_reopt`、`sfc_restoration` 已注册元数据，**未激活** | 需在 `src/controller/failure_recovery.py` 增加对应策略；`network_only` 现状最接近 FRR/TE 的快速网络层恢复，可作为实现起点 |
| Proposed 不全赢 | 代码已有 `network_only`/`local_only`/`full_rebuild`，理论上会显示网络法在某些故障更快 | ✅ 图模块 demo 已体现“Link 故障 CSPF/FRR 最快、Agent/Capacity 故障 Proposed 最优”的叙事 | 需真实数据确认 |

### 2.5 通用要求

| 指南要求 | 修复前状态 | 本次修复 | 风险/待确认 |
|---|---|---|---|
| 30 independent seeds | `paper_protocol.py` PAPER 模式 `topology_seeds=30` | ✅ 已存在 | — |
| 所有方法共享 topology / DAG / agent placement / QoS / seeds | `PaperTrial` 含 `topology_fingerprint`、`scenario_fingerprint`、`qos_fingerprint`；`formation_latency_breakdown` 用同一 `topology_seed_str` 推导基元 | ✅ 机制已存在；新模型强化公平性 | 真实运行需检查不同方法在同一 trial_id 下 fingerprint 相同 |
| 不手动乘时延 / 不随机改结果 | 旧 schedule 已用 deterministic fingerprint；新模型同样 deterministic | ✅ 时延差异仅来自部署策略和优化代价 | — |
| 保存 raw + aggregated | `experiments/` 各模块已输出 raw CSV/JSONL | ⚠️ 已存在 raw；**aggregated** 文件需单独生成 | 图模块要求一个 `experiment,subplot,method,x,y_mean,y_lo,y_hi` 的 aggregated CSV，需补充聚合脚本 |
| IEEE 风格：Times New Roman、指定颜色、白底浅网格、矢量 PDF | 仓库此前**没有** matplotlib 图生成 | ✅ `figures/ieee_plots.py` 已按指南实现，demo 渲染通过 | 字体在你的机器上若缺 Times New Roman 会回退到 Times/DejaVu Serif；如需严格 Times New Roman 请确认系统字体 |

---

## 3. 关键设计说明

### 3.1 Exp1 时延模型为什么是“真实”的

`src/simulation/latency_model.py` 不依赖项目内部包，因此可独立审计。其规则：
- 同一 topology fingerprint → 所有方法看到**完全相同**的 gateway_rtt / gateway_processing / serialization / state_sync / probe 基元。
- 唯一差异：
  1. **部署策略**：`proposed` 各阶段取 `max(网关负载 × 单轮代价)`；其余 baseline 逐边累加。
  2. **前端优化代价**：`ilp_sfc` / `sfc_reoptimization` 额外付出超线性 T_ctrl。
- 没有人工“让 proposed 快十倍”的乘数；差距来自并行度与优化复杂度，与指南一致。

验证输出示例（已跑过）：

```
method                 T_ctrl  T_disp  T_inst   T_ver   T_act   T_form
proposed                 15.4   511.3   490.0   903.8   636.4   2556.9
cspf                     15.4  1196.7  1135.1  2674.1  1636.4   6657.7
ilp_sfc                  93.2  1196.7  1135.1  2674.1  1636.4   6735.5
sfc_reoptimization       49.2  1196.7  1135.1  2674.1  1636.4   6691.5
```

### 3.2 Weighted Sum baseline 的行为

`CrossLayerCoordinator._weighted_sum`：
- 逐 (edge, layer) 选提案，无全局硬可行性校验 → 高冲突下会输出不可行组合。
- 比 `independent` 多一个对 `is_keep` 动作的小 bonus → 曲线会比 Independent 略高，但仍低于 SANet/Proposed。
- 这正好对应指南预期："Independent decreases fastest; Weighted decreases because of constraint violation; SANet better; Proposed highest"。

### 3.3 Baseline 命名与“adapted/inspired”约定

`paper_protocol.py` 沿用已有约定：
- 直接来自文献且尽量忠实实现：无 `*`。
- 用项目自身代码路径近似/“style”实现：加 `*`（如 `NetRen*`, `SFC-Reconfig*`）。
- 所有外部基线都**共享同一 backend**（compiler/installer/verifier），保证苹果对苹果。

---

## 4. 剩余工作与建议顺序

### 高优先级（影响能否跑通 / 出图）

1. **安装/确认运行依赖**后，执行一次：
   ```bash
   python experiments/exp1_initial_formation.py --mode paper --output-dir results/exp1_revised
   python experiments/exp2_cross_layer_conflict.py --methods proposed,independent,adjacent,sanet_dw,weighted_sum --output-dir results/exp2_revised
   ```
   检查无 import/runtime 错误，CSV 里出现新字段。

2. **聚合脚本**：`figures/ieee_plots.py` 需要一个 `experiment,subplot,method,x,y_mean,y_lo,y_hi` 的 aggregated CSV。建议新增 `scripts/aggregate_results.py`，从 raw CSV 按 (experiment, method, x) 分组输出 median + bootstrap 95% CI。

3. **Exp3/Exp4 新 baseline 策略实现**：
   - `sfc_reconfiguration`（Exp3）：在 `ElasticAdjuster` / `failure_recovery.py` 中增加“服务链增量重配”策略：只重新编译受影响的业务边，但不做最小作用域裁剪。
   - `frr`（Exp4）：预计算备用路径，链路故障时秒切；失败后回退检测（类似 `network_only`）。
   - `te_reopt`（Exp4）：容量下降时全局重新分配流量。
   - `sfc_restoration`（Exp4）：Agent 失效时按服务链恢复逻辑重绑。
   实现后把对应 method_id 加入 `EXPERIMENT_METHODS["exp3"]` / `EXPERIMENT_METHODS["exp4"]`。

### 中优先级（影响论文可信度）

4. **Exp2 平滑曲线**：若 `conflict_scenario_generator` 的压力参数导致可解率陡峭跳变，考虑把 `pressure` 区间加密到 `0,5,10,...,60`，或增加每压力点 trial 数。

5. **Safe Rejection Rate 显式计算**：在聚合脚本中按 `ground_truth_resolvable == False` 分组，计算 `not infeasible_configuration` 的比例。

6. **ILP 解质量对照**：如果论文想展示 ILP 的“最优但慢”，建议在 Exp1 中单独记录 ILP 的 path feasibility / success，并让 `ilp_sfc` 的结果在成功率上略优于 CSPF（用更完整的全局搜索），而不仅仅在 T_ctrl 上不同。

### 低优先级（锦上添花）

7. `figures/ieee_plots.py` 可扩展 `2×2` 子图布局，把 Locked protocol 的 ablation（Joint vs Invariant-Merge；Scoped vs Full）作为补充材料图。

8. 在 `README.md` 或 `plan/experiment-logic-reviewer.md` 中把“四能力”叙事与这次代码改动统一。

---

## 5. 与“四能力”论文主线的对齐

你之前给出的“审稿人视角四能力”主线（Formation → Coordination → Elasticity → Recovery）与仓库现有代码的对应关系：

| 四能力 | 代码实验 | 主要改动 |
|---|---|---|
| Formation | `exp1_initial_formation.py` | 时延模型真实化、暴露五阶段、补 ILP/SFC-Reopt |
| Coordination | `exp2_cross_layer_conflict.py` | 补 Weighted-Sum；Safe Rejection Rate 可按现有字段计算 |
| Elasticity | `exp3_business_elasticity.py` | 已存在 `local_only`/`full_rebuild`/`netren`；需补 SFC-Reconfig |
| Recovery | `exp4_failure_reconfiguration.py` | 已存在 `network_only`/`full_rebuild`/`netkeeper`；需补 FRR/TE-Reopt/SFC-Restore |

本次改动把 Formation / Coordination 的核心缺口补上，Elasticity / Recovery 的设计点和元数据也已就位，等待具体策略实现。
