# TCANet 原型系统现场演示 — 设计文档

日期：2026-10-06
状态：待审阅

## 1. 目标与范围

### 1.1 目标（按优先级）

1. **现场演示（主目标）**：在一台 Ubuntu 机器上，当着老师的面实际运行 TCANet：
   提交任务 → 构建任务子网 → 现场注入故障 → 控制器自行检测并只重配受影响部分 →
   实测曲线恢复。证明 TCANet 是真实可运行的系统，而非动画或剧本。
2. **系统演示图（次目标）**：参考 SANet（TMC 2026）Fig. 5 的形式，由同一套系统
   产出「各 Agent 窗口截图 + 整机照片」的原型图，以及一张实测恢复时间轴图。

### 1.2 成功标准

老师能直接看到并可当场追问验证：

- 任务 DAG 被翻译为子网，转发状态是网关上真实的 Linux 策略路由（可 `ip rule show` 核对）；
- 四层 Agent 是独立进程（可 `ps` 核对），各自上报状态、执行动作；
- 故障由真实系统操作制造，控制器通过 Agent 上报自行感知；
- 故障时只有受影响依赖的实测曲线掉落，范围外的依赖（e3）全程平稳；
- 控制器日志逐步对应论文 Algorithm 1：E^aff 扩展 → C^ad/C^feas → J* → M_m →
  Apply → Assess → Commit v_m+1（或 Rollback → 重试，至多 K_max 次）；
- 恢复时延、M_m 均为实测/实算值。

### 1.3 非目标

- 不做多机部署（单机 + network namespace 模拟多网关）。
- 物理层不接真实无线硬件：PhyAgent 的 SNR/容量由仿真模型产生，界面标注 SIM。
- 本期不接入真实 AppAgent（用户后续提供）；本期用基于 iperf3 的合成 AppAgent，
  但必须通过适配器接口实现，保证后续替换不影响其他部分。
- 不重写 `src/tcanet` 的机制逻辑；不改内部函数/类名。

## 2. 总体架构

```
                    ┌───────────── 宿主机（root netns）─────────────┐
                    │  Controller 进程（Algorithm 1, src/tcanet）    │
                    │  Monitor 窗口进程（Qt / rich 终端）            │
                    │  tcanetctl（操作员命令）                       │
                    └──────────────┬────────────────────────────────┘
                                   │ Unix domain socket 总线（JSON lines）
     ┌─────────────┬───────────────┼───────────────┬─────────────┐
  [ns a1 无人机] [ns a2 摄像头]  [ns G1..G4 网关]  [ns a3 边缘AI] [ns a4 救援车]
   AppAgent       AppAgent        NetAgent×4       AppAgent      AppAgent
   TransAgent     TransAgent      PhyAgent×4       TransAgent    TransAgent
```

- **数据面**：网关 netns G1–G4，网关间 veth 链路 L1–L5（必要时增加 L6，见 §4.4），
  链路用 `tc` 按场景参数限速/加时延；每个 AppAgent 一个端点 netns，经接入 veth
  挂到所属网关。
- **总线**：Unix domain socket（基于文件系统，跨 netns 可达，无需额外组网）。
  发布/订阅，按 topic 路由。
- **故障注入**：`tcanetctl` 直接执行真实系统操作，**不经过总线通知控制器**。

### 2.1 进程清单

| 进程 | 所在位置 | 职责 |
|---|---|---|
| Controller | 宿主机 | 订阅 Agent 上报 → StateAggregator 维护 s_m；检测事件；调用 `src/tcanet` 执行 Algorithm 1；经 `NetnsDataplane` 下发 ΔFT；收集实测样本做 Assess |
| AppAgent ×4 | 端点 netns | 通过 `AppAdapter` 产生任务流量（本期：iperf3 UDP 按 r_m,e 发包），上报需求与应用层 QoS；执行应用层动作（源速率/质量档位） |
| TransAgent（每个目的端点一个进程，内含该端点各依赖会话） | 目的端点 netns | 运行每依赖一个 iperf3 服务端，逐 0.5 s 读接收吞吐/丢包并上报；执行传输档位动作 |
| NetAgent ×4 | 网关 netns | 读 `/sys/class/net/*/statistics` 算链路利用率；检测网卡 carrier；对经过本网关的依赖做 ping RTT 测量并上报 |
| PhyAgent ×4 | 网关 netns | 仿真 SNR → 接入容量（标 SIM），并把容量写入接入 veth 的 `tc` 限速（真实影响数据面）；执行接入资源动作 |
| Monitor | 宿主机 | 只订阅，渲染窗口 |

