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
  --forecast-method adaptive \
  --sample-interval-s 0.2 \
  --ping-count 3 \
  --iperf-seconds 1
```

单独测量 Controller 内部任务子网构建时延，不进入真实规则装载和业务验证：

```bash
python3 -m experiments.measure_networking_latency \
  --rounds 30 \
  --warmup 5 \
  --report \
  --json-output results/networking_latency.json \
  --csv-output results/networking_latency.csv
```

该命令输出的主指标是 `controller_build_ms`。旧字段 `networking_latency_ms` 仅作为兼容别名保留，二者都只包含进程内成员确认、支撑 Agent 选择、session/route table 生成和内存态 Gateway ACK，不能直接与“端到端组网小于 5 秒”比较。

端到端组网实验（规则语义解析 + Controller 构建 + 并行模拟装载 + 业务边验证）：

```bash
python3 -m experiments.e2e_build \
  --scenario rescue \
  --semantic-mode rule \
  --install-mode simulated \
  --verify-mode synthetic \
  --rounds 30 \
  --json-output results/e2e_build_simulated.json \
  --csv-output results/e2e_build_simulated.csv
```

真实 netns 端到端组网实验。底层 namespace/veth/基础路由应先由 `setup_topology.sh` 建好；本命令实际装载幂等 `/32` 路由，并用并行 `ping/iperf3/ss` 验证三条业务边：

```bash
sudo -v
python3 -m experiments.e2e_build \
  --sudo \
  --scenario rescue \
  --semantic-mode rule \
  --install-mode netns \
  --verify-mode netns \
  --ping-count 3 \
  --iperf-seconds 1 \
  --json-output results/e2e_build_netns.json \
  --csv-output results/e2e_build_netns.csv
```

弹性恢复实验。真实 netns 模式会在故障前启动三条持续 `flowgen` TCP 业务流；故障后用 `ping + ss -tin` 按窗口检查，连续 3 个健康窗口后记录 `t_restore`。规则决策模式可直接运行：

```bash
sudo -v
python3 -m experiments.e2e_recovery \
  --sudo \
  --scenario rescue \
  --fault link_degrade \
  --install-mode netns \
  --verify-mode netns \
  --sample-interval-s 0.2 \
  --healthy-windows 3 \
  --json-output results/e2e_recovery_netns.json \
  --csv-output results/e2e_recovery_netns.csv
```

接入本地 Qwen2.5-7B-Instruct 时，先在另一个终端启动 OpenAI-compatible vLLM 服务。模型加载和实验预热不计入恢复时延：

```bash
CUDA_VISIBLE_DEVICES=1 \
/home/fzy/miniconda3/envs/vllm/bin/python \
  -m vllm.entrypoints.openai.api_server \
  --model /home/shair_dir_llm/Qwen2.5-7B-Instruct \
  --served-model-name qwen-recovery \
  --host 127.0.0.1 \
  --port 8001 \
  --dtype bfloat16 \
  --max-model-len 4096 \
  --max-num-seqs 4 \
  --guided-decoding-backend lm-format-enforcer \
  --gpu-memory-utilization 0.6
```

然后执行一次真实 LLM + netns 冒烟实验：

```bash
sudo -v
python3 -m experiments.e2e_recovery \
  --sudo \
  --scenario rescue \
  --fault agent_offline \
  --decision-mode llm \
  --llm-base-url http://127.0.0.1:8001/v1 \
  --llm-model qwen-recovery \
  --llm-timeout-s 1.0 \
  --llm-max-tokens 32 \
  --install-mode netns \
  --verify-mode netns \
  --sample-interval-s 0.2 \
  --healthy-windows 3 \
  --rounds 1 \
  --skip-full-rebuild \
  --json-output results/e2e_recovery_llm_agent_offline.json \
  --csv-output results/e2e_recovery_llm_agent_offline.csv
