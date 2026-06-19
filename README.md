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

语义控制器单元测试：

```bash
python3 -m unittest discover -v
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

## 模块结构

- `models.py`：核心数据模型。
- `agents.py`：业务 Agent、pAgent、nAgent 仿真对象。
- `gateway.py`：节点网关、白名单校验和规则加载。
- `controller.py`：子网控制器和运行期调整逻辑。
- `scenarios.py`：灾害现场协同任务场景。
- `main.py`：一键运行入口。
- `services/`：Docker HTTP 版 Controller、Gateway 和 Agent 服务。
- `semantic_controller/`：SANet Semantic Task Plan 第一阶段实现，独立于原任务通信子网仿真。
- `semantic_demo.py`：语义控制器端到端 demo。
- `tests/`：标准库 `unittest` 测试。
- `scripts/`：Docker 实验运行和结果导出脚本。
- `docker-compose.yml`：多节点实验拓扑。
- `.env`：固定 Docker Compose 项目名，避免中文目录名导致 Compose 项目名异常。