### 2.2 总线消息

统一格式：`{"topic": str, "src": str, "ts": float, "payload": {...}}`，一行一条。

| topic | 方向 | 内容 |
|---|---|---|
| `hello` / `heartbeat` | Agent → * | agent_id、layer、gateway、能力；心跳周期 0.5 s |
| `report.link` | NetAgent → * | link_id、up、tx_mbps、utilization |
| `report.flow` | TransAgent/NetAgent → * | dep_id、rx_mbps、loss、rtt_ms、source=MEASURED/SIM |
| `report.access` | PhyAgent → * | gateway、snr_db、capacity_mbps、source=SIM |
| `report.app` | AppAgent → * | dep_id、demand_mbps、qos |
| `cmd.action` | Controller → Agent | 层动作（目标、参数、action_id） |
| `ack` | Agent → Controller | action_id、ok、detail |
| `ctrl.log` | Controller → * | Algorithm 1 每步的结构化日志（供窗口与事件日志） |
| `ctl.*` | tcanetctl → AppAgent | 仅用于改变 AppAgent 自身需求（任务更新）；**Controller 不订阅 `ctl.*`** |

## 3. 数据面

### 3.1 拓扑与场景 `paper_fig1`

对齐论文新版 Fig. 1：a1 无人机、a2 摄像头 → a3 边缘 AI → a4 救援车。

| 依赖 | 端点 | 初始需求 | 初始路径 |
|---|---|---|---|
| e1 | a1@G1 → a3@G4 | 15 Mbps | G1→G2→G4（L1、L2） |
| e2 | a2@G3 → a3@G4 | 8 Mbps | G3→G4（L5） |
| e3 | a3@G4 → a4@G3 | 5 Mbps | G4→G3（L4） |

链路参数沿用 `src/tcanet/scenario.py`（L1 40 Mbps/8 ms/保护负载 12 → 可用 28 等）。
挂载位置为初定，实现时以真实闭包代码验证：G2 故障时 E^dir={e1}，经 L5 资源耦合
扩展出 e2，e3 落在 E^aff 之外。若接入资源建模把 e3 拉入范围，则将接入资源按
上行/下行分开建模（veth 全双工，物理上更准确）。

### 3.2 转发状态 = 策略路由

FT^g_m = {(e, next_g)} 的每个条目落为网关 g 上：

```
ip rule add from <src_ip> to <dst_ip> table <100+依赖序号>
ip route add <dst_ip> via <next_hop_ip> table <100+依赖序号>   # 终点网关为直连交付
```

- Apply ΔFT：在对应网关 netns 中增删上述规则；
- Rollback：恢复到 v_m 的规则集合；
- 回程流量（ping 回包、iperf 控制连接）使用固定静态路由，不计入 FT。

### 3.3 测量

| 量 | 来源 | 频率 |
|---|---|---|
| 吞吐、丢包 | 目的端 iperf3 UDP 服务端接收统计（每依赖一个端口），源端 `-b r_m,e` | 0.5 s |
| 时延 | 源端点 → 目的端点 ping（策略路由保证走依赖实际路径） | 0.2 s |
| 链路利用率 | 网关 netns `/sys/class/net/*/statistics` | 0.5 s |
| 接入容量 | PhyAgent 仿真模型（SIM）→ 写入 tc | 1 s |

### 3.4 故障注入（`tcanetctl`）

