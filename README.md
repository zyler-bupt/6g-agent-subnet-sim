# 6G 异构多智能体任务通信子网仿真实验

本仓库用于验证“跨层组网意图 -> 任务通信子网 -> 网关规则 -> 状态反馈 -> 增量调整”的软件仿真闭环。

## 运行方式

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

- SANet 语义控制器第一阶段：用户输入触发、上下文构建、GoalSpec、PlanSpec、aAgent/nAgent mock 预测、跨层安全余量评价、视频策略建议和目标评价。
- 阶段四双 Agent 语义链路：上下文 Embedding、目标原型 Top-3 检索、Qwen 兼容 GoalSpec 输出、受限规划适配器、严格 DAG 校验、规则回退和 HTTP API。
- SANet 官方源码快照及双 Agent 基线：保留 aAgent 应用需求和 nAgent 网络带宽任务，暂不接入 pAgent/CSI。
- 正常建网：输入灾害现场协同任务意图，确认业务 Agent，绑定 pAgent/nAgent，生成任务通信图并下发网关规则。
- 白名单隔离：合法业务流可转发，未授权业务流会被节点网关拦截。
- 承载冲突调整：nAgent 上报路径拥塞后，子网控制器生成 `rule_delta` 并局部更新相关网关。
- Docker 多节点实验：Controller、UE/MEC/Cloud 网关、业务 Agent、pAgent、nAgent 以独立容器服务运行，通过 HTTP 完成接入确认、规则下发、业务转发、状态反馈和增量更新。
- 应用层链路模型：网关按规则模拟 `latency_ms`、`bandwidth_mbps`、`drop_rate`、`congestion_level`，用于容器化机制实验。

## 主要指标

运行脚本后会输出：

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
