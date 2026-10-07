# TCANet 原型系统现场演示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在单台 Ubuntu 上实现一个可现场演示的 TCANet 原型：16 个独立 Agent 进程运行在 network namespace 构成的 4 网关数据面上，控制器调用 `src/tcanet` 真实执行论文 Algorithm 1（Apply 下发真实策略路由、Assess 用真实测量、失败真实 Rollback），配套 Ubuntu 原生窗口（Qt 曲线 + rich 终端）、故障注入命令、演示图产出工具，以及无需 root 的 `--sim` 回退模式。

**Architecture:** 宿主机上一个 Unix-socket 消息总线（broker）连接所有进程；root 侧（`root.py`）建 netns 拓扑并在各 namespace 内拉起 App/Trans/Net/Phy Agent；用户侧（`launcher.py`）运行控制器与窗口。控制器通过 `BusDataplane`（实现新增的 `Dataplane` 协议）把 ΔFT/Φ/层动作作为 `cmd.action` 下发给 Agent 执行，通过 `BusMeasurement` 从 Agent 上报的真实流量样本做 Assess。故障由 `tcanetctl` 直接作用于内核/进程，控制器只通过 Agent 上报感知。`--sim` 模式下数据面换成 `SimNetwork` 进程，其余不变。

**Tech Stack:** Python ≥3.10、asyncio、iproute2（`ip netns`/`ip rule`/`ip route`）、`tc netem`、PySide6 + pyqtgraph（窗口）、rich（终端）、matplotlib + Pillow（演示图）、gnome-terminal + tmux + wmctrl（布局）、unittest 风格测试（pytest 运行）。

**Spec:** `docs/superpowers/specs/2026-10-06-tcanet-prototype-demo-design.md`（含 §10 实现阶段修订，Task 15 写入）

## Global Constraints

- Python `requires-python = ">=3.10"`；新依赖只进可选 extra `[demo]`：`PySide6>=6.5`、`pyqtgraph>=0.13`、`rich>=13`（matplotlib/Pillow 已在 `[experiments]`，`[demo]` 也列入）。
- 现有 `tests/tcanet` 83 项测试在每个任务结束时必须全部通过：`python -m pytest -q tests/tcanet`。
- 不改 `src/tcanet` 内部函数/类名；所有新增参数必须有默认值，默认行为与现在逐位一致。
- 场景对齐论文新版 Fig. 1：a1 无人机@G1、a2 摄像头@G3 → a3 边缘 AI@G4 → a4 救援车@G3；依赖 e1 a1→a3 15 Mbps、e2 a2→a3 8 Mbps、e3 a3→a4 5 Mbps；链路 L1–L5 沿用 `scenario.py` 参数，新增 L6 G1→G4 30 Mbps/20 ms。
- 物理层数据一律标注 `SIM`；真实测量标注 `MEASURED`；`--sim` 模式下所有窗口标题含 `SIMULATION`。
- 控制器**不得订阅** `ctl.`、`sim.`、`ops.` 前缀的总线消息（故障只能经 Agent 上报感知）。
- 演示路径不得使用 `scenario.FirstAttemptViolating`；回滚只能由真实 Assess 失败触发。
- 网络命名空间统一前缀 `tc-`（`tc-g1`…`tc-g4`、`tc-a1`…`tc-a4`），避免与已有 `testbed/`（`h-*`）冲突。
- 运行目录默认 `/tmp/tcanet-demo`（环境变量 `TCANET_RUN_DIR` 覆盖），总线 socket 为 `<run_dir>/bus.sock`，权限 0666；全量消息记录 `<run_dir>/events.jsonl`。
- 时间参数统一乘以 `TCANET_TIME_SCALE`（默认 1.0；测试用 0.2）。
- 测试中的 Unix socket 一律放在 `tempfile.mkdtemp(dir="/tmp")` 下（macOS 默认 TMPDIR 过长会超出 socket 路径上限）。
- Ubuntu 演示机需登录 **Ubuntu on Xorg** 会话（`wmctrl` 在 Wayland 下无法摆放窗口），GUI 进程以普通用户运行，只有 `root.py` 与 `tcanetctl` 的内核操作用 sudo。

## Review Focus

1. **连续多幕运行**（不 reset 直接第 2 幕→第 3 幕）：第 2 幕若改了路径，第 3 幕仍须能识别经过故障网关的依赖并找到备选路径——由 Task 1 的 `stage_decision(..., world)` 补全 `link_ids` 与 Task 1 测试 `test_rerouted_path_keeps_link_ids_for_next_episode` 覆盖。
2. **Agent 被杀后的 Apply**：动作下发给已死 Agent 时必须超时失败并回滚，而不是卡死——Task 11 测试 `test_dead_executor_times_out_and_fails_batch`。
3. **故障网关上的 remove_rule**：网关已死时撤销其转发项不能等待 ack——Task 11 测试 `test_remove_rule_at_failed_gateway_needs_no_ack`。
4. **零样本 Assess**（流量完全中断、sink 无包）：观测须判为不达标而不是格式化 `None` 崩溃——Task 11 测试 `test_no_packets_yields_violating_observation`。
5. **邻居上报不同步**：G2 故障时 G1、G4 的 NetAgent 上报相差最多一个采样周期，检测器必须归类为一个 `gateway_failure(G2)` 而非两个 `link_failure`——Task 10 测试 `test_staggered_reports_classify_as_gateway_failure`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/tcanet/spec.py`（改） | `World.access_model`、`World.min_path_alternates`、`World.access_resource_ids()` |
| `src/tcanet/feasibility.py`（改） | 接入资源按 `access_resource_ids` 计费；BOOST 作用于三种接入 id |
| `src/tcanet/closure.py`（改） | `dependency_relations(..., world=None)` 使用方向化接入资源 |
| `src/tcanet/subnet.py`（改） | `path_link_ids(world, gateway_path)` |
| `src/tcanet/executor.py`（改） | `stage_decision(..., world=None)` 为 REROUTE 补全 `link_ids` |
| `src/tcanet/candidates.py`（改） | `_alternate_paths` 支持 `min_path_alternates`、缓存键含链路状态 |
| `src/tcanet/scenario_fig1.py`（新） | 论文新版 Fig. 1 场景 |
| `src/tcanet/dataplane.py`（新） | `Dataplane` 协议 + `NullDataplane` |
| `src/tcanet/verify.py`（改） | dataplane 注入、异步测量、Rollback、`on_rollback`、K_max 术语 |
| `src/tcanet/scenario.py`（改） | `run_formation(..., dataplane=None, measure=None)` |
| `src/tcanet/prototype/config.py` | 运行目录、时间参数 |
| `src/tcanet/prototype/addressing.py` | 地址规划、`Cmd`、FT 条目 → `ip rule/route` 命令 |
| `src/tcanet/prototype/topology.py` | netns/veth/tc 构建与清理命令、命令执行器 |
| `src/tcanet/prototype/bus.py` | Broker + BusClient（JSON lines over Unix socket） |
| `src/tcanet/prototype/flow.py` | 带序号/时间戳的 UDP 流量发送器与接收统计 |
| `src/tcanet/prototype/simnet.py` | `--sim` 数据面（按已安装规则逐跳转发的模型） |
| `src/tcanet/prototype/fabric.py` | `NetnsFabric` / `SimFabric`：Agent 访问数据面的统一接口 |
| `src/tcanet/prototype/agents/base.py` | Agent 基类（hello/心跳/命令分发/日志） |
| `src/tcanet/prototype/agents/{net,phy,trans,app}.py` | 四类 Agent |
| `src/tcanet/prototype/agents/__main__.py` | Agent 进程入口 |
| `src/tcanet/prototype/aggregator.py` | `StateAggregator`、`FlowBook`、`EventDetector` |
| `src/tcanet/prototype/bus_dataplane.py` | `BusDataplane`、`AckWaiter`、命令构造 |
| `src/tcanet/prototype/assess.py` | `BusMeasurement` |
| `src/tcanet/prototype/log_format.py` | Algorithm 1 日志条目与渲染 |
| `src/tcanet/prototype/controller.py` | 控制器进程 |
| `src/tcanet/prototype/runtime.py` | 进程表（pid 文件、启动/杀死 Agent） |
| `src/tcanet/prototype/background.py` | 链路保护负载 d^prot（L1 上 12 Mbps 背景流） |
| `src/tcanet/prototype/root.py` | 特权侧 up/down |
| `src/tcanet/prototype/ctl.py` | `tcanetctl` |
| `src/tcanet/prototype/launcher.py` | `tcanet-demo` up/down/status/logs/capture，窗口布局 |
| `src/tcanet/prototype/ui/{theme,model,qtbus,common,flows,phy,app_dag,console}.py` | 配色、窗口数据模型与界面 |
| `src/tcanet/prototype/capture.py` / `figure.py` / `timeline.py` | 截图、拼图、时间轴图 |
| `tests/tcanet/test_fig1_scenario.py`、`tests/tcanet/test_dataplane.py` | 核心改造测试 |
| `tests/tcanet/prototype/test_*.py`、`harness.py` | 原型各模块测试与总线测试工具 |
| `docs/prototype-rehearsal.md` | Ubuntu 彩排清单 |

---

### Task 0: 工作分支

- [ ] **Step 1: 建分支（保留工作区现有未提交内容）**

```bash
git checkout -b feat/prototype-demo
git status --short | head
```
Expected: 切到新分支，工作区原有修改与未跟踪文件保持不变。后续每个任务的 commit 只 `git add` 本任务列出的文件。

---

### Task 1: 核心资源模型与 paper_fig1 场景

**Files:**
- Modify: `src/tcanet/spec.py`（`World` 类）
- Modify: `src/tcanet/feasibility.py:126-131,170-174`
- Modify: `src/tcanet/closure.py:144-180`
- Modify: `src/tcanet/subnet.py`（在 `compile_forwarding` 之前新增函数）
- Modify: `src/tcanet/executor.py:50-75`（`stage_decision`）及 import
- Modify: `src/tcanet/candidates.py:198-236`（`_ALT_CACHE`、`_alternate_paths`）及 import
- Modify: `src/tcanet/verify.py:349`、`:418`（两处调用）
- Create: `src/tcanet/scenario_fig1.py`
- Test: `tests/tcanet/test_fig1_scenario.py`

**Interfaces:**
- Produces:
  - `World.access_model: str = "shared"`，`World.min_path_alternates: int = 0`，`World.access_resource_ids(source_gateway: str, target_gateway: str) -> tuple[str, str]`
  - `path_link_ids(world: World, gateway_path: tuple[str, ...]) -> tuple[str, ...]`（`src.tcanet.subnet`）
  - `stage_decision(task, subnet, actions, world: World | None = None) -> StagedDecision`
  - `dependency_relations(task, subnet, world: World | None = None) -> DependencyRelations`
  - `src.tcanet.scenario_fig1`: `GATEWAYS`, `LINKS`, `ENDPOINTS`, `DEPENDENCIES`, `ACCESS_CAPACITY_MBPS = 60.0`, `SCENARIO_ID = "paper_fig1"`, `build_world() -> World`, `fig1_task(world: World) -> TaskSpecification`, `load(scenario_id: str) -> tuple[World, TaskSpecification]`

- [ ] **Step 1: 写失败测试**

Create `tests/tcanet/test_fig1_scenario.py`:

```python
"""paper_fig1 scenario: duplex access, alternates, scoped recovery."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.tcanet.candidates import coordination_candidates
from src.tcanet.closure import demand_change, gateway_failure, support_failure
from src.tcanet.executor import stage_decision
from src.tcanet.feasibility import evaluate_joint_decision
from src.tcanet.scenario import fail_gateway, fail_support_agent, run_formation, with_demand
from src.tcanet.scenario_fig1 import build_world, fig1_task, load
from src.tcanet.verify import RecoveryController, observe_state, projected_measurements
from tests.tcanet.common import mini_world


def _formed():
    world = build_world()
    task = fig1_task(world)
    subnet = asyncio.run(run_formation(task, world)).subnet
    return world, task, subnet


def _l3_lossy(task):
    """Measurement source reporting 10% loss on any path through L3."""

    def measure(staged, world, dep_ids):
        observations = projected_measurements(staged, world, dep_ids, task=task)
        return tuple(
            replace(obs, loss_rate=0.10)
            if "L3" in staged.subnet.paths[obs.dep_id].link_ids
            else obs
            for obs in observations
        )

    return measure


class Fig1ScenarioTests(unittest.TestCase):
    def test_load_returns_fresh_world_and_task(self) -> None:
        world, task = load("paper_fig1")
        self.assertEqual(world.access_model, "duplex")
        self.assertEqual(
            [dep.dep_id for dep in task.dag.dependencies], ["e1", "e2", "e3"]
        )
        with self.assertRaises(KeyError):
            load("nope")

    def test_formation_paths_and_forwarding(self) -> None:
        _world, _task, subnet = _formed()
        self.assertEqual(subnet.paths["e1"].gateway_path, ("G1", "G2", "G4"))
        self.assertEqual(subnet.paths["e2"].gateway_path, ("G3", "G4"))
        self.assertEqual(subnet.paths["e3"].gateway_path, ("G4", "G3"))
        self.assertEqual(len(subnet.forwarding), 7)

    def test_gateway_failure_scope_excludes_e3(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(set(result.closure.initial), {"e1"})
        self.assertEqual(result.closure.final, frozenset({"e1", "e2"}))
        # J ties between L6 and G3 detours; the smaller M (4 < 5) wins.
        self.assertEqual(result.subnet.paths["e1"].gateway_path, ("G1", "G3", "G4"))
        self.assertEqual(result.subnet.paths["e3"], subnet.paths["e3"])
        self.assertEqual(result.attempts[0].selection.selected.modification_scope, 4)

    def test_rejected_assess_falls_back_to_second_alternate(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        controller = RecoveryController(measure=_l3_lossy(task))
        result = asyncio.run(
            controller.recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(
            [attempt.outcome for attempt in result.attempts],
            ["window_rejected", "accepted"],
        )
        self.assertEqual(result.subnet.paths["e1"].gateway_path, ("G1", "G4"))

    def test_demand_increase_rejects_reliable_mode_on_l1(self) -> None:
        world, task, subnet = _formed()
        task = with_demand(task, "e1", 25.0)
        candidates = coordination_candidates(
            task, world, subnet, observe_state(task, world, subnet),
            focus_dep_ids=("e1",),
        )
        reliable = next(
            action
            for actions in candidates.values()
            for action in actions
            if action.action_id == "transport:SWITCH_MODE:e1:reliable"
        )
        result = evaluate_joint_decision(task, world, subnet, (reliable,))
        self.assertFalse(result.feasible)
        self.assertIn("shared_resource:link:L1", result.violations)  # 25x1.2=30>28

    def test_demand_change_recovers(self) -> None:
        world, task, subnet = _formed()
        task = with_demand(task, "e1", 25.0)
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, demand_change("e1", 25 / 15))
        )
        self.assertTrue(result.recovered, result.error)

    def test_support_failure_rebinds_only_e3(self) -> None:
        world, task, subnet = _formed()
        fail_support_agent(world, "physical-G4")
        result = asyncio.run(
            RecoveryController().recover(
                task, subnet, world, support_failure("physical-G4")
            )
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(result.closure.final, frozenset({"e3"}))
        self.assertNotEqual(
            result.subnet.bindings.binding("e3").p_agent_id, "physical-G4"
        )
        self.assertEqual(result.subnet.paths, subnet.paths)

    def test_rerouted_path_keeps_link_ids_for_next_episode(self) -> None:
        world, task, subnet = _formed()
        fail_gateway(world, "G2")
        result = asyncio.run(
            RecoveryController().recover(task, subnet, world, gateway_failure("G2"))
        )
        self.assertEqual(result.subnet.paths["e1"].link_ids, ("L3", "L5"))
        staged = stage_decision(task, subnet, (), world)
        self.assertEqual(staged.subnet.paths["e1"].link_ids, ("L1", "L2"))

    def test_shared_access_model_is_default(self) -> None:
        world = mini_world()
        self.assertEqual(
            world.access_resource_ids("GA", "GC"), ("access:GA", "access:GC")
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/test_fig1_scenario.py`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.tcanet.scenario_fig1'`）

- [ ] **Step 3: 修改 `World`（`src/tcanet/spec.py`）**

在 `World` 的 `failed_gateways` 字段之后插入：

```python
    # "shared": one access resource per gateway (both directions).
    # "duplex": separate uplink/downlink access resources per gateway.
    access_model: str = "shared"
    # Minimum number of alternate paths offered per dependency (0 = only
    # the single-link-exclusion alternates).
    min_path_alternates: int = 0

    def access_resource_ids(
        self, source_gateway: str, target_gateway: str
    ) -> tuple[str, str]:
        """Access resources charged by a flow entering at ``source_gateway``
        and leaving at ``target_gateway``."""
        if self.access_model == "duplex":
            return (f"access-ul:{source_gateway}", f"access-dl:{target_gateway}")
        return (f"access:{source_gateway}", f"access:{target_gateway}")
```

- [ ] **Step 4: 修改 `feasibility.py`**

替换 BOOST 段：

```python
    for gateway_id, boost in capacity_boost.items():
        for access_id in (
            f"access:{gateway_id}",
            f"access-ul:{gateway_id}",
            f"access-dl:{gateway_id}",
        ):
            if access_id in resource_capacity:
                resource_capacity[access_id] += boost
```

替换接入计费段（原 `for gateway_id in (path[0], path[-1]) if path else ():` 四行）：

```python
        access_ids = world.access_resource_ids(path[0], path[-1]) if path else ()
        for access_id in access_ids:
            if access_id in resource_capacity:
                resource_load[access_id] = resource_load.get(access_id, 0.0) + effective
```

- [ ] **Step 5: 修改 `closure.py`**

签名改为：

```python
def dependency_relations(
    task: TaskSpecification,
    subnet: SubnetState,
    world: World | None = None,
) -> DependencyRelations:
```

替换 `if record.gateway_path:` 块：

```python
        if record.gateway_path:
            first, last = record.gateway_path[0], record.gateway_path[-1]
            access_ids = (
                world.access_resource_ids(first, last)
                if world is not None
                else (f"access:{first}", f"access:{last}")
            )
            for access_id in access_ids:
                access_users.setdefault(access_id, set()).add(dep.dep_id)
```

- [ ] **Step 6: 新增 `path_link_ids`（`subnet.py`，放在 `compile_forwarding` 之前）**

```python
def path_link_ids(world: World, gateway_path: tuple[str, ...]) -> tuple[str, ...]:
    """Link ids traversed by ``gateway_path`` (first live link per hop)."""
    link_ids: list[str] = []
    for source, target in zip(gateway_path, gateway_path[1:]):
        links = world.graph.links_between(source, target)
        live = [link for link in links if link.up] or list(links)
        if live:
            link_ids.append(live[0].link_id)
    return tuple(link_ids)
```

- [ ] **Step 7: 修改 `executor.py`**

import 改为：

```python
from src.tcanet.subnet import (
    ExecutableAction,
    ForwardingEntry,
    PathRecord,
    SubnetState,
    compile_forwarding,
    path_link_ids,
)
```

`stage_decision` 签名增加 `world: World | None = None`，REROUTE 分支替换为：

```python
        if action.action == "REROUTE":
            gateway_path = tuple(
                str(node) for node in action.parameters["gateway_path"]
            )
            new_paths[action.target] = PathRecord(
                dep_id=action.target,
                gateway_path=gateway_path,
                link_ids=(
                    path_link_ids(world, gateway_path) if world is not None else ()
                ),
            )
```

- [ ] **Step 8: 修改 `candidates.py`**

import 改为 `from src.tcanet.subnet import SubnetState, path_link_ids, solve_dependency_paths`；缓存类型改为 `_ALT_CACHE: dict[tuple[object, ...], tuple[tuple[str, ...], ...]] = {}`；在 `_alternate_paths` 中：

```python
    cache_key = (
        dep_id,
        f"v{subnet.version}:{current.path_id}",
        tuple(sorted(link.link_id for link in world.graph.links if link.up)),
        world.min_path_alternates,
    )
```

把 `for link_id in current.link_ids:` 循环改为先计算 `current_links`：

```python
    current_links = current.link_ids or path_link_ids(world, current.gateway_path)
    for link_id in current_links:
```

并在 `_ALT_CACHE[cache_key] = ...` 之前插入：

```python
    frontier = list(alternates)
    while frontier and len(alternates) < world.min_path_alternates:
        base = frontier.pop(0)
        for link_id in base.link_ids:
            paths, _failures = solve_dependency_paths(
                task,
                world,
                exclude_link_ids=frozenset(current_links) | {link_id},
            )
            alt = paths.get(dep_id)
            if alt is not None and alt.gateway_path not in seen:
                seen.add(alt.gateway_path)
                alternates.append(alt)
                frontier.append(alt)
            if len(alternates) >= world.min_path_alternates:
                break
```

- [ ] **Step 9: 修改 `verify.py` 两处调用**

```python
        relations = dependency_relations(task, subnet, world)
```

```python
            staged = stage_decision(task, subnet, selected.actions, world)
```

- [ ] **Step 10: 新增 `src/tcanet/scenario_fig1.py`**

```python
"""Paper Fig. 1 (current draft) rescue scenario for the live prototype.

Task DAG: a1 (UAV) and a2 (roadside camera) stream to a3 (edge AI), which
alerts a4 (rescue vehicle).  Gateways G1-G4 reuse the rescue links L1-L5
and add the direct G1->G4 link L6 so a rejected detour still leaves a
second alternate (paper Alg. 1, lines 19-21).  Access resources are duplex:
a3 receives e1/e2 on G4's downlink and sends e3 on G4's uplink, so e3 is
not coupled to e1 through access capacity.
"""
from __future__ import annotations

from src.tcanet.spec import (
    Dependency,
    Endpoint,
    GatewayGraph,
    GatewayLink,
    HardRequirements,
    Layer,
    SharedResource,
    SoftTarget,
    SupportAgent,
    TaskDAG,
    TaskSpecification,
    World,
)

SCENARIO_ID = "paper_fig1"
GATEWAYS = ("G1", "G2", "G3", "G4")

# (id, source, target, capacity Mbps, delay ms, protected Mbps)
LINKS = (
    ("L1", "G1", "G2", 40.0, 8.0, 12.0),  # 28 Mbps available (Eq. 7 example)
    ("L2", "G2", "G4", 40.0, 10.0, 0.0),
    ("L3", "G1", "G3", 30.0, 18.0, 0.0),
    ("L4", "G4", "G3", 30.0, 12.0, 0.0),
    ("L5", "G3", "G4", 30.0, 12.0, 0.0),
    ("L6", "G1", "G4", 30.0, 20.0, 0.0),
)
ACCESS_CAPACITY_MBPS = 60.0

# (agent id, role description, gateway)
ENDPOINTS = (
    ("a1", "UAV imagery (drone)", "G1"),
    ("a2", "roadside camera", "G3"),
    ("a3", "edge AI", "G4"),
    ("a4", "rescue vehicle", "G3"),
)

# (dependency, source, target, flow type, demand Mbps)
DEPENDENCIES = (
    ("e1", "a1", "a3", "video", 15.0),
    ("e2", "a2", "a3", "video", 8.0),
    ("e3", "a3", "a4", "alert", 5.0),
)


def build_world() -> World:
    """Fresh Fig. 1 world: gateways, links, duplex access, support agents."""
    links = tuple(
        GatewayLink(link_id, source, target, capacity, delay)
        for link_id, source, target, capacity, delay, _ in LINKS
    )
    resources = {
        f"link:{link_id}": SharedResource(f"link:{link_id}", capacity, protected)
        for link_id, _s, _t, capacity, _d, protected in LINKS
    }
    for gateway in GATEWAYS:
        for direction in ("ul", "dl"):
            resource_id = f"access-{direction}:{gateway}"
            resources[resource_id] = SharedResource(resource_id, ACCESS_CAPACITY_MBPS)
    agents = {
        f"{role.value}-{gateway}": SupportAgent(
            agent_id=f"{role.value}-{gateway}",
            layer=role,
            gateway_id=gateway,
            name=f"{role.value}Agent@{gateway}",
        )
        for gateway in GATEWAYS
        for role in (Layer.TRANSPORT, Layer.NETWORK, Layer.PHYSICAL)
    }
    return World(
        graph=GatewayGraph(gateways=GATEWAYS, links=links),
        resources=resources,
        support_agents=agents,
        access_model="duplex",
        min_path_alternates=2,
    )


def fig1_task(world: World) -> TaskSpecification:
    """``T_m`` for the Fig. 1 DAG; also binds endpoints into ``world``."""
    endpoints = tuple(Endpoint(agent_id, name, gw) for agent_id, name, gw in ENDPOINTS)
    deps = tuple(
        Dependency(dep_id=dep_id, source=src, target=dst, flow_type=kind, demand_mbps=rate)
        for dep_id, src, dst, kind, rate in DEPENDENCIES
    )
    dag = TaskDAG(
        task_id=SCENARIO_ID,
        goal="edge-intelligence guided rescue",
        endpoints=endpoints,
        dependencies=deps,
    )
    task = TaskSpecification(
        dag=dag,
        hard=HardRequirements(min_throughput_mbps=4.0, max_delay_ms=60.0, max_loss_rate=0.05),
        soft=(
            SoftTarget("mean_delay", "upper", 25.0, "mean_delay_ms"),
            SoftTarget("utilization", "upper", 0.85, "max_resource_utilization"),
            SoftTarget("total_demand", "lower", 28.0, "total_effective_demand_mbps"),
            SoftTarget("delivered_floor", "lower", 4.0, "min_delivered_mbps"),
        ),
    )
    world.endpoints = {endpoint.agent_id: endpoint for endpoint in endpoints}
    return task


_SCENARIOS = {SCENARIO_ID: (build_world, fig1_task)}


def load(scenario_id: str) -> tuple[World, TaskSpecification]:
    """Fresh ``(world, task)`` for a registered scenario id."""
    build, make_task = _SCENARIOS[scenario_id]
    world = build()
    return world, make_task(world)
```

- [ ] **Step 11: 运行测试**

Run: `python -m pytest -q tests/tcanet`
Expected: 全部通过（83 + 9 = 92 passed）。

- [ ] **Step 12: Commit**

```bash
git add src/tcanet/spec.py src/tcanet/feasibility.py src/tcanet/closure.py src/tcanet/subnet.py src/tcanet/executor.py src/tcanet/candidates.py src/tcanet/verify.py src/tcanet/scenario_fig1.py tests/tcanet/test_fig1_scenario.py
git commit -m "feat(tcanet): duplex access, path alternates and paper Fig.1 scenario"
```

---

### Task 2: Dataplane 接口、异步测量与真实 Rollback

**Files:**
- Create: `src/tcanet/dataplane.py`
- Modify: `src/tcanet/verify.py`（模块 docstring、`DEFAULT_MAX_ATTEMPTS` 注释、`MeasurementProvider`、`RecoveryHooks`、`RecoveryController`）
- Modify: `src/tcanet/scenario.py`（`run_formation`）
- Test: `tests/tcanet/test_dataplane.py`

**Interfaces:**
- Consumes: Task 1 的 `scenario_fig1.build_world/fig1_task`
- Produces:
  - `class Dataplane(Protocol)`: `async apply(staged: StagedDecision, world: World) -> ExecutionRecord`；`async rollback(staged: StagedDecision, world: World) -> None`
  - `class NullDataplane`: 上述实现，`rollbacks: list[int]` 记录被回滚的 staged 版本号
  - `RecoveryController(*, max_attempts=3, window_ms=2000.0, measure=None, dataplane: Dataplane | None = None)`；`measure` 可返回 awaitable
  - `RecoveryHooks.on_rollback: Callable[[RecoveryAttempt], Awaitable[None]] | None`
  - `run_formation(task, world, *, dataplane: Dataplane | None = None, measure: MeasurementProvider | None = None) -> FormationOutcome`

- [ ] **Step 1: 写失败测试**

Create `tests/tcanet/test_dataplane.py`:

```python
"""Dataplane seam: Apply/Rollback and asynchronous Assess (paper Alg. 1)."""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from src.tcanet.closure import gateway_failure
from src.tcanet.dataplane import NullDataplane
from src.tcanet.scenario import fail_gateway, run_formation
from src.tcanet.scenario_fig1 import build_world, fig1_task
from src.tcanet.verify import (
    DepObservation,
    RecoveryController,
    RecoveryHooks,
    projected_measurements,
)


class RecordingDataplane(NullDataplane):
    def __init__(self) -> None:
        super().__init__()
        self.applied: list[int] = []

    async def apply(self, staged, world):
        self.applied.append(staged.subnet.version)
        return await super().apply(staged, world)


def _async_l3_lossy(task):
    async def measure(staged, world, dep_ids):
        await asyncio.sleep(0)
        observations = projected_measurements(staged, world, dep_ids, task=task)
        return tuple(
            replace(obs, loss_rate=0.10)
            if "L3" in staged.subnet.paths[obs.dep_id].link_ids
            else obs
            for obs in observations
        )

    return measure


class DataplaneTests(unittest.TestCase):
    def _formed(self):
        world = build_world()
        task = fig1_task(world)
        subnet = asyncio.run(run_formation(task, world)).subnet
        return world, task, subnet

    def test_rejected_window_rolls_back_then_retries(self) -> None:
        world, task, subnet = self._formed()
        fail_gateway(world, "G2")
        dataplane = RecordingDataplane()
        rolled: list[int] = []

        async def on_rollback(attempt):
            rolled.append(attempt.index)

        controller = RecoveryController(
            measure=_async_l3_lossy(task), dataplane=dataplane
        )
        result = asyncio.run(
            controller.recover(
                task, subnet, world, gateway_failure("G2"),
                hooks=RecoveryHooks(on_rollback=on_rollback),
            )
        )
        self.assertTrue(result.recovered, result.error)
        self.assertEqual(dataplane.applied, [2, 2])
        self.assertEqual(dataplane.rollbacks, [2])
        self.assertEqual(rolled, [1])

    def test_formation_rolls_back_when_assess_fails(self) -> None:
        world = build_world()
        task = fig1_task(world)
        dataplane = RecordingDataplane()

        async def silent(staged, world, dep_ids):
            return ()

        outcome = asyncio.run(
            run_formation(task, world, dataplane=dataplane, measure=silent)
        )
        self.assertIsNone(outcome.subnet)
        self.assertEqual(dataplane.applied, [1])
        self.assertEqual(dataplane.rollbacks, [1])

    def test_formation_accepts_with_async_measure(self) -> None:
        world = build_world()
        task = fig1_task(world)

        async def good(staged, world, dep_ids):
            return tuple(
                DepObservation(dep, 100.0, 10.0, 20.0, 0.001) for dep in dep_ids
            )

        outcome = asyncio.run(run_formation(task, world, measure=good))
        self.assertIsNotNone(outcome.subnet)
        self.assertEqual(outcome.subnet.version, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/test_dataplane.py`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.tcanet.dataplane'`）

- [ ] **Step 3: 新增 `src/tcanet/dataplane.py`**

```python
"""Data-plane seam for Apply and Rollback (paper Alg. 1, lines 15 and 19).

The controller hands every staged configuration to a ``Dataplane``.  The
default ``NullDataplane`` only revalidates preconditions (the behaviour of
the in-process simulator); the live prototype installs the forwarding,
binding and layer actions on real executors and undoes them on rollback.
"""
from __future__ import annotations

from typing import Protocol

from src.tcanet.executor import ExecutionRecord, StagedDecision, execute_staged
from src.tcanet.spec import World


class Dataplane(Protocol):
    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord: ...

    async def rollback(self, staged: StagedDecision, world: World) -> None: ...


class NullDataplane:
    """In-process executor: precondition revalidation, no device state."""

    def __init__(self) -> None:
        self.rollbacks: list[int] = []

    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord:
        return await execute_staged(staged, world)

    async def rollback(self, staged: StagedDecision, world: World) -> None:
        self.rollbacks.append(staged.subnet.version)
```

- [ ] **Step 4: 修改 `verify.py`**

1. 模块 docstring 末段替换为（术语对齐新版论文 Sec. IV-C / Alg. 1）：

```python
"""Post-installation assessment and bounded reconfiguration (paper Sec. IV-C, Alg. 1).

After a joint configuration is applied, TCANet assesses it before commit:

1. the updates were installed successfully,
2. superseded forwarding state was withdrawn,
3. every affected dependency remains reachable, and
4. freshly measured service levels satisfy the hard requirements ``q^H``
   (normalized residuals ``g_{m,k} <= 0``, Eq. 5-6).