| 命令 | 真实操作 | 控制器如何得知 |
|---|---|---|
| `fail gateway G2` | G2 所有接口 `ip link set down` + 终止 G2 上的 Agent | NetAgent 报 carrier 断 / 心跳超时 |
| `fail link L1` | 对应 veth down | NetAgent 报 carrier 断 |
| `degrade L3 loss 10%` | `tc netem` 注入丢包 | 不主动上报；仅在 Assess 实测时暴露 |
| `demand e1 25` | 通过 `ctl.demand` 让 AppAgent 提速 | AppAgent `report.app` 报需求更新（任务更新） |
| `kill phy-G4` | 终止该 PhyAgent 进程 | 心跳超时（1.5 s） |
| `reset` | 清除所有注入，重建初始状态 | — |

## 4. 核心代码改造（`src/tcanet`）

原则：机制逻辑不重写，只补真实执行/测量/回滚接口；现有测试全部保持通过。

### 4.1 `Dataplane` 接口（新增 `src/tcanet/dataplane.py`）

```python
class Dataplane(Protocol):
    async def apply(self, staged: StagedDecision) -> ExecutionRecord: ...   # Alg.1 第15行
    async def rollback(self, previous: SubnetState) -> None: ...            # Alg.1 第19行
```

- `NullDataplane`：保持当前行为（仅检查前置条件），老测试不变；
- `run_formation` 与 `RecoveryController` 增加可选 `dataplane=` 参数，默认 `NullDataplane`。

### 4.2 真实 Rollback

`RecoveryController.recover` 中 Assess 失败后：调用 `dataplane.rollback(v_m)` →
刷新 s_m → 排除该候选 → 重试，至多 K_max 次；新增 `on_rollback` hook。

### 4.3 异步测量与 Assess

`MeasurementProvider` 允许返回 awaitable：Apply 后在观测窗口（默认 2 s）内收集
受影响依赖的真实样本，按论文 Eq. 5–6 计算 g_m,k 与 V^H 判定；窗口内缺样本视为不通过。

### 4.4 去除写死失败

演示路径不使用 `FirstAttemptViolating`。回滚只在 Assess 实测失败时出现；演示用
`degrade` 在备选链路上注入控制器不知道的劣化来合法触发。若现有拓扑缺少「回滚后
仍有替代方案」的路径，增加一条链路（候选：G1→G4 直连 L6），以真实选择代码验证后确定。

### 4.5 `StateAggregator`

把总线上报映射为 `World` 更新（链路通断、实测可用容量、接入容量、Agent 在线），
作为控制器的 s_m 唯一来源。

### 4.6 新场景与论文对齐

- 新增 `paper_fig1` 场景（§3.1）；保留原 rescue 场景供老测试/实验使用。
- 注释与日志术语对齐新版论文：Eq. 5–8 可行性、Eq. 9–12 两级选择、Eq. 13–15
  范围扩展、M_m、K_max、Assess/Rollback/Commit。内部函数/类名不改。

### 4.7 AppAgent 适配器

```python
class AppAdapter(Protocol):
    def demand(self) -> dict[str, float]: ...        # dep_id -> Mbps
    def qos(self) -> dict[str, dict]: ...
    async def start_traffic(self, dep_id: str, dst_ip: str, rate_mbps: float) -> None: ...
    async def apply(self, action: dict) -> bool: ...  # 应用层动作
```

本期实现 `IperfAppAdapter`；用户后续提供的真实 Agent 实现同一接口，在其端点
netns 内收发真实业务流量。

## 5. 界面与现场演示

### 5.1 技术选型

- 日志终端：`rich` 彩色输出，运行在 gnome-terminal；
- 曲线/DAG 窗口：PySide6 + pyqtgraph 原生窗口；
- 布局：`tcanet-demo up` 用 `wmctrl` 将窗口摆到固定位置；Agent 日志用 tmux 四格。

### 5.2 单屏布局（1920×1080）

