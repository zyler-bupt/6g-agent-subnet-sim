# 6G 异构多智能体任务通信子网仿真实验

本仓库用于验证“跨层组网意图 -> 任务通信子网 -> 网关规则 -> 状态反馈 -> 增量调整”的软件仿真闭环。

## 运行方式

asyncio / 离散事件主线原型：

```bash
python3 -m sim.run_topology
python3 -m sim.run_rescue_task
python3 -m experiments.run --scenario rescue
```

轻量机制回归测试：

```bash
python3 main.py
```

SANet 语义控制器第一阶段 demo：

```bash
python3 semantic_demo.py
```

语义控制器 HTTP 服务：

```bash
python3 -m services.semantic_controller_service
```

默认使用本地 Hashing Embedding、规则识别和规则规划。连接 Qwen 兼容服务时配置：

```bash
export EMBEDDING_BASE_URL=http://localhost:8001/v1
export EMBEDDING_MODEL=Qwen3-Embedding-0.6B
export QWEN_BASE_URL=http://localhost:8002/v1
export QWEN_MODEL=Qwen2.5-7B-Instruct
export PLANNER_MODE=openmanus
```

语义控制器单元测试：

```bash
python3 -m unittest discover -v
```

语义控制器冒烟评测：

```bash
python3 -m evaluation.evaluate_semantic
```

Docker 多节点 HTTP 实验：

```bash
docker compose up --build -d
python3 scripts/run_experiment.py --scenario all
python3 scripts/export_results.py
```

建议在 Linux 服务器上运行 Docker 实验，详见 `SERVER_DEPLOY.md`。

## 当前已实现

- P0-P4 主线仿真原型：基于 asyncio/离散事件的 Controller、Gateway、aAgent/tAgent/nAgent、任务通信子网、mock 指标、跨层预测、风险计算和最小调整实验。
- 应急救援示范场景：无人机/摄像头采集 -> 边缘识别 -> 云端决策调度 -> 现场反馈，可输出云/边/端拓扑、`G_m` 成员、跨层支撑边和端到端会话列表。
- 可插拔指标接口：`MetricProvider` 固定接口，当前使用 `MockMetricProvider`，`RealMetricProvider` 作为 P5 真实测量/ns-3/Mininet 适配占位。当前所有链路指标为合成 mock，非真实测量。
- 三层 Agent 闭环：aAgent 预测业务需求并调整非关键质量，tAgent 预测端到端时延/丢包并调传输参数，nAgent 预测承载/拥塞并给出承载建议。
- 触发条件：风险超阈值（预测）或支撑 Agent/网关失效（`F_m=1`，`Gateway.fail_agent`）均可触发运行期调整。
- 三级最小弹性调整（按序就近处理）：tier1 局部调参 -> tier2 用本地备用替换失效支撑 Agent -> tier3 无本地备用时跨子网改接通信关系；仅触碰受影响范围。
- 真实全量重建基线：`AgentController.rebuild_task_subnet` 真正拆除并重建整张子网，对比数值均为实测，非硬编码；与最小调整共用一套透明 `CostModel`（`src/controller/cost.py`）。
- 可读实验报告：`src/report.py` 输出 CJK 对齐的分节表格与结论，`python3 -m experiments.run` 直接给出三类事件下『最小调整 vs 全量重建』的对比与中断降幅。
- 物理层暂不实装：保留 `PhyAgentStub`、`AgentLayer.PHYSICAL` 和风险项 `lambda_h`，默认 `lambda_h=0`，不参与 P0-P4 主路径映射与调整。
- SANet 语义控制器第一阶段：用户输入触发、上下文构建、GoalSpec、PlanSpec、aAgent/nAgent mock 预测、跨层安全余量评价、视频策略建议和目标评价。
- 阶段四双 Agent 语义链路：上下文 Embedding、目标原型 Top-3 检索、Qwen 兼容 GoalSpec 输出、受限规划适配器、严格 DAG 校验、规则回退和 HTTP API。
- SANet 官方源码快照及双 Agent 基线：保留 aAgent 应用需求和 nAgent 网络带宽任务，暂不接入 pAgent/CSI。
- 旧同步原型正常建网：输入灾害现场协同任务意图，确认业务 Agent，生成任务通信图并下发网关规则。
- 白名单隔离：合法业务流可转发，未授权业务流会被节点网关拦截。
- 承载冲突调整：nAgent 上报路径拥塞后，子网控制器生成 `rule_delta` 并局部更新相关网关。
- Docker 多节点实验：Controller、UE/MEC/Cloud 网关、业务 Agent、pAgent、nAgent 以独立容器服务运行，通过 HTTP 完成接入确认、规则下发、业务转发、状态反馈和增量更新。
- 应用层链路模型：网关按规则模拟 `latency_ms`、`bandwidth_mbps`、`drop_rate`、`congestion_level`，用于容器化机制实验。