Observations that have not arrived keep the check pending; if the window
expires with observations missing, or a measurement violates ``q^H``, the
attempt fails: the configuration is rolled back to ``c^{v_m}``, the state is
refreshed, the candidate is removed and the next attempt selects again, for
at most ``K_max`` attempts (default 3).
"""
```

2. `DEFAULT_MAX_ATTEMPTS = 3  # K_max (paper Alg. 1)`，`DEFAULT_WINDOW_MS = 2000.0  # assessment window`。

3. import 增加 `import inspect` 与 `from src.tcanet.dataplane import Dataplane, NullDataplane`。

4. `MeasurementProvider.__call__` 返回类型改为 `tuple[DepObservation, ...] | Awaitable[tuple[DepObservation, ...]]`。

5. `RecoveryHooks`：删除类体中多余的空行，并在 `on_rejected` 之后增加：

```python
    on_rollback: Callable[[RecoveryAttempt], Awaitable[None]] | None = None
```

6. `RecoveryController.__init__` 增加参数 `dataplane: Dataplane | None = None`，保存 `self._dataplane = dataplane or NullDataplane()`。

7. 在 `recover` 中：
   - `execution = await execute_staged(staged, world)` 改为 `execution = await self._dataplane.apply(staged, world)`。
   - 执行失败分支（`if not execution.ok:`）在 `attempts.append(...)` 之后、`continue` 之前插入：

```python
                await self._dataplane.rollback(staged, world)
                if hooks is not None and hooks.on_rollback is not None:
                    await hooks.on_rollback(attempts[-1])
```

   - 测量段替换为：

```python
            if self._measure is not None:
                observations = self._measure(staged, world, affected)
                if inspect.isawaitable(observations):
                    observations = await observations
            else:
                observations = projected_measurements(
                    staged, world, affected, task=task
                )
```

   - 窗口拒绝分支：在已有的 `on_rejected` hook 调用之后插入（先让 `on_window`/`on_rejected` 记录 Assess 失败，再回滚，日志顺序才是 Assess → Rollback）：

```python
            await self._dataplane.rollback(staged, world)
            if hooks is not None and hooks.on_rollback is not None:
                await hooks.on_rollback(attempts[-1])
```

- [ ] **Step 5: 修改 `scenario.py` 的 `run_formation`**

import 增加 `import inspect`、`from src.tcanet.dataplane import Dataplane, NullDataplane`、`from src.tcanet.verify import MeasurementProvider`（并入已有 verify import）。函数替换为：

```python
async def run_formation(
    task: TaskSpecification,
    world: World,
    *,
    dataplane: Dataplane | None = None,
    measure: MeasurementProvider | None = None,
) -> FormationOutcome:
    """Endpoint confirmation -> paths -> FT -> Apply -> Assess -> commit v1.

    A failed assessment rolls the staged state back (paper Alg. 1, line 19).
    """
    dataplane = dataplane or NullDataplane()
    started = perf_counter()
    bindings = default_bindings(task.dag, world)
    plan = await TaskSubnetBuilder().build(task, world, bindings)
    v0 = SubnetState(
        task_id=task.dag.task_id, version=0, bindings=BindingTable()
    )
    v1 = SubnetState(
        task_id=task.dag.task_id,
        version=plan.next_version,
        paths=plan.paths,
        forwarding=plan.forwarding,
        bindings=plan.bindings,
        accepted=False,
    )
    staged = StagedDecision(
        previous=v0, subnet=v1, actions=(), executable=plan.actions
    )
    execution = await dataplane.apply(staged, world)
    affected = tuple(sorted(dep.dep_id for dep in task.dag.dependencies))
    if measure is not None:
        observations = measure(staged, world, affected)
        if inspect.isawaitable(observations):
            observations = await observations
    else:
        observations = projected_measurements(
            staged, world, affected, task=task, arrival_ms=40.0
        )
    window = evaluate_window(
        task, world, staged, execution, affected, observations
    )
    if not window.accepted:
        await dataplane.rollback(staged, world)
    accepted = replace(v1, accepted=True) if window.accepted else None
    return FormationOutcome(
        plan=plan,
        staged=staged,
        execution=execution,
        window=window,
        subnet=accepted,
        formation_latency_ms=(perf_counter() - started) * 1000.0,
    )
```

注意：`scenario.py` 已从 `verify` 导入多个名字，`MeasurementProvider` 加进同一个 import 语句即可；`dataplane.py` 只依赖 `executor`/`spec`，不存在循环导入。

- [ ] **Step 6: 运行测试**

Run: `python -m pytest -q tests/tcanet`
Expected: 全部通过（95 passed）。

- [ ] **Step 7: Commit**

```bash
git add src/tcanet/dataplane.py src/tcanet/verify.py src/tcanet/scenario.py tests/tcanet/test_dataplane.py
git commit -m "feat(tcanet): dataplane seam with rollback and async assessment"
```

---

### Task 3: 原型包骨架、运行配置与地址规划

**Files:**
- Create: `src/tcanet/prototype/__init__.py`
- Create: `src/tcanet/prototype/config.py`
- Create: `src/tcanet/prototype/addressing.py`
- Create: `tests/tcanet/prototype/__init__.py`
- Test: `tests/tcanet/prototype/test_addressing.py`

**Interfaces:**
- Consumes: Task 1 `scenario_fig1.load`、`run_formation`
- Produces:
  - `config`: `run_dir()`, `bus_path()`, `events_path()`, `mode_path()`, `pids_dir()`, `logs_dir()`, `captures_dir()`, `time_scale()`, `heartbeat_s()`(0.5), `heartbeat_timeout_s()`(1.5), `sample_period_s()`(0.5), `radio_period_s()`(1.0), `assess_window_s()`(1.0), `assess_settle_s()`(0.3), `detect_debounce_s()`(0.7), `ack_timeout_s()`(2.0), `read_mode() -> "netns"|"sim"`, 常量 `FLOW_PORT_BASE=47000`, `PACKET_BYTES=1200`, `K_MAX=3`（均乘 `TCANET_TIME_SCALE`）
  - `addressing`: `Cmd(argv: tuple[str,...], check=True).text()`, `in_ns(ns, cmd) -> Cmd`, `gateway_ns(g)`, `endpoint_ns(a)`, `LinkAddr`, `EndpointAddr`, `AddressPlan(links, endpoints)` + `.link_between(src_gw, dst_gw)`, `.links_at(gw)`, `.endpoints_at(gw)`, `build_plan(world) -> AddressPlan`, `dep_table/dep_priority/flow_port(task, dep_id) -> int`, `rule_params(entry, task, plan) -> dict`, `rule_commands(params, op: "add"|"del") -> list[Cmd]`

`tests/tcanet/prototype/__init__.py` 为空文件（Step 3 中创建，内容为空即可）。

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_addressing.py`**

```python
from __future__ import annotations

import asyncio
import unittest

from src.tcanet.prototype.addressing import (
    Cmd,
    build_plan,
    flow_port,
    gateway_ns,
    in_ns,
    rule_commands,
    rule_params,
)
from src.tcanet.scenario import run_formation
from src.tcanet.scenario_fig1 import load


class AddressingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, self.task = load("paper_fig1")
        self.plan = build_plan(self.world)
        self.subnet = asyncio.run(run_formation(self.task, self.world)).subnet

    def test_plan_is_deterministic(self) -> None:
        self.assertEqual(self.plan.links["L1"].source_ip, "10.0.1.1")
        self.assertEqual(self.plan.links["L6"].target_ip, "10.0.6.2")
        self.assertEqual(self.plan.endpoints["a3"].endpoint_ip, "10.1.3.2")
        self.assertEqual(self.plan.endpoints["a3"].ifname, "acc-a3")
        self.assertEqual(gateway_ns("G1"), "tc-g1")
        self.assertEqual(flow_port(self.task, "e2"), 47001)
        self.assertEqual(
            [link.link_id for link in self.plan.links_at("G1")], ["L1", "L3", "L6"]
        )

    def test_forward_entry_params(self) -> None:
        params = rule_params(self.subnet.forwarding["e1:G1:0"], self.task, self.plan)
        self.assertEqual(params["via"], "10.0.1.2")
        self.assertEqual(params["dev"], "l1")
        self.assertEqual(params["table"], 100)
        self.assertEqual(params["src_ip"], "10.1.1.2")
        self.assertEqual(params["dst_ip"], "10.1.3.2")

    def test_local_delivery_params(self) -> None:
        params = rule_params(self.subnet.forwarding["e1:G4:2"], self.task, self.plan)
        self.assertIsNone(params["via"])
        self.assertEqual(params["dev"], "acc-a3")

    def test_add_and_del_commands(self) -> None:
        params = rule_params(self.subnet.forwarding["e1:G1:0"], self.task, self.plan)
        add = rule_commands(params, "add")
        self.assertEqual(
            add[0].text(),
            "ip route replace 10.1.3.2/32 via 10.0.1.2 dev l1 table 100",
        )
        self.assertFalse(add[1].check)
        self.assertEqual(
            add[2].text(),
            "ip rule add from 10.1.1.2 to 10.1.3.2 priority 1000 table 100",
        )
        delete = rule_commands(params, "del")
        self.assertTrue(all(not cmd.check for cmd in delete))
        with self.assertRaises(ValueError):
            rule_commands(params, "bogus")

    def test_in_ns_prefix(self) -> None:
        cmd = in_ns("tc-g1", Cmd(("ip", "rule", "show")))
        self.assertEqual(cmd.text(), "ip netns exec tc-g1 ip rule show")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_addressing.py`
Expected: FAIL（`ModuleNotFoundError: No module named 'src.tcanet.prototype'`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/__init__.py`**

```python
"""Live TCANet prototype: netns data plane, agent processes, controller, UI."""
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/config.py`**

```python
"""Runtime paths and timing for the live prototype.

All periods scale with ``TCANET_TIME_SCALE`` (default 1.0) so tests can run
the full pipeline faster; paths follow ``TCANET_RUN_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path

FLOW_PORT_BASE = 47000
PACKET_BYTES = 1200
K_MAX = 3  # paper Alg. 1 attempt bound


def run_dir() -> Path:
    return Path(os.environ.get("TCANET_RUN_DIR", "/tmp/tcanet-demo"))


def bus_path() -> Path:
    return run_dir() / "bus.sock"


def events_path() -> Path:
    return run_dir() / "events.jsonl"


def mode_path() -> Path:
    return run_dir() / "mode"


def pids_dir() -> Path:
    return run_dir() / "pids"


def logs_dir() -> Path:
    return run_dir() / "logs"


def captures_dir() -> Path:
    return run_dir() / "captures"


def time_scale() -> float:
    return float(os.environ.get("TCANET_TIME_SCALE", "1.0"))


def heartbeat_s() -> float:
    return 0.5 * time_scale()


def heartbeat_timeout_s() -> float:
    return 1.5 * time_scale()


def sample_period_s() -> float:
    return 0.5 * time_scale()


def radio_period_s() -> float:
    return 1.0 * time_scale()


def assess_window_s() -> float:
    return 1.0 * time_scale()


def assess_settle_s() -> float:
    return 0.3 * time_scale()


def detect_debounce_s() -> float:
    return 0.7 * time_scale()


def ack_timeout_s() -> float:
    return 2.0 * time_scale()


def read_mode() -> str:
    """``"netns"`` or ``"sim"`` as written by ``root up`` (default netns)."""
    try:
        return mode_path().read_text(encoding="utf-8").strip() or "netns"
    except FileNotFoundError:
        return "netns"
```

- [ ] **Step 5: 实现 `src/tcanet/prototype/addressing.py`**

```python
"""Address plan and forwarding-entry -> Linux policy-routing commands.

Each task-specific forwarding entry ``(e, next_g(pi_{m,e}))`` at gateway ``g``
(paper Eq. 3) becomes, inside ``g``'s network namespace::

    ip route replace <dst>/32 via <next-hop> dev <link> table <100+i>
    ip rule add from <src> to <dst> priority <1000+i> table <100+i>

so ``ip rule show`` on a gateway lists exactly its share of ``FT_m``.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.tcanet.prototype.config import FLOW_PORT_BASE
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import ForwardingEntry


@dataclass(frozen=True)
class Cmd:
    """One shell command; ``check=False`` tolerates a non-zero exit."""

    argv: tuple[str, ...]
    check: bool = True

    def text(self) -> str:
        return " ".join(self.argv)


def in_ns(ns: str, cmd: Cmd) -> Cmd:
    return Cmd(("ip", "netns", "exec", ns, *cmd.argv), cmd.check)


def gateway_ns(gateway_id: str) -> str:
    return f"tc-{gateway_id.lower()}"


def endpoint_ns(agent_id: str) -> str:
    return f"tc-{agent_id.lower()}"


@dataclass(frozen=True)
class LinkAddr:
    link_id: str
    source_gw: str
    target_gw: str
    ifname: str
    source_ip: str
    target_ip: str


@dataclass(frozen=True)
class EndpointAddr:
    agent_id: str
    gateway_id: str
    ifname: str  # gateway-side access interface
    endpoint_ip: str
    gateway_ip: str


@dataclass(frozen=True)
class AddressPlan:
    links: dict[str, LinkAddr]
    endpoints: dict[str, EndpointAddr]

    def link_between(self, source_gw: str, target_gw: str) -> LinkAddr | None:
        for link_id in sorted(self.links):
            link = self.links[link_id]
            if link.source_gw == source_gw and link.target_gw == target_gw:
                return link
        return None

    def links_at(self, gateway_id: str) -> tuple[LinkAddr, ...]:
        return tuple(
            self.links[link_id]
            for link_id in sorted(self.links)
            if gateway_id in (self.links[link_id].source_gw, self.links[link_id].target_gw)
        )

    def endpoints_at(self, gateway_id: str) -> tuple[EndpointAddr, ...]:
        return tuple(
            self.endpoints[agent_id]
            for agent_id in sorted(self.endpoints)
            if self.endpoints[agent_id].gateway_id == gateway_id
        )


def build_plan(world: World) -> AddressPlan:
    """Deterministic addressing: link k -> 10.0.k.0/30, endpoint k -> 10.1.k.0/24."""
    links = {}
    for index, link in enumerate(
        sorted(world.graph.links, key=lambda item: item.link_id), start=1
    ):
        links[link.link_id] = LinkAddr(
            link_id=link.link_id,
            source_gw=link.source_gateway,
            target_gw=link.target_gateway,
            ifname=link.link_id.lower(),
            source_ip=f"10.0.{index}.1",
            target_ip=f"10.0.{index}.2",
        )
    endpoints = {}
    for index, agent_id in enumerate(sorted(world.endpoints), start=1):
        endpoint = world.endpoints[agent_id]
        endpoints[agent_id] = EndpointAddr(
            agent_id=agent_id,
            gateway_id=endpoint.gateway_id,
            ifname=f"acc-{agent_id.lower()}",
            endpoint_ip=f"10.1.{index}.2",
            gateway_ip=f"10.1.{index}.1",
        )
    return AddressPlan(links=links, endpoints=endpoints)


def dep_index(task: TaskSpecification, dep_id: str) -> int:
    return sorted(dep.dep_id for dep in task.dag.dependencies).index(dep_id)


def dep_table(task: TaskSpecification, dep_id: str) -> int:
    return 100 + dep_index(task, dep_id)


def dep_priority(task: TaskSpecification, dep_id: str) -> int:
    return 1000 + dep_index(task, dep_id)


def flow_port(task: TaskSpecification, dep_id: str) -> int:
    return FLOW_PORT_BASE + dep_index(task, dep_id)


def rule_params(
    entry: ForwardingEntry, task: TaskSpecification, plan: AddressPlan
) -> dict:
    """Concrete, JSON-serializable parameters of one ``FT^g_m`` entry."""
    src = plan.endpoints[entry.src_agent]
    dst = plan.endpoints[entry.dst_agent]
    params = {
        "gateway": entry.gateway_id,
        "dep_id": entry.dep_id,
        "rule_id": entry.rule_id,
        "src_ip": src.endpoint_ip,
        "dst_ip": dst.endpoint_ip,
        "table": dep_table(task, entry.dep_id),
        "priority": dep_priority(task, entry.dep_id),
        "mode": entry.action_mode,
        "next_hop_gateway": entry.next_hop_gateway,
        "via": None,
        "link_id": None,
    }
    if entry.action_mode == "local_delivery":
        params["dev"] = dst.ifname
        return params
    link = plan.link_between(entry.gateway_id, entry.next_hop_gateway or "")
    if link is None:
        raise ValueError(
            f"no link {entry.gateway_id}->{entry.next_hop_gateway} for {entry.rule_id}"
        )
    params.update(dev=link.ifname, via=link.target_ip, link_id=link.link_id)
    return params


def rule_commands(params: dict, op: str) -> list[Cmd]:
    """``op="add"`` installs (idempotently), ``op="del"`` withdraws."""
    table = str(params["table"])
    priority = str(params["priority"])
    dst = params["dst_ip"]
    selector = ("from", params["src_ip"], "to", dst, "priority", priority, "table", table)
    if op == "add":
        route = ("ip", "route", "replace", f"{dst}/32")
        if params.get("via"):
            route += ("via", params["via"])
        route += ("dev", params["dev"], "table", table)
        return [
            Cmd(route),
            Cmd(("ip", "rule", "del", *selector), check=False),
            Cmd(("ip", "rule", "add", *selector)),
        ]
    if op == "del":
        return [
            Cmd(("ip", "rule", "del", *selector), check=False),
            Cmd(("ip", "route", "del", f"{dst}/32", "table", table), check=False),
        ]
    raise ValueError(f"unknown op {op!r}")
```

- [ ] **Step 6: 实现 `tests/tcanet/prototype/__init__.py`**

```python

```

- [ ] **Step 7: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_addressing.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 8: Commit**

```bash
git add src/tcanet/prototype/__init__.py src/tcanet/prototype/config.py src/tcanet/prototype/addressing.py tests/tcanet/prototype/__init__.py tests/tcanet/prototype/test_addressing.py
git commit -m "feat(prototype): runtime config and FT-to-policy-routing address plan"
```

---

### Task 4: netns 拓扑命令

**Files:**
- Create: `src/tcanet/prototype/topology.py`
- Test: `tests/tcanet/prototype/test_topology.py`

**Interfaces:**
- Consumes: Task 3 `Cmd`, `in_ns`, `gateway_ns`, `endpoint_ns`, `AddressPlan`
- Produces: `netem_args(delay_ms, rate_mbps, loss_pct=0)`, `link_qdisc(world, plan, link_id, *, loss_pct=0, op="replace") -> Cmd`, `access_qdiscs(plan, agent_id, rate_mbps, *, op="replace") -> list[Cmd]`, `namespaces(world, plan)`, `build_commands(world, plan, access_mbps) -> list[Cmd]`, `teardown_commands(world, plan)`, `gateway_state_commands(plan, gateway_id, up) -> list[Cmd]`, `link_state_command(plan, link_id, up) -> Cmd`, `run_commands(cmds, *, dry_run=False, runner=subprocess.run) -> list[str]`, `preflight(which=shutil.which) -> list[str]`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_topology.py`**

```python
from __future__ import annotations

import subprocess
import unittest

from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.topology import (
    access_qdiscs,
    build_commands,
    gateway_state_commands,
    link_qdisc,
    link_state_command,
    preflight,
    run_commands,
    teardown_commands,
)
from src.tcanet.scenario_fig1 import load


class TopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _task = load("paper_fig1")
        self.plan = build_plan(self.world)

    def test_build_creates_all_namespaces_and_shapes_links(self) -> None:
        texts = [cmd.text() for cmd in build_commands(self.world, self.plan, 60.0)]
        for ns in ("tc-g1", "tc-g4", "tc-a1", "tc-a4"):
            self.assertIn(f"ip netns add {ns}", texts)
        self.assertIn(
            "ip netns exec tc-g1 tc qdisc add dev l1 root netem delay 8ms rate 40mbit limit 10000",
            texts,
        )
        self.assertIn("ip netns exec tc-a3 ip route add default via 10.1.3.1", texts)
        self.assertIn("ip netns exec tc-g2 sysctl -qw net.ipv4.conf.all.rp_filter=0", texts)
        self.assertFalse(any("10.1." in t and " route add 10.1" in t for t in texts))

    def test_veth_names_fit_kernel_limit(self) -> None:
        for cmd in build_commands(self.world, self.plan, 60.0):
            if cmd.argv[:3] == ("ip", "link", "add"):
                self.assertLessEqual(len(cmd.argv[3]), 15)
                self.assertLessEqual(len(cmd.argv[-1]), 15)

    def test_degrade_and_access(self) -> None:
        self.assertIn("loss 10%", link_qdisc(self.world, self.plan, "L3", loss_pct=10).text())
        up, down = access_qdiscs(self.plan, "a3", 45.0)
        self.assertIn("tc-g4 tc qdisc replace dev acc-a3", up.text())
        self.assertIn("tc-a3 tc qdisc replace dev eth0", down.text())

    def test_failure_commands(self) -> None:
        texts = [c.text() for c in gateway_state_commands(self.plan, "G2", up=False)]
        self.assertEqual(
            texts,
            ["ip netns exec tc-g2 ip link set l1 down",
             "ip netns exec tc-g2 ip link set l2 down"],
        )
        self.assertEqual(
            link_state_command(self.plan, "L6", up=False).text(),
            "ip netns exec tc-g1 ip link set l6 down",
        )
        self.assertTrue(all(not c.check for c in teardown_commands(self.world, self.plan)))

    def test_run_commands_raises_on_checked_failure(self) -> None:
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        cmds = teardown_commands(self.world, self.plan)
        self.assertEqual(len(run_commands(cmds, runner=runner)), len(cmds))
        with self.assertRaises(RuntimeError):
            run_commands(build_commands(self.world, self.plan, 60.0)[:1], runner=runner)
        self.assertEqual(
            run_commands(cmds, dry_run=True, runner=None)[0], "ip netns del tc-g1"
        )

    def test_preflight_reports_missing(self) -> None:
        self.assertEqual(preflight(lambda name: None), ["ip", "tc", "sysctl"])
        self.assertEqual(preflight(lambda name: "/usr/sbin/" + name), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_topology.py`
Expected: FAIL（`ModuleNotFoundError: ... topology`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/topology.py`**

```python
"""Network-namespace topology for the prototype data plane.

Builds one namespace per gateway (``tc-g1``...) and per task endpoint
(``tc-a1``...).  Every gateway link becomes a veth pair shaped with
``tc netem`` (delay + rate) on its source side; every endpoint attaches to
its gateway through an access veth shaped in both directions.  No
endpoint routes are installed in the gateways' main tables, so a task flow
is forwarded only by the per-dependency FT rules the controller installs.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from src.tcanet.prototype.addressing import (
    AddressPlan,
    Cmd,
    endpoint_ns,
    gateway_ns,
    in_ns,
)
from src.tcanet.spec import World

LINK_QUEUE_LIMIT = 10000
ACCESS_DELAY_MS = 1.0
REQUIRED_TOOLS = ("ip", "tc", "sysctl")


def netem_args(delay_ms: float, rate_mbps: float, loss_pct: float = 0.0) -> tuple[str, ...]:
    args: tuple[str, ...] = (
        "netem", "delay", f"{delay_ms:g}ms", "rate", f"{rate_mbps:g}mbit",
    )
    if loss_pct > 0.0:
        args += ("loss", f"{loss_pct:g}%")
    return args + ("limit", str(LINK_QUEUE_LIMIT))


def link_qdisc(
    world: World, plan: AddressPlan, link_id: str, *, loss_pct: float = 0.0, op: str = "replace"
) -> Cmd:
    """Shaping of a link's forward direction (source-side egress)."""
    addr = plan.links[link_id]
    link = world.graph.link(link_id)
    return in_ns(
        gateway_ns(addr.source_gw),
        Cmd(("tc", "qdisc", op, "dev", addr.ifname, "root",
             *netem_args(link.delay_ms, link.capacity_mbps, loss_pct))),
    )


def access_qdiscs(plan: AddressPlan, agent_id: str, rate_mbps: float, *, op: str = "replace") -> list[Cmd]:
    """Downlink (gateway egress) and uplink (endpoint egress) access shaping."""
    endpoint = plan.endpoints[agent_id]
    args = netem_args(ACCESS_DELAY_MS, rate_mbps)
    return [
        in_ns(gateway_ns(endpoint.gateway_id),
              Cmd(("tc", "qdisc", op, "dev", endpoint.ifname, "root", *args))),
        in_ns(endpoint_ns(agent_id),
              Cmd(("tc", "qdisc", op, "dev", "eth0", "root", *args))),
    ]


def namespaces(world: World, plan: AddressPlan) -> list[str]:
    return [gateway_ns(g) for g in world.graph.gateways] + [
        endpoint_ns(agent_id) for agent_id in sorted(plan.endpoints)
    ]


def build_commands(world: World, plan: AddressPlan, access_mbps: float) -> list[Cmd]:
    cmds: list[Cmd] = []
    for ns in namespaces(world, plan):
        cmds.append(Cmd(("ip", "netns", "add", ns)))
        cmds.append(in_ns(ns, Cmd(("ip", "link", "set", "lo", "up"))))
    for gateway in world.graph.gateways:
        ns = gateway_ns(gateway)
        for key in ("net.ipv4.ip_forward=1",
                    "net.ipv4.conf.all.rp_filter=0",
                    "net.ipv4.conf.default.rp_filter=0"):
            cmds.append(in_ns(ns, Cmd(("sysctl", "-qw", key))))
    for link_id in sorted(plan.links):
        addr = plan.links[link_id]
        src_ns, dst_ns = gateway_ns(addr.source_gw), gateway_ns(addr.target_gw)
        tmp_a, tmp_b = f"t{addr.ifname}a", f"t{addr.ifname}b"
        cmds += [
            Cmd(("ip", "link", "add", tmp_a, "type", "veth", "peer", "name", tmp_b)),
            Cmd(("ip", "link", "set", tmp_a, "netns", src_ns)),
            Cmd(("ip", "link", "set", tmp_b, "netns", dst_ns)),
            in_ns(src_ns, Cmd(("ip", "link", "set", tmp_a, "name", addr.ifname))),
            in_ns(dst_ns, Cmd(("ip", "link", "set", tmp_b, "name", addr.ifname))),
            in_ns(src_ns, Cmd(("ip", "addr", "add", f"{addr.source_ip}/30", "dev", addr.ifname))),
            in_ns(dst_ns, Cmd(("ip", "addr", "add", f"{addr.target_ip}/30", "dev", addr.ifname))),
            in_ns(src_ns, Cmd(("ip", "link", "set", addr.ifname, "up"))),
            in_ns(dst_ns, Cmd(("ip", "link", "set", addr.ifname, "up"))),
            link_qdisc(world, plan, link_id, op="add"),
        ]
    for agent_id in sorted(plan.endpoints):
        endpoint = plan.endpoints[agent_id]
        gw_ns, ep_ns = gateway_ns(endpoint.gateway_id), endpoint_ns(agent_id)
        tmp_g, tmp_e = f"t{endpoint.ifname}g", f"t{endpoint.ifname}e"
        cmds += [
            Cmd(("ip", "link", "add", tmp_g, "type", "veth", "peer", "name", tmp_e)),
            Cmd(("ip", "link", "set", tmp_g, "netns", gw_ns)),
            Cmd(("ip", "link", "set", tmp_e, "netns", ep_ns)),
            in_ns(gw_ns, Cmd(("ip", "link", "set", tmp_g, "name", endpoint.ifname))),
            in_ns(ep_ns, Cmd(("ip", "link", "set", tmp_e, "name", "eth0"))),
            in_ns(gw_ns, Cmd(("ip", "addr", "add", f"{endpoint.gateway_ip}/24", "dev", endpoint.ifname))),
            in_ns(ep_ns, Cmd(("ip", "addr", "add", f"{endpoint.endpoint_ip}/24", "dev", "eth0"))),
            in_ns(gw_ns, Cmd(("ip", "link", "set", endpoint.ifname, "up"))),
            in_ns(ep_ns, Cmd(("ip", "link", "set", "eth0", "up"))),
            in_ns(ep_ns, Cmd(("ip", "route", "add", "default", "via", endpoint.gateway_ip))),
            *access_qdiscs(plan, agent_id, access_mbps, op="add"),
        ]
    return cmds


def teardown_commands(world: World, plan: AddressPlan) -> list[Cmd]:
    return [Cmd(("ip", "netns", "del", ns), check=False) for ns in namespaces(world, plan)]


def gateway_state_commands(plan: AddressPlan, gateway_id: str, up: bool) -> list[Cmd]:
    """Take every interface of a gateway down (gateway failure) or up."""
    state = "up" if up else "down"
    ifnames = [link.ifname for link in plan.links_at(gateway_id)] + [
        endpoint.ifname for endpoint in plan.endpoints_at(gateway_id)
    ]
    return [
        in_ns(gateway_ns(gateway_id), Cmd(("ip", "link", "set", ifname, state)))
        for ifname in ifnames
    ]


def link_state_command(plan: AddressPlan, link_id: str, up: bool) -> Cmd:
    addr = plan.links[link_id]
    return in_ns(
        gateway_ns(addr.source_gw),
        Cmd(("ip", "link", "set", addr.ifname, "up" if up else "down")),
    )


Runner = Callable[..., subprocess.CompletedProcess]


def run_commands(cmds: list[Cmd], *, dry_run: bool = False, runner: Runner = subprocess.run) -> list[str]:
    """Execute ``cmds`` in order; raise on the first failing checked command."""
    lines: list[str] = []
    for cmd in cmds:
        lines.append(cmd.text())
        if dry_run:
            continue
        proc = runner(list(cmd.argv), capture_output=True, text=True)
        if cmd.check and proc.returncode != 0:
            raise RuntimeError(f"{cmd.text()} failed: {proc.stderr.strip()}")
    return lines


def preflight(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Names of required tools missing from PATH."""
    return [tool for tool in REQUIRED_TOOLS if which(tool) is None]
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_topology.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 5: Commit**

```bash
git add src/tcanet/prototype/topology.py tests/tcanet/prototype/test_topology.py
git commit -m "feat(prototype): netns topology build/teardown commands"
```

---

### Task 5: Unix-socket 消息总线

**Files:**
- Create: `src/tcanet/prototype/bus.py`
- Test: `tests/tcanet/prototype/test_bus.py`

**Interfaces:**
- Produces: `message(topic, src, payload, ts=None) -> dict`；`Broker(path, record_path=None)` + `async start()/stop()`；`BusClient.connect(path, name, topics=(), *, retries=100, delay=0.05)`，`async subscribe(*prefixes)`，`async publish(topic, payload) -> dict`，`async recv(timeout=None) -> dict | None`（超时抛 `asyncio.TimeoutError`），`async close()`；`python -m src.tcanet.prototype.bus` 在 `config.bus_path()` 上运行 broker 并记录到 `config.events_path()`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_bus.py`**

```python
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.tcanet.prototype.bus import Broker, BusClient


class BusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.sock = self.dir / "bus.sock"
        self.record = self.dir / "events.jsonl"

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_prefix_routing_and_record(self) -> None:
        async def scenario():
            broker = Broker(self.sock, self.record)
            await broker.start()
            a = await BusClient.connect(self.sock, "a", ("report.",))
            b = await BusClient.connect(self.sock, "b", ("ctl.",))
            c = await BusClient.connect(self.sock, "c")
            await asyncio.sleep(0.05)
            await c.publish("report.flow", {"dep_id": "e1"})
            await c.publish("ctl.demand", {"dep_id": "e1", "mbps": 25})
            got_a = await a.recv(timeout=1.0)
            got_b = await b.recv(timeout=1.0)
            with self.assertRaises(asyncio.TimeoutError):
                await a.recv(timeout=0.1)
            for client in (a, b, c):
                await client.close()
            await broker.stop()
            return got_a, got_b

        got_a, got_b = asyncio.run(scenario())
        self.assertEqual(got_a["topic"], "report.flow")
        self.assertEqual(got_a["src"], "c")
        self.assertEqual(got_b["payload"]["mbps"], 25)
        lines = [json.loads(line) for line in self.record.read_text().splitlines()]
        self.assertEqual([m["topic"] for m in lines], ["report.flow", "ctl.demand"])
        self.assertFalse(self.sock.exists())

    def test_sender_does_not_receive_own_message(self) -> None:
        async def scenario():
            broker = Broker(self.sock)
            await broker.start()
            a = await BusClient.connect(self.sock, "a", ("x",))
            await asyncio.sleep(0.05)
            await a.publish("x.y", {})
            try:
                await a.recv(timeout=0.15)
                received = True
            except asyncio.TimeoutError:
                received = False
            await a.close()
            await broker.stop()
            return received

        self.assertFalse(asyncio.run(scenario()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_bus.py`
Expected: FAIL（`ModuleNotFoundError: ... bus`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/bus.py`**

```python
"""Publish/subscribe bus over a Unix domain socket (JSON lines).

A Unix socket lives in the filesystem, so processes in every network
namespace reach the same broker without any extra networking.  Frames:

* ``{"op": "sub", "topics": [prefix, ...]}`` — subscribe by topic prefix;
* ``{"op": "pub", "msg": {"topic", "src", "ts", "payload"}}`` — publish.

The broker forwards each message to every other client with a matching
prefix and appends it to the run's ``events.jsonl`` record.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

_LIMIT = 1 << 20


def message(topic: str, src: str, payload: dict, ts: float | None = None) -> dict:
    return {
        "topic": topic,
        "src": src,
        "ts": time.time() if ts is None else ts,
        "payload": payload,
    }


def _encode(frame: dict) -> bytes:
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode()


def _matches(topic: str, prefixes: set[str]) -> bool:
    return any(topic.startswith(prefix) for prefix in prefixes)


class Broker:
    def __init__(self, path: Path, record_path: Path | None = None) -> None:
        self.path = Path(path)
        self.record_path = record_path
        self._clients: dict[asyncio.StreamWriter, set[str]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._record = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._serve, path=str(self.path), limit=_LIMIT
        )
        os.chmod(self.path, 0o666)
        if self.record_path is not None:
            self._record = open(self.record_path, "a", encoding="utf-8")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self._record is not None:
            self._record.close()
            self._record = None
        if self.path.exists():
            self.path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients[writer] = set()
        try:
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if frame.get("op") == "sub":
                    self._clients.setdefault(writer, set()).update(frame.get("topics", []))
                elif frame.get("op") == "pub" and isinstance(frame.get("msg"), dict):
                    self._fanout(writer, frame["msg"])
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._clients.pop(writer, None)
            writer.close()

    def _fanout(self, sender: asyncio.StreamWriter, msg: dict) -> None:
        data = _encode(msg)
        if self._record is not None:
            self._record.write(data.decode())
            self._record.flush()
        topic = str(msg.get("topic", ""))
        for writer, prefixes in list(self._clients.items()):
            if writer is sender or not _matches(topic, prefixes):
                continue
            if writer.is_closing():
                self._clients.pop(writer, None)
                continue
            writer.write(data)


class BusClient:
    def __init__(self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.name = name
        self._reader = reader
        self._writer = writer
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._pump_task = asyncio.create_task(self._pump())

    @classmethod
    async def connect(
        cls,
        path: Path,
        name: str,
        topics: tuple[str, ...] = (),
        *,
        retries: int = 100,
        delay: float = 0.05,
    ) -> "BusClient":
        for attempt in range(retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(path), limit=_LIMIT)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay)
        client = cls(name, reader, writer)
        if topics:
            await client.subscribe(*topics)
        return client

    async def subscribe(self, *prefixes: str) -> None:
        await self._send({"op": "sub", "topics": list(prefixes)})

    async def publish(self, topic: str, payload: dict) -> dict:
        msg = message(topic, self.name, payload)
        await self._send({"op": "pub", "msg": msg})
        return msg

    async def _send(self, frame: dict) -> None:
        async with self._lock:
            self._writer.write(_encode(frame))
            await self._writer.drain()

    async def _pump(self) -> None:
        try:
            while line := await self._reader.readline():
                try:
                    await self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._queue.put_nowait(None)

    async def recv(self, timeout: float | None = None) -> dict | None:
        """Next message; ``None`` when the broker closed.  Raises
        ``asyncio.TimeoutError`` if ``timeout`` elapses first."""
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)

    async def close(self) -> None:
        self._pump_task.cancel()
        self._writer.close()


