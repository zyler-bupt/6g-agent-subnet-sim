# 6G 跨层异构 Agent 任务通信子网

本仓库用于验证“业务协作关系 -> 跨层任务通信子网 -> 真实网络感知 -> 预测/决策 -> 运行期弹性调整”的闭环。

当前主线已经从早期应用层合成指标推进到 Linux 真实测试床：在同一台服务器上用 network namespace 构建云-边-端三子网，用真实 TCP 业务流、真实 `ping/iperf3/ss` 测量和 `tc netem` 链路损伤驱动 aAgent/tAgent/nAgent。

## 快速运行

离线机制回归和可视化不需要 root，使用 synthetic 指标源：

```bash
python3 -m sim.run_rescue_task
python3 -m experiments.run --scenario rescue
python3 -m unittest discover -v
```

可视化界面：

```bash
pip install -r viz/requirements.txt
python3 -m viz.server
```

打开 `http://127.0.0.1:5000`，可以查看 tier1/tier2/tier3 三类弹性调整和全量重建基线对比。

真实测试床需要 Linux + root/CAP_NET_ADMIN：

```bash
sudo bash testbed/preflight.sh
sudo bash testbed/setup_topology.sh
```

单次真实指标探测：

```bash
python3 -m experiments.real_probe --sudo --ping-count 5 --iperf-seconds 1
```

真实 Agent 感知、历史窗口、未来 5 步预测和动作输出：

```bash
python3 -m experiments.real_run \
  --sudo \
  --target-profile rescue \
  --summary-only \
  --history-steps 10 \
  --horizon 5 \
  --sample-interval-s 0.2 \
  --ping-count 3 \
  --iperf-seconds 1
```

注入真实链路损伤后再跑 Agent 闭环：

```bash
python3 -m experiments.real_run \
  --sudo \
  --target-profile rescue \
  --summary-only \
  --history-steps 10 \
  --horizon 5 \
  --sample-interval-s 0.2 \
  --ping-count 3 \
  --iperf-seconds 1 \
  --netem-delay-ms 80 \
  --netem-loss-percent 5
```

语义控制器 demo：

```bash
python3 semantic_demo.py
python3 -m services.semantic_controller_service
python3 -m evaluation.evaluate_semantic
```

## 当前能力

- 真实云-边-端测试床：`testbed/setup_topology.sh` 创建 `h-term`、`h-edge`、`h-cloud`、`h-router` 四个 network namespace，并通过 veth/路由连通。
- 真实 TCP 业务流：`testbed/flowgen.py` 支持可控发送速率、TCP 拥塞控制、`TCP_NODELAY` 和 DSCP/ToS 参数。
- 真实指标接入：`NetnsMetricProvider` 调用 `ping`、`iperf3 -J`、`ss -tin`，解析成统一 `MetricSnapshot`。
- 分 Agent 链路感知：`real_run --target-profile rescue` 将 UE/MEC/Cloud 的 Agent 映射到 `term->edge`、`edge->cloud`、`cloud->term` 等真实链路。
- SANet trace 回放：`TraceMetricProvider` 可用 `third_party/SANet/data/example_band_n1/traffic.npy` 驱动应用层业务需求。
- Agent 闭环：aAgent/tAgent/nAgent 均包含感知、历史窗口、预测、决策、真实 action 执行和反馈接口。
- 运行期弹性调整：Controller 支持 tier1 本地调参、tier2 本地备用替换、tier3 跨子网改接，并可与全量重建基线对比。
- 语义控制器：支持目标识别、受限规划、语义检索、Qwen 兼容客户端和 HTTP API。
- SANet baseline：`sanet_dual/` 保留 aAgent/nAgent 联合训练与推理入口，后续用于替换当前轻量预测函数。

## 真实测试床结构

```text
h-term  10.10.1.2  终端/无人机
   |
h-router           中间路由与损伤注入点
   |---------|
h-edge  10.10.2.2  边缘节点
h-cloud 10.10.3.2  云端节点
```

常用验证命令：

```bash
sudo ip netns exec h-term ping -c 5 10.10.3.2
sudo ip netns exec h-term iperf3 -c 10.10.3.2 -t 3 -J
sudo ip netns exec h-router tc qdisc replace dev rt-cloud0 root netem delay 80ms loss 5%
sudo ip netns exec h-router tc qdisc del dev rt-cloud0 root
```

## 模块结构

- `src/core/`：任务、Agent、消息、Gateway、TaskSubnet、Session 和实验指标模型。
- `src/agents/`：aAgent、tAgent、nAgent、pAgent stub，以及真实 action executor。
- `src/metrics/`：指标接口、真实 netns 测量 provider、SANet trace provider、离线 synthetic provider。
- `src/controller/`：任务子网构建、跨层风险计算、三级弹性调整和全量重建基线。
- `src/sim/`：应急救援拓扑、场景与离线仿真入口。
- `experiments/`：离线弹性对比、真实探测、真实 Agent 闭环。
- `testbed/`：Linux netns/veth/tc 测试床脚本和 flowgen。
- `viz/`：Flask + SVG 分步动画。
- `semantic_controller/`：语义目标识别、语义检索、受限规划和 Qwen 兼容接入。
- `services/semantic_controller_service.py`：语义控制器 HTTP API。
- `third_party/SANet/`：SANet 作者仓库源码快照。
- `sanet_dual/`：aAgent/nAgent 预测 baseline。
- `tests/`：标准库 `unittest` 测试。

## 边界

- 物理层目前仍是 `PhyAgentStub`，风险项 `lambda_h=0`，不参与主路径调整。
- `SyntheticMetricProvider` 仅用于无 root 的离线回归、可视化和全量重建基线演示；真实实验应使用 `NetnsMetricProvider`。
- 当前预测函数仍是轻量指数平滑/趋势外推，后续计划替换为正式时序预测模型或 SANet 风格模型。