## 主要指标

asyncio 主线实验会输出：

- 组网成功率/组网时延
- 端到端会话数量
- 涉及网关数量
- 风险调整前后数值
- QoS 满足情况
- 变更 Agent/关系/网关数量
- 控制更新次数
- 业务中断时长

旧脚本还会输出：

- 建网耗时
- 下发规则数量
- 涉及网关数量
- 非法流量拦截次数
- 增量更新规则数量
- 增量更新时间
- 业务转发时延
- 成功转发次数
- 丢包次数
- 受影响网关数量
- 控制消息数量

## Docker 实验场景

```bash
python3 scripts/run_experiment.py --scenario normal-build
python3 scripts/run_experiment.py --scenario whitelist
python3 scripts/run_experiment.py --scenario network-congestion
python3 scripts/run_experiment.py --scenario physical-degradation
python3 scripts/run_experiment.py --scenario agent-failure
python3 scripts/run_experiment.py --scenario full-rebuild
python3 scripts/run_experiment.py --scenario all
```

每次运行会在 `results/` 下生成 JSON 文件。执行 `python3 scripts/export_results.py` 可生成 `results/summary.csv`。

当前 Docker 版属于 `docker-http-simulation`：它包含真实容器进程、HTTP 通信、序列化、端口和网关转发，但仍不是真实 5G/6G 硬件实验。链路质量、拥塞、带宽下降和丢包由网关应用层模型模拟。

SANet官方示例预测结果可与Last-value基线对比：

```bash
python3 scripts/evaluate_sanet_predictions.py \
  --prediction-dir results/sanet_official_gpu_10e/predictions
```

生成代表性预测窗口和全量预测散点图：

```bash
python3 scripts/plot_sanet_predictions.py \
  --prediction-dir results/sanet_official_gpu_10e/predictions \
  --output-dir results/sanet_official_gpu_10e/plots
```

## 模块结构

- `src/core/`：主线仿真的任务、Agent、消息、Gateway、TaskSubnet、Session 和实验指标模型。
- `src/sim/`：asyncio 消息总线、仿真时钟、应急救援拓扑、场景与运行入口。
- `src/metrics/`：指标提供器接口、mock 指标源和真实指标适配占位。
- `src/agents/`：aAgent、tAgent、nAgent 和 pAgent stub 的感知-预测-动作闭环。
- `src/controller/`：任务子网构建、跨层风险计算和弹性最小调整。
- `sim/`：命令包装入口，使 `python3 -m sim.run_topology` 可直接运行。
- `experiments/`：批量实验、指标导出和可选绘图。
- `models.py`：核心数据模型。
- `agents.py`：业务 Agent、pAgent、nAgent 仿真对象。
- `gateway.py`：节点网关、白名单校验和规则加载。
- `controller.py`：子网控制器和运行期调整逻辑。
- `scenarios.py`：灾害现场协同任务场景。
- `main.py`：一键运行入口。
- `services/`：Docker HTTP 版 Controller、Gateway 和 Agent 服务。
- `semantic_controller/`：SANet Semantic Task Plan 第一阶段实现，独立于原任务通信子网仿真。
  - `embedding.py`：语义编码、目标原型库和 Top-K 检索。
  - `qwen.py`：OpenAI 兼容 Qwen 客户端、GoalSpec 校验和回退。
  - `planner.py`：规则规划与受限 OpenManus 风格规划适配器。
- `services/semantic_controller_service.py`：语义控制器 HTTP API。
- `third_party/SANet/`：SANet 作者仓库源码快照，不在其中进行本项目修改。
- `sanet_dual/`：排除物理层后的 aAgent/nAgent 联合训练与推理入口。
- `semantic_demo.py`：语义控制器端到端 demo。
- `tests/`：标准库 `unittest` 测试。
- `scripts/`：Docker 实验运行和结果导出脚本。
- `docker-compose.yml`：多节点实验拓扑。
- `.env`：固定 Docker Compose 项目名，避免中文目录名导致 Compose 项目名异常。