async def serve(path: Path, record_path: Path | None) -> None:
    """Run a broker until SIGTERM/SIGINT."""
    broker = Broker(path, record_path)
    await broker.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await broker.stop()


def main() -> None:
    from src.tcanet.prototype import config

    config.run_dir().mkdir(parents=True, exist_ok=True)
    asyncio.run(serve(config.bus_path(), config.events_path()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_bus.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 5: Commit**

```bash
git add src/tcanet/prototype/bus.py tests/tcanet/prototype/test_bus.py
git commit -m "feat(prototype): unix-socket pub/sub bus with event record"
```

---

### Task 6: 可测量的 UDP 任务流量

**Files:**
- Create: `src/tcanet/prototype/flow.py`
- Test: `tests/tcanet/prototype/test_flow.py`

**Interfaces:**
- Produces: `encode(session, seq, send_ns, size, redundant=False) -> bytes`，`decode(data) -> (session, seq, send_ns) | None`，`SinkStats.add(session, seq, send_ns, recv_ns, nbytes)` / `.snapshot(period_s) -> {"rx_mbps", "loss"|None, "owd_ms"|None, "packets"}`，`FlowSender(dst_ip, port, rate_mbps, *, mode="standard")` + `set_rate()`/`set_mode()`/`async run(stop)`，`FlowSink(port, on_sample, *, period_s, bind_ip="0.0.0.0")` + `async run(stop)`，`MODES = ("standard","reliable","lightweight")`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_flow.py`**

```python
from __future__ import annotations

import asyncio
import socket
import unittest

from src.tcanet.prototype.flow import FlowSender, FlowSink, SinkStats, decode, encode


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FlowCodecTests(unittest.TestCase):
    def test_roundtrip_and_padding(self) -> None:
        data = encode(7, 42, 123456789, 1200)
        self.assertEqual(len(data), 1200)
        self.assertEqual(decode(data), (7, 42, 123456789))
        self.assertIsNone(decode(b"garbage-not-a-packet-at-all-xxxx"))

    def test_loss_and_duplicates(self) -> None:
        stats = SinkStats()
        for seq in range(100):
            if seq % 10 == 3:
                continue
            stats.add(1, seq, 0, 5_000_000, 1000)
        stats.add(1, 4, 0, 5_000_000, 1000)  # duplicate copy ignored
        sample = stats.snapshot(1.0)
        self.assertAlmostEqual(sample["loss"], 0.10, places=2)
        self.assertAlmostEqual(sample["owd_ms"], 5.0)
        self.assertAlmostEqual(sample["rx_mbps"], 90 * 1000 * 8 / 1e6)
        empty = stats.snapshot(1.0)
        self.assertEqual(empty["rx_mbps"], 0.0)
        self.assertIsNone(empty["loss"])
        self.assertIsNone(empty["owd_ms"])

    def test_new_session_resets(self) -> None:
        stats = SinkStats()
        for seq in range(1000):
            stats.add(1, seq, 0, 0, 100)
        stats.snapshot(1.0)
        for seq in range(10):
            stats.add(2, seq, 0, 0, 100)
        self.assertEqual(stats.snapshot(1.0)["loss"], 0.0)

    def test_mode_validation(self) -> None:
        sender = FlowSender("127.0.0.1", 9, 10.0, mode="lightweight")
        self.assertAlmostEqual(sender.packets_per_second(), 10e6 / 9600 * 0.95)
        with self.assertRaises(ValueError):
            sender.set_mode("turbo")


class FlowLoopbackTests(unittest.TestCase):
    def test_loopback_goodput(self) -> None:
        port = _free_port()
        samples: list[dict] = []

        async def scenario():
            stop = asyncio.Event()
            sink = FlowSink(port, samples.append, period_s=0.5, bind_ip="127.0.0.1")
            sender = FlowSender("127.0.0.1", port, 8.0)
            tasks = [asyncio.create_task(sink.run(stop)), asyncio.create_task(sender.run(stop))]
            await asyncio.sleep(1.6)
            stop.set()
            await asyncio.gather(*tasks)

        asyncio.run(scenario())
        steady = samples[1:3]
        for sample in steady:
            self.assertGreater(sample["rx_mbps"], 5.0)
            self.assertLess(sample["rx_mbps"], 11.0)
            self.assertLess(sample["loss"], 0.05)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_flow.py`
Expected: FAIL（`ModuleNotFoundError: ... flow`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/flow.py`**

```python
"""Measured task traffic: sequenced, timestamped UDP streams.

Each packet carries a session id, a sequence number and the sender's
``time.time_ns()``.  Sender and receiver share one host clock, so the sink
measures true one-way delay, loss (sequence gaps) and goodput per period —
and keeps reporting zero goodput through an outage, where a TCP-controlled
tool would abort.  Transport modes follow the TransAgent profiles: the
``reliable`` mode duplicates every fifth packet (+20 % overhead, the copy
masks a lost original); ``lightweight`` sends 5 % fewer packets.
"""
from __future__ import annotations

import asyncio
import random
import socket
import statistics
import struct
import time
from typing import Awaitable, Callable

from src.tcanet.prototype.config import PACKET_BYTES

HEADER = struct.Struct("!4sIQQB")
MAGIC = b"TCAF"
MODES = ("standard", "reliable", "lightweight")


def encode(session: int, seq: int, send_ns: int, size: int, redundant: bool = False) -> bytes:
    head = HEADER.pack(MAGIC, session, seq, send_ns, 1 if redundant else 0)
    return head + b"\x00" * max(0, size - len(head))


def decode(data: bytes) -> tuple[int, int, int] | None:
    """``(session, seq, send_ns)`` or ``None`` for a foreign datagram."""
    if len(data) < HEADER.size:
        return None
    magic, session, seq, send_ns, _flags = HEADER.unpack_from(data)
    if magic != MAGIC:
        return None
    return session, seq, send_ns


class SinkStats:
    """Per-period goodput, loss and one-way delay of one stream."""

    def __init__(self) -> None:
        self._session: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._high = -1
        self._window_start = -1
        self._seen: set[int] = set()
        self._bytes = 0
        self._unique = 0
        self._owd_ms: list[float] = []

    def add(self, session: int, seq: int, send_ns: int, recv_ns: int, nbytes: int) -> None:
        if session != self._session:
            self._session = session
            self._reset()
        if seq in self._seen:
            return
        self._seen.add(seq)
        self._unique += 1
        self._bytes += nbytes
        self._owd_ms.append((recv_ns - send_ns) / 1e6)
        if seq > self._high:
            self._high = seq

    def snapshot(self, period_s: float) -> dict:
        expected = self._high - self._window_start
        loss = None if expected <= 0 else max(0.0, min(1.0, 1.0 - self._unique / expected))
        sample = {
            "rx_mbps": self._bytes * 8 / period_s / 1e6,
            "loss": loss,
            "owd_ms": statistics.median(self._owd_ms) if self._owd_ms else None,
            "packets": self._unique,
        }
        self._window_start = self._high
        self._bytes = 0
        self._unique = 0
        self._owd_ms = []
        if len(self._seen) > 200_000:
            self._seen = {seq for seq in self._seen if seq > self._high - 10_000}
        return sample


class FlowSender:
    def __init__(
        self,
        dst_ip: str,
        port: int,
        rate_mbps: float,
        *,
        mode: str = "standard",
        packet_bytes: int = PACKET_BYTES,
        tick_s: float = 0.005,
    ) -> None:
        self.dst_ip = dst_ip
        self.port = port
        self.packet_bytes = packet_bytes
        self.tick_s = tick_s
        self.session = random.getrandbits(32)
        self.seq = 0
        self.rate_mbps = 0.0
        self.mode = "standard"
        self.set_rate(rate_mbps)
        self.set_mode(mode)

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_mbps = max(0.0, float(rate_mbps))

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown transport mode {mode!r}")
        self.mode = mode

    def packets_per_second(self) -> float:
        pps = self.rate_mbps * 1e6 / (8 * self.packet_bytes)
        return pps * 0.95 if self.mode == "lightweight" else pps

    async def run(self, stop: asyncio.Event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        credit = 0.0
        last = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                pps = self.packets_per_second()
                credit = min(credit + (now - last) * pps, pps * 0.05 + 1.0)
                last = now
                while credit >= 1.0:
                    credit -= 1.0
                    self._send_one(sock)
                await asyncio.sleep(self.tick_s)
        finally:
            sock.close()

    def _send_one(self, sock: socket.socket) -> None:
        target = (self.dst_ip, self.port)
        try:
            sock.sendto(encode(self.session, self.seq, time.time_ns(), self.packet_bytes), target)
            if self.mode == "reliable" and self.seq % 5 == 0:
                sock.sendto(
                    encode(self.session, self.seq, time.time_ns(), self.packet_bytes, True),
                    target,
                )
        except OSError:
            pass  # unreachable/full buffer: the sink sees the gap as loss
        self.seq += 1


SampleCallback = Callable[[dict], Awaitable[None] | None]


class FlowSink:
    def __init__(self, port: int, on_sample: SampleCallback, *, period_s: float, bind_ip: str = "0.0.0.0") -> None:
        self.port = port
        self.on_sample = on_sample
        self.period_s = period_s
        self.bind_ip = bind_ip
        self.stats = SinkStats()

    async def run(self, stop: asyncio.Event) -> None:
        stats = self.stats

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                decoded = decode(data)
                if decoded is not None:
                    session, seq, send_ns = decoded
                    stats.add(session, seq, send_ns, time.time_ns(), len(data))

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            _Protocol, local_addr=(self.bind_ip, self.port)
        )
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.period_s)
                except asyncio.TimeoutError:
                    pass
                result = self.on_sample(stats.snapshot(self.period_s))
                if asyncio.iscoroutine(result):
                    await result
        finally:
            transport.close()
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_flow.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 5: Commit**

```bash
git add src/tcanet/prototype/flow.py tests/tcanet/prototype/test_flow.py
git commit -m "feat(prototype): sequenced timestamped UDP flows with one-way delay"
```

---

### Task 7: 仿真数据面（--sim）

**Files:**
- Create: `src/tcanet/prototype/simnet.py`
- Test: `tests/tcanet/prototype/test_simnet.py`

**Interfaces:**
- Consumes: Task 5 `BusClient`；Task 1 `scenario_fig1.load`, `ACCESS_CAPACITY_MBPS`
- Produces: `SimFabricState(world)` + `set_rule(gw, dep, next_hop|None, op)`, `set_gateway(gw, up)`, `set_link(link, up)`, `degrade(link, loss)`, `start_flow(dep, src_agent, dst_agent, rate, mode)`, `stop_flow(dep)`, `route(dep) -> list[link_id] | None`, `evaluate() -> (samples_by_dep, link_states)`；`SimNetworkProcess(bus, state)` 处理 `sim.rule|sim.flow|sim.access|sim.fault`，周期发布 `sim.sample` 与 `sim.linkstate`；`python -m src.tcanet.prototype.simnet --scenario paper_fig1`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_simnet.py`**

```python
from __future__ import annotations

import unittest

from src.tcanet.prototype.simnet import SimFabricState
from src.tcanet.scenario_fig1 import load


def _state_with_e1(path=("G1", "G2", "G4")):
    world, _task = load("paper_fig1")
    state = SimFabricState(world)
    for here, nxt in zip(path, path[1:]):
        state.set_rule(here, "e1", nxt, "add")
    state.set_rule(path[-1], "e1", None, "add")
    state.start_flow("e1", "a1", "a3", 15.0, "standard")
    return state


class SimNetTests(unittest.TestCase):
    def test_routes_follow_installed_rules(self) -> None:
        state = _state_with_e1()
        self.assertEqual(state.route("e1"), ["L1", "L2"])
        sample = state.evaluate()[0]["e1"]
        self.assertAlmostEqual(sample["rx_mbps"], 15.0)
        self.assertAlmostEqual(sample["owd_ms"], 8.0 + 10.0 + 2.0)

    def test_missing_rule_or_dead_gateway_blackholes(self) -> None:
        state = _state_with_e1()
        state.set_rule("G2", "e1", None, "del")
        self.assertIsNone(state.route("e1"))
        state = _state_with_e1()
        state.set_gateway("G2", up=False)
        sample = state.evaluate()[0]["e1"]
        self.assertEqual(sample["rx_mbps"], 0.0)
        self.assertIsNone(sample["loss"])
        self.assertFalse(state.evaluate()[1]["L1"]["up"])

    def test_overload_and_injected_loss(self) -> None:
        state = _state_with_e1()
        state.flows["e1"].rate_mbps = 25.0
        state.flows["e1"].mode = "reliable"  # 30 offered + 12 protected > 40
        sample = state.evaluate()[0]["e1"]
        self.assertGreater(sample["loss"], 0.04)
        state = _state_with_e1(("G1", "G3", "G4"))
        state.degrade("L3", 0.10)
        self.assertAlmostEqual(state.evaluate()[0]["e1"]["loss"], 0.10, places=3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_simnet.py`
Expected: FAIL（`ModuleNotFoundError: ... simnet`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/simnet.py`**

```python
"""Simulated data plane for ``--sim`` runs (no root, no namespaces).

The model forwards each flow hop by hop along the FT rules the NetAgents
installed — a missing rule, a dead gateway or a down link stops it, just
as in the kernel — and derives goodput, loss and one-way delay from link
capacity (incl. protected load), delay and injected loss.  Faults arrive
on ``sim.fault``; results go out as ``sim.sample`` / ``sim.linkstate``.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from dataclasses import dataclass

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load
from src.tcanet.spec import World

ACCESS_DELAY_MS = 1.0
_MAX_HOPS = 8
_OVERHEAD = {"standard": 1.0, "reliable": 1.2, "lightweight": 0.95}


@dataclass
class SimLink:
    link_id: str
    source: str
    target: str
    capacity_mbps: float
    delay_ms: float
    protected_mbps: float
    loss: float = 0.0


@dataclass
class SimFlow:
    dep_id: str
    src_gateway: str
    dst_gateway: str
    rate_mbps: float
    mode: str = "standard"

    @property
    def offered_mbps(self) -> float:
        return self.rate_mbps * _OVERHEAD.get(self.mode, 1.0)


class SimFabricState:
    def __init__(self, world: World) -> None:
        self.links = {
            link.link_id: SimLink(
                link.link_id,
                link.source_gateway,
                link.target_gateway,
                link.capacity_mbps,
                link.delay_ms,
                world.resources[f"link:{link.link_id}"].protected_load_mbps
                if f"link:{link.link_id}" in world.resources
                else 0.0,
            )
            for link in world.graph.links
        }
        self.endpoint_gateway = {
            agent_id: endpoint.gateway_id for agent_id, endpoint in world.endpoints.items()
        }
        self.access = {gateway: ACCESS_CAPACITY_MBPS for gateway in world.graph.gateways}
        self.rules: dict[tuple[str, str], str] = {}  # (gateway, dep) -> next gw | "local"
        self.flows: dict[str, SimFlow] = {}
        self.failed_links: set[str] = set()
        self.failed_gateways: set[str] = set()

    # --- control -----------------------------------------------------
    def set_rule(self, gateway: str, dep_id: str, next_hop: str | None, op: str) -> None:
        if op == "add":
            self.rules[(gateway, dep_id)] = next_hop or "local"
        else:
            self.rules.pop((gateway, dep_id), None)

    def set_gateway(self, gateway: str, up: bool) -> None:
        if up:
            self.failed_gateways.discard(gateway)
        else:
            self.failed_gateways.add(gateway)
            self.rules = {key: hop for key, hop in self.rules.items() if key[0] != gateway}

    def set_link(self, link_id: str, up: bool) -> None:
        if up:
            self.failed_links.discard(link_id)
        else:
            self.failed_links.add(link_id)

    def degrade(self, link_id: str, loss: float) -> None:
        self.links[link_id].loss = loss

    def start_flow(self, dep_id: str, src_agent: str, dst_agent: str, rate: float, mode: str) -> None:
        self.flows[dep_id] = SimFlow(
            dep_id, self.endpoint_gateway[src_agent], self.endpoint_gateway[dst_agent], rate, mode
        )

    def stop_flow(self, dep_id: str) -> None:
        self.flows.pop(dep_id, None)

    # --- model -------------------------------------------------------
    def link_up(self, link_id: str) -> bool:
        link = self.links[link_id]
        return (
            link_id not in self.failed_links
            and link.source not in self.failed_gateways
            and link.target not in self.failed_gateways
        )

    def route(self, dep_id: str) -> list[str] | None:
        """Link ids the flow traverses following installed rules, or None."""
        flow = self.flows[dep_id]
        gateway, used = flow.src_gateway, []
        for _ in range(_MAX_HOPS):
            if gateway in self.failed_gateways:
                return None
            hop = self.rules.get((gateway, dep_id))
            if hop is None:
                return None
            if hop == "local":
                return used if gateway == flow.dst_gateway else None
            link = next(
                (l for l in self.links.values()
                 if l.source == gateway and l.target == hop and self.link_up(l.link_id)),
                None,
            )
            if link is None:
                return None
            used.append(link.link_id)
            gateway = hop
        return None

    def evaluate(self) -> tuple[dict[str, dict], dict[str, dict]]:
        routes = {dep: self.route(dep) for dep in self.flows}
        load = {link_id: link.protected_mbps for link_id, link in self.links.items()}
        access_load: dict[tuple[str, str], float] = {}
        for dep, links in routes.items():
            if links is None:
                continue
            flow = self.flows[dep]
            for link_id in links:
                load[link_id] += flow.offered_mbps
            access_load[("ul", flow.src_gateway)] = access_load.get(("ul", flow.src_gateway), 0.0) + flow.offered_mbps
            access_load[("dl", flow.dst_gateway)] = access_load.get(("dl", flow.dst_gateway), 0.0) + flow.offered_mbps
        samples: dict[str, dict] = {}
        for dep, links in routes.items():
            flow = self.flows[dep]
            if links is None:
                samples[dep] = {"rx_mbps": 0.0, "loss": None, "owd_ms": None}
                continue
            share = 1.0
            delay = 2 * ACCESS_DELAY_MS
            survive = 1.0
            for link_id in links:
                link = self.links[link_id]
                share = min(share, link.capacity_mbps / max(load[link_id], 1e-9))
                delay += link.delay_ms + (5.0 if load[link_id] > 0.9 * link.capacity_mbps else 0.0)
                link_loss = link.loss * (0.5 if flow.mode == "reliable" else 1.0)
                survive *= 1.0 - link_loss
            for key in (("ul", flow.src_gateway), ("dl", flow.dst_gateway)):
                share = min(share, self.access[key[1]] / max(access_load[key], 1e-9))
            loss = 1.0 - survive * min(1.0, share)
            samples[dep] = {
                "rx_mbps": flow.rate_mbps * (1.0 - loss),
                "loss": loss,
                "owd_ms": delay,
            }
        states = {
            link_id: {
                "up": self.link_up(link_id),
                "tx_mbps": min(load[link_id], link.capacity_mbps) if self.link_up(link_id) else 0.0,
            }
            for link_id, link in self.links.items()
        }
        return samples, states


class SimNetworkProcess:
    def __init__(self, bus: BusClient, state: SimFabricState, *, seed: int = 7) -> None:
        self.bus = bus
        self.state = state
        self.rng = random.Random(seed)

    def handle(self, msg: dict) -> None:
        topic, p = msg["topic"], msg["payload"]
        state = self.state
        if topic == "sim.rule":
            state.set_rule(p["gateway"], p["dep_id"], p.get("next_hop"), p["op"])
        elif topic == "sim.flow":
            if p["op"] == "start":
                state.start_flow(p["dep_id"], p["src_agent"], p["dst_agent"], p["rate_mbps"], p.get("mode", "standard"))
            elif p["op"] == "update" and p["dep_id"] in state.flows:
                flow = state.flows[p["dep_id"]]
                flow.rate_mbps = float(p.get("rate_mbps", flow.rate_mbps))
                flow.mode = p.get("mode", flow.mode)
            elif p["op"] == "stop":
                state.stop_flow(p["dep_id"])
        elif topic == "sim.access":
            state.access[p["gateway"]] = float(p["capacity_mbps"])
        elif topic == "sim.fault":
            kind = p["kind"]
            if kind == "gateway":
                state.set_gateway(p["gateway"], bool(p["up"]))
            elif kind == "link":
                state.set_link(p["link_id"], bool(p["up"]))
            elif kind == "degrade":
                state.degrade(p["link_id"], float(p["loss"]))
            elif kind == "reset":
                state.failed_links.clear()
                state.failed_gateways.clear()
                for link in state.links.values():
                    link.loss = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.subscribe("sim.rule", "sim.flow", "sim.access", "sim.fault")
        ticker = asyncio.create_task(self._tick(stop))
        try:
            while not stop.is_set():
                try:
                    msg = await self.bus.recv(timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    break
                self.handle(msg)
        finally:
            ticker.cancel()

    async def _tick(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(config.sample_period_s())
            samples, states = self.state.evaluate()
            for dep, sample in samples.items():
                noisy = dict(sample)
                if noisy["rx_mbps"] > 0:
                    noisy["rx_mbps"] = max(0.0, noisy["rx_mbps"] * self.rng.gauss(1.0, 0.02))
                    noisy["owd_ms"] = noisy["owd_ms"] + abs(self.rng.gauss(0.0, 0.4))
                await self.bus.publish("sim.sample", {"dep_id": dep, **noisy})
            await self.bus.publish("sim.linkstate", {"links": states})


async def _main(scenario: str) -> None:
    world, _task = load(scenario)
    bus = await BusClient.connect(config.bus_path(), "simnet")
    stop = asyncio.Event()
    await SimNetworkProcess(bus, SimFabricState(world)).run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet simulated data plane")
    parser.add_argument("--scenario", default="paper_fig1")
    asyncio.run(_main(parser.parse_args().scenario))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_simnet.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 5: Commit**

```bash
git add src/tcanet/prototype/simnet.py tests/tcanet/prototype/test_simnet.py
git commit -m "feat(prototype): rule-following simulated data plane"
```

---

### Task 8: 数据面访问层与四类 Agent

**Files:**
- Create: `src/tcanet/prototype/fabric.py`
- Create: `src/tcanet/prototype/agents/__init__.py`
- Create: `src/tcanet/prototype/agents/base.py`
- Create: `src/tcanet/prototype/agents/net.py`
- Create: `src/tcanet/prototype/agents/phy.py`
- Create: `src/tcanet/prototype/agents/trans.py`
- Create: `src/tcanet/prototype/agents/app.py`
- Create: `src/tcanet/prototype/agents/__main__.py`
- Create: `tests/tcanet/prototype/harness.py`
- Test: `tests/tcanet/prototype/test_agents.py`

**Interfaces:**
- Consumes: Tasks 3–7
- Produces:
  - `NetnsFabric(plan)` / `SimFabric(bus)`：`async apply_rule(params, op) -> (ok, detail)`, `async read_links({link_id: ifname}) -> {link_id: (up, tx_mbps)}`, `async set_access(gateway, mbps)`, `sender(dep_id, src_agent, dst_agent, dst_ip, port, rate_mbps, mode)`, `sink(dep_id, port, on_sample, period_s)`, 属性 `source = "MEASURED"|"SIM"`；`SimFabric.feed(msg)` 消费 `sim.linkstate`/`sim.sample`
  - `AgentBase(agent_id, gateway, *, bus_path, fabric_factory)`：`async run(stop)`、`topics()`、`background(stop)`、`async handle(payload) -> (ok, detail)`、`async on_message(msg)`、`async log(text, level)`；发布 `hello`/`heartbeat`/`ack`/`agent.log`
  - `NetAgent`（`report.link`，执行 `install_rule`/`remove_rule`）、`PhyAgent`（`report.access`，`BOOST_ACCESS`，`access_capacity(snr_db, boost_mbps)`）、`TransAgent`（`report.session`，`SWITCH_MODE` → `cmd.session`）、`AppAgent`（`report.app`/`report.flow`，订阅 `ctl.demand`/`ctrl.staged`/`ctrl.subnet`/`cmd.session`，`ADJUST_RATE`）、`AppAdapter` 协议 + `SyntheticAppAdapter`
  - `agents.__main__.build_agent(role, agent_id, gateway, *, scenario, sim, bus_path=None)`；CLI `python -m src.tcanet.prototype.agents --role R --agent-id ID --gateway G [--sim]`
  - 测试工具 `tests/tcanet/prototype/harness.py`：`BusHarness`（临时 run dir + broker，`TCANET_TIME_SCALE=0.2`）与 `wait_for(client, predicate, timeout)`

`harness.py` 是测试工具，与实现一同在实现步骤中创建（测试会 import 它）。

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_agents.py`**

```python
from __future__ import annotations

import asyncio
import unittest

from src.tcanet.prototype.addressing import build_plan, rule_params
from src.tcanet.prototype.agents.__main__ import build_agent
from src.tcanet.prototype.agents.phy import access_capacity
from src.tcanet.prototype.simnet import SimFabricState, SimNetworkProcess
from src.tcanet.scenario import run_formation
from src.tcanet.scenario_fig1 import load
from tests.tcanet.prototype.harness import BusHarness, wait_for


def _install_payload(rule_id: str) -> dict:
    world, task = load("paper_fig1")
    subnet = asyncio.run(run_formation(task, world)).subnet
    params = rule_params(subnet.forwarding[rule_id], task, build_plan(world))
    return {"action_id": f"install:{rule_id}#1", "kind": "install_rule",
            "executor": f"network-{params['gateway']}", "target": rule_id,
            "value": params["mode"], "params": params}


class AgentTests(unittest.TestCase):
    def test_access_capacity_model(self) -> None:
        self.assertEqual(access_capacity(20.0, 0.0), 60.0)
        self.assertEqual(access_capacity(15.0, 5.0), 50.0)

    def test_net_agent_installs_rule_and_reports_links(self) -> None:
        payload = _install_payload("e1:G1:0")

        async def scenario():
            async with BusHarness() as h:
                world, _task = load("paper_fig1")
                state = SimFabricState(world)
                stop = asyncio.Event()
                simnet = SimNetworkProcess(await h.client("simnet"), state)
                agent = build_agent("network", "network-G1", "G1", scenario="paper_fig1",
                                    sim=True, bus_path=h.sock)
                probe = await h.client("probe", "ack", "report.link")
                tasks = [asyncio.create_task(simnet.run(stop)), asyncio.create_task(agent.run(stop))]
                await wait_for(probe, lambda m: m["topic"] == "report.link" and m["payload"]["link_id"] == "L1")
                ctl = await h.client("ctl")
                await ctl.publish("cmd.action", payload)
                ack = await wait_for(probe, lambda m: m["topic"] == "ack")
                await asyncio.sleep(0.1)
                stop.set()
                await asyncio.gather(*tasks)
                return ack, state.rules

        ack, rules = asyncio.run(scenario())
        self.assertTrue(ack["payload"]["ok"], ack)
        self.assertIn("[SIM] ip route replace 10.1.3.2/32 via 10.0.1.2 dev l1 table 100", ack["payload"]["detail"])
        self.assertEqual(rules[("G1", "e1")], "G2")

    def test_app_agent_demand_update_and_activation(self) -> None:
        async def scenario():
            async with BusHarness() as h:
                stop = asyncio.Event()
                agent = build_agent("application", "app-a1", "G1", scenario="paper_fig1",
                                    sim=True, bus_path=h.sock)
                probe = await h.client("probe", "report.app", "sim.flow")
                task = asyncio.create_task(agent.run(stop))
                await wait_for(probe, lambda m: m["topic"] == "report.app")
                ctl = await h.client("ctl")
                await ctl.publish("ctl.demand", {"dep_id": "e1", "mbps": 25.0})
                demand = await wait_for(
                    probe, lambda m: m["topic"] == "report.app" and m["payload"]["demand_mbps"] == 25.0)
                await ctl.publish("ctrl.staged", {"version": 1})
                start = await wait_for(probe, lambda m: m["topic"] == "sim.flow" and m["payload"]["op"] == "start")
                await ctl.publish("cmd.action", {"action_id": "exec:x#1", "kind": "layer_operation",
                                                 "executor": "app-a1", "target": "e1",
                                                 "value": "ADJUST_RATE", "params": {"rate_mbps": 17.5}})
                update = await wait_for(probe, lambda m: m["topic"] == "sim.flow" and m["payload"]["op"] == "update")
                stop.set()
                await task
                return demand, start, update

        demand, start, update = asyncio.run(scenario())
        self.assertEqual(demand["payload"]["rate_mbps"], 25.0)
        self.assertEqual(start["payload"]["rate_mbps"], 25.0)
        self.assertEqual(update["payload"]["rate_mbps"], 17.5)

    def test_phy_boost_and_unsupported_action(self) -> None:
        async def scenario():
            async with BusHarness() as h:
                stop = asyncio.Event()
                agent = build_agent("physical", "physical-G4", "G4", scenario="paper_fig1",
                                    sim=True, bus_path=h.sock)
                probe = await h.client("probe", "ack", "report.access")
                task = asyncio.create_task(agent.run(stop))
                await wait_for(probe, lambda m: m["topic"] == "report.access")
                ctl = await h.client("ctl")
                await ctl.publish("cmd.action", {"action_id": "a#1", "kind": "layer_operation",
                                                 "executor": "physical-G4", "target": "G4",
                                                 "value": "BOOST_ACCESS", "params": {"boost_mbps": 10.0}})
                boost = await wait_for(probe, lambda m: m["topic"] == "ack")
                await ctl.publish("cmd.action", {"action_id": "b#1", "kind": "install_rule",
                                                 "executor": "physical-G4", "target": "x",
                                                 "value": "", "params": {}})
                refused = await wait_for(probe, lambda m: m["topic"] == "ack")
                stop.set()
                await task
                return boost, refused, agent.boost_mbps

        boost, refused, total = asyncio.run(scenario())
        self.assertTrue(boost["payload"]["ok"])
        self.assertEqual(total, 10.0)
        self.assertFalse(refused["payload"]["ok"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_agents.py`
Expected: FAIL（`ModuleNotFoundError: ... agents`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/fabric.py`**

```python
"""How agents touch the data plane: kernel (netns) or simulator (sim).

Agents call the same methods in both modes:

* ``apply_rule(params, op)`` — NetAgent installs/withdraws one FT entry;
* ``read_links(ifnames)`` — NetAgent reads ``{link_id: (up, tx_mbps)}``;
* ``set_access(gateway, mbps)`` — PhyAgent shapes the gateway's access links;
* ``sender(...)`` / ``sink(...)`` — AppAgent task traffic.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from src.tcanet.prototype.addressing import AddressPlan, rule_commands
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.flow import FlowSender, FlowSink
from src.tcanet.prototype.topology import access_qdiscs, run_commands


class NetnsFabric:
    """Kernel-backed fabric; the agent process already runs in its netns."""

    source = "MEASURED"

    def __init__(self, plan: AddressPlan, *, runner=subprocess.run, sysfs: Path = Path("/sys/class/net")) -> None:
        self.plan = plan
        self.runner = runner
        self.sysfs = sysfs
        self._last: dict[str, tuple[float, int]] = {}

    async def apply_rule(self, params: dict, op: str) -> tuple[bool, str]:
        cmds = rule_commands(params, op)
        try:
            lines = await asyncio.to_thread(run_commands, cmds, runner=self.runner)
        except RuntimeError as exc:
            return False, str(exc)
        return True, "; ".join(lines)

    async def read_links(self, ifnames: dict[str, str]) -> dict[str, tuple[bool, float]]:
        now = time.monotonic()
        result: dict[str, tuple[bool, float]] = {}
        for link_id, ifname in ifnames.items():
            base = self.sysfs / ifname
            try:
                up = (base / "operstate").read_text().strip() == "up"
                tx_bytes = int((base / "statistics" / "tx_bytes").read_text())
            except (FileNotFoundError, ValueError, OSError):
                result[link_id] = (False, 0.0)
                continue
            last = self._last.get(link_id)
            self._last[link_id] = (now, tx_bytes)
            rate = 0.0
            if last is not None and now > last[0]:
                rate = max(0, tx_bytes - last[1]) * 8 / (now - last[0]) / 1e6
            result[link_id] = (up, rate)
        return result

    async def set_access(self, gateway: str, mbps: float) -> None:
        cmds = [
            cmd
            for endpoint in self.plan.endpoints_at(gateway)
            for cmd in access_qdiscs(self.plan, endpoint.agent_id, mbps)
        ]
        await asyncio.to_thread(run_commands, cmds, runner=self.runner)

    def sender(self, dep_id: str, src_agent: str, dst_agent: str, dst_ip: str, port: int,
               rate_mbps: float, mode: str) -> FlowSender:
        return FlowSender(dst_ip, port, rate_mbps, mode=mode)

    def sink(self, dep_id: str, port: int, on_sample, period_s: float) -> FlowSink:
        return FlowSink(port, on_sample, period_s=period_s)


class _SimSender:
    def __init__(self, bus: BusClient, dep_id: str, src_agent: str, dst_agent: str,
                 rate_mbps: float, mode: str) -> None:
        self.bus, self.dep_id = bus, dep_id
        self.src_agent, self.dst_agent = src_agent, dst_agent
        self.rate_mbps, self.mode = rate_mbps, mode
        self._dirty = False

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_mbps = float(rate_mbps)
        self._dirty = True

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._dirty = True

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.publish("sim.flow", {
            "op": "start", "dep_id": self.dep_id, "src_agent": self.src_agent,
            "dst_agent": self.dst_agent, "rate_mbps": self.rate_mbps, "mode": self.mode,
        })
        try:
            while not stop.is_set():
                if self._dirty:
                    self._dirty = False
                    await self.bus.publish("sim.flow", {
                        "op": "update", "dep_id": self.dep_id,
                        "rate_mbps": self.rate_mbps, "mode": self.mode,
                    })
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.bus.publish("sim.flow", {"op": "stop", "dep_id": self.dep_id})


class _SimSink:
    def __init__(self, fabric: "SimFabric", dep_id: str, on_sample) -> None:
        self.fabric, self.dep_id, self.on_sample = fabric, dep_id, on_sample

    async def run(self, stop: asyncio.Event) -> None:
        self.fabric._sinks[self.dep_id] = self.on_sample
        try:
            await stop.wait()
        finally:
            self.fabric._sinks.pop(self.dep_id, None)


class SimFabric:
    """Simulator-backed fabric; talks to ``SimNetworkProcess`` over the bus."""

    source = "SIM"

    def __init__(self, bus: BusClient) -> None:
        self.bus = bus
        self._links: dict[str, dict] = {}
        self._sinks: dict[str, object] = {}

    async def feed(self, msg: dict) -> None:
        if msg["topic"] == "sim.linkstate":
            self._links = msg["payload"]["links"]
        elif msg["topic"] == "sim.sample":
            callback = self._sinks.get(msg["payload"]["dep_id"])
            if callback is not None:
                sample = {k: v for k, v in msg["payload"].items() if k != "dep_id"}
                result = callback(sample)
                if asyncio.iscoroutine(result):
                    await result

    async def apply_rule(self, params: dict, op: str) -> tuple[bool, str]:
        next_hop = params["next_hop_gateway"] if params["mode"] == "forward_to_gateway" else None
        await self.bus.publish("sim.rule", {
            "gateway": params["gateway"], "dep_id": params["dep_id"],
            "next_hop": next_hop, "op": op,
        })
        return True, "; ".join(f"[SIM] {cmd.text()}" for cmd in rule_commands(params, op))

    async def read_links(self, ifnames: dict[str, str]) -> dict[str, tuple[bool, float]]:
        return {
            link_id: (bool(self._links[link_id]["up"]), float(self._links[link_id]["tx_mbps"]))
            for link_id in ifnames
            if link_id in self._links
        }

    async def set_access(self, gateway: str, mbps: float) -> None:
        await self.bus.publish("sim.access", {"gateway": gateway, "capacity_mbps": mbps})

    def sender(self, dep_id: str, src_agent: str, dst_agent: str, dst_ip: str, port: int,
               rate_mbps: float, mode: str) -> _SimSender:
        return _SimSender(self.bus, dep_id, src_agent, dst_agent, rate_mbps, mode)

    def sink(self, dep_id: str, port: int, on_sample, period_s: float) -> _SimSink:
        return _SimSink(self, dep_id, on_sample)
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/agents/__init__.py`**

```python
"""Cross-layer agent processes (AppAgent / TransAgent / NetAgent / PhyAgent)."""
```

- [ ] **Step 5: 实现 `src/tcanet/prototype/agents/base.py`**

```python
"""Common agent process skeleton: hello, heartbeat, command dispatch, log."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from rich.console import Console

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

_COLORS = {"application": "green", "transport": "dark_orange", "network": "dodger_blue1",
           "physical": "magenta"}


class AgentBase:
    role = "agent"

    def __init__(self, agent_id: str, gateway: str, *, bus_path: Path,
                 fabric_factory: Callable[[BusClient], object]) -> None:
        self.agent_id = agent_id
        self.gateway = gateway
        self.bus_path = bus_path
        self._fabric_factory = fabric_factory
        self.bus: BusClient | None = None
        self.fabric = None
        self.console = Console(highlight=False)

    def topics(self) -> tuple[str, ...]:
        return ("cmd.action", "sim.")

    def background(self, stop: asyncio.Event) -> tuple:
        return ()

    async def run(self, stop: asyncio.Event) -> None:
        self.bus = await BusClient.connect(self.bus_path, self.agent_id, self.topics())
        self.fabric = self._fabric_factory(self.bus)
        await self.bus.publish("hello", {"agent_id": self.agent_id, "role": self.role,
                                         "gateway": self.gateway})
        await self.started()
        tasks = [asyncio.create_task(self._heartbeat(stop))]
        tasks += [asyncio.create_task(coro) for coro in self.background(stop)]
        try:
            while not stop.is_set():
                try:
                    msg = await self.bus.recv(timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if msg is None:
                    break
                await self.dispatch(msg)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stopped()
            await self.bus.close()

    async def started(self) -> None:
        await self.log(f"online at {self.gateway} ({type(self.fabric).__name__})")

    async def stopped(self) -> None:
        pass

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.bus.publish("heartbeat", {"agent_id": self.agent_id})
            await asyncio.sleep(config.heartbeat_s())

    async def dispatch(self, msg: dict) -> None:
        topic, payload = msg["topic"], msg["payload"]
        if topic == "cmd.action" and payload.get("executor") == self.agent_id:
            try:
                ok, detail = await self.handle(payload)
            except Exception as exc:  # report executor failure instead of dying
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            await self.bus.publish("ack", {"action_id": payload["action_id"], "ok": ok,
                                           "detail": detail, "executor": self.agent_id})
            return
        if topic.startswith("sim.") and hasattr(self.fabric, "feed"):
            await self.fabric.feed(msg)
            return
        await self.on_message(msg)

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "bind_support":
            role = {"t": "TransAgent", "n": "NetAgent", "p": "PhyAgent"}[payload["value"]]
            verb = "released" if payload["action_id"].startswith("undo:") else "bound"
            await self.log(f"Φ: {verb} as {role} of {payload['target']}")
            return True, verb
        return False, f"{self.agent_id} cannot execute {payload['kind']}"

    async def on_message(self, msg: dict) -> None:
        pass

    async def log(self, text: str, level: str = "info") -> None:
        color = _COLORS.get(self.role, "white")
        style = {"warn": "bold yellow", "fail": "bold red", "ok": "bold green"}.get(level, "")
        self.console.print(f"[{color}]{self.agent_id:>13}[/] [{style}]{text}[/]" if style
                           else f"[{color}]{self.agent_id:>13}[/] {text}")
        if self.bus is not None:
            await self.bus.publish("agent.log", {"agent_id": self.agent_id, "role": self.role,
                                                 "gateway": self.gateway, "text": text,
                                                 "level": level})
```

- [ ] **Step 6: 实现 `src/tcanet/prototype/agents/net.py`**

```python
"""NetAgent: link observation and task forwarding-state execution."""
from __future__ import annotations

import asyncio

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.spec import World


class NetAgent(AgentBase):
    role = "network"

    def __init__(self, agent_id: str, gateway: str, *, world: World, plan: AddressPlan, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.world = world
        self.plan = plan
        self.links = {link.link_id: link for link in plan.links_at(gateway)}

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._probe(stop),)

    async def _probe(self, stop: asyncio.Event) -> None:
        last_up: dict[str, bool] = {}
        while not stop.is_set():
            states = await self.fabric.read_links(
                {link_id: link.ifname for link_id, link in self.links.items()}
            )
            for link_id, (up, tx_mbps) in sorted(states.items()):
                egress = self.links[link_id].source_gw == self.gateway
                capacity = self.world.graph.link(link_id).capacity_mbps
                await self.bus.publish("report.link", {
                    "link_id": link_id, "up": up, "reporter": self.agent_id,
                    "tx_mbps": tx_mbps if egress else None,
                    "utilization": tx_mbps / capacity if egress else None,
                    "source": self.fabric.source,
                })
                if link_id in last_up and last_up[link_id] != up:
                    await self.log(f"{link_id} {'carrier restored' if up else 'carrier LOST'}",
                                   "ok" if up else "fail")
                last_up[link_id] = up
            await asyncio.sleep(config.sample_period_s())

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] in ("install_rule", "remove_rule"):
            op = "add" if payload["kind"] == "install_rule" else "del"
            params = payload["params"]
            ok, detail = await self.fabric.apply_rule(params, op)
            hop = "local delivery" if params["mode"] == "local_delivery" else f"→ {params['next_hop_gateway']}"
            sign = "+" if op == "add" else "−"
            await self.log(f"FT {sign} {params['dep_id']} {hop} (table {params['table']})"
                           + ("" if ok else f"  FAILED: {detail}"), "info" if ok else "fail")
            return ok, detail
        return await super().handle(payload)
```

- [ ] **Step 7: 实现 `src/tcanet/prototype/agents/phy.py`**

```python
"""PhyAgent: simulated access-link radio state that really shapes the data plane."""
from __future__ import annotations

import asyncio
import random

from src.tcanet.prototype import config
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS

SNR_NOMINAL_DB = 20.0


def access_capacity(snr_db: float, boost_mbps: float) -> float:
    """Access capacity from SNR (SIM model): nominal 60 Mbps at 20 dB."""
    return round(ACCESS_CAPACITY_MBPS * min(1.0, snr_db / SNR_NOMINAL_DB) + boost_mbps, 1)


class PhyAgent(AgentBase):
    role = "physical"

    def __init__(self, agent_id: str, gateway: str, *, seed: int = 0, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.rng = random.Random(seed or hash(agent_id) & 0xFFFF)
        self.snr_db = SNR_NOMINAL_DB
        self.boost_mbps = 0.0

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._radio(stop),)

    def step(self) -> float:
        drift = self.rng.gauss(0.0, 0.6) + 0.1 * (SNR_NOMINAL_DB - self.snr_db)
        self.snr_db = max(12.0, min(26.0, self.snr_db + drift))
        return access_capacity(self.snr_db, self.boost_mbps)

    async def _radio(self, stop: asyncio.Event) -> None:
        applied: float | None = None
        while not stop.is_set():
            capacity = self.step()
            if applied is None or abs(capacity - applied) >= 2.0:
                await self.fabric.set_access(self.gateway, capacity)
                applied = capacity
            await self.bus.publish("report.access", {
                "gateway": self.gateway, "snr_db": round(self.snr_db, 2),
                "capacity_mbps": capacity, "source": "SIM", "reporter": self.agent_id,
            })
            await asyncio.sleep(config.radio_period_s())

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "BOOST_ACCESS":
            delta = float(payload["params"]["boost_mbps"])
            self.boost_mbps += delta
            await self.log(f"access boost {delta:+g} Mbps → {access_capacity(self.snr_db, self.boost_mbps)} Mbps")
            return True, f"boost {self.boost_mbps:g}"
        return await super().handle(payload)
```

- [ ] **Step 8: 实现 `src/tcanet/prototype/agents/trans.py`**

```python
"""TransAgent: per-dependency transport-session state and profile actions."""
from __future__ import annotations

import asyncio

from src.tcanet.prototype import config
from src.tcanet.prototype.agents.base import AgentBase

_ALPHA = 0.3


class TransAgent(AgentBase):
    role = "transport"

    def __init__(self, agent_id: str, gateway: str, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.bound: set[str] = set()
        self.modes: dict[str, str] = {}
        self.sessions: dict[str, dict] = {}

    def topics(self) -> tuple[str, ...]:
        return super().topics() + ("report.flow", "ctrl.subnet")

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._summaries(stop),)

    async def on_message(self, msg: dict) -> None:
        payload = msg["payload"]
        if msg["topic"] == "ctrl.subnet":
            self.bound = {
                dep for dep, (t_agent, _n, _p) in payload.get("bindings", {}).items()
                if t_agent == self.agent_id
            }
        elif msg["topic"] == "report.flow" and payload["dep_id"] in self.bound:
            session = self.sessions.setdefault(payload["dep_id"], {"rx": 0.0, "loss": 0.0, "owd": 0.0})
            session["rx"] += _ALPHA * (payload["rx_mbps"] - session["rx"])
            if payload.get("loss") is not None:
                session["loss"] += _ALPHA * (payload["loss"] - session["loss"])
            if payload.get("owd_ms") is not None:
                session["owd"] += _ALPHA * (payload["owd_ms"] - session["owd"])

    async def _summaries(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(4 * config.sample_period_s())
            for dep in sorted(self.bound):
                session = self.sessions.get(dep)
                if session is None:
                    continue
                mode = self.modes.get(dep, "standard")
                await self.bus.publish("report.session", {"dep_id": dep, "mode": mode, **session})
                await self.log(f"session {dep}: {session['rx']:.1f} Mbps, loss {session['loss']*100:.1f}%, "
                               f"owd {session['owd']:.1f} ms [{mode}]")

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "SWITCH_MODE":
            params = payload["params"]
            self.modes[payload["target"]] = params["mode"]
            await self.bus.publish("cmd.session", {"dep_id": payload["target"], "mode": params["mode"],
                                                   "app_agent": params["app_agent"]})
            await self.log(f"transport profile {payload['target']} → {params['mode']}")
            return True, params["mode"]
        return await super().handle(payload)
```

- [ ] **Step 9: 实现 `src/tcanet/prototype/agents/app.py`**

```python
"""AppAgent: task endpoint workload behind a replaceable ``AppAdapter``.

The synthetic adapter streams one measured UDP flow per outgoing task
dependency at the requested demand ``r_{m,e}``.  A real application agent
implements the same ``AppAdapter`` protocol and sends its own traffic from
the endpoint's namespace; nothing else changes.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, flow_port
from src.tcanet.prototype.agents.base import AgentBase
from src.tcanet.spec import Dependency, TaskSpecification


class AppAdapter(Protocol):
    async def start(self, agent: "AppAgent") -> None: ...

    async def activate(self) -> None: ...

    async def deactivate(self) -> None: ...

    async def set_demand(self, dep_id: str, mbps: float) -> None: ...

    async def set_rate(self, dep_id: str, mbps: float) -> None: ...

    async def set_mode(self, dep_id: str, mode: str) -> None: ...

    def demand(self) -> dict[str, dict]: ...

    async def stop(self) -> None: ...


class SyntheticAppAdapter:
    """One measured UDP stream per outgoing dependency."""

    def __init__(self) -> None:
        self.agent: AppAgent | None = None
        self.out: dict[str, dict] = {}
        self._sink_stop = asyncio.Event()
        self._send_stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._senders: dict[str, object] = {}

    async def start(self, agent: "AppAgent") -> None:
        self.agent = agent
        for dep in agent.outgoing:
            self.out[dep.dep_id] = {"demand": dep.demand_mbps, "rate": dep.demand_mbps, "mode": "standard"}
        for dep in agent.incoming:
            sink = agent.fabric.sink(
                dep.dep_id, flow_port(agent.task, dep.dep_id),
                lambda sample, dep_id=dep.dep_id: agent.report_sample(dep_id, sample),
                config.sample_period_s(),
            )
            self._tasks.append(asyncio.create_task(sink.run(self._sink_stop)))

    async def activate(self) -> None:
        if self._send_stop is not None:
            return
        self._send_stop = asyncio.Event()
        agent = self.agent
        for dep in agent.outgoing:
            state = self.out[dep.dep_id]
            sender = agent.fabric.sender(
                dep.dep_id, dep.source, dep.target,
                agent.plan.endpoints[dep.target].endpoint_ip,
                flow_port(agent.task, dep.dep_id), state["rate"], state["mode"],
            )
            self._senders[dep.dep_id] = sender
            self._tasks.append(asyncio.create_task(sender.run(self._send_stop)))

    async def deactivate(self) -> None:
        if self._send_stop is not None:
            self._send_stop.set()
            self._send_stop = None
        self._senders.clear()

    async def set_demand(self, dep_id: str, mbps: float) -> None:
        self.out[dep_id]["demand"] = mbps
        await self.set_rate(dep_id, mbps)

    async def set_rate(self, dep_id: str, mbps: float) -> None:
        self.out[dep_id]["rate"] = mbps
        if dep_id in self._senders:
            self._senders[dep_id].set_rate(mbps)

    async def set_mode(self, dep_id: str, mode: str) -> None:
        self.out[dep_id]["mode"] = mode
        if dep_id in self._senders:
            self._senders[dep_id].set_mode(mode)

    def demand(self) -> dict[str, dict]:
        return {dep_id: dict(state) for dep_id, state in self.out.items()}

    async def stop(self) -> None:
        await self.deactivate()
        self._sink_stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class AppAgent(AgentBase):
    role = "application"

    def __init__(self, agent_id: str, gateway: str, *, endpoint: str, task: TaskSpecification,
                 plan: AddressPlan, adapter: AppAdapter | None = None, **kwargs) -> None:
        super().__init__(agent_id, gateway, **kwargs)
        self.endpoint = endpoint
        self.task = task
        self.plan = plan
        self.adapter = adapter or SyntheticAppAdapter()
        self.outgoing: tuple[Dependency, ...] = tuple(
            dep for dep in task.dag.dependencies if dep.source == endpoint
        )
        self.incoming: tuple[Dependency, ...] = tuple(
            dep for dep in task.dag.dependencies if dep.target == endpoint
        )
        self.active = False

    def topics(self) -> tuple[str, ...]:
        return super().topics() + ("ctl.demand", "ctrl.subnet", "ctrl.staged", "cmd.session")

    async def started(self) -> None:
        await self.adapter.start(self)
        await super().started()

    async def stopped(self) -> None:
        await self.adapter.stop()

    def background(self, stop: asyncio.Event) -> tuple:
        return (self._report_demand(stop),)

    async def _report_demand(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._publish_demand()
            await asyncio.sleep(config.radio_period_s())

    async def _publish_demand(self) -> None:
        for dep_id, state in self.adapter.demand().items():
            await self.bus.publish("report.app", {
                "dep_id": dep_id, "demand_mbps": state["demand"], "rate_mbps": state["rate"],
                "mode": state["mode"], "active": self.active, "reporter": self.agent_id,
            })

    async def report_sample(self, dep_id: str, sample: dict) -> None:
        await self.bus.publish("report.flow", {"dep_id": dep_id, **sample,
                                               "source": self.fabric.source,
                                               "reporter": self.agent_id})

    async def on_message(self, msg: dict) -> None:
        topic, payload = msg["topic"], msg["payload"]
        outgoing = {dep.dep_id for dep in self.outgoing}
        if topic == "ctl.demand" and payload["dep_id"] in outgoing:
            old = self.adapter.demand()[payload["dep_id"]]["demand"]
            await self.adapter.set_demand(payload["dep_id"], float(payload["mbps"]))
            await self.log(f"task update: {payload['dep_id']} demand {old:g} → {float(payload['mbps']):g} Mbps", "warn")
            await self._publish_demand()
        elif topic in ("ctrl.staged", "ctrl.subnet"):
            if payload.get("version", 0) >= 1 and not self.active and self.outgoing:
                self.active = True
                await self.adapter.activate()
                await self.log(f"subnet v{payload['version']} {'staged' if topic == 'ctrl.staged' else 'active'}: streaming "
                               + ", ".join(dep.dep_id for dep in self.outgoing))
            elif topic == "ctrl.subnet" and payload.get("version", 0) == 0 and self.active:
                self.active = False
                await self.adapter.deactivate()
                await self.log("subnet withdrawn: streaming stopped")
        elif topic == "cmd.session" and payload["app_agent"] == self.agent_id:
            await self.adapter.set_mode(payload["dep_id"], payload["mode"])

    async def handle(self, payload: dict) -> tuple[bool, str]:
        if payload["kind"] == "layer_operation" and payload["value"] == "ADJUST_RATE":
            rate = float(payload["params"]["rate_mbps"])
            await self.adapter.set_rate(payload["target"], rate)
            await self.log(f"source rate {payload['target']} → {rate:g} Mbps")
            return True, f"rate {rate:g}"
        return await super().handle(payload)
```

- [ ] **Step 10: 实现 `src/tcanet/prototype/agents/__main__.py`**

```python
"""Agent process entry point.

``python -m src.tcanet.prototype.agents --role network --agent-id network-G1 --gateway G1``
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.fabric import NetnsFabric, SimFabric
from src.tcanet.scenario_fig1 import load


def build_agent(role: str, agent_id: str, gateway: str, *, scenario: str, sim: bool, bus_path=None):
    from src.tcanet.prototype.agents.app import AppAgent
    from src.tcanet.prototype.agents.net import NetAgent
    from src.tcanet.prototype.agents.phy import PhyAgent
    from src.tcanet.prototype.agents.trans import TransAgent

    world, task = load(scenario)
    plan = build_plan(world)
    factory = SimFabric if sim else (lambda bus: NetnsFabric(plan))
    common = {"bus_path": bus_path or config.bus_path(), "fabric_factory": factory}
    if role == "application":
        endpoint = agent_id.removeprefix("app-")
        return AppAgent(agent_id, gateway, endpoint=endpoint, task=task, plan=plan, **common)
    if role == "network":
        return NetAgent(agent_id, gateway, world=world, plan=plan, **common)
    if role == "physical":
        return PhyAgent(agent_id, gateway, **common)
    if role == "transport":
        return TransAgent(agent_id, gateway, **common)
    raise ValueError(f"unknown role {role!r}")


async def _main(args: argparse.Namespace) -> None:
    agent = build_agent(args.role, args.agent_id, args.gateway, scenario=args.scenario, sim=args.sim)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await agent.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet agent process")
    parser.add_argument("--role", required=True, choices=["application", "transport", "network", "physical"])
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--scenario", default="paper_fig1")
    parser.add_argument("--sim", action="store_true")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 11: 实现 `tests/tcanet/prototype/harness.py`**

```python
"""In-process bus harness for prototype tests (fast time scale)."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from src.tcanet.prototype.bus import Broker, BusClient

TIME_SCALE = "0.2"


class BusHarness:
    """Temporary run dir + broker; use as ``async with BusHarness() as h``."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.sock = self.dir / "bus.sock"
        self.broker = Broker(self.sock, self.dir / "events.jsonl")
        self._env: dict[str, str | None] = {}

    async def __aenter__(self) -> "BusHarness":
        for key, value in (("TCANET_RUN_DIR", str(self.dir)), ("TCANET_TIME_SCALE", TIME_SCALE)):
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        await self.broker.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.broker.stop()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    async def client(self, name: str, *topics: str) -> BusClient:
        client = await BusClient.connect(self.sock, name, topics)
        await asyncio.sleep(0.02)
        return client


async def wait_for(client: BusClient, predicate, timeout: float = 3.0) -> dict:
    """First message satisfying ``predicate`` within ``timeout`` seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("expected bus message did not arrive")
        msg = await client.recv(timeout=remaining)
        if msg is not None and predicate(msg):
            return msg
```

- [ ] **Step 12: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_agents.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 13: Commit**

```bash
git add src/tcanet/prototype/fabric.py src/tcanet/prototype/agents/__init__.py src/tcanet/prototype/agents/base.py src/tcanet/prototype/agents/net.py src/tcanet/prototype/agents/phy.py src/tcanet/prototype/agents/trans.py src/tcanet/prototype/agents/app.py src/tcanet/prototype/agents/__main__.py tests/tcanet/prototype/harness.py tests/tcanet/prototype/test_agents.py
git commit -m "feat(prototype): agent processes over netns/sim fabrics"
```

---

### Task 9: 状态聚合与事件检测

**Files:**
- Create: `src/tcanet/prototype/aggregator.py`
- Test: `tests/tcanet/prototype/test_aggregator.py`

**Interfaces:**
- Consumes: `scenario.fail_gateway/fail_support_agent`、`closure` 事件构造函数
- Produces: `Evidence(kind, subject, ts, detail)`；`FlowBook.add(dep, ts, sample)` / `.since(dep, t0) -> list[(ts, sample)]` / `.latest(dep)`；`StateAggregator(world)` + `.apply(msg) -> list[Evidence]`、`.silent_agents(now, timeout_s)`、`.link_up(link_id)`、`.flowbook`、`.demands`；`EventDetector(world, *, debounce_s)` + `.feed(evidences)`、`.poll(now) -> list[(RuntimeEvent, first_evidence_ts)]`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_aggregator.py`**

```python
from __future__ import annotations

import unittest

from src.tcanet.prototype.aggregator import EventDetector, FlowBook, StateAggregator
from src.tcanet.scenario_fig1 import load


def _msg(topic: str, payload: dict, ts: float, src: str = "x") -> dict:
    return {"topic": topic, "src": src, "ts": ts, "payload": payload}


def _link(link_id: str, up: bool, reporter: str, ts: float) -> dict:
    return _msg("report.link", {"link_id": link_id, "up": up, "reporter": reporter}, ts)


class AggregatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _task = load("paper_fig1")
        self.agg = StateAggregator(self.world)
        self.det = EventDetector(self.world, debounce_s=0.8)

    def _feed(self, msg: dict) -> None:
        self.det.feed(self.agg.apply(msg))

    def test_staggered_reports_classify_as_gateway_failure(self) -> None:
        for reporter, link in (("network-G1", "L1"), ("network-G2", "L1"),
                               ("network-G2", "L2"), ("network-G4", "L2")):
            self._feed(_link(link, True, reporter, 0.0))
        self._feed(_link("L1", False, "network-G1", 10.0))
        self.assertEqual(self.det.poll(10.3), [])
        self._feed(_link("L2", False, "network-G4", 10.5))  # one period later
        events = self.det.poll(10.9)
        self.assertEqual([(e.kind, e.gateway_id) for e, _ in events], [("gateway_failure", "G2")])
        self.assertEqual(events[0][1], 10.0)
        self.assertIn("G2", self.world.failed_gateways)

    def test_gateway_restores_when_links_report_up(self) -> None:
        self.test_staggered_reports_classify_as_gateway_failure()
        self._feed(_link("L1", True, "network-G1", 20.0))
        self.assertIn("G2", self.world.failed_gateways)
        self._feed(_link("L2", True, "network-G4", 20.5))
        self.assertNotIn("G2", self.world.failed_gateways)
        self.assertTrue(self.world.graph.link("L1").up)
        self.assertTrue(self.world.support_agents["network-G2"].online)

    def test_single_link_down_is_link_failure(self) -> None:
        self._feed(_link("L6", True, "network-G1", 0.0))
        self._feed(_link("L6", False, "network-G1", 5.0))
        events = self.det.poll(6.0)
        self.assertEqual([(e.kind, e.link_id) for e, _ in events], [("link_failure", "L6")])
        self.assertFalse(self.world.graph.link("L6").up)

    def test_silent_support_agent_is_support_failure(self) -> None:
        self._feed(_msg("heartbeat", {"agent_id": "physical-G4"}, 1.0))
        self.det.feed(self.agg.silent_agents(now=2.0, timeout_s=1.5))
        self.assertEqual(self.det.poll(2.0), [])
        self.det.feed(self.agg.silent_agents(now=3.0, timeout_s=1.5))
        events = self.det.poll(3.5)
        self.assertEqual([(e.kind, e.agent_ids) for e, _ in events],
                         [("support_failure", ("physical-G4",))])
        self.assertFalse(self.world.support_agents["physical-G4"].online)

    def test_agents_of_failed_gateway_are_not_separate_events(self) -> None:
        self.world.failed_gateways.add("G2")
        self._feed(_msg("heartbeat", {"agent_id": "network-G2"}, 1.0))
        self.det.feed(self.agg.silent_agents(now=5.0, timeout_s=1.5))
        self.assertEqual(self.det.poll(9.0), [])

    def test_demand_change_and_access_capacity(self) -> None:
        self._feed(_msg("report.app", {"dep_id": "e1", "demand_mbps": 15.0}, 1.0))
        self._feed(_msg("report.app", {"dep_id": "e1", "demand_mbps": 25.0}, 2.0))
        events = self.det.poll(2.0)
        self.assertEqual(events[0][0].kind, "demand_change")
        self.assertAlmostEqual(events[0][0].payload["factor"], 25 / 15)
        self._feed(_msg("report.access", {"gateway": "G4", "capacity_mbps": 45.0}, 3.0))
        self.assertEqual(self.world.resources["access-dl:G4"].capacity_mbps, 45.0)

    def test_flowbook_window(self) -> None:
        book = FlowBook(horizon_s=10.0)
        for ts in range(20):
            book.add("e1", float(ts), {"rx_mbps": ts})
        self.assertEqual(len(book.since("e1", 15.0)), 5)
        self.assertEqual(book.latest("e1")["rx_mbps"], 19)
        self.assertEqual(len(book.since("e1", 0.0)), 11)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_aggregator.py`
Expected: FAIL（`ModuleNotFoundError: ... aggregator`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/aggregator.py`**

```python
"""Controller-side state from agent reports (``s_m``) and event detection.

``StateAggregator`` is the controller's only view of the network: link
liveness, access capacity, agent heartbeats, flow samples and application
demand all come from agent reports.  ``EventDetector`` turns the evidence
into the runtime events of paper Sec. IV-B, debouncing so that the two
neighbours of a failed gateway (which report one sampling period apart)
yield a single ``gateway_failure`` instead of two ``link_failure`` events.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace

from src.tcanet.closure import (
    RuntimeEvent,
    demand_change,
    gateway_failure,
    link_failure,
    support_failure,
)
from src.tcanet.scenario import fail_gateway, fail_support_agent
from src.tcanet.spec import SharedResource, World


@dataclass(frozen=True)
class Evidence:
    kind: str  # link_down | link_up | agent_silent | demand
    subject: str
    ts: float
    detail: dict = field(default_factory=dict)


class FlowBook:
    """Recent per-dependency flow samples keyed by report timestamp."""

    def __init__(self, horizon_s: float = 120.0) -> None:
        self.horizon_s = horizon_s
        self._samples: dict[str, deque[tuple[float, dict]]] = defaultdict(deque)

    def add(self, dep_id: str, ts: float, sample: dict) -> None:
        samples = self._samples[dep_id]
        samples.append((ts, sample))
        while samples and samples[0][0] < ts - self.horizon_s:
            samples.popleft()

    def since(self, dep_id: str, t0: float) -> list[tuple[float, dict]]:
        return [(ts, sample) for ts, sample in self._samples.get(dep_id, ()) if ts >= t0]

    def latest(self, dep_id: str) -> dict | None:
        samples = self._samples.get(dep_id)
        return samples[-1][1] if samples else None


def _set_link_up(world: World, link_id: str, up: bool) -> None:
    world.graph = replace(
        world.graph,
        links=tuple(
            replace(link, up=up) if link.link_id == link_id else link
            for link in world.graph.links
        ),
    )


class StateAggregator:
    def __init__(self, world: World) -> None:
        self.world = world
        self.flowbook = FlowBook()
        self.last_seen: dict[str, float] = {}
        self.silent: set[str] = set()
        self.link_reports: dict[str, dict[str, bool]] = defaultdict(dict)
        self.demands: dict[str, float] = {}
        self.access: dict[str, dict] = {}

    def apply(self, msg: dict) -> list[Evidence]:
        topic, payload, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if topic in ("hello", "heartbeat"):
            agent_id = payload.get("agent_id", msg["src"])
            self.last_seen[agent_id] = ts
            self.silent.discard(agent_id)
            agent = self.world.support_agents.get(agent_id)
            if agent is not None and not agent.online and agent.gateway_id not in self.world.failed_gateways:
                self.world.support_agents[agent_id] = replace(agent, online=True)
            return []
        if topic == "report.link":
            return self._link(payload, ts)
        if topic == "report.access":
            self._access(payload)
            return []
        if topic == "report.flow":
            self.flowbook.add(payload["dep_id"], ts, payload)
            return []
        if topic == "report.app":
            dep_id, new = payload["dep_id"], float(payload["demand_mbps"])
            old = self.demands.get(dep_id)
            self.demands[dep_id] = new
            if old is not None and abs(new - old) > 1e-6:
                return [Evidence("demand", dep_id, ts, {"old": old, "new": new})]
        return []

    def link_up(self, link_id: str) -> bool:
        reports = self.link_reports.get(link_id)
        return all(reports.values()) if reports else True

    def _link(self, payload: dict, ts: float) -> list[Evidence]:
        link_id = payload["link_id"]
        before = self.link_up(link_id)
        self.link_reports[link_id][payload["reporter"]] = bool(payload["up"])
        after = self.link_up(link_id)
        if before and not after:
            _set_link_up(self.world, link_id, False)
            return [Evidence("link_down", link_id, ts, {"reporter": payload["reporter"]})]
        if not before and after:
            link = self.world.graph.link(link_id)
            for gateway in (link.source_gateway, link.target_gateway):
                if gateway in self.world.failed_gateways and self._gateway_links_up(gateway):
                    self._restore_gateway(gateway)
            if not ({link.source_gateway, link.target_gateway} & self.world.failed_gateways):
                _set_link_up(self.world, link_id, True)
            return [Evidence("link_up", link_id, ts)]
        return []

    def _gateway_links_up(self, gateway: str) -> bool:
        return all(
            self.link_up(link.link_id)
            for link in self.world.graph.links
            if gateway in (link.source_gateway, link.target_gateway)
        )

    def _restore_gateway(self, gateway: str) -> None:
        """A failed gateway whose links all report up again is live again."""
        self.world.failed_gateways.discard(gateway)
        for link in self.world.graph.links:
            if gateway in (link.source_gateway, link.target_gateway):
                _set_link_up(self.world, link.link_id, True)
        for agent_id, agent in self.world.support_agents.items():
            if agent.gateway_id == gateway and agent_id not in self.silent:
                self.world.support_agents[agent_id] = replace(agent, online=True)

    def _access(self, payload: dict) -> None:
        gateway, capacity = payload["gateway"], float(payload["capacity_mbps"])
        self.access[gateway] = payload
        for resource_id in (f"access-ul:{gateway}", f"access-dl:{gateway}", f"access:{gateway}"):
            if resource_id in self.world.resources:
                self.world.resources[resource_id] = SharedResource(resource_id, capacity)

    def silent_agents(self, now: float, timeout_s: float) -> list[Evidence]:
        found = []
        for agent_id, seen in sorted(self.last_seen.items()):
            if agent_id not in self.silent and now - seen > timeout_s:
                self.silent.add(agent_id)
                found.append(Evidence("agent_silent", agent_id, seen + timeout_s))
        return found


class EventDetector:
    def __init__(self, world: World, *, debounce_s: float) -> None:
        self.world = world
        self.debounce_s = debounce_s
        self._pending: list[Evidence] = []
        self._ready: list[tuple[RuntimeEvent, float]] = []

    def feed(self, evidences: list[Evidence]) -> None:
        for evidence in evidences:
            if evidence.kind == "demand":
                factor = evidence.detail["new"] / max(evidence.detail["old"], 1e-9)
                self._ready.append((demand_change(evidence.subject, factor), evidence.ts))
            elif evidence.kind in ("link_down", "agent_silent"):
                self._pending.append(evidence)

    def poll(self, now: float) -> list[tuple[RuntimeEvent, float]]:
        ready, self._ready = self._ready, []
        if self._pending and now - min(e.ts for e in self._pending) >= self.debounce_s:
            ready += self._classify()
            self._pending = []
        return ready

    def _adjacent(self, gateway: str) -> set[str]:
        return {
            link.link_id for link in self.world.graph.links
            if gateway in (link.source_gateway, link.target_gateway)
        }

    def _classify(self) -> list[tuple[RuntimeEvent, float]]:
        events: list[tuple[RuntimeEvent, float]] = []
        down_now = {link.link_id for link in self.world.graph.links if not link.up}
        link_evidence = {e.subject: e for e in self._pending if e.kind == "link_down"}
        first_ts = min(e.ts for e in self._pending)
        covered: set[str] = set()
        for gateway in self.world.graph.gateways:
            adjacent = self._adjacent(gateway)
            if (
                gateway not in self.world.failed_gateways
                and adjacent
                and adjacent <= down_now
                and adjacent & set(link_evidence)
            ):
                fail_gateway(self.world, gateway)
                events.append((gateway_failure(gateway), first_ts))
                covered |= adjacent
        for link_id in sorted(set(link_evidence) - covered):
            events.append((link_failure(link_id), link_evidence[link_id].ts))
        for evidence in self._pending:
            if evidence.kind != "agent_silent":
                continue
            agent = self.world.support_agents.get(evidence.subject)
            if agent is None or agent.gateway_id in self.world.failed_gateways or not agent.online:
                continue
            fail_support_agent(self.world, evidence.subject)
            events.append((support_failure(evidence.subject), evidence.ts))
        return events
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_aggregator.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 5: Commit**

```bash
git add src/tcanet/prototype/aggregator.py tests/tcanet/prototype/test_aggregator.py
git commit -m "feat(prototype): report-driven world state and debounced event detection"
```

---

### Task 10: 总线数据面（Apply/Rollback）与实测 Assess

**Files:**
- Create: `src/tcanet/prototype/bus_dataplane.py`
- Create: `src/tcanet/prototype/assess.py`
- Test: `tests/tcanet/prototype/test_bus_dataplane.py`

**Interfaces:**
- Consumes: Task 2 `Dataplane` 协议；Task 3 `rule_params`；Task 9 `FlowBook`
- Produces: `AckWaiter.expect(action_id) -> Future` / `.feed(ack_payload)`；`resolve_executor(action, staged, task) -> agent_id | None`；`command_payload(action, staged, task, plan, *, undo=False) -> dict | None`；`BusDataplane(bus, acks, plan, task_provider, *, on_action=None, ack_timeout_s=None)` 实现 `apply/rollback`，另有 `async send(payload, world) -> (ok, detail)`，Apply 前发布 `ctrl.staged`，Rollback 后发布 `ctrl.rollback`；`BusMeasurement(flowbook, *, window_s=None, settle_s=None, clock, sleep)` 为异步 `MeasurementProvider`，属性 `window_ms`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_bus_dataplane.py`**

```python
from __future__ import annotations

import asyncio
import unittest

from src.tcanet.closure import gateway_failure
from src.tcanet.executor import stage_decision
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.aggregator import FlowBook
from src.tcanet.prototype.assess import BusMeasurement
from src.tcanet.prototype.bus_dataplane import (
    AckWaiter,
    BusDataplane,
    command_payload,
    resolve_executor,
)
from src.tcanet.candidates import CandidateAction
from src.tcanet.scenario import fail_gateway, run_formation
from src.tcanet.spec import Layer
from src.tcanet.scenario_fig1 import load
from src.tcanet.verify import RecoveryController
from tests.tcanet.prototype.harness import BusHarness


def _formed():
    world, task = load("paper_fig1")
    subnet = asyncio.run(run_formation(task, world)).subnet
    return world, task, subnet


def _g2_reroute(world, task, subnet):
    """The staged decision TCANet selects after G2 fails."""
    fail_gateway(world, "G2")
    result = asyncio.run(RecoveryController().recover(task, subnet, world, gateway_failure("G2")))
    return stage_decision(task, subnet, result.attempts[-1].selection.selected.actions, world)


class FakeAgents:
    """Acks every cmd.action except for agents listed as dead."""

    def __init__(self, client, dead=()):
        self.client, self.dead, self.seen = client, set(dead), []

    async def run(self):
        while True:
            msg = await self.client.recv()
            if msg is None:
                return
            if msg["topic"] == "cmd.action":
                self.seen.append(msg["payload"])
                if msg["payload"]["executor"] not in self.dead:
                    await self.client.publish("ack", {"action_id": msg["payload"]["action_id"],
                                                      "ok": True, "detail": "ok"})


async def _run_dataplane(world, task, staged, dead=(), rollback=False):
    async with BusHarness() as h:
        controller = await h.client("controller", "ack")
        agents = FakeAgents(await h.client("agents", "cmd.action"), dead)
        agents_task = asyncio.create_task(agents.run())
        acks = AckWaiter()

        async def pump():
            while (msg := await controller.recv()) is not None:
                acks.feed(msg["payload"])

        pump_task = asyncio.create_task(pump())
        dataplane = BusDataplane(controller, acks, build_plan(world), lambda: task,
                                 ack_timeout_s=0.3)
        record = await dataplane.apply(staged, world)
        if rollback:
            await dataplane.rollback(staged, world)
        await asyncio.sleep(0.05)
        for task_ in (agents_task, pump_task):
            task_.cancel()
        return record, agents.seen


class CommandTests(unittest.TestCase):
    def test_executor_resolution_and_inverse_rules(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        by_id = {action.action_id: action for action in staged.executable}
        replace_g1 = by_id["install:e1:G1:0"]
        self.assertEqual(resolve_executor(replace_g1, staged, task), "network-G1")
        plan = build_plan(world)
        forward = command_payload(replace_g1, staged, task, plan)
        self.assertEqual(forward["params"]["next_hop_gateway"], "G3")
        undo = command_payload(replace_g1, staged, task, plan, undo=True)
        self.assertEqual(undo["kind"], "install_rule")
        self.assertEqual(undo["params"]["next_hop_gateway"], "G2")
        fresh = command_payload(by_id["install:e1:G3:1"], staged, task, plan, undo=True)
        self.assertEqual(fresh["kind"], "remove_rule")
        removed = command_payload(by_id["remove:e1:G2:1"], staged, task, plan, undo=True)
        self.assertEqual(removed["kind"], "install_rule")


    def test_same_gateway_replacement_is_not_withdrawn(self) -> None:
        world, task, subnet = _formed()
        reroute = CandidateAction(
            action_id="network:REROUTE:e1:G1|G4", layer=Layer.NETWORK, action="REROUTE",
            target="e1", parameters={"gateway_path": ("G1", "G4")},
        )
        staged = stage_decision(task, subnet, (reroute,), world)
        by_id = {action.action_id: action for action in staged.executable}
        plan = build_plan(world)
        self.assertIsNone(command_payload(by_id["remove:e1:G4:2"], staged, task, plan))
        undo = command_payload(by_id["install:e1:G4:1"], staged, task, plan, undo=True)
        self.assertEqual((undo["kind"], undo["params"]["rule_id"]), ("install_rule", "e1:G4:2"))


class BusDataplaneTests(unittest.TestCase):
    def test_make_before_break_and_rollback(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, seen = asyncio.run(_run_dataplane(world, task, staged, rollback=True))
        self.assertTrue(record.ok, record)
        kinds = [p["kind"] for p in seen]
        self.assertEqual(kinds[0], "install_rule")
        self.assertTrue(any(p["action_id"].startswith("undo:") for p in seen))

    def test_remove_rule_at_failed_gateway_needs_no_ack(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, seen = asyncio.run(_run_dataplane(world, task, staged))
        removal = next(o for o in record.outcomes if o.action_id == "remove:e1:G2:1")
        self.assertTrue(removal.ok)
        self.assertIn("withdrawn with failed gateway G2", removal.detail)
        self.assertNotIn("network-G2", {p["executor"] for p in seen})

    def test_dead_executor_times_out_and_fails_batch(self) -> None:
        world, task, subnet = _formed()
        staged = _g2_reroute(world, task, subnet)
        record, _seen = asyncio.run(_run_dataplane(world, task, staged, dead={"network-G3"}))
        self.assertFalse(record.ok)
        self.assertIn("did not acknowledge", record.outcomes[-1].detail)


class AssessTests(unittest.TestCase):
    def _measure(self, book, now=100.0):
        async def no_sleep(_s):
            return None

        measurement = BusMeasurement(book, window_s=2.0, settle_s=0.5,
                                     clock=lambda: now, sleep=no_sleep)
        return asyncio.run(measurement(None, None, ("e1", "e2")))

    def test_summaries(self) -> None:
        book = FlowBook()
        for i, (rate, owd, loss) in enumerate([(15, 20, 0.0), (14, 22, 0.02), (16, 30, 0.01), (15, 21, 0.0)]):
            book.add("e1", 100.6 + 0.4 * i, {"rx_mbps": rate, "owd_ms": owd, "loss": loss})
        book.add("e1", 100.1, {"rx_mbps": 0.0, "owd_ms": None, "loss": None})  # before settle
        (obs,) = self._measure(book)
        self.assertEqual(obs.dep_id, "e1")
        self.assertEqual(obs.throughput_mbps, 15)
        self.assertEqual(obs.delay_ms, 22)
        self.assertAlmostEqual(obs.loss_rate, 0.0075)
        self.assertLessEqual(obs.observed_at_ms, 2000.0)

    def test_no_packets_yields_violating_observation(self) -> None:
        book = FlowBook()
        book.add("e2", 101.0, {"rx_mbps": 0.0, "owd_ms": None, "loss": None})
        (obs,) = self._measure(book)
        self.assertEqual(obs.throughput_mbps, 0.0)
        self.assertEqual(obs.delay_ms, float("inf"))
        self.assertEqual(obs.loss_rate, 1.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_bus_dataplane.py`
Expected: FAIL（`ModuleNotFoundError: ... bus_dataplane`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/bus_dataplane.py`**

```python
"""``Dataplane`` over the agent bus: Apply/Rollback on real executors.

Every executable action of a staged configuration becomes a ``cmd.action``
addressed to the agent that owns it — FT entries to the gateway's NetAgent,
bindings to the supporting agent, layer actions to the App/Trans/Phy agent —
and Apply waits for each ``ack``.  New state is installed before superseded
state is withdrawn (make-before-break).  Rollback replays the inverse of
the actions that were acknowledged, in reverse order.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Awaitable, Callable

from src.tcanet.executor import ActionOutcome, ExecutionRecord, StagedDecision, execute_staged
from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, rule_params
from src.tcanet.prototype.bus import BusClient
from src.tcanet.spec import TaskSpecification, World
from src.tcanet.subnet import ExecutableAction

_ORDER = {"install_rule": 0, "bind_support": 1, "layer_operation": 2, "remove_rule": 3}
_counter = itertools.count(1)


class AckWaiter:
    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}

    def expect(self, action_id: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._futures[action_id] = future
        return future

    def feed(self, payload: dict) -> None:
        future = self._futures.pop(payload.get("action_id", ""), None)
        if future is not None and not future.done():
            future.set_result(payload)


def _dep(task: TaskSpecification, dep_id: str):
    return next((dep for dep in task.dag.dependencies if dep.dep_id == dep_id), None)


def resolve_executor(action: ExecutableAction, staged: StagedDecision, task: TaskSpecification) -> str | None:
    if action.kind in ("install_rule", "remove_rule"):
        return f"network-{action.executor}"
    if action.kind == "bind_support":
        return action.executor
    if action.executor == "application":
        dep = _dep(task, action.target)
        return f"app-{dep.source}" if dep is not None else None
    if action.executor == "transport":
        return staged.subnet.bindings.binding(action.target).t_agent_id
    if action.executor == "physical":
        return f"physical-{action.target}"
    return None


def _entry_at(forwarding: dict, gateway_id: str, dep_id: str):
    """The FT entry of ``dep_id`` at ``gateway_id`` (kernel state is keyed so)."""
    return next(
        (e for e in forwarding.values() if e.gateway_id == gateway_id and e.dep_id == dep_id),
        None,
    )


def _candidate(staged: StagedDecision, action: ExecutableAction):
    action_id = action.action_id.removeprefix("exec:")
    return next((item for item in staged.actions if item.action_id == action_id), None)


def command_payload(
    action: ExecutableAction,
    staged: StagedDecision,
    task: TaskSpecification,
    plan: AddressPlan,
    *,
    undo: bool = False,
) -> dict | None:
    """``cmd.action`` payload for ``action`` (or its inverse).

    FT state is keyed by (gateway, dependency): a removal superseded by an
    install at the same gateway is a no-op (``None``), and undoing an
    install restores whatever entry that gateway held before.
    """
    kind, executor = action.kind, resolve_executor(action, staged, task)
    params: dict = {}
    if kind in ("install_rule", "remove_rule"):
        if kind == "install_rule":
            entry = staged.subnet.forwarding[action.target]
            if undo:
                previous = _entry_at(staged.previous.forwarding, entry.gateway_id, entry.dep_id)
                kind, entry = ("install_rule", previous) if previous else ("remove_rule", entry)
        else:
            entry = staged.previous.forwarding[action.target]
            if _entry_at(staged.subnet.forwarding, entry.gateway_id, entry.dep_id) is not None:
                return None  # replaced in place by the install at the same gateway
            if undo:
                kind = "install_rule"
        params = rule_params(entry, task, plan)
    elif kind == "bind_support":
        params = {"dep_id": action.target, "role": action.value}
    else:
        candidate = _candidate(staged, action)
        params = dict(candidate.parameters) if candidate is not None else {}
        dep = _dep(task, action.target)
        if action.value == "SWITCH_MODE" and dep is not None:
            params["app_agent"] = f"app-{dep.source}"
        if undo:
            if action.value == "ADJUST_RATE":
                params["rate_mbps"] = params.get("base_mbps", params.get("rate_mbps"))
            elif action.value == "SWITCH_MODE":
                params["mode"] = "standard"
            elif action.value == "BOOST_ACCESS":
                params["boost_mbps"] = -float(params.get("boost_mbps", 0.0))
    return {
        "action_id": f"{'undo:' if undo else ''}{action.action_id}#{next(_counter)}",
        "kind": kind,
        "executor": executor,
        "target": action.target,
        "value": action.value,
        "params": params,
    }


Logger = Callable[[dict, bool, str], Awaitable[None]]


class BusDataplane:
    def __init__(
        self,
        bus: BusClient,
        acks: AckWaiter,
        plan: AddressPlan,
        task: Callable[[], TaskSpecification],
        *,
        on_action: Logger | None = None,
        ack_timeout_s: float | None = None,
    ) -> None:
        self.bus = bus
        self.acks = acks
        self.plan = plan
        self.task = task
        self.on_action = on_action
        self.ack_timeout_s = ack_timeout_s
        self._applied: dict[int, list[ExecutableAction]] = {}

    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord:
        checked = await execute_staged(staged, world)
        if not checked.ok:
            return checked
        await self.bus.publish("ctrl.staged", {"version": staged.subnet.version})
        applied: list[ExecutableAction] = []
        outcomes: list[ActionOutcome] = []
        self._applied[id(staged)] = applied
        for action in sorted(staged.executable, key=lambda a: (_ORDER.get(a.kind, 9), a.action_id)):
            payload = command_payload(action, staged, self.task(), self.plan)
            if payload is None:
                ok, detail = True, "replaced in place"
            else:
                ok, detail = await self.send(payload, world)
            outcomes.append(ActionOutcome(action.action_id, ok, detail))
            if not ok:
                return ExecutionRecord(False, tuple(outcomes), checked.serialization_order)
            applied.append(action)
        return ExecutionRecord(True, tuple(outcomes), checked.serialization_order)

    async def rollback(self, staged: StagedDecision, world: World) -> None:
        for action in reversed(self._applied.pop(id(staged), [])):
            payload = command_payload(action, staged, self.task(), self.plan, undo=True)
            if payload is not None:
                await self.send(payload, world)
        await self.bus.publish("ctrl.rollback", {"version": staged.subnet.version,
                                                 "restored": staged.previous.version})

    async def send(self, payload: dict, world: World) -> tuple[bool, str]:
        """Deliver one command and wait for its ack (or decide it locally)."""
        gateway = payload["params"].get("gateway") if payload["kind"].endswith("_rule") else None
        if gateway is not None and not world.gateway_online(gateway):
            if payload["kind"] == "remove_rule":
                ok, detail = True, f"withdrawn with failed gateway {gateway}"
            elif payload["action_id"].startswith("undo:"):
                ok, detail = True, f"not restored: gateway {gateway} offline"
            else:
                ok, detail = False, f"gateway {gateway} offline"
        elif payload["executor"] is None:
            ok, detail = False, "no executor for action"
        else:
            future = self.acks.expect(payload["action_id"])
            await self.bus.publish("cmd.action", payload)
            try:
                ack = await asyncio.wait_for(future, self.ack_timeout_s or config.ack_timeout_s())
                ok, detail = bool(ack["ok"]), str(ack.get("detail", ""))
            except asyncio.TimeoutError:
                ok, detail = False, f"{payload['executor']} did not acknowledge"
        if self.on_action is not None:
            await self.on_action(payload, ok, detail)
        return ok, detail
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/assess.py`**

```python
"""Assessment from real flow samples (paper Alg. 1, line 16).

After Apply, wait one assessment window, then summarise every affected
dependency's samples (median goodput, 90th-percentile one-way delay, mean
loss).  A dependency without packets is reported as zero goodput, infinite
delay and total loss, so the hard-constraint check rejects it.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import Awaitable, Callable

from src.tcanet.executor import StagedDecision
from src.tcanet.prototype import config
from src.tcanet.prototype.aggregator import FlowBook
from src.tcanet.spec import World
from src.tcanet.verify import DepObservation


class BusMeasurement:
    def __init__(
        self,
        flowbook: FlowBook,
        *,
        window_s: float | None = None,
        settle_s: float | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.flowbook = flowbook
        self.window_s = window_s
        self.settle_s = settle_s
        self.clock = clock
        self.sleep = sleep

    @property
    def window_ms(self) -> float:
        return (self.window_s or config.assess_window_s()) * 1000.0

    async def __call__(self, staged: StagedDecision, world: World, dep_ids: tuple[str, ...]) -> tuple[DepObservation, ...]:
        window_s = self.window_s or config.assess_window_s()
        settle_s = self.settle_s if self.settle_s is not None else config.assess_settle_s()
        started = self.clock()
        await self.sleep(window_s)
        observations = []
        for dep_id in sorted(dep_ids):
            samples = self.flowbook.since(dep_id, started + settle_s)
            if not samples:
                continue
            rates = [sample["rx_mbps"] for _ts, sample in samples]
            delays = sorted(s["owd_ms"] for _ts, s in samples if s.get("owd_ms") is not None)
            losses = [s["loss"] for _ts, s in samples if s.get("loss") is not None]
            observations.append(DepObservation(
                dep_id=dep_id,
                observed_at_ms=min(window_s * 1000.0, (samples[-1][0] - started) * 1000.0),
                throughput_mbps=statistics.median(rates),
                delay_ms=delays[int(0.9 * (len(delays) - 1))] if delays else float("inf"),
                loss_rate=statistics.mean(losses) if losses else 1.0,
            ))
        return tuple(observations)
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_bus_dataplane.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/tcanet/prototype/bus_dataplane.py src/tcanet/prototype/assess.py tests/tcanet/prototype/test_bus_dataplane.py
git commit -m "feat(prototype): bus dataplane with make-before-break apply, rollback and measured assess"
```

---

### Task 11: 进程管理、tcanetctl 与特权侧 up/down

**Files:**
- Create: `src/tcanet/prototype/runtime.py`
- Create: `src/tcanet/prototype/background.py`
- Create: `src/tcanet/prototype/ctl.py`
- Create: `src/tcanet/prototype/root.py`
- Test: `tests/tcanet/prototype/test_root_runtime.py`

**Interfaces:**
- Consumes: Tasks 3–8
- Produces: `AgentSpec(agent_id, role, gateway, ns)`、`agent_specs(world, plan)`（16 个）、`agent_argv(spec, *, sim, scenario, python)`、`ProcessTable(dir)` + `start/pid/alive/kill/names/stop_all`；`ctl` 协程 `submit/withdraw/demand/set_gateway/set_link/degrade/kill/revive/reset`（参数 `bus, rt, world, plan, ...`）与 `Runtime` 协议（`mode`、`async kernel(cmds)`、`kill(agent_id)`、`revive(agent_id)`）、`ProcessRuntime`、`status_lines()`、CLI `tcanetctl`（netns 模式下内核操作自动 `sudo -E` 重执行）；`root.process_plan(mode, world, plan, scenario, python)`、`root.up(mode)`、`root.down()`；`background` 保护流量 CLI

`SimUpDownTests` 启动真实子进程（sim 模式，无需 root），约 1–3 秒；结束后用 `pgrep -fl src.tcanet.prototype` 确认无残留进程。

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_root_runtime.py`**

```python
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.root import down, process_plan, up
from src.tcanet.prototype.runtime import ProcessTable, agent_argv, agent_specs
from src.tcanet.scenario_fig1 import load


class ProcessPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world, _task = load("paper_fig1")
        self.plan = build_plan(self.world)

    def test_sixteen_agents_in_their_namespaces(self) -> None:
        specs = agent_specs(self.world, self.plan)
        self.assertEqual(len(specs), 16)
        app = next(spec for spec in specs if spec.agent_id == "app-a3")
        self.assertEqual((app.gateway, app.ns), ("G4", "tc-a3"))
        argv = agent_argv(app, sim=False, scenario="paper_fig1", python="py")
        self.assertEqual(argv[:5], ["ip", "netns", "exec", "tc-a3", "py"])
        self.assertNotIn("ip", agent_argv(app, sim=True, scenario="paper_fig1", python="py")[:1])

    def test_netns_plan_has_protected_traffic_on_l1_only(self) -> None:
        names = [name for name, _argv in process_plan("netns", self.world, self.plan, "paper_fig1", "py")]
        self.assertEqual(names[0], "bus")
        self.assertIn("bg-L1", names)
        self.assertNotIn("bg-L2", names)
        self.assertNotIn("simnet", names)
        sim_names = [name for name, _ in process_plan("sim", self.world, self.plan, "paper_fig1", "py")]
        self.assertIn("simnet", sim_names)
        self.assertEqual(len(sim_names), 18)


class SimUpDownTests(unittest.TestCase):
    """Real subprocesses: ``root up --mode sim`` brings all agents online."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.env = mock.patch.dict(os.environ, {"TCANET_RUN_DIR": str(self.dir),
                                                 "TCANET_TIME_SCALE": "0.5"})
        self.env.start()

    def tearDown(self) -> None:
        down()
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_all_agents_say_hello(self) -> None:
        up("sim")
        self.assertEqual(config.read_mode(), "sim")

        async def collect():
            client = await BusClient.connect(config.bus_path(), "probe", ("heartbeat",))
            seen: set[str] = set()
            deadline = asyncio.get_running_loop().time() + 20
            while len(seen) < 16 and asyncio.get_running_loop().time() < deadline:
                try:
                    msg = await client.recv(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                seen.add(msg["payload"]["agent_id"])
            await client.close()
            return seen

        seen = asyncio.run(collect())
        self.assertEqual(len(seen), 16, sorted(seen))
        self.assertTrue(ProcessTable(config.pids_dir()).alive("simnet"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_root_runtime.py`
Expected: FAIL（`ModuleNotFoundError: ... runtime`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/runtime.py`**

```python
"""Process table and agent launch specs for the prototype."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.tcanet.prototype.addressing import AddressPlan, endpoint_ns, gateway_ns
from src.tcanet.spec import World

SUPPORT_ROLES = ("transport", "network", "physical")


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    gateway: str
    ns: str


def agent_specs(world: World, plan: AddressPlan) -> list[AgentSpec]:
    specs = [
        AgentSpec(f"app-{agent_id}", "application", endpoint.gateway_id, endpoint_ns(agent_id))
        for agent_id, endpoint in sorted(plan.endpoints.items())
    ]
    specs += [
        AgentSpec(f"{role}-{gateway}", role, gateway, gateway_ns(gateway))
        for gateway in world.graph.gateways
        for role in SUPPORT_ROLES
    ]
    return specs


def agent_argv(spec: AgentSpec, *, sim: bool, scenario: str, python: str = sys.executable) -> list[str]:
    argv = [python, "-m", "src.tcanet.prototype.agents", "--role", spec.role,
            "--agent-id", spec.agent_id, "--gateway", spec.gateway, "--scenario", scenario]
    if sim:
        return argv + ["--sim"]
    return ["ip", "netns", "exec", spec.ns, *argv]


class ProcessTable:
    """Named background processes tracked through ``<dir>/<name>.pid``."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _pidfile(self, name: str) -> Path:
        return self.directory / f"{name}.pid"

    def start(self, name: str, argv: list[str], *, log_path: Path, cwd: Path | None = None,
              env: dict[str, str] | None = None) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=cwd,
                                    env=env, start_new_session=True)
        self._pidfile(name).write_text(str(proc.pid))
        return proc.pid

    def pid(self, name: str) -> int | None:
        try:
            return int(self._pidfile(name).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def alive(self, name: str) -> bool:
        pid = self.pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill(self, name: str, sig: int = signal.SIGKILL) -> bool:
        pid = self.pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.pid"))

    def stop_all(self) -> None:
        for name in self.names():
            self.kill(name, signal.SIGTERM)
            self._pidfile(name).unlink(missing_ok=True)
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/background.py`**

```python
"""Protected background traffic (``d^prot_r``) on a gateway link."""
from __future__ import annotations

import argparse
import asyncio
import signal

from src.tcanet.prototype.flow import FlowSender, FlowSink

BACKGROUND_PORT = 46999


async def _run(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    if args.mode == "send":
        await FlowSender(args.dst, args.port, args.mbps).run(stop)
    else:
        await FlowSink(args.port, lambda sample: None, period_s=1.0).run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="protected background traffic")
    parser.add_argument("mode", choices=["send", "sink"])
    parser.add_argument("--dst", default="")
    parser.add_argument("--port", type=int, default=BACKGROUND_PORT)
    parser.add_argument("--mbps", type=float, default=0.0)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 实现 `src/tcanet/prototype/ctl.py`**

```python
"""``tcanetctl`` — orchestrator input and operator fault injection.

Faults act on the kernel (netns mode) or the simulator (sim mode) and on
agent processes; they are never announced to the controller, which learns
of them only from agent reports.  Each operator action is also published
on ``ops.fault`` for the event record (the controller does not subscribe).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, Cmd, build_plan
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.runtime import SUPPORT_ROLES, ProcessTable, agent_argv, agent_specs
from src.tcanet.prototype.topology import (
    gateway_state_commands,
    link_qdisc,
    link_state_command,
    run_commands,
)
from src.tcanet.scenario_fig1 import load
from src.tcanet.spec import World

REPO_ROOT = Path(__file__).resolve().parents[3]


class Runtime(Protocol):
    mode: str

    async def kernel(self, cmds: list[Cmd]) -> None: ...

    def kill(self, agent_id: str) -> bool: ...

    def revive(self, agent_id: str) -> bool: ...


class ProcessRuntime:
    def __init__(self, mode: str, world: World, plan: AddressPlan, scenario: str) -> None:
        self.mode = mode
        self.table = ProcessTable(config.pids_dir())
        self.specs = {spec.agent_id: spec for spec in agent_specs(world, plan)}
        self.scenario = scenario

    async def kernel(self, cmds: list[Cmd]) -> None:
        await asyncio.to_thread(run_commands, cmds)

    def kill(self, agent_id: str) -> bool:
        return self.table.kill(agent_id)

    def revive(self, agent_id: str) -> bool:
        if self.table.alive(agent_id) or agent_id not in self.specs:
            return False
        argv = agent_argv(self.specs[agent_id], sim=self.mode == "sim", scenario=self.scenario)
        self.table.start(agent_id, argv, log_path=config.logs_dir() / f"{agent_id}.log",
                         cwd=REPO_ROOT, env=dict(os.environ))
        return True


async def _record(bus: BusClient, op: str, **fields) -> None:
    await bus.publish("ops.fault", {"op": op, **fields})


async def submit(bus: BusClient, scenario: str) -> None:
    await bus.publish("task.submit", {"scenario": scenario})
    await _record(bus, "submit", scenario=scenario)


async def withdraw(bus: BusClient) -> None:
    await bus.publish("task.withdraw", {})
    await _record(bus, "withdraw")


async def demand(bus: BusClient, dep_id: str, mbps: float) -> None:
    await bus.publish("ctl.demand", {"dep_id": dep_id, "mbps": mbps})
    await _record(bus, "demand", dep_id=dep_id, mbps=mbps)


async def set_gateway(bus: BusClient, rt: Runtime, plan: AddressPlan, gateway: str, up: bool) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "gateway", "gateway": gateway, "up": up})
    else:
        await rt.kernel(gateway_state_commands(plan, gateway, up))
    for role in SUPPORT_ROLES:
        (rt.revive if up else rt.kill)(f"{role}-{gateway}")
    await _record(bus, "gateway", target=gateway, up=up)


async def set_link(bus: BusClient, rt: Runtime, plan: AddressPlan, link_id: str, up: bool) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "link", "link_id": link_id, "up": up})
    else:
        await rt.kernel([link_state_command(plan, link_id, up)])
    await _record(bus, "link", target=link_id, up=up)


async def degrade(bus: BusClient, rt: Runtime, world: World, plan: AddressPlan, link_id: str, loss_pct: float) -> None:
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "degrade", "link_id": link_id, "loss": loss_pct / 100.0})
    else:
        await rt.kernel([link_qdisc(world, plan, link_id, loss_pct=loss_pct)])
    await _record(bus, "degrade", target=link_id, loss_pct=loss_pct)


async def kill(bus: BusClient, rt: Runtime, agent_id: str) -> bool:
    killed = rt.kill(agent_id)
    await _record(bus, "kill", target=agent_id)
    return killed


async def revive(bus: BusClient, rt: Runtime, agent_id: str) -> bool:
    revived = rt.revive(agent_id)
    await _record(bus, "revive", target=agent_id)
    return revived


async def reset(bus: BusClient, rt: Runtime, world: World, plan: AddressPlan) -> None:
    """Heal every injected fault, revive dead agents, withdraw the task."""
    if rt.mode == "sim":
        await bus.publish("sim.fault", {"kind": "reset"})
    else:
        cmds = [cmd for gateway in world.graph.gateways for cmd in gateway_state_commands(plan, gateway, True)]
        cmds += [link_state_command(plan, link_id, True) for link_id in sorted(plan.links)]
        cmds += [link_qdisc(world, plan, link_id) for link_id in sorted(plan.links)]
        await rt.kernel(cmds)
    for agent_id in sorted(getattr(rt, "specs", {})):
        rt.revive(agent_id)
    await asyncio.sleep(3 * config.sample_period_s())  # let stale link reports drain
    await withdraw(bus)
    await _record(bus, "reset")


def status_lines(world: World, plan: AddressPlan) -> list[str]:
    table = ProcessTable(config.pids_dir())
    lines = [f"mode: {config.read_mode()}   run dir: {config.run_dir()}"]
    for spec in agent_specs(world, plan):
        lines.append(f"  {spec.agent_id:<13} {'alive' if table.alive(spec.agent_id) else 'DEAD'}")
    if config.read_mode() == "netns":
        for gateway in world.graph.gateways:
            out = subprocess.run(["ip", "-n", f"tc-{gateway.lower()}", "rule", "show"],
                                 capture_output=True, text=True).stdout
            rules = [line for line in out.splitlines() if "lookup 1" in line]
            lines.append(f"  {gateway}: {len(rules)} task FT rule(s)")
    return lines


_ROOT_OPS = {"fail", "restore", "degrade", "clear", "kill", "revive", "reset"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcanetctl", description="TCANet prototype control")
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("submit").add_argument("scenario", nargs="?", default="paper_fig1")
    sub.add_parser("withdraw")
    p = sub.add_parser("demand")
    p.add_argument("dep_id")
    p.add_argument("mbps", type=float)
    for name in ("fail", "restore"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["gateway", "link"])
        p.add_argument("target")
    p = sub.add_parser("degrade")
    p.add_argument("link_id")
    p.add_argument("loss_pct", type=float)
    sub.add_parser("clear").add_argument("link_id")
    sub.add_parser("kill").add_argument("agent_id")
    sub.add_parser("revive").add_argument("agent_id")
    sub.add_parser("reset")
    sub.add_parser("status")
    return parser


async def run(args: argparse.Namespace, bus: BusClient, rt: Runtime, world: World, plan: AddressPlan) -> None:
    op = args.op
    if op == "submit":
        await submit(bus, args.scenario)
    elif op == "withdraw":
        await withdraw(bus)
    elif op == "demand":
        await demand(bus, args.dep_id, args.mbps)
    elif op in ("fail", "restore"):
        up = op == "restore"
        if args.kind == "gateway":
            await set_gateway(bus, rt, plan, args.target, up)
        else:
            await set_link(bus, rt, plan, args.target, up)
    elif op == "degrade":
        await degrade(bus, rt, world, plan, args.link_id, args.loss_pct)
    elif op == "clear":
        await degrade(bus, rt, world, plan, args.link_id, 0.0)
    elif op == "kill":
        await kill(bus, rt, args.agent_id)
    elif op == "revive":
        await revive(bus, rt, args.agent_id)
    elif op == "reset":
        await reset(bus, rt, world, plan)


async def _main(args: argparse.Namespace) -> None:
    world, _task = load("paper_fig1")
    plan = build_plan(world)
    if args.op == "status":
        print("\n".join(status_lines(world, plan)))
        return
    bus = await BusClient.connect(config.bus_path(), "tcanetctl")
    rt = ProcessRuntime(config.read_mode(), world, plan, "paper_fig1")
    await run(args, bus, rt, world, plan)
    await asyncio.sleep(0.05)  # let the last frame flush
    await bus.close()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.op in _ROOT_OPS and config.read_mode() == "netns" and os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-E", sys.executable, "-m", "src.tcanet.prototype.ctl", *argv])
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 实现 `src/tcanet/prototype/root.py`**

```python
"""Privileged side of the prototype: data plane up/down and agent launch.

``sudo -E python -m src.tcanet.prototype.root up --mode netns`` builds the
namespaces and starts the broker, protected background traffic and all
agents inside their namespaces.  ``--mode sim`` (no root) starts the
simulated data plane instead.  ``down`` stops everything and removes the
namespaces.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.addressing import AddressPlan, build_plan, gateway_ns
from src.tcanet.prototype.background import BACKGROUND_PORT
from src.tcanet.prototype.runtime import ProcessTable, agent_argv, agent_specs
from src.tcanet.prototype.topology import build_commands, preflight, run_commands, teardown_commands
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load
from src.tcanet.spec import World

REPO_ROOT = Path(__file__).resolve().parents[3]


def process_plan(mode: str, world: World, plan: AddressPlan, scenario: str,
                 python: str = sys.executable) -> list[tuple[str, list[str]]]:
    """``(name, argv)`` of every background process, in start order."""
    processes: list[tuple[str, list[str]]] = [("bus", [python, "-m", "src.tcanet.prototype.bus"])]
    if mode == "sim":
        processes.append(("simnet", [python, "-m", "src.tcanet.prototype.simnet", "--scenario", scenario]))
    else:
        for link_id in sorted(plan.links):
            protected = world.resources[f"link:{link_id}"].protected_load_mbps
            if protected <= 0:
                continue
            addr = plan.links[link_id]
            processes.append((f"bg-{link_id}-sink", [
                "ip", "netns", "exec", gateway_ns(addr.target_gw), python, "-m",
                "src.tcanet.prototype.background", "sink", "--port", str(BACKGROUND_PORT)]))
            processes.append((f"bg-{link_id}", [
                "ip", "netns", "exec", gateway_ns(addr.source_gw), python, "-m",
                "src.tcanet.prototype.background", "send", "--dst", addr.target_ip,
                "--port", str(BACKGROUND_PORT), "--mbps", f"{protected:g}"]))
    processes += [(spec.agent_id, agent_argv(spec, sim=mode == "sim", scenario=scenario, python=python))
                  for spec in agent_specs(world, plan)]
    return processes


def _shared_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o777)


def up(mode: str, scenario: str = "paper_fig1") -> None:
    for path in (config.run_dir(), config.pids_dir(), config.logs_dir(), config.captures_dir()):
        _shared_dir(path)
    config.mode_path().write_text(mode, encoding="utf-8")
    world, _task = load(scenario)
    plan = build_plan(world)
    if mode == "netns":
        missing = preflight()
        if missing:
            raise SystemExit(f"missing tools: {', '.join(missing)} (apt install iproute2)")
        run_commands(teardown_commands(world, plan))
        run_commands(build_commands(world, plan, ACCESS_CAPACITY_MBPS))
    table = ProcessTable(config.pids_dir())
    env = dict(os.environ)
    for name, argv in process_plan(mode, world, plan, scenario):
        table.start(name, argv, log_path=config.logs_dir() / f"{name}.log", cwd=REPO_ROOT, env=env)
        if name == "bus":
            deadline = time.time() + 5.0
            while not config.bus_path().exists():
                if time.time() > deadline:
                    raise SystemExit("bus did not start; see logs/bus.log")
                time.sleep(0.05)
    print(f"TCANet prototype up ({mode}): {len(table.names())} processes, run dir {config.run_dir()}")


def down() -> None:
    world, _task = load("paper_fig1")
    plan = build_plan(world)
    ProcessTable(config.pids_dir()).stop_all()
    if config.read_mode() == "netns" and os.geteuid() == 0:
        run_commands(teardown_commands(world, plan))
    config.bus_path().unlink(missing_ok=True)
    print("TCANet prototype down")


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet prototype data plane")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("up")
    p.add_argument("--mode", choices=["netns", "sim"], default="netns")
    p.add_argument("--scenario", default="paper_fig1")
    sub.add_parser("down")
    args = parser.parse_args()
    if args.op == "up":
        up(args.mode, args.scenario)
    else:
        down()


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_root_runtime.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 8: Commit**

```bash
git add src/tcanet/prototype/runtime.py src/tcanet/prototype/background.py src/tcanet/prototype/ctl.py src/tcanet/prototype/root.py tests/tcanet/prototype/test_root_runtime.py
git commit -m "feat(prototype): process table, tcanetctl fault injection and privileged up/down"
```

---

### Task 12: Algorithm 1 日志与控制器进程（端到端验收）

**Files:**
- Create: `src/tcanet/prototype/log_format.py`
- Create: `src/tcanet/prototype/controller.py`
- Test: `tests/tcanet/prototype/test_end_to_end_sim.py`

**Interfaces:**
- Consumes: Tasks 1–11
- Produces: `log_format.entry(step, title, lines, level, **data)`、`event_entry`、`scope_entry(closure, all_deps)`（`data.affected`）、`selection_entry(trace)`、`apply_line(payload, ok, detail)`、`assess_entry(window, observations, task)`、`rollback_entry(version, restored)`、`commit_entry(subnet, latency_ms, stats, label)`、`render(entry) -> rich.Text`；`ControllerApp(bus, *, scenario, console=None, auto_capture=False)` + `async run(stop)`、`history: list[dict]`；控制器发布 `ctrl.log`、`ctrl.subnet`、`ctrl.metrics`、`ui.capture`，只订阅 `CONTROLLER_TOPICS = ("report.", "hello", "heartbeat", "ack", "task.")`；CLI `python -m src.tcanet.prototype.controller [--auto-capture]`

端到端测试在一个进程里跑完五幕（约 6 秒）：组网 v1 → e1 需求 25 Mbps → G2 故障（e1 改走 G1→G3→G4、e3 不变、日志含 `retained unchanged: {e3}`）→ reset 后 L3 隐藏丢包 + G2 故障（Assess `loss_hard:e1` → rollback → e1 走 G1→G4）→ 杀 physical-G4（e3 的 p 绑定换掉、路径不变）。

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_end_to_end_sim.py`**

```python
"""All demo acts end to end on the simulated data plane, in one process."""
from __future__ import annotations

import asyncio
import io
import unittest

from rich.console import Console

from src.tcanet.prototype import ctl
from src.tcanet.prototype.addressing import build_plan
from src.tcanet.prototype.agents.__main__ import build_agent
from src.tcanet.prototype.controller import ControllerApp
from src.tcanet.prototype.runtime import agent_specs
from src.tcanet.prototype.simnet import SimFabricState, SimNetworkProcess
from src.tcanet.scenario_fig1 import load
from tests.tcanet.prototype.harness import BusHarness, wait_for


class InProcessRuntime:
    """``ctl.Runtime`` over asyncio tasks instead of OS processes."""

    mode = "sim"

    def __init__(self, harness: BusHarness, world, plan) -> None:
        self.harness = harness
        self.specs = {spec.agent_id: spec for spec in agent_specs(world, plan)}
        self.running: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}

    def revive(self, agent_id: str) -> bool:
        if agent_id in self.running or agent_id not in self.specs:
            return False
        spec = self.specs[agent_id]
        agent = build_agent(spec.role, spec.agent_id, spec.gateway, scenario="paper_fig1",
                            sim=True, bus_path=self.harness.sock)
        agent.console = Console(file=io.StringIO())
        stop = asyncio.Event()
        self.running[agent_id] = (stop, asyncio.create_task(agent.run(stop)))
        return True

    def kill(self, agent_id: str) -> bool:
        entry = self.running.pop(agent_id, None)
        if entry is None:
            return False
        entry[1].cancel()  # abrupt: no goodbye, heartbeats just stop
        return True

    async def kernel(self, cmds) -> None:
        raise AssertionError("sim runtime must not touch the kernel")

    async def shutdown(self) -> None:
        for stop, task in self.running.values():
            stop.set()
            task.cancel()
        await asyncio.gather(*(task for _s, task in self.running.values()), return_exceptions=True)


def _commit(version: int):
    return lambda m: m["topic"] == "ctrl.subnet" and m["payload"]["version"] == version


class EndToEndSimTests(unittest.TestCase):
    def test_all_acts(self) -> None:
        asyncio.run(self._acts())

    async def _acts(self) -> None:
        async with BusHarness() as h:
            world, _task = load("paper_fig1")
            plan = build_plan(world)
            stop = asyncio.Event()
            simnet = SimNetworkProcess(await h.client("simnet"), SimFabricState(world))
            controller = ControllerApp(await h.client("controller"), console=Console(file=io.StringIO()))
            background = [asyncio.create_task(simnet.run(stop)), asyncio.create_task(controller.run(stop))]
            rt = InProcessRuntime(h, world, plan)
            for agent_id in rt.specs:
                rt.revive(agent_id)
            watch = await h.client("watch", "ctrl.", "ack")
            op = await h.client("tcanetctl")
            try:
                await asyncio.sleep(0.6)
                # Act 1: formation
                await ctl.submit(op, "paper_fig1")
                v1 = await wait_for(watch, _commit(1), timeout=8)
                self.assertEqual(v1["payload"]["paths"]["e1"], ["G1", "G2", "G4"])

                # Act 2: demand 15 -> 25 Mbps on e1 (task update)
                await ctl.demand(op, "e1", 25.0)
                v2 = await wait_for(watch, _commit(2), timeout=8)
                steps = [item["step"] for item in controller.history]
                self.assertIn("select", steps)

                # Act 3: transit gateway G2 fails
                mark = len(controller.history)
                await ctl.set_gateway(op, rt, plan, "G2", up=False)
                v3 = await wait_for(watch, _commit(3), timeout=10)
                self.assertEqual(v3["payload"]["paths"]["e1"], ["G1", "G3", "G4"])
                self.assertEqual(v3["payload"]["paths"]["e3"], v2["payload"]["paths"]["e3"])
                scope = next(i for i in controller.history[mark:] if i["step"] == "scope")
                self.assertIn("retained unchanged: {e3}", scope["lines"][-1])

                # Act 4: hidden L3 loss -> Assess fails -> Rollback -> L6
                await ctl.reset(op, rt, world, plan)
                await wait_for(watch, _commit(0), timeout=5)
                await asyncio.sleep(0.6)
                await ctl.submit(op, "paper_fig1")
                await wait_for(watch, _commit(1), timeout=8)
                await ctl.degrade(op, rt, world, plan, "L3", 10.0)
                mark = len(controller.history)
                await ctl.set_gateway(op, rt, plan, "G2", up=False)
                v2b = await wait_for(watch, _commit(2), timeout=12)
                self.assertEqual(v2b["payload"]["paths"]["e1"], ["G1", "G4"])
                steps = [i["step"] for i in controller.history[mark:]]
                self.assertIn("rollback", steps)
                assess_fail = next(i for i in controller.history[mark:]
                                   if i["step"] == "assess" and i["level"] == "fail")
                self.assertIn("loss_hard:e1", assess_fail["title"])

                # Act 5: PhyAgent at G4 dies -> rebind e3 only
                await ctl.kill(op, rt, "physical-G4")
                v3b = await wait_for(watch, _commit(3), timeout=10)
                self.assertNotEqual(v3b["payload"]["bindings"]["e3"][2], "physical-G4")
                self.assertEqual(v3b["payload"]["paths"], v2b["payload"]["paths"])
            finally:
                stop.set()
                await rt.shutdown()
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_end_to_end_sim.py`
Expected: FAIL（`ModuleNotFoundError: ... controller`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/log_format.py`**

```python
"""Algorithm 1 log entries for the controller terminal, UI and timeline.

Each entry is a JSON-serializable dict ``{"step", "title", "lines", "level"}``
whose ``step`` names the Algorithm 1 stage it documents.
"""
from __future__ import annotations

from collections import Counter

from rich.text import Text

from src.tcanet.closure import ClosureResult, RuntimeEvent
from src.tcanet.metrics import ModificationStats
from src.tcanet.prototype.addressing import rule_commands
from src.tcanet.scenario import event_description
from src.tcanet.selection import SelectionTrace
from src.tcanet.spec import TaskSpecification
from src.tcanet.subnet import SubnetState
from src.tcanet.verify import DepObservation, WindowResult

_ADMISSIBILITY = ("write_conflict", "executor_unavailable", "precondition_failed", "binding_offline")
_STYLE = {"info": "white", "ok": "bold green", "warn": "bold yellow", "fail": "bold red"}
_STEP_STYLE = {"event": "bold red", "scope": "cyan", "select": "magenta", "apply": "blue",
               "assess": "yellow", "rollback": "bold red", "commit": "bold green",
               "formation": "bold cyan", "withdraw": "dim"}


def entry(step: str, title: str, lines: list[str] | None = None, level: str = "info",
          **data) -> dict:
    """One log entry; ``data`` carries machine-readable fields for the UI."""
    return {"step": step, "title": title, "lines": list(lines or []), "level": level, "data": data}


def _fmt(deps) -> str:
    return "{" + ", ".join(sorted(deps)) + "}"


def event_entry(event: RuntimeEvent, evidence_ts: float, detected_ts: float) -> dict:
    return entry("event", f"Event: {event_description(event)}",
                 [f"first agent evidence → event: {(detected_ts - evidence_ts) * 1000:.0f} ms"], "fail",
                 kind=event.kind, evidence_ts=evidence_ts)


def scope_entry(closure: ClosureResult, all_deps: list[str]) -> dict:
    lines = ["E^aff_0 = " + _fmt(closure.initial) + "   (Eq. 14)"]
    lines += [f"  {dep}: {reason}" for dep, reason in sorted(closure.initial.items())]
    for round_ in closure.rounds:
        added = ", ".join(f"{dep} ← {reason}" for dep, reason in sorted(round_.added.items()))
        lines.append(f"round {round_.index}: + {added}   (Eq. 15)")
    untouched = sorted(set(all_deps) - set(closure.final))
    lines.append(f"fixed point E^aff = {_fmt(closure.final)}; retained unchanged: {_fmt(untouched)}   (Eq. 16)")
    return entry("scope", "Determine scope", lines, affected=sorted(closure.final))


def selection_entry(trace: SelectionTrace) -> dict:
    evaluations = trace.evaluations
    admissible = [e for e in evaluations
                  if not any(v.startswith(_ADMISSIBILITY) for v in e.feasibility.violations)]
    feasible = [e for e in evaluations if e.feasibility.feasible]
    lines = [f"|C| = {len(evaluations)}   |C^ad| = {len(admissible)}   |C^feas| = {len(feasible)}   (Eq. 8)"]
    reasons = Counter(v for e in admissible if not e.feasibility.feasible for v in e.feasibility.violations)
    for violation, count in reasons.most_common(3):
        lines.append(f"  rejected ×{count}: {violation}")
    if trace.selected is None:
        lines.append("no feasible joint configuration")
        return entry("select", "Joint feasibility", lines, "fail")
    lines.append(f"J* = {trace.stage1_min_v:.3f} attained by {len(trace.stage1_survivors)} configs   (Eq. 11)")
    by_scope = Counter(e.modification_scope for e in trace.stage1_survivors)
    lines.append("M over J*-optimal configs: " + ", ".join(
        f"M={scope}×{count}" for scope, count in sorted(by_scope.items())[:4]))
    lines.append(f"c* = {trace.selected.label}  (min M = {trace.stage2_min_r}, Eq. 12)")
    if trace.tiebreak_note:
        lines.append(f"  tie: {trace.tiebreak_note}")
    return entry("select", "Joint feasibility → minimize J, then M", lines)


def apply_line(payload: dict, ok: bool, detail: str) -> dict:
    kind, params = payload["kind"], payload["params"]
    undo = payload["action_id"].startswith("undo:")
    if kind.endswith("_rule"):
        op = "add" if kind == "install_rule" else "del"
        hop = "local" if params.get("mode") == "local_delivery" else f"→{params.get('next_hop_gateway')}"
        text = f"{'+' if op == 'add' else '−'}FT {params['dep_id']}@{params['gateway']} {hop}"
        if detail.startswith(("withdrawn", "not restored")):
            text += f"   ({detail})"
        else:
            text += "   " + rule_commands(params, op)[-1].text()
    elif kind == "bind_support":
        text = f"Φ {payload['target']}.{params['role']} → {payload['executor']}"
    else:
        shown = {k: v for k, v in params.items() if k not in ("app_agent",)}
        text = f"u {payload['value']} {payload['target']} {shown} @ {payload['executor']}"
    if detail == "replaced in place":
        text += "   (replaced in place)"
    text = ("undo " if undo else "") + text + ("" if ok else f"   ✗ {detail}")
    return entry("rollback" if undo else "apply", text, [], "info" if ok else "fail")


def assess_entry(window: WindowResult, observations: tuple[DepObservation, ...], task: TaskSpecification) -> dict:
    lines = []
    deps = {dep.dep_id: dep for dep in task.dag.dependencies}
    for obs in observations:
        req = task.requirements_for(deps[obs.dep_id])
        g_thr = (req.min_throughput_mbps - obs.throughput_mbps) / req.min_throughput_mbps
        g_delay = (obs.delay_ms - req.max_delay_ms) / req.max_delay_ms
        g_loss = (obs.loss_rate - req.max_loss_rate) / req.max_loss_rate
        marks = ["✓" if g <= 0 else "✗" for g in (g_thr, g_delay, g_loss)]
        lines.append(
            f"{obs.dep_id}: {obs.throughput_mbps:5.1f} Mbps {marks[0]}  owd {obs.delay_ms:5.1f} ms {marks[1]}  "
            f"loss {obs.loss_rate * 100:4.1f}% {marks[2]}   g = ({g_thr:+.2f}, {g_delay:+.2f}, {g_loss:+.2f})"
        )
    for dep in window.pending:
        lines.append(f"{dep}: no measurement in window")
    verdict = "PASS" if window.accepted else "FAIL " + ", ".join(window.violations or window.pending)
    return entry("assess", f"Assess (Eq. 5–6): {verdict}", lines, "ok" if window.accepted else "fail")


def rollback_entry(version: int, restored: int) -> dict:
    return entry("rollback", f"Rollback v{version} → v{restored}; refresh s_m; C ← C \\ {{c*}}", [], "warn")


def commit_entry(subnet: SubnetState, latency_ms: float, stats: ModificationStats | None, label: str) -> dict:
    lines = [f"{label} latency {latency_ms:.0f} ms"]
    if stats is not None:
        lines.append(
            f"M_m = {stats.changed} (ΔΠ={stats.changed_paths}, ΔFT={stats.changed_forwarding}, "
            f"ΔΦ={stats.changed_bindings})   Mod = {stats.ratio * 100:.0f}% of {stats.installed} records"
        )
    lines += [f"π {dep}: {'→'.join(path.gateway_path)}" for dep, path in sorted(subnet.paths.items())]
    return entry("commit", f"Commit v{subnet.version}", lines, "ok", version=subnet.version,
                 latency_ms=latency_ms)


def render(item: dict) -> Text:
    text = Text()
    text.append(f"[{item['step']:^9}] ", style=_STEP_STYLE.get(item["step"], "white"))
    text.append(item["title"], style=_STYLE.get(item["level"], "white"))
    for line in item["lines"]:
        text.append("\n            " + line, style="dim" if item["level"] == "info" else "")
    return text
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/controller.py`**

```python
"""TCANet controller process: Algorithm 1 over the live agent system.

The controller learns the network only from agent reports (``report.*``,
``hello``/``heartbeat``, ``ack``) and task input from the orchestrator
(``task.*``).  It never subscribes to ``ctl.*``/``sim.*``/``ops.*``: faults
injected by the operator are seen only through their effects.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import time

from rich.console import Console

from src.tcanet.closure import RuntimeEvent
from src.tcanet.metrics import modification_stats
from src.tcanet.prototype import config
from src.tcanet.prototype import log_format as fmt
from src.tcanet.prototype.addressing import build_plan, rule_params
from src.tcanet.prototype.aggregator import EventDetector, StateAggregator
from src.tcanet.prototype.assess import BusMeasurement
from src.tcanet.prototype.bus import BusClient
from src.tcanet.prototype.bus_dataplane import AckWaiter, BusDataplane
from src.tcanet.scenario import run_formation, with_demand
from src.tcanet.scenario_fig1 import load
from src.tcanet.subnet import SubnetState
from src.tcanet.verify import RecoveryController, RecoveryHooks

CONTROLLER_TOPICS = ("report.", "hello", "heartbeat", "ack", "task.")


class ControllerApp:
    def __init__(self, bus: BusClient, *, scenario: str = "paper_fig1", console: Console | None = None,
                 auto_capture: bool = False) -> None:
        self.bus = bus
        self.scenario = scenario
        self.console = console or Console(highlight=False)
        self.auto_capture = auto_capture
        self.acks = AckWaiter()
        self.queue: asyncio.Queue[tuple] = asyncio.Queue()
        self.subnet: SubnetState | None = None
        self.history: list[dict] = []
        self._reset_state()

    def _reset_state(self) -> None:
        self.world, self.task = load(self.scenario)
        self.plan = build_plan(self.world)
        self.aggregator = StateAggregator(self.world)
        self.detector = EventDetector(self.world, debounce_s=config.detect_debounce_s())
        self.measure = BusMeasurement(self.aggregator.flowbook)
        self.dataplane = BusDataplane(self.bus, self.acks, self.plan, lambda: self.task,
                                      on_action=self._log_action)
        self.subnet = None

    async def log(self, item: dict) -> None:
        self.history.append(item)
        self.console.print(fmt.render(item))
        await self.bus.publish("ctrl.log", item)

    async def _log_action(self, payload: dict, ok: bool, detail: str) -> None:
        await self.log(fmt.apply_line(payload, ok, detail))

    async def run(self, stop: asyncio.Event) -> None:
        await self.bus.subscribe(*CONTROLLER_TOPICS)
        await self.log(fmt.entry("formation", f"TCANet controller ready — scenario {self.scenario}",
                                 [f"agents report via {config.bus_path()}"], "ok"))
        tasks = [asyncio.create_task(self._pump(stop)), asyncio.create_task(self._tick(stop)),
                 asyncio.create_task(self._worker(stop))]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            msg = await self.bus.recv()
            if msg is None:
                stop.set()
                return
            topic = msg["topic"]
            if topic == "ack":
                self.acks.feed(msg["payload"])
            elif topic == "task.submit":
                await self.queue.put(("submit",))
            elif topic == "task.withdraw":
                await self.queue.put(("withdraw",))
            else:
                self.detector.feed(self.aggregator.apply(msg))

    async def _tick(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(0.1 * config.time_scale())
            now = time.time()
            self.detector.feed(self.aggregator.silent_agents(now, config.heartbeat_timeout_s()))
            for event, evidence_ts in self.detector.poll(now):
                await self.queue.put(("event", event, evidence_ts, now))

    async def _worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            item = await self.queue.get()
            try:
                if item[0] == "submit":
                    await self.form()
                elif item[0] == "withdraw":
                    await self.withdraw()
                elif item[0] == "event":
                    await self.reconfigure(*item[1:])
            except Exception as exc:  # keep the controller alive on the demo floor
                await self.log(fmt.entry("event", f"controller error: {type(exc).__name__}: {exc}", [], "fail"))

    async def _publish_subnet(self) -> None:
        subnet = self.subnet
        await self.bus.publish("ctrl.subnet", {
            "task_id": self.task.dag.task_id,
            "version": subnet.version if subnet else 0,
            "paths": {dep: list(rec.gateway_path) for dep, rec in (subnet.paths.items() if subnet else ())},
            "forwarding": sorted(subnet.forwarding) if subnet else [],
            "bindings": {dep: list(b.as_tuple()) for dep, b in (subnet.bindings.records.items() if subnet else ())},
        })

    async def _capture(self, tag: str) -> None:
        if self.auto_capture:
            await self.bus.publish("ui.capture", {"tag": tag})

    async def form(self) -> None:
        if self.subnet is not None:
            await self.log(fmt.entry("formation", "task already active; withdraw first", [], "warn"))
            return
        deps = [dep.dep_id for dep in self.task.dag.dependencies]
        await self.log(fmt.entry("formation", f"T_m received: {self.task.dag.task_id}",
                                 [f"E^b,old = ∅ → E^aff = E^b,new = {{{', '.join(deps)}}}   (Eq. 13–14)"]))
        started = time.time()
        observed: list = []

        async def measure(staged, world, dep_ids):
            observed[:] = await self.measure(staged, world, dep_ids)
            return tuple(observed)

        outcome = await run_formation(self.task, self.world, dataplane=self.dataplane, measure=measure)
        await self.log(fmt.assess_entry(outcome.window, tuple(observed), self.task))
        if outcome.subnet is None:
            await self.log(fmt.rollback_entry(1, 0))
            await self._publish_subnet()
            return
        self.subnet = outcome.subnet
        await self.log(fmt.commit_entry(self.subnet, (time.time() - started) * 1000.0, None, "formation"))
        await self._publish_subnet()
        await self.bus.publish("ctrl.metrics", {"kind": "formation", "version": 1,
                                                "latency_ms": (time.time() - started) * 1000.0})
        await self._capture("formed")

    async def withdraw(self) -> None:
        if self.subnet is not None:
            for rule_id in sorted(self.subnet.forwarding):
                entry = self.subnet.forwarding[rule_id]
                if not self.world.gateway_online(entry.gateway_id):
                    continue
                payload = {"action_id": f"withdraw:{rule_id}#{time.time_ns()}", "kind": "remove_rule",
                           "executor": f"network-{entry.gateway_id}", "target": rule_id, "value": "withdrawn",
                           "params": rule_params(entry, self.task, self.plan)}
                await self.dataplane.send(payload, self.world)
        self._reset_state()
        await self._publish_subnet()
        await self.log(fmt.entry("withdraw", "task withdrawn; controller state reset", [], "warn"))

    async def reconfigure(self, event: RuntimeEvent, evidence_ts: float, detected_ts: float) -> None:
        if self.subnet is None:
            await self.log(fmt.entry("event", f"ignored (no active subnet): {event.kind}", [], "warn"))
            return
        await self.log(fmt.event_entry(event, evidence_ts, detected_ts))
        await self._capture("fault")
        if event.kind == "demand_change":
            new = self.aggregator.demands[event.dep_id]
            self.task = with_demand(self.task, event.dep_id, new)
        deps = [dep.dep_id for dep in self.task.dag.dependencies]

        async def on_closure(closure):
            await self.log(fmt.scope_entry(closure, deps))

        async def on_decision(trace, _selected):
            await self.log(fmt.selection_entry(trace))

        async def on_window(window, observations):
            await self.log(fmt.assess_entry(window, observations, self.task))

        async def on_rollback(attempt):
            await self.log(fmt.rollback_entry(self.subnet.version + 1, self.subnet.version))

        controller = RecoveryController(max_attempts=config.K_MAX, window_ms=self.measure.window_ms,
                                        measure=self.measure, dataplane=self.dataplane)
        before = self.subnet
        result = await controller.recover(
            self.task, before, self.world, event,
            hooks=RecoveryHooks(on_closure=on_closure, on_decision=on_decision,
                                on_window=on_window, on_rollback=on_rollback),
        )
        if not result.recovered:
            if result.attempts and result.attempts[-1].selection and result.attempts[-1].selection.selected is None:
                await self.log(fmt.selection_entry(result.attempts[-1].selection))
            await self.log(fmt.entry("commit", f"reconfiguration failed: {result.error}; v{before.version} retained",
                                     [], "fail"))
            return
        self.subnet = result.subnet
        for action in result.attempts[-1].selection.selected.actions:
            if action.action == "ADJUST_RATE":
                self.task = with_demand(self.task, action.target, float(action.parameters["rate_mbps"]))
        latency_ms = (time.time() - evidence_ts) * 1000.0
        stats = modification_stats(before, self.subnet)
        await self.log(fmt.commit_entry(self.subnet, latency_ms, stats, "recovery"))
        await self._publish_subnet()
        await self.bus.publish("ctrl.metrics", {"kind": "recovery", "version": self.subnet.version,
                                                "latency_ms": latency_ms, "changed": stats.changed,
                                                "installed": stats.installed, "ratio": stats.ratio,
                                                "attempts": len(result.attempts)})
        await self._capture("recovered")


async def _main(args: argparse.Namespace) -> None:
    bus = await BusClient.connect(config.bus_path(), "controller")
    app = ControllerApp(bus, scenario=args.scenario, auto_capture=args.auto_capture)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await app.run(stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet controller")
    parser.add_argument("--scenario", default="paper_fig1")
    parser.add_argument("--auto-capture", action="store_true")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_end_to_end_sim.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 6: Commit**

```bash
git add src/tcanet/prototype/log_format.py src/tcanet/prototype/controller.py tests/tcanet/prototype/test_end_to_end_sim.py
git commit -m "feat(prototype): controller running Algorithm 1 over live agents"
```

---

### Task 13: 演示窗口（Qt）与终端视图

**Files:**
- Create: `src/tcanet/prototype/ui/__init__.py`
- Create: `src/tcanet/prototype/ui/theme.py`
- Create: `src/tcanet/prototype/ui/model.py`
- Create: `src/tcanet/prototype/ui/qtbus.py`
- Create: `src/tcanet/prototype/ui/common.py`
- Create: `src/tcanet/prototype/ui/flows.py`
- Create: `src/tcanet/prototype/ui/phy.py`
- Create: `src/tcanet/prototype/ui/app_dag.py`
- Create: `src/tcanet/prototype/ui/console.py`
- Test: `tests/tcanet/prototype/test_ui_models.py`
- Test: `tests/tcanet/prototype/test_ui_smoke.py`

**Interfaces:**
- Consumes: Task 5 bus、Task 12 日志条目格式
- Produces: `FlowModel(dep_ids)`、`AccessModel(gateways, links)`、`TaskModel(task)`（均为 `.apply(msg) -> bool`）；窗口 `FlowsWindow`/`PhyWindow`/`DagWindow`（`python -m src.tcanet.prototype.ui.flows|phy|app_dag`），收到 `ui.capture{tag}` 时保存 `<run_dir>/captures/<tag>/<flows|phy|app_dag>.png`；`ui.console agents --layer L` 与 `ui.console operator`

配色取自 dataviz 参考调色板深色档（已用 `validate_palette.js --mode dark` 校验 4 槽全部 PASS）；状态色总是配文字标签。`test_ui_smoke.py` 在未安装 `[demo]` extra 时自动 skip；安装后设 `QT_QPA_PLATFORM=offscreen` 运行。

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_ui_models.py`**

```python
from __future__ import annotations

import math
import unittest

from src.tcanet.prototype.ui.model import AccessModel, FlowModel, TaskModel
from src.tcanet.scenario_fig1 import load


def _m(topic, payload, ts):
    return {"topic": topic, "src": "x", "ts": ts, "payload": payload}


class FlowModelTests(unittest.TestCase):
    def test_series_break_across_gaps_and_markers(self) -> None:
        model = FlowModel(["e1"])
        for ts in (0.0, 0.5, 1.0, 4.0):
            model.apply(_m("report.flow", {"dep_id": "e1", "rx_mbps": 15.0, "owd_ms": 20.0,
                                           "source": "MEASURED"}, 100 + ts))
        xs, ys = model.series("e1", "owd")
        self.assertEqual(len(xs), 5)
        self.assertTrue(math.isnan(ys[3]))
        model.apply(_m("ctrl.log", {"step": "commit", "title": "Commit v2", "level": "ok", "lines": []}, 105))
        model.apply(_m("ctrl.log", {"step": "apply", "title": "+FT", "level": "info", "lines": []}, 105))
        model.apply(_m("ops.fault", {"op": "gateway", "target": "G2", "up": False}, 106))
        self.assertEqual([(label, kind) for _t, label, kind in model.markers],
                         [("v2", "commit"), ("gateway G2", "ops")])
        self.assertFalse(model.apply(_m("report.flow", {"dep_id": "zz", "rx_mbps": 1}, 107)))


class AccessModelTests(unittest.TestCase):
    def test_link_down_if_any_reporter_says_down(self) -> None:
        model = AccessModel(["G1"], ["L1"])
        model.apply(_m("report.link", {"link_id": "L1", "up": True, "reporter": "network-G2",
                                       "utilization": None}, 1))
        model.apply(_m("report.link", {"link_id": "L1", "up": False, "reporter": "network-G1",
                                       "utilization": 0.0}, 2))
        self.assertFalse(model.link_up["L1"])
        model.apply(_m("report.access", {"gateway": "G1", "capacity_mbps": 55.0, "snr_db": 18.0}, 3))
        self.assertEqual(model.capacity_series("G1")[1], [55.0])


class TaskModelTests(unittest.TestCase):
    def test_status_against_hard_requirements(self) -> None:
        _world, task = load("paper_fig1")
        model = TaskModel(task)
        model.apply(_m("report.flow", {"dep_id": "e1", "rx_mbps": 15.0, "owd_ms": 20.0, "loss": 0.0}, 1))
        self.assertEqual(model.status("e1"), "inactive")
        model.apply(_m("ctrl.subnet", {"version": 1, "paths": {"e1": ["G1", "G2", "G4"]}, "bindings": {}}, 2))
        self.assertEqual(model.status("e1"), "ok")
        model.apply(_m("report.flow", {"dep_id": "e1", "rx_mbps": 15.0, "owd_ms": 20.0, "loss": 0.1}, 3))
        self.assertEqual(model.status("e1"), "violated")
        model.apply(_m("ctrl.log", {"step": "scope", "title": "", "level": "info", "lines": [],
                                    "data": {"affected": ["e1", "e2"]}}, 4))
        self.assertEqual(model.affected, {"e1", "e2"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 写失败测试 `tests/tcanet/prototype/test_ui_smoke.py`**

```python
"""Offscreen render of every Qt window (skipped without PySide6/pyqtgraph)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import pyqtgraph  # noqa: F401
    from PySide6 import QtWidgets
except ImportError:  # pragma: no cover - optional [demo] extra
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "PySide6/pyqtgraph not installed")
class WindowSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.env = mock.patch.dict(os.environ, {"TCANET_RUN_DIR": str(self.dir)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _feed(self, window) -> None:
        msgs = [
            {"topic": "ctrl.subnet", "ts": 1.0, "src": "c", "payload": {
                "version": 1, "paths": {"e1": ["G1", "G2", "G4"]},
                "bindings": {"e1": ["transport-G1", "network-G1", "physical-G1"]}}},
            {"topic": "report.flow", "ts": 1.5, "src": "a", "payload": {
                "dep_id": "e1", "rx_mbps": 15.0, "owd_ms": 20.0, "loss": 0.0, "source": "MEASURED"}},
            {"topic": "report.access", "ts": 1.5, "src": "p", "payload": {
                "gateway": "G1", "capacity_mbps": 60.0, "snr_db": 20.0}},
            {"topic": "report.link", "ts": 1.6, "src": "n", "payload": {
                "link_id": "L1", "up": False, "reporter": "network-G1", "utilization": 0.0}},
            {"topic": "ctrl.log", "ts": 2.0, "src": "c", "payload": {
                "step": "commit", "title": "Commit v1", "level": "ok", "lines": [], "data": {}}},
        ]
        for msg in msgs:
            window.on_message(msg)
        window.refresh()

    def test_windows_render_and_capture(self) -> None:
        from src.tcanet.prototype.ui.app_dag import DagWindow
        from src.tcanet.prototype.ui.flows import FlowsWindow
        from src.tcanet.prototype.ui.phy import PhyWindow

        for cls in (FlowsWindow, PhyWindow, DagWindow):
            window = cls()
            self._feed(window)
            path = Path(window.capture("smoke"))
            self.assertTrue(path.exists(), cls.__name__)
            self.assertGreater(path.stat().st_size, 1000)
            window.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_ui_models.py tests/tcanet/prototype/test_ui_smoke.py`
Expected: FAIL（`ModuleNotFoundError: ... ui`）

- [ ] **Step 4: 实现 `src/tcanet/prototype/ui/__init__.py`**

```python
"""Native demo windows (PySide6 + pyqtgraph) and rich terminal views."""
```

- [ ] **Step 5: 实现 `src/tcanet/prototype/ui/theme.py`**

```python
"""Validated dark chart palette (dataviz reference instance, dark mode)."""
from __future__ import annotations

SURFACE = "#1a1a19"
PAGE = "#0d0d0d"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

# Categorical slots 1-4 (dark), validated adjacent CVD ΔE ≥ 8.4.
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")

# Status palette — always paired with a text label.
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"


def series_color(index: int) -> str:
    return SERIES[index % len(SERIES)]
```

- [ ] **Step 6: 实现 `src/tcanet/prototype/ui/model.py`**

```python
"""Window data models: bus messages in, plot-ready series out (Qt-free)."""
from __future__ import annotations

from collections import defaultdict, deque

from src.tcanet.spec import TaskSpecification

_HORIZON = 600


class FlowModel:
    """Per-dependency goodput / one-way delay series plus event markers."""

    def __init__(self, dep_ids: list[str]) -> None:
        self.dep_ids = dep_ids
        self.t0: float | None = None
        self.rx: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.owd: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.markers: deque[tuple[float, str, str]] = deque(maxlen=40)
        self.source = "MEASURED"

    def _t(self, ts: float) -> float:
        if self.t0 is None:
            self.t0 = ts
        return ts - self.t0

    def apply(self, msg: dict) -> bool:
        topic, p, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if topic == "report.flow" and p["dep_id"] in self.dep_ids:
            t = self._t(ts)
            self.source = p.get("source", self.source)
            self.rx[p["dep_id"]].append((t, float(p["rx_mbps"])))
            if p.get("owd_ms") is not None:
                self.owd[p["dep_id"]].append((t, float(p["owd_ms"])))
            return True
        if topic == "ctrl.log" and p["step"] in ("event", "rollback", "commit") and p["level"] != "info":
            label = {"event": "detected", "rollback": "rollback",
                     "commit": p["title"].replace("Commit ", "")}[p["step"]]
            if p["step"] == "rollback" and not p["title"].startswith("Rollback"):
                return False
            self.markers.append((self._t(ts), label, p["step"]))
            return True
        if topic == "ops.fault" and p["op"] in ("gateway", "link", "degrade", "kill", "demand"):
            target = p.get("target", p.get("dep_id", ""))
            self.markers.append((self._t(ts), f"{p['op']} {target}", "ops"))
            return True
        return False

    def series(self, dep_id: str, kind: str, gap_s: float = 1.5) -> tuple[list[float], list[float]]:
        """Points of one series; a NaN breaks the line across sampling gaps."""
        xs: list[float] = []
        ys: list[float] = []
        for t, v in (self.rx if kind == "rx" else self.owd).get(dep_id, ()):
            if xs and t - xs[-1] > gap_s:
                xs.append(xs[-1])
                ys.append(float("nan"))
            xs.append(t)
            ys.append(v)
        return xs, ys


class AccessModel:
    """PhyAgent access capacity per gateway and NetAgent link utilization."""

    def __init__(self, gateways: list[str], links: list[str]) -> None:
        self.gateways, self.links = gateways, links
        self.t0: float | None = None
        self.capacity: dict[str, deque] = defaultdict(lambda: deque(maxlen=_HORIZON))
        self.snr: dict[str, float] = {}
        self.utilization: dict[str, float] = {link: 0.0 for link in links}
        self.link_up: dict[str, bool] = {link: True for link in links}
        self._reports: dict[str, dict[str, bool]] = defaultdict(dict)

    def apply(self, msg: dict) -> bool:
        topic, p, ts = msg["topic"], msg["payload"], float(msg["ts"])
        if self.t0 is None:
            self.t0 = ts
        if topic == "report.access":
            self.capacity[p["gateway"]].append((ts - self.t0, float(p["capacity_mbps"])))
            self.snr[p["gateway"]] = float(p["snr_db"])
            return True
        if topic == "report.link" and p["link_id"] in self.link_up:
            self._reports[p["link_id"]][p["reporter"]] = bool(p["up"])
            self.link_up[p["link_id"]] = all(self._reports[p["link_id"]].values())
            if p.get("utilization") is not None:
                self.utilization[p["link_id"]] = float(p["utilization"])
            return True
        return False

    def capacity_series(self, gateway: str) -> tuple[list[float], list[float]]:
        points = self.capacity.get(gateway, ())
        return [t for t, _ in points], [v for _, v in points]


class TaskModel:
    """Task DAG state: version, paths, bindings, per-dependency q^H status."""

    def __init__(self, task: TaskSpecification) -> None:
        self.task = task
        self.version = 0
        self.paths: dict[str, list[str]] = {}
        self.bindings: dict[str, list[str | None]] = {}
        self.affected: set[str] = set()
        self.latest: dict[str, dict] = {}

    def apply(self, msg: dict) -> bool:
        topic, p = msg["topic"], msg["payload"]
        if topic == "ctrl.subnet":
            self.version = int(p["version"])
            self.paths = p["paths"]
            self.bindings = p["bindings"]
            self.affected = set()
            return True
        if topic == "ctrl.log" and p["step"] == "scope":
            self.affected = set(p.get("data", {}).get("affected", []))
            return True
        if topic == "report.flow":
            self.latest[p["dep_id"]] = p
            return True
        return False

    def status(self, dep_id: str) -> str:
        """``inactive`` | ``ok`` | ``violated`` against the hard requirements."""
        sample = self.latest.get(dep_id)
        if self.version == 0 or sample is None:
            return "inactive"
        dep = next(d for d in self.task.dag.dependencies if d.dep_id == dep_id)
        req = self.task.requirements_for(dep)
        owd = sample.get("owd_ms")
        loss = sample.get("loss")
        ok = (
            sample["rx_mbps"] >= req.min_throughput_mbps
            and owd is not None and owd <= req.max_delay_ms
            and loss is not None and loss <= req.max_loss_rate
        )
        return "ok" if ok else "violated"
```

- [ ] **Step 7: 实现 `src/tcanet/prototype/ui/qtbus.py`**

```python
"""Bridge the asyncio bus into Qt: a worker thread emits one signal per message."""
from __future__ import annotations

import asyncio
import threading

from PySide6 import QtCore

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient


class BusBridge(QtCore.QObject):
    message = QtCore.Signal(dict)
    connected = QtCore.Signal(bool)

    def __init__(self, name: str, topics: tuple[str, ...]) -> None:
        super().__init__()
        self.name = name
        self.topics = topics
        self.publish_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def publish(self, topic: str, payload: dict) -> None:
        if self._loop is not None and self.publish_queue is not None:
            self._loop.call_soon_threadsafe(self.publish_queue.put_nowait, (topic, payload))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.publish_queue = asyncio.Queue()
        while True:
            try:
                client = await BusClient.connect(config.bus_path(), self.name, self.topics, retries=1)
            except (FileNotFoundError, ConnectionRefusedError):
                self.connected.emit(False)
                await asyncio.sleep(1.0)
                continue
            self.connected.emit(True)
            sender = asyncio.create_task(self._send(client))
            while (msg := await client.recv()) is not None:
                self.message.emit(msg)
            sender.cancel()
            self.connected.emit(False)
            await asyncio.sleep(1.0)

    async def _send(self, client: BusClient) -> None:
        while True:
            topic, payload = await self.publish_queue.get()
            await client.publish(topic, payload)
```

- [ ] **Step 8: 实现 `src/tcanet/prototype/ui/common.py`**

```python
"""Shared window chrome: dark theme, status badge, capture-on-request."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from src.tcanet.prototype import config
from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.qtbus import BusBridge


def make_plot(title: str, y_label: str) -> pg.PlotWidget:
    plot = pg.PlotWidget(background=theme.SURFACE)
    plot.setTitle(title, color=theme.INK, size="11pt")
    plot.showGrid(x=False, y=True, alpha=0.25)
    for side in ("left", "bottom"):
        axis = plot.getAxis(side)
        axis.setPen(pg.mkPen(theme.AXIS))
        axis.setTextPen(pg.mkPen(theme.MUTED))
    plot.setLabel("left", y_label, color=theme.MUTED)
    plot.setLabel("bottom", "time (s)", color=theme.MUTED)
    plot.addLegend(offset=(-10, 6), labelTextColor=theme.INK_2, colCount=4)
    return plot


class DemoWindow(QtWidgets.QMainWindow):
    """Base window: bus bridge, connection badge, ``ui.capture`` handling."""

    capture_name = "window"
    topics: tuple[str, ...] = ()

    def __init__(self, title: str) -> None:
        super().__init__()
        self.base_title = title
        self.setWindowTitle(title)
        self.setStyleSheet(f"QMainWindow, QWidget {{ background: {theme.PAGE}; color: {theme.INK}; }}")
        self.badge = QtWidgets.QLabel("connecting…")
        self.badge.setStyleSheet(f"color: {theme.MUTED}; padding: 2px 6px;")
        self.statusBar().addPermanentWidget(self.badge)
        self.bridge = BusBridge(f"ui-{self.capture_name}", self.topics + ("ui.capture",))
        self.bridge.message.connect(self._on_message)
        self.bridge.connected.connect(self._on_connected)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)

    def start(self) -> None:
        self.bridge.start()

    def _on_connected(self, ok: bool) -> None:
        self.badge.setText("● bus connected" if ok else "○ bus disconnected")
        self.badge.setStyleSheet(f"color: {theme.GOOD if ok else theme.CRITICAL}; padding: 2px 6px;")

    def _on_message(self, msg: dict) -> None:
        if msg["topic"] == "ui.capture":
            self.capture(msg["payload"].get("tag", "manual"))
            return
        self.on_message(msg)

    def on_message(self, msg: dict) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        pass

    def capture(self, tag: str) -> str:
        target = config.captures_dir() / tag
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{self.capture_name}.png"
        self.grab().save(str(path))
        return str(path)


def run_window(window_cls) -> None:
    app = QtWidgets.QApplication([])
    pg.setConfigOptions(antialias=True)
    window = window_cls()
    window.show()
    window.start()
    app.exec()
```

- [ ] **Step 9: 实现 `src/tcanet/prototype/ui/flows.py`**

```python
"""Window ②: per-dependency goodput and one-way delay (TransAgent/NetAgent view)."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, make_plot, run_window
from src.tcanet.prototype.ui.model import FlowModel
from src.tcanet.scenario_fig1 import load

WINDOW_S = 60.0
_MARKER_COLOR = {"event": theme.CRITICAL, "rollback": theme.WARNING, "commit": theme.GOOD, "ops": theme.MUTED}


class FlowsWindow(DemoWindow):
    capture_name = "flows"
    topics = ("report.flow", "ctrl.log", "ops.fault")

    def __init__(self) -> None:
        super().__init__("TCANet · TransAgent / NetAgent — task flows")
        _world, task = load("paper_fig1")
        self.deps = [dep.dep_id for dep in task.dag.dependencies]
        self.names = {dep.dep_id: f"{dep.dep_id} {dep.source}→{dep.target}" for dep in task.dag.dependencies}
        self.model = FlowModel(self.deps)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.rx_plot = make_plot("Goodput per dependency", "Mbps")
        self.owd_plot = make_plot("One-way delay per dependency", "ms")
        layout.addWidget(self.rx_plot)
        layout.addWidget(self.owd_plot)
        self.setCentralWidget(central)
        self.curves = {}
        self.labels = {}
        for index, dep in enumerate(self.deps):
            pen = pg.mkPen(theme.series_color(index), width=2)
            for kind, plot in (("rx", self.rx_plot), ("owd", self.owd_plot)):
                self.curves[(dep, kind)] = plot.plot([], [], pen=pen, name=self.names[dep])
                label = pg.TextItem(color=theme.INK_2, anchor=(0, 0.5))
                plot.addItem(label)
                self.labels[(dep, kind)] = label
        self.marker_items: list = []
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def refresh(self) -> None:
        source = "SIMULATION" if self.model.source == "SIM" else self.model.source
        self.setWindowTitle(f"{self.base_title} [{source}]")
        self.rx_plot.setTitle(f"Goodput per dependency  [{source}]", color=theme.INK, size="11pt")
        self.owd_plot.setTitle(f"One-way delay per dependency  [{source}]", color=theme.INK, size="11pt")
        latest = 0.0
        for (dep, kind), curve in self.curves.items():
            xs, ys = self.model.series(dep, kind)
            curve.setData(xs, ys, connect="finite")
            if xs and ys[-1] == ys[-1]:
                latest = max(latest, xs[-1])
                unit = "Mbps" if kind == "rx" else "ms"
                self.labels[(dep, kind)].setText(f"{dep} {ys[-1]:.1f} {unit}")
                self.labels[(dep, kind)].setPos(xs[-1], ys[-1])
        for plot in (self.rx_plot, self.owd_plot):
            plot.setXRange(max(0.0, latest - WINDOW_S), latest + 8.0, padding=0)
        for item in self.marker_items:
            item.getViewBox() and item.getViewBox().removeItem(item)
        self.marker_items = []
        for index, (t, label, kind) in enumerate(self.model.markers):
            if t < latest - WINDOW_S:
                continue
            for plot, show_label in ((self.rx_plot, True), (self.owd_plot, False)):
                line = pg.InfiniteLine(
                    pos=t, angle=90, pen=pg.mkPen(_MARKER_COLOR[kind], width=1, style=pg.QtCore.Qt.DashLine),
                    label=label if show_label else None,
                    labelOpts={"color": theme.INK_2, "position": 0.9 - 0.18 * (index % 4),
                               "rotateAxis": (1, 0)},
                )
                plot.addItem(line)
                self.marker_items.append(line)


def main() -> None:
    run_window(FlowsWindow)


if __name__ == "__main__":
    main()
```

- [ ] **Step 10: 实现 `src/tcanet/prototype/ui/phy.py`**

```python
"""Window ③: PhyAgent access capacity [SIM] and NetAgent link utilization."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, make_plot, run_window
from src.tcanet.prototype.ui.model import AccessModel
from src.tcanet.scenario_fig1 import load

WINDOW_S = 60.0


class PhyWindow(DemoWindow):
    capture_name = "phy"
    topics = ("report.access", "report.link")

    def __init__(self) -> None:
        super().__init__("TCANet · PhyAgent access [SIM] / NetAgent links [MEASURED]")
        world, _task = load("paper_fig1")
        self.gateways = list(world.graph.gateways)
        self.links = sorted(link.link_id for link in world.graph.links)
        self.model = AccessModel(self.gateways, self.links)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.cap_plot = make_plot("Access capacity per gateway — PhyAgent [SIM model]", "Mbps")
        self.util_plot = make_plot("Gateway-link utilization — NetAgent", "utilization")
        self.cap_plot.setYRange(0, 75)
        self.util_plot.setLabel("bottom", "link", color=theme.MUTED)
        self.util_plot.setYRange(0, 1.05)
        self.util_plot.getAxis("bottom").setTicks([[(i, link) for i, link in enumerate(self.links)]])
        layout.addWidget(self.cap_plot)
        layout.addWidget(self.util_plot)
        self.setCentralWidget(central)
        self.curves = {
            gw: self.cap_plot.plot([], [], pen=pg.mkPen(theme.series_color(i), width=2), name=gw)
            for i, gw in enumerate(self.gateways)
        }
        self.bars = pg.BarGraphItem(x=list(range(len(self.links))), height=[0] * len(self.links),
                                    width=0.6, brush=theme.SERIES[0])
        self.util_plot.addItem(self.bars)
        self.down_labels = []
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def refresh(self) -> None:
        latest = 0.0
        for gw, curve in self.curves.items():
            xs, ys = self.model.capacity_series(gw)
            curve.setData(xs, ys)
            if xs:
                latest = max(latest, xs[-1])
        self.cap_plot.setXRange(max(0.0, latest - WINDOW_S), latest + 2.0, padding=0)
        heights = [self.model.utilization[link] if self.model.link_up[link] else 0.0 for link in self.links]
        brushes = [theme.SERIES[0] if self.model.link_up[link] else theme.CRITICAL for link in self.links]
        self.bars.setOpts(height=heights, brushes=brushes)
        for item in self.down_labels:
            self.util_plot.removeItem(item)
        self.down_labels = []
        for index, link in enumerate(self.links):
            text = "DOWN" if not self.model.link_up[link] else f"{heights[index] * 100:.0f}%"
            label = pg.TextItem(text, color=theme.CRITICAL if text == "DOWN" else theme.INK_2, anchor=(0.5, 1))
            label.setPos(index, max(heights[index], 0.0) + 0.04)
            self.util_plot.addItem(label)
            self.down_labels.append(label)


def main() -> None:
    run_window(PhyWindow)


if __name__ == "__main__":
    main()
```

- [ ] **Step 11: 实现 `src/tcanet/prototype/ui/app_dag.py`**

```python
"""Window ④: AppAgent task DAG, per-dependency q^H status, subnet version and Φ."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from src.tcanet.prototype.ui import theme
from src.tcanet.prototype.ui.common import DemoWindow, run_window
from src.tcanet.prototype.ui.model import TaskModel
from src.tcanet.scenario_fig1 import load

# Column/row positions of the Fig. 1 DAG: a1, a2 -> a3 -> a4.
_POS = {"a1": (0, 0), "a2": (0, 2), "a3": (1, 1), "a4": (2, 1)}
_STATUS = {"ok": (theme.GOOD, "q^H met"), "violated": (theme.CRITICAL, "q^H VIOLATED"),
           "inactive": (theme.MUTED, "inactive")}


def _short(agent_id: str | None) -> str:
    """``transport-G1`` -> ``t@G1`` for the binding column."""
    if not agent_id:
        return "—"
    role, _, gateway = agent_id.partition("-")
    return f"{role[0]}@{gateway}"


class DagWindow(DemoWindow):
    capture_name = "app_dag"
    topics = ("ctrl.subnet", "ctrl.log", "report.flow")

    def __init__(self) -> None:
        super().__init__("TCANet · AppAgent — task DAG and subnet state")
        _world, self.task = load("paper_fig1")
        self.model = TaskModel(self.task)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.header = QtWidgets.QLabel()
        self.header.setStyleSheet(f"font-size: 16pt; color: {theme.INK}; padding: 4px;")
        layout.addWidget(self.header)
        self.scene = QtWidgets.QGraphicsScene()
        self.view = QtWidgets.QGraphicsView(self.scene)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing)
        self.view.setStyleSheet(f"background: {theme.SURFACE}; border: none;")
        layout.addWidget(self.view, stretch=3)
        self.table = QtWidgets.QTableWidget(len(self.task.dag.dependencies), 5)
        self.table.setHorizontalHeaderLabels(["dep", "path π", "Φ (t, n, p)", "measured", "status"])
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet(f"background: {theme.SURFACE}; color: {theme.INK_2}; gridline-color: {theme.GRID};")
        layout.addWidget(self.table, stretch=2)
        self.setCentralWidget(central)
        self.resize(900, 640)

    def on_message(self, msg: dict) -> None:
        self.model.apply(msg)

    def _node_point(self, agent_id: str) -> QtCore.QPointF:
        col, row = _POS[agent_id]
        return QtCore.QPointF(40 + col * 260, 30 + row * 90)

    def refresh(self) -> None:
        model = self.model
        state = f"v{model.version}" if model.version else "not formed"
        self.header.setText(f"Task {self.task.dag.task_id} · subnet {state}"
                            + (f" · reconfiguring {', '.join(sorted(model.affected))}" if model.affected else ""))
        self.scene.clear()
        names = {ep.agent_id: ep.name for ep in self.task.dag.endpoints}
        for dep in self.task.dag.dependencies:
            color, label = _STATUS[model.status(dep.dep_id)]
            start, end = self._node_point(dep.source), self._node_point(dep.target)
            width = 4 if dep.dep_id in model.affected else 2
            pen = QtGui.QPen(QtGui.QColor(color), width)
            if dep.dep_id in model.affected:
                pen.setStyle(QtCore.Qt.DashLine)
            self.scene.addLine(start.x() + 70, start.y() + 20, end.x(), end.y() + 20, pen)
            mid = (start + end) / 2
            text = self.scene.addText(f"{dep.dep_id}: {label}")
            text.setDefaultTextColor(QtGui.QColor(theme.INK_2))
            text.setPos(mid.x() + 20, mid.y() - 6)
        for agent_id, name in names.items():
            point = self._node_point(agent_id)
            rect = self.scene.addRect(point.x(), point.y(), 140, 40, QtGui.QPen(QtGui.QColor(theme.AXIS)),
                                      QtGui.QBrush(QtGui.QColor(theme.PAGE)))
            rect.setZValue(1)
            text = self.scene.addText(f"{agent_id}  {name}")
            text.setDefaultTextColor(QtGui.QColor(theme.INK))
            text.setPos(point.x() + 4, point.y() + 8)
            text.setZValue(2)
        for row, dep in enumerate(self.task.dag.dependencies):
            sample = model.latest.get(dep.dep_id)
            measured = "—" if sample is None else (
                f"{sample['rx_mbps']:.1f} Mbps / "
                + ("—" if sample.get("owd_ms") is None else f"{sample['owd_ms']:.1f} ms"))
            color, label = _STATUS[model.status(dep.dep_id)]
            cells = [dep.dep_id, "→".join(model.paths.get(dep.dep_id, [])) or "—",
                     ", ".join(_short(a) for a in model.bindings.get(dep.dep_id, [])) or "—",
                     measured, label]
            for col, value in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(value)
                if col == 4:
                    item.setForeground(QtGui.QColor(color))
                self.table.setItem(row, col, item)


def main() -> None:
    run_window(DagWindow)


if __name__ == "__main__":
    main()
```

- [ ] **Step 12: 实现 `src/tcanet/prototype/ui/console.py`**

```python
"""Terminal views: per-layer agent logs (tmux panes) and the operator banner."""
from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

_COLORS = {"application": "green", "transport": "dark_orange", "network": "dodger_blue1", "physical": "magenta"}
_TITLES = {"application": "AppAgents", "transport": "TransAgents", "network": "NetAgents", "physical": "PhyAgents"}
_LEVEL = {"warn": "bold yellow", "fail": "bold red", "ok": "bold green"}

OPERATOR_BANNER = """[bold]TCANet operator console[/]   (orchestrator input + fault injection)
  tcanetctl submit                 Act 1  form the task subnet
  tcanetctl demand e1 25           Act 2  drone switches to a 25 Mbps stream
  tcanetctl fail gateway G2        Act 3  transit gateway G2 fails
  tcanetctl reset; tcanetctl submit; tcanetctl degrade L3 10; tcanetctl fail gateway G2
                                   Act 4  hidden loss on L3 → Assess fails → Rollback
  tcanetctl kill physical-G4       Act 5  PhyAgent at G4 dies → Φ rebinding
  tcanetctl status | tcanet-demo capture <tag>
  sudo ip netns exec tc-g1 ip rule show      (inspect FT on a gateway)"""


def format_agent_line(payload: dict) -> str:
    color = _COLORS.get(payload["role"], "white")
    style = _LEVEL.get(payload.get("level", "info"), "")
    text = f"[{style}]{payload['text']}[/]" if style else payload["text"]
    return f"[{color}]{payload['agent_id']:>13}[/] {text}"


async def agents_view(layer: str) -> None:
    console = Console(highlight=False)
    console.rule(f"[{_COLORS[layer]}]{_TITLES[layer]}")
    while True:
        try:
            client = await BusClient.connect(config.bus_path(), f"view-{layer}", ("agent.log",), retries=1)
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(1.0)
            continue
        while (msg := await client.recv()) is not None:
            if msg["payload"].get("role") == layer:
                console.print(format_agent_line(msg["payload"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet terminal views")
    sub = parser.add_subparsers(dest="view", required=True)
    sub.add_parser("agents").add_argument("--layer", required=True, choices=sorted(_TITLES))
    sub.add_parser("operator")
    args = parser.parse_args()
    if args.view == "agents":
        asyncio.run(agents_view(args.layer))
    else:
        Console().print(OPERATOR_BANNER)


if __name__ == "__main__":
    main()
```

- [ ] **Step 13: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_ui_models.py tests/tcanet/prototype/test_ui_smoke.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 14: Commit**

```bash
git add src/tcanet/prototype/ui/__init__.py src/tcanet/prototype/ui/theme.py src/tcanet/prototype/ui/model.py src/tcanet/prototype/ui/qtbus.py src/tcanet/prototype/ui/common.py src/tcanet/prototype/ui/flows.py src/tcanet/prototype/ui/phy.py src/tcanet/prototype/ui/app_dag.py src/tcanet/prototype/ui/console.py tests/tcanet/prototype/test_ui_models.py tests/tcanet/prototype/test_ui_smoke.py
git commit -m "feat(prototype): native Qt demo windows and terminal views"
```

---

### Task 14: 启动器、截图、拼图与时间轴图

**Files:**
- Create: `src/tcanet/prototype/launcher.py`
- Create: `src/tcanet/prototype/capture.py`
- Create: `src/tcanet/prototype/figure.py`
- Create: `src/tcanet/prototype/timeline.py`
- Test: `tests/tcanet/prototype/test_launcher_figures.py`

**Interfaces:**
- Consumes: Tasks 11–13
- Produces: `launcher`：`parse_dimensions`、`screen_size(override)`、`layout(w, h) -> {key: (x,y,w,h)}`、`wmctrl_commands(geometry)`、`terminal_argv(title, command)`、`agents_tmux_script(python)`、CLI `tcanet-demo up [--sim] [--no-layout] [--auto-capture] [--screen WxH] | down | status | capture TAG | record start|stop`；`capture.capture(tag) -> list[str]`、`terminal_capture_commands(wmctrl_list, target)`；`figure.compose(captures, photo, out, anchors=None)`、`parse_anchors(text)`；`timeline.load_events(path)`、`build_series(events, start=None, end=None)`、`plot(series, out)`

- [ ] **Step 1: 写失败测试 `tests/tcanet/prototype/test_launcher_figures.py`**

```python
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.tcanet.prototype.capture import terminal_capture_commands
from src.tcanet.prototype.figure import compose, parse_anchors
from src.tcanet.prototype.launcher import (
    agents_tmux_script,
    layout,
    parse_dimensions,
    terminal_argv,
    wmctrl_commands,
)
from src.tcanet.prototype.timeline import build_series, load_events, plot


class LauncherTests(unittest.TestCase):
    def test_grid_layout_below_top_bar(self) -> None:
        geometry = layout(1920, 1080)
        self.assertEqual(geometry["controller"], (0, 32, 640, 524))
        self.assertEqual(geometry["operator"], (1280, 556, 640, 524))
        cmds = wmctrl_commands({"flows": geometry["flows"]})
        self.assertEqual(cmds[1], ["wmctrl", "-r", "task flows", "-e", "0,640,32,640,524"])

    def test_terminal_and_tmux_commands(self) -> None:
        argv = terminal_argv("TCANet Controller", "python -m x")
        self.assertEqual(argv[:2], ["gnome-terminal", "--title=TCANet Controller"])
        self.assertIn("python -m x; exec bash", argv[-1])
        script = agents_tmux_script("/usr/bin/python3")
        for layer in ("application", "transport", "network", "physical"):
            self.assertIn(f"--layer {layer}", script)
        self.assertTrue(script.endswith("tmux attach -t tcanet-agents"))

    def test_parse_dimensions(self) -> None:
        self.assertEqual(parse_dimensions("  dimensions:    2560x1440 pixels (677x381 mm)"), (2560, 1440))
        self.assertIsNone(parse_dimensions("nothing"))

    def test_terminal_capture_commands(self) -> None:
        listing = ("0x04000007  0 host TCANet Controller\n"
                   "0x04200007  0 host Firefox\n"
                   "0x04400007  0 host TCANet Agents")
        cmds = terminal_capture_commands(listing, Path("/tmp/c"))
        self.assertEqual(cmds, [["import", "-window", "0x04000007", "/tmp/c/controller.png"],
                                ["import", "-window", "0x04400007", "/tmp/c/agents.png"]])


class FigureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_compose_with_missing_panels(self) -> None:
        from PIL import Image

        Image.new("RGB", (900, 640), "#1a1a19").save(self.dir / "flows.png")
        out = compose(self.dir, None, self.dir / "fig.png", parse_anchors("0.2,0.4;0.4,0.4;0.6,0.4;0.8,0.4"))
        self.assertTrue(out.exists())
        self.assertEqual(parse_anchors(None), None)

    def test_timeline_from_events(self) -> None:
        events = [{"topic": "report.flow", "ts": 100 + t * 0.5, "src": "a",
                   "payload": {"dep_id": "e1", "rx_mbps": 0.0 if 10 <= t < 14 else 15.0,
                               "owd_ms": None if 10 <= t < 14 else 20.0}} for t in range(30)]
        events += [{"topic": "ops.fault", "ts": 105.0, "src": "ctl", "payload": {"op": "gateway", "target": "G2"}},
                   {"topic": "ctrl.log", "ts": 105.4, "src": "c", "payload": {"step": "event", "title": "Event", "level": "fail", "lines": []}},
                   {"topic": "ctrl.log", "ts": 107.0, "src": "c", "payload": {"step": "commit", "title": "Commit v3", "level": "ok", "lines": []}}]
        path = self.dir / "events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n")
        series = build_series(load_events(path))
        self.assertEqual(len(series["rx"]["e1"]), 30)
        self.assertEqual(len(series["owd"]["e1"]), 26)
        self.assertEqual([kind for _t, _l, kind in series["markers"]], ["ops", "event", "commit"])
        self.assertTrue(plot(series, self.dir / "timeline.png").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest -q tests/tcanet/prototype/test_launcher_figures.py`
Expected: FAIL（`ModuleNotFoundError: ... launcher`）

- [ ] **Step 3: 实现 `src/tcanet/prototype/launcher.py`**

```python
"""``tcanet-demo`` — bring the whole demo up on the Ubuntu desk with one command.

``up`` starts the privileged data plane (``sudo`` + netns, or ``--sim``),
then — as the normal user — the controller terminal, the agent-log tmux
terminal, the operator terminal and the three Qt windows, and tiles them
with ``wmctrl`` (Ubuntu on Xorg).  ``down`` stops everything.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.runtime import ProcessTable

REPO_ROOT = Path(__file__).resolve().parents[3]
TOP_BAR_PX = 32
TMUX_SESSION = "tcanet-agents"

# key -> (window-title substring used by wmctrl, grid column, grid row)
WINDOWS = {
    "controller": ("TCANet Controller", 0, 0),
    "flows": ("task flows", 1, 0),
    "phy": ("PhyAgent access", 2, 0),
    "dag": ("task DAG", 0, 1),
    "agents": ("TCANet Agents", 1, 1),
    "operator": ("TCANet Operator", 2, 1),
}
QT_WINDOWS = {"flows": "src.tcanet.prototype.ui.flows", "phy": "src.tcanet.prototype.ui.phy",
              "dag": "src.tcanet.prototype.ui.app_dag"}


def parse_dimensions(xdpyinfo_output: str) -> tuple[int, int] | None:
    match = re.search(r"dimensions:\s+(\d+)x(\d+)", xdpyinfo_output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def screen_size(override: str | None) -> tuple[int, int]:
    if override:
        width, height = override.lower().split("x")
        return int(width), int(height)
    if shutil.which("xdpyinfo"):
        found = parse_dimensions(subprocess.run(["xdpyinfo"], capture_output=True, text=True).stdout)
        if found:
            return found
    return 1920, 1080


def layout(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    """3×2 grid below the Ubuntu top bar: key -> (x, y, w, h)."""
    cell_w, cell_h = width // 3, (height - TOP_BAR_PX) // 2
    return {key: (col * cell_w, TOP_BAR_PX + row * cell_h, cell_w, cell_h)
            for key, (_title, col, row) in WINDOWS.items()}


def wmctrl_commands(geometry: dict[str, tuple[int, int, int, int]]) -> list[list[str]]:
    cmds = []
    for key, (x, y, w, h) in geometry.items():
        title = WINDOWS[key][0]
        cmds.append(["wmctrl", "-r", title, "-b", "remove,maximized_vert,maximized_horz"])
        cmds.append(["wmctrl", "-r", title, "-e", f"0,{x},{y},{w},{h}"])
    return cmds


def terminal_argv(title: str, command: str) -> list[str]:
    script = f"printf '\\033]0;{title}\\007'; cd {shlex.quote(str(REPO_ROOT))}; {command}; exec bash"
    return ["gnome-terminal", f"--title={title}", "--", "bash", "-lc", script]


def agents_tmux_script(python: str) -> str:
    view = f"{shlex.quote(python)} -m src.tcanet.prototype.ui.console agents --layer"
    return " && ".join([
        f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null; tmux new-session -d -s {TMUX_SESSION} '{view} application'",
        f"tmux split-window -h -t {TMUX_SESSION} '{view} transport'",
        f"tmux split-window -v -t {TMUX_SESSION}:0.0 '{view} network'",
        f"tmux split-window -v -t {TMUX_SESSION}:0.1 '{view} physical'",
        f"tmux select-layout -t {TMUX_SESSION} tiled",
        f"tmux attach -t {TMUX_SESSION}",
    ])


def terminal_specs(python: str, *, auto_capture: bool) -> dict[str, list[str]]:
    controller = f"{shlex.quote(python)} -m src.tcanet.prototype.controller" + (" --auto-capture" if auto_capture else "")
    return {
        "controller": terminal_argv("TCANet Controller", controller),
        "agents": terminal_argv("TCANet Agents", agents_tmux_script(python)),
        "operator": terminal_argv("TCANet Operator", f"{shlex.quote(python)} -m src.tcanet.prototype.ui.console operator"),
    }


def _user_table() -> ProcessTable:
    return ProcessTable(config.run_dir() / "user-pids")


def up(args: argparse.Namespace) -> None:
    python = sys.executable
    sim = args.sim or platform.system() != "Linux"
    env = dict(os.environ)
    if sim:
        subprocess.run([python, "-m", "src.tcanet.prototype.root", "up", "--mode", "sim"], check=True, cwd=REPO_ROOT, env=env)
    else:
        subprocess.run(["sudo", "-E", python, "-m", "src.tcanet.prototype.root", "up", "--mode", "netns"],
                       check=True, cwd=REPO_ROOT, env=env)
    table = _user_table()
    logs = config.logs_dir()
    for key, module in QT_WINDOWS.items():
        table.start(f"ui-{key}", [python, "-m", module], log_path=logs / f"ui-{key}.log", cwd=REPO_ROOT, env=env)
    if shutil.which("gnome-terminal"):
        for key, argv in terminal_specs(python, auto_capture=args.auto_capture).items():
            subprocess.Popen(argv, cwd=REPO_ROOT, env=env, start_new_session=True)
    else:
        table.start("controller", [python, "-m", "src.tcanet.prototype.controller"],
                    log_path=logs / "controller.log", cwd=REPO_ROOT, env=env)
        print("gnome-terminal not found: controller log -> " + str(logs / "controller.log"))
        print(f"agent logs: {python} -m src.tcanet.prototype.ui.console agents --layer network")
    if not args.no_layout and shutil.which("wmctrl"):
        time.sleep(3.0)
        for cmd in wmctrl_commands(layout(*screen_size(args.screen))):
            subprocess.run(cmd, check=False)
    print("demo up — operator commands: tcanetctl submit | demand e1 25 | fail gateway G2 | reset")


def down(_args: argparse.Namespace) -> None:
    _user_table().stop_all()
    subprocess.run(["pkill", "-f", "src.tcanet.prototype.controller"], check=False)
    subprocess.run(["pkill", "-f", "src.tcanet.prototype.ui.console"], check=False)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], check=False, capture_output=True)
    if config.read_mode() == "netns" and platform.system() == "Linux":
        subprocess.run(["sudo", "-E", sys.executable, "-m", "src.tcanet.prototype.root", "down"], cwd=REPO_ROOT, check=False)
    else:
        subprocess.run([sys.executable, "-m", "src.tcanet.prototype.root", "down"], cwd=REPO_ROOT, check=False)


def record(args: argparse.Namespace) -> None:
    table = _user_table()
    if args.action == "stop":
        table.kill("recorder", 2)  # SIGINT lets ffmpeg finalize the file
        return
    width, height = screen_size(args.screen)
    out = config.run_dir() / f"demo-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    table.start("recorder", ["ffmpeg", "-y", "-f", "x11grab", "-framerate", "15", "-video_size",
                             f"{width}x{height}", "-i", os.environ.get("DISPLAY", ":0"), "-c:v", "libx264",
                             "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
                log_path=config.logs_dir() / "recorder.log")
    print(f"recording → {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="tcanet-demo", description="TCANet live prototype demo")
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("up")
    p.add_argument("--sim", action="store_true", help="simulated data plane (no root)")
    p.add_argument("--no-layout", action="store_true")
    p.add_argument("--auto-capture", action="store_true", help="capture windows at formed/fault/recovered")
    p.add_argument("--screen", help="override screen size, e.g. 2560x1440")
    sub.add_parser("down")
    sub.add_parser("status")
    p = sub.add_parser("capture")
    p.add_argument("tag")
    p = sub.add_parser("record")
    p.add_argument("action", choices=["start", "stop"])
    p.add_argument("--screen")
    args = parser.parse_args()
    if args.op == "up":
        up(args)
    elif args.op == "down":
        down(args)
    elif args.op == "status":
        from src.tcanet.prototype.ctl import main as ctl_main
        ctl_main(["status"])
    elif args.op == "capture":
        from src.tcanet.prototype.capture import capture
        print("\n".join(capture(args.tag)))
    elif args.op == "record":
        record(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 实现 `src/tcanet/prototype/capture.py`**

```python
"""Screenshot every demo window into ``<run_dir>/captures/<tag>/``.

Qt windows save themselves on ``ui.capture``; terminal windows are grabbed
by X11 window id (``wmctrl -l`` + ImageMagick ``import``).
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient

TERMINALS = {"TCANet Controller": "controller", "TCANet Agents": "agents", "TCANet Operator": "operator"}


def terminal_capture_commands(wmctrl_list: str, target: Path) -> list[list[str]]:
    """``import`` commands for the terminal windows listed by ``wmctrl -l``."""
    cmds = []
    for line in wmctrl_list.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        window_id, title = parts[0], parts[3]
        for prefix, name in TERMINALS.items():
            if title.startswith(prefix):
                cmds.append(["import", "-window", window_id, str(target / f"{name}.png")])
    return cmds


async def _request_qt_capture(tag: str) -> None:
    client = await BusClient.connect(config.bus_path(), "capture")
    await client.publish("ui.capture", {"tag": tag})
    await asyncio.sleep(0.2)
    await client.close()


def capture(tag: str) -> list[str]:
    target = config.captures_dir() / tag
    target.mkdir(parents=True, exist_ok=True)
    asyncio.run(_request_qt_capture(tag))
    done = [f"Qt windows → {target}"]
    if shutil.which("wmctrl") and shutil.which("import"):
        listing = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        for cmd in terminal_capture_commands(listing, target):
            subprocess.run(cmd, check=False)
            done.append(cmd[-1])
    else:
        done.append("terminal capture skipped (needs wmctrl + imagemagick)")
    return done
```

- [ ] **Step 5: 实现 `src/tcanet/prototype/figure.py`**

```python
"""SANet-style prototype figure: labelled window captures over a desk photo."""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import ConnectionPatch  # noqa: E402
from PIL import Image  # noqa: E402

from src.tcanet.prototype import config  # noqa: E402

PANELS = (("controller", "Agent Controller"), ("app_dag", "AppAgent"),
          ("flows", "TransAgent / NetAgent"), ("phy", "PhyAgent"))


def parse_anchors(text: str | None) -> list[tuple[float, float]] | None:
    """``"x,y;x,y;..."`` in photo fractions (0..1, origin top-left)."""
    if not text:
        return None
    return [tuple(float(v) for v in pair.split(",")) for pair in text.split(";")]


def compose(captures: Path, photo: Path | None, out: Path,
            anchors: list[tuple[float, float]] | None = None) -> Path:
    fig = plt.figure(figsize=(12, 7.2))
    grid = fig.add_gridspec(2, len(PANELS), height_ratios=(1.0, 1.25), hspace=0.18, wspace=0.06)
    panel_axes = []
    for index, (name, label) in enumerate(PANELS):
        ax = fig.add_subplot(grid[0, index])
        path = Path(captures) / f"{name}.png"
        if path.exists():
            ax.imshow(Image.open(path))
        else:
            ax.text(0.5, 0.5, f"missing\n{path.name}", ha="center", va="center", color="#898781")
        ax.set_title(label, fontsize=11, fontweight="bold")
        ax.set_xticks([])
        ax.set_yticks([])
        panel_axes.append(ax)
    photo_ax = fig.add_subplot(grid[1, :])
    photo_ax.set_xticks([])
    photo_ax.set_yticks([])
    if photo is not None and Path(photo).exists():
        photo_ax.imshow(Image.open(photo))
    else:
        photo_ax.text(0.5, 0.5, "desk photo (Ubuntu host running the prototype)",
                      ha="center", va="center", color="#898781", transform=photo_ax.transAxes)
    anchors = anchors or [((i + 0.5) / len(PANELS), 0.3) for i in range(len(PANELS))]
    for ax, (x, y) in zip(panel_axes, anchors):
        fig.add_artist(ConnectionPatch(
            xyA=(0.5, 0.0), coordsA=ax.transAxes, xyB=(x, 1.0 - y), coordsB=photo_ax.transAxes,
            arrowstyle="-|>", color="#2a78d6", linewidth=1.4))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="SANet-style TCANet prototype figure")
    parser.add_argument("--captures", default=str(config.captures_dir() / "recovered"))
    parser.add_argument("--photo")
    parser.add_argument("--anchors", help='photo points per panel, e.g. "0.2,0.4;0.4,0.4;0.6,0.4;0.8,0.4"')
    parser.add_argument("--out", default=str(config.run_dir() / "prototype_figure.pdf"))
    args = parser.parse_args()
    print(compose(Path(args.captures), Path(args.photo) if args.photo else None, Path(args.out),
                  parse_anchors(args.anchors)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 实现 `src/tcanet/prototype/timeline.py`**

```python
"""Recovery timeline figure from a run's ``events.jsonl`` (paper style, light).

Two stacked panels on one time axis — goodput and one-way delay per
dependency — with vertical markers for operator faults, controller event
detection, rollbacks and commits.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from src.tcanet.prototype import config  # noqa: E402

SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")
MARKERS = {"ops": ("#898781", ":"), "event": ("#d03b3b", "--"), "rollback": ("#c98500", "--"),
           "commit": ("#0ca30c", "-")}


def load_events(path: Path) -> list[dict]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def build_series(events: list[dict], start: float | None = None, end: float | None = None) -> dict:
    flows = [e for e in events if e["topic"] == "report.flow"]
    t0 = start if start is not None else (flows[0]["ts"] if flows else 0.0)
    rx: dict[str, list[tuple[float, float]]] = {}
    owd: dict[str, list[tuple[float, float]]] = {}
    markers: list[tuple[float, str, str]] = []
    for event in events:
        ts = float(event["ts"])
        if ts < t0 or (end is not None and ts > end):
            continue
        t, p = ts - t0, event["payload"]
        if event["topic"] == "report.flow":
            rx.setdefault(p["dep_id"], []).append((t, p["rx_mbps"]))
            if p.get("owd_ms") is not None:
                owd.setdefault(p["dep_id"], []).append((t, p["owd_ms"]))
        elif event["topic"] == "ops.fault" and p["op"] in ("gateway", "link", "degrade", "kill", "demand"):
            markers.append((t, f"{p['op']} {p.get('target', p.get('dep_id', ''))}", "ops"))
        elif event["topic"] == "ctrl.log" and p["step"] == "event":
            markers.append((t, "detected", "event"))
        elif event["topic"] == "ctrl.log" and p["step"] == "rollback" and p["title"].startswith("Rollback"):
            markers.append((t, "rollback", "rollback"))
        elif event["topic"] == "ctrl.log" and p["step"] == "commit" and p["level"] == "ok":
            markers.append((t, p["title"].replace("Commit ", "commit "), "commit"))
    return {"rx": rx, "owd": owd, "markers": markers}


def _with_gaps(data: list[tuple[float, float]], gap_s: float = 1.5) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for t, v in data:
        if xs and t - xs[-1] > gap_s:
            xs.append(xs[-1])
            ys.append(float("nan"))
        xs.append(t)
        ys.append(v)
    return xs, ys


def plot(series: dict, out: Path) -> Path:
    fig, (ax_rx, ax_owd) = plt.subplots(2, 1, figsize=(7.2, 4.6), sharex=True)
    for index, dep in enumerate(sorted(series["rx"])):
        color = SERIES_LIGHT[index % len(SERIES_LIGHT)]
        for ax, data in ((ax_rx, series["rx"].get(dep, [])), (ax_owd, series["owd"].get(dep, []))):
            if data:
                xs, ys = _with_gaps(data)
                ax.plot(xs, ys, color=color, linewidth=1.6, label=dep)
                ax.annotate(dep, (xs[-1], ys[-1]), xytext=(4, 0), textcoords="offset points",
                            color="#52514e", fontsize=8, va="center")
    for index, (t, label, kind) in enumerate(series["markers"]):
        color, style = MARKERS[kind]
        for ax in (ax_rx, ax_owd):
            ax.axvline(t, color=color, linestyle=style, linewidth=1.0)
        ax_rx.annotate(label, (t, 1.0 - 0.2 * (index % 3)), xycoords=("data", "axes fraction"),
                       rotation=90, fontsize=7, color="#52514e", va="top", ha="right")
    ax_rx.set_ylabel("Goodput (Mbps)")
    ax_owd.set_ylabel("One-way delay (ms)")
    ax_owd.set_xlabel("Time (s)")
    for ax in (ax_rx, ax_owd):
        ax.grid(axis="y", color="#e1e0d9", linewidth=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    handles, labels = ax_rx.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, fontsize=8, frameon=False,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200)
    plt.close(fig)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="TCANet recovery timeline figure")
    parser.add_argument("--events", default=str(config.events_path()))
    parser.add_argument("--out", default=str(config.run_dir() / "timeline.pdf"))
    parser.add_argument("--start", type=float, help="epoch seconds to start from")
    parser.add_argument("--end", type=float)
    args = parser.parse_args()
    out = plot(build_series(load_events(Path(args.events)), args.start, args.end), Path(args.out))
    print(out)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 运行测试**

Run: `python -m pytest -q tests/tcanet/prototype/test_launcher_figures.py`
Expected: PASS

然后 `python -m pytest -q tests/tcanet`，全部通过。

- [ ] **Step 8: Commit**

```bash
git add src/tcanet/prototype/launcher.py src/tcanet/prototype/capture.py src/tcanet/prototype/figure.py src/tcanet/prototype/timeline.py tests/tcanet/prototype/test_launcher_figures.py
git commit -m "feat(prototype): tcanet-demo launcher, captures, figure and timeline"
```

---

### Task 15: 打包、彩排文档、内核集成测试与规格修订

**Files:**
- Modify: `pyproject.toml`
- Create: `docs/prototype-rehearsal.md`
- Test: `tests/tcanet/prototype/test_netns_integration.py`
- Modify: `docs/superpowers/specs/2026-10-06-tcanet-prototype-demo-design.md`（追加 §10）

**Interfaces:**
- Produces: `pip install -e '.[demo]'` 提供 `tcanet-demo`、`tcanetctl` 两个命令。

- [ ] **Step 1: 写内核集成测试 `tests/tcanet/prototype/test_netns_integration.py`**

```python
"""Real kernel data plane: FT rules forward a measured flow (Linux + root only).

Run on the Ubuntu demo host:
    sudo -E .venv/bin/python -m pytest -q tests/tcanet/prototype/test_netns_integration.py
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from src.tcanet.prototype.addressing import Cmd, build_plan, gateway_ns, in_ns, rule_commands, rule_params
from src.tcanet.prototype.topology import build_commands, run_commands, teardown_commands
from src.tcanet.scenario import run_formation
from src.tcanet.scenario_fig1 import ACCESS_CAPACITY_MBPS, load

REPO_ROOT = Path(__file__).resolve().parents[3]
_SINK = (
    "import asyncio,sys\n"
    "from src.tcanet.prototype.flow import FlowSink\n"
    "async def main():\n"
    "    stop=asyncio.Event()\n"
    "    def show(s): print(round(s['rx_mbps'],2), flush=True)\n"
    "    task=asyncio.create_task(FlowSink(47000, show, period_s=0.5).run(stop))\n"
    "    await asyncio.sleep(float(sys.argv[1])); stop.set(); await task\n"
    "asyncio.run(main())\n"
)


def _runnable() -> bool:
    return (platform.system() == "Linux" and os.geteuid() == 0
            and shutil.which("ip") is not None and shutil.which("tc") is not None)


@unittest.skipUnless(_runnable(), "needs Linux, root, iproute2")
class NetnsDataPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        existing = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
        if "tc-g1" in existing:
            self.skipTest("a prototype topology is running; run `tcanet-demo down` first")
        self.world, self.task = load("paper_fig1")
        self.plan = build_plan(self.world)
        run_commands(build_commands(self.world, self.plan, ACCESS_CAPACITY_MBPS))

    def tearDown(self) -> None:
        run_commands(teardown_commands(self.world, self.plan))

    def _install_e1(self) -> None:
        subnet = asyncio.run(run_formation(self.task, self.world)).subnet
        for rule_id, entry in subnet.forwarding.items():
            if entry.dep_id != "e1":
                continue
            params = rule_params(entry, self.task, self.plan)
            run_commands([in_ns(gateway_ns(entry.gateway_id), cmd) for cmd in rule_commands(params, "add")])

    def _measure(self, seconds: float = 3.0) -> list[float]:
        sink = subprocess.Popen(["ip", "netns", "exec", "tc-a3", sys.executable, "-c", _SINK, str(seconds)],
                                cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True)
        time.sleep(0.3)
        sender = subprocess.Popen(["ip", "netns", "exec", "tc-a1", sys.executable, "-m",
                                   "src.tcanet.prototype.background", "send", "--dst", "10.1.3.2",
                                   "--port", "47000", "--mbps", "10"], cwd=REPO_ROOT)
        out, _ = sink.communicate(timeout=seconds + 5)
        sender.terminate()
        sender.wait(timeout=5)
        return [float(line) for line in out.split()]

    def test_flow_needs_ft_rules(self) -> None:
        self.assertLess(max(self._measure(2.0) or [0.0]), 0.5)  # no FT -> no forwarding
        self._install_e1()
        rates = self._measure(3.0)
        self.assertGreater(max(rates), 7.0, rates)
        rules = subprocess.run(["ip", "-n", "tc-g1", "rule", "show"], capture_output=True, text=True).stdout
        self.assertIn("lookup 100", rules)

    def test_gateway_down_stops_the_flow(self) -> None:
        self._install_e1()
        run_commands([in_ns("tc-g2", Cmd(("ip", "link", "set", "l1", "down")))])
        self.assertLess(max(self._measure(2.0) or [0.0]), 0.5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 本机运行（非 Linux/非 root 时应为 skipped）**

Run: `python -m pytest -q tests/tcanet/prototype/test_netns_integration.py`
Expected: `2 skipped`（Ubuntu 上 `sudo -E .venv/bin/python -m pytest -q tests/tcanet/prototype/test_netns_integration.py` 应 `2 passed`）

- [ ] **Step 3: 修改 `pyproject.toml`**

在 `[project.optional-dependencies]` 中加入：

```toml
demo = [
  "PySide6>=6.5",
  "pyqtgraph>=0.13",
  "rich>=13",
  "matplotlib>=3.8",
  "numpy>=1.23",
  "Pillow>=10.0",
]
```

并新增：

```toml
[project.scripts]
tcanet-demo = "src.tcanet.prototype.launcher:main"
tcanetctl = "src.tcanet.prototype.ctl:main"
```

- [ ] **Step 4: 新增 `docs/prototype-rehearsal.md`**

````markdown
# TCANet 原型现场演示 — Ubuntu 彩排清单

## 0. 一次性准备（演示机）

1. 登录界面右下角齿轮选择 **Ubuntu on Xorg**（Wayland 下 `wmctrl` 无法摆放窗口）。
2. 系统包：

```bash
sudo apt install -y iproute2 tmux wmctrl imagemagick x11-utils ffmpeg tcpdump python3-venv
```

3. Python 环境（仓库根目录）：

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[demo,dev]'
```

4. 自检：

```bash
.venv/bin/python -m pytest -q tests/tcanet
sudo -E .venv/bin/python -m pytest -q tests/tcanet/prototype/test_netns_integration.py
```

两条都必须全绿；第二条验证内核策略路由真正转发任务流量。

## 1. 开场

```bash
source .venv/bin/activate
tcanet-demo up --auto-capture
```

输入 sudo 密码后屏幕自动排成 3×2：①控制器 ②流量曲线 ③PhyAgent/链路 ④任务 DAG ⑤各层 Agent 日志 ⑥操作终端。
可先 `tcanet-demo record start` 全程录屏备份。

## 2. 五幕（在 ⑥ 操作终端执行）

| 幕 | 命令 | 讲解要点 |
|---|---|---|
| 1 组网 | `tcanetctl submit` | ①：E^aff = 全部依赖 → FT 下发为 `ip rule` → Φ 绑定 → Assess 实测 → Commit v1；②三条流量出现 |
| 2 跨层冲突 | `tcanetctl demand e1 25` | AppAgent 提速（任务更新）；①：拒绝的组合含 `shared_resource:link:L1`（25×1.2=30>28），J* 后按 M 选择 |
| 3 网关故障 | `tcanetctl fail gateway G2` | ③ L1/L2 DOWN；①：NetAgent 上报 → `gateway failure: G2` → E^aff={e1,e2}，**e3 retained unchanged** → J 相同、M=4 胜 M=5 → G1→G3→G4；② e3 全程平稳 |
| 4 回滚 | `tcanetctl reset` → `tcanetctl submit` → `tcanetctl degrade L3 10` → `tcanetctl fail gateway G2` | 控制器不知道 L3 丢包；首选经 L3 的方案 Assess 实测 `loss_hard:e1` → Rollback → 改走 L6 |
| 5 支撑代理失效 | `tcanetctl kill physical-G4` | 心跳超时 → support failure → 只重绑 e3 的 PhyAgent（M=1，路径不变） |

## 3. 老师追问时的现场验证

```bash
ps -ef | grep prototype.agents | grep -v grep
sudo ip netns exec tc-g1 ip rule show
sudo ip netns exec tc-g1 ip route show table 100
sudo ip netns exec tc-g1 tc -s qdisc show dev l1
sudo ip netns exec tc-g3 tcpdump -ni l5 udp -c 5
tcanetctl status
```

## 4. 产出演示图

```bash
tcanet-demo capture recovered
python -m src.tcanet.prototype.figure --captures /tmp/tcanet-demo/captures/recovered --photo desk.jpg --anchors "0.15,0.45;0.38,0.45;0.62,0.45;0.85,0.45" --out tcanet_prototype.pdf
python -m src.tcanet.prototype.timeline --out tcanet_timeline.pdf
```

`--anchors` 是照片上四块屏幕区域的位置（照片宽高的比例，原点左上）。

## 5. 收场与回退

```bash
tcanet-demo record stop
tcanet-demo down
```

若现场网络命名空间异常：`tcanet-demo down` 后 `tcanet-demo up --sim`，窗口与剧本不变，所有窗口标注 SIMULATION。

## 6. 常见问题

- 窗口没自动排列：确认是 Xorg 会话（`echo $XDG_SESSION_TYPE` 应为 `x11`），或 `tcanet-demo up --screen 2560x1440`。
- `bus did not start`：看 `/tmp/tcanet-demo/logs/bus.log`；上次未清理时先 `tcanet-demo down`。
- 某个 Agent 死了：`tcanetctl revive <agent-id>`；日志在 `/tmp/tcanet-demo/logs/<agent-id>.log`。
````

- [ ] **Step 5: 在设计文档末尾追加 §10 实现阶段修订**

```markdown
## 10. 实现阶段修订（2026-10-07）

1. 接入资源按上/下行建模（`World.access_model="duplex"`）：e1/e2 用 G4 下行，e3 用 G4 上行，G2 故障时 e2 经「共享 G4 下行接入」被拉入 E^aff，e3 在范围外。
2. 新增 L6（G1→G4，30 Mbps/20 ms）与 `World.min_path_alternates=2`，回滚后仍有替代路径；第 4 幕隐藏劣化注入 **L3**（首选方案经 L3）。
3. 测量改用自写的序号+时间戳 UDP 流（同机时钟 → 真实单向时延；断流期间持续报 0），iperf3/ping/tcpdump 保留为现场人工核验工具。
4. 进程模型与 World 目录一致：每网关 Trans/Net/Phy 各一个 + 4 个 AppAgent，共 16 个。
5. 权限拆分：`root.py` 与 `tcanetctl` 内核操作用 sudo；控制器与窗口以普通用户运行；演示机使用 Xorg 会话。
6. 时间参数：Assess 窗口 1.0 s（稳定期 0.3 s），检测去抖 0.7 s，心跳 0.5 s/超时 1.5 s。
7. FT 内核状态以 (网关, 依赖) 为键：同一网关上的替换不再先装后删，回滚恢复该网关原有条目；故障网关上的撤销/恢复不等待 ack。
8. 第 2 幕实测选择为 AppAgent 降码率至 17.5 Mbps（L1 利用率软目标），同时列出被拒组合（含 25×1.2>28）。
```

- [ ] **Step 6: 全量测试**

Run: `python -m pytest -q tests/tcanet`
Expected: 全部通过（未装 `[demo]` 时 Qt 冒烟测试 skip、netns 集成测试 skip）。装好 `[demo]` 后再跑一次，Qt 冒烟测试应 PASS。

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml docs/prototype-rehearsal.md tests/tcanet/prototype/test_netns_integration.py docs/superpowers/specs/2026-10-06-tcanet-prototype-demo-design.md
git commit -m "docs(prototype): packaging, rehearsal checklist and kernel integration test"
```

---

### Task 16: Ubuntu 彩排验收（人工，需演示机）

- [ ] **Step 1:** 按 `docs/prototype-rehearsal.md` §0 完成准备，两条自检全绿。
- [ ] **Step 2:** `tcanet-demo up --auto-capture`，依次执行五幕，核对每幕「讲解要点」列出的现象全部出现。
- [ ] **Step 3:** `tcanet-demo capture recovered`，生成 `figure` 与 `timeline`，检查图中无标签重叠、数据来源标注为 MEASURED（PhyAgent 为 SIM）。
- [ ] **Step 4:** `tcanet-demo down`，确认 `ip netns list` 无 `tc-*`、`pgrep -f src.tcanet.prototype` 无输出。