```

`--fault` 还支持 `link_degrade` 和 `gateway_rule_loss`，正式统计时应分别运行 30 轮。`agent_offline` 和 `gateway_rule_loss` 会用黑洞路由让受影响业务边真实中断，再由支撑 Agent 替换或 netns installer 重装任务路由。每轮默认还在独立环境中执行一次全量重建恢复基线，写入 `full_rebuild_ms`；只测模型辅助增量恢复时使用 `--skip-full-rebuild`。

检测器不限定在 Controller。Agent、网关审计或业务 QoS 探针确认异常后统一生成 `AnomalyEvent`，字段包含冲突类型、Agent 状态、网关缺失规则和失败业务边，不包含实验注入的 `fault_type` 真值。恢复关键路径按以下顺序计时：

```text
0. 故障发生并被任意检测器发现                 -> fault_detect_ms（单独报告，不计入 3 秒）
1. Controller 聚合 AnomalyEvent 并规范化证据    -> controller_validation_ms 的前半部分
2. Qwen 分析冲突并输出受限 RecoveryPlan          -> model_analysis_ms
3. Controller 校验 ID、证据、置信度和可执行策略  -> controller_validation_ms 的后半部分
4. Controller 生成/应用具体增量                  -> controller_apply_ms
5. 网关或链路执行实际修复并返回 ACK               -> delta_install_ms
6. 业务探针连续 3 个窗口满足 QoS                  -> business_restore_ms
```

模型只能从固定策略集合中选择，不具备 Shell/root 权限。为控制生成时延，模型在线只返回严格的诊断/策略/置信度三元组（如 `{"d":"agent","s":"standby","c":95}`），Controller 根据已校验的异常证据补全受影响 Session、Agent 和 Gateway，形成完整 `RecoveryPlan`。模型超过 1 秒、返回非法 JSON、置信度低于 0.7 或方案不可执行时，不重试模型，立即使用确定性规则回退。输出中的 `decision_source`、`model_timeout`、`fallback_reason`、`llm_plan_valid` 和 `llm_plan_adopted` 可区分模型方案成功、系统回退成功和恢复失败。

当前 netns 拓扑只有单路由器、无冗余承载路径，因此 `link_degrade` 的真实修复动作是清除故障 qdisc（结果中的 `remediation=clear_netem`），它测的是检测、决策、链路修复和业务恢复，不是备用路径切换。若论文要报告“重路由恢复时延”，需要先给测试床增加可切换的第二条数据面路径。

时延口径如下：

```text
controller_build_ms = 控制器内部任务图和规则生成 + 进程内 Gateway ACK
e2e_build_ms        = semantic + task_mapping + controller_build + gateway_install + business_verify
elastic_recovery_ms = t_restore - t_detected
e2e_recovery_ms     = t_restore - t_fault = fault_detect_ms + elastic_recovery_ms
```

论文指标应使用多轮实验的 `e2e_build_ms P95 <= 5 s` 和 `elastic_recovery_ms P95 <= 3 s`。`e2e_recovery_ms` 保留为故障到恢复的参考口径，同时报告模型分析、Controller 校验/执行、增量安装和业务恢复各分项。

`simulated + synthetic` 的结果只验证计时流程、并行装载模型和 CSV 统计是否正确，不能作为真实网络性能结论。论文中的 5 秒/3 秒结果应来自 `netns + netns` 多轮实验，并保留命令日志与每条业务边验证明细。

`--forecast-method` 支持 `adaptive`、`ewma`、`holt`、`kalman`。真实采样数据不足时建议先用默认 `adaptive`，由短历史波动自动在 EWMA、Holt 趋势和 Kalman-style 滤波之间选择。

注入真实链路损伤后再跑 Agent 闭环：

```bash
python3 -m experiments.real_run \
  --sudo \
  --target-profile rescue \
  --summary-only \
  --history-steps 10 \
  --horizon 5 \
  --forecast-method adaptive \
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
- 当前预测函数是面向小样本的轻量 adaptive/EWMA/Holt/Kalman 预测，后续可在数据充足时替换为正式 LSTM 或 SANet 风格模型。
- `semantic-mode=rule` 是可复现的救援场景规则解析，并非本地大模型推理；接入模型后其真实推理时间才应计入 `semantic_ms`。
- `NetnsGatewayInstaller` 是轻量 `ip route` 后端：远端转发规则映射为真实 `/32` 路由，本地交付只做内核路由绑定校验。它尚未覆盖完整五元组、DSCP、ACL，也没有远程 HTTP/gRPC 网关控制消息；需要此口径时应增加 nftables/OVS/P4/eBPF 或远程 Gateway adapter。