```
┌───────────────────┬───────────────────┬───────────────────┐
│ ① Controller      │ ② Net/TransAgent  │ ③ PhyAgent  [SIM] │
│ Algorithm 1 日志   │ e1/e2/e3 实测吞吐  │ 接入 SNR/容量      │
│                   │ + RTT，事件竖线    │ 链路利用率柱状图    │
├───────────────────┼───────────────────┼───────────────────┤
│ ④ AppAgent         │ ⑤ Agents (tmux)    │ ⑥ Operator        │
│ 任务DAG + q^H 灯    │ App/Trans/Net/Phy  │ tcanetctl         │
│ + 版本 v_m         │ 各层日志            │                   │
└───────────────────┴───────────────────┴───────────────────┘
```

每条曲线、每个窗口标题标注数据来源（MEASURED / SIM）。

### 5.3 演示剧本

| 幕 | 命令 | 现场效果 |
|---|---|---|
| 0 启动 | `sudo tcanet-demo up` | 建拓扑，全部 Agent 进程（约 14 个：App×4、Trans×2〔每个目的端点一个，内含各依赖会话〕、Net×4、Phy×4）上线、心跳 |
| 1 组网 | `tcanetctl submit paper_fig1` | 日志：E^aff=全集 → 候选数 → \|C^feas\| → J* → M → Apply（列出 ip rule）→ Assess 实测 → Commit v1；三条流量曲线出现 |
| 2 跨层冲突 | `tcanetctl demand e1 25` | 日志列出被拒组合「25×1.2=30>28」，选出联合可行跨层方案，Commit v2 |
| 3 网关故障 | `tcanetctl fail gateway G2` | e1 掉 0 → NetAgent 检测 → E^dir={e1} → 经 L5 扩展 e2 → 改路由 → e1 恢复；e3 平稳；显示实测恢复时延、M_m |
| 4 回滚 | `degrade <备选链路> loss 10%` 后触发故障 | Assess 实测丢包超标 → Rollback → 下一候选 → Commit |
| 5 支撑代理失效 | `tcanetctl kill phy-G4` | Φ 重绑定，路径不变，M 只计 binding |
| 复位 | `tcanetctl reset` | 回到第 0 幕 |

另提供 `tcanet-demo status`（进程/命名空间/规则概览）方便老师追问时展示。

### 5.4 时延口径

- 组网时延：`submit` 到 Commit v1（含 Apply 与 Assess 窗口，真实墙钟）；
- 恢复时延：首个 Agent 故障证据时间戳到 Commit（真实墙钟）；
- `tcanetctl` 的注入时刻只写入事件日志用于时间轴图，不供控制器使用。

## 6. 演示图产出

1. **自动截图**：Commit v1 后、故障发生时、恢复后三个时刻，Qt 窗口自 grab（高分辨率），
   终端窗口用系统截图；
2. **拼图脚本**：上排各窗口截图并标注 Agent 角色，下排整机照片（用户拍摄），
   箭头连线，导出 PDF/PNG；
3. **恢复时间轴图**：全程事件日志（JSONL）→ matplotlib，按论文配色画三条依赖实测吞吐
   + 故障/检测/Apply/Assess/Commit 竖线；
4. **录屏备份**：ffmpeg x11grab 全程录制，现场故障时可直接播放。

## 7. 仿真回退、容错与测试

### 7.1 `--sim` 模式

无需 sudo/netns（macOS 可运行，用于开发与保底）；进程、窗口、剧本不变；测量由
投影模型加噪声产生；路由命令以 `[SIM]` 前缀显示而不执行；窗口显著标注 SIMULATION。

### 7.2 容错

- `tcanet-demo up` 前置自检：root、iperf3/ip/tc/wmctrl/tmux 是否可用及版本；
- `down` 幂等，捕获 Ctrl-C 与异常确保清理 netns/进程；
- 心跳 0.5 s，超时 1.5 s 判离线；窗口断开总线时显示「已断开」；
- Assess 窗口缺样本判不通过。

### 7.3 测试

- 现有 `tests/tcanet` 83 项保持通过；
- 新增单元测试：策略路由命令生成（dry-run）、StateAggregator、异步 Assess（模拟样本）、
  Rollback 路径、`paper_fig1` 闭包范围（G2 故障时 e3 不在 E^aff）；
- `--sim` 端到端：无界面跑完全部剧本（macOS 可运行）；
- netns 集成测试：仅在 Linux + root 下运行，否则 skip；
- Ubuntu 彩排检查清单（文档）。

## 8. 文件布局（拟）

```
src/tcanet/dataplane.py              # Dataplane 协议 + NullDataplane
src/tcanet/scenario_fig1.py          # paper_fig1 场景
src/tcanet/verify.py                 # 改：rollback、异步测量、on_rollback hook
src/tcanet/prototype/
  bus.py                             # Unix socket 发布/订阅
  topology.py                        # netns/veth/tc 构建与清理（支持 dry-run）
  netns_dataplane.py                 # FT → ip rule / ip route
  aggregator.py                      # StateAggregator
  controller.py                      # 控制器进程
  agents/{app,trans,net,phy}.py      # 各层 Agent 进程；app.py 含 AppAdapter
  sim.py                             # --sim 测量源
  ctl.py                             # tcanetctl
  launcher.py                        # tcanet-demo up/down/status，wmctrl/tmux 布局
  ui/{console,flows,phy,app_dag}.py  # 各窗口
  capture.py / figure.py / timeline.py
tests/tcanet/prototype/...
docs/prototype-rehearsal.md          # 彩排清单
pyproject.toml                       # 新增 [demo] extra 与命令入口
```

## 9. 待实现时确定的事项

- 端点挂载位置与是否增加 L6：以真实闭包/选择代码验证 §1.2 中「e3 不受影响」与
  「回滚后有替代方案」两条后确定；
- 目标 Ubuntu 版本的 iperf3 版本（影响逐秒输出解析方式）；
- 屏幕分辨率（影响布局坐标，布局参数化）。

## 10. 实现阶段修订（2026-10-07）

1. 接入资源按上/下行建模（`World.access_model="duplex"`）：e1/e2 用 G4 下行，e3 用 G4 上行，G2 故障时 e2 经「共享 G4 下行接入」被拉入 E^aff，e3 在范围外。
2. 新增 L6（G1→G4，30 Mbps/20 ms）与 `World.min_path_alternates=2`，回滚后仍有替代路径；第 4 幕隐藏劣化注入 **L3**（首选方案经 L3）。
3. 测量改用自写的序号+时间戳 UDP 流（同机时钟 → 真实单向时延；断流期间持续报 0），iperf3/ping/tcpdump 保留为现场人工核验工具。
4. 进程模型与 World 目录一致：每网关 Trans/Net/Phy 各一个 + 4 个 AppAgent，共 16 个。
5. 权限拆分：`root.py` 与 `tcanetctl` 内核操作用 sudo；控制器与窗口以普通用户运行；演示机使用 Xorg 会话。
6. 时间参数：Assess 窗口 1.3 s（稳定期 0.6 s，大于 0.5 s 采样周期，只采用 Apply 之后完整的采样期），检测去抖 0.7 s，心跳 0.5 s/超时 1.5 s。丢包按每个采样期内实际收到的序号区间计算，路径切换间隙的中断不计入新路径的丢包。
7. FT 内核状态以 (网关, 依赖) 为键：同一网关上的替换不再先装后删，回滚恢复该网关原有条目；故障网关上的撤销/恢复不等待 ack。
8. 第 2 幕实测选择为 AppAgent 降码率至 17.5 Mbps（L1 利用率软目标），同时列出被拒组合（含 25×1.2>28）。
9. 测试按用户要求精简为关键几组：核心场景、Dataplane 回滚、事件检测、总线数据面边界、五幕端到端（sim）、sim 真实进程 up/down、Qt 冒烟、内核 netns 集成。
10. 已在特权 Linux 容器（Docker，aarch64）中以真实 netns 验证：内核集成测试 2/2 通过；完整五幕连续两遍全部按设计提交（组网 ≈1.35 s，G2 故障恢复 ≈2.1 s，回滚幕 ≈3.5 s）。
