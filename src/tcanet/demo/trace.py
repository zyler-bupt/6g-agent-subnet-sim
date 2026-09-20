"""Web 演示 trace 生成（与 viz 前端同 schema，与 CLI 同一核心）。

场景（scenario key → 幕次）：

* ``tcanet_formation``    — 任务 DAG → 端点确认 → Φ 绑定 → π 路径 → FT
                             编译 → 安装 → W_m → 验收 v1
* ``tcanet_coordination`` — 需求-容量失配：式 3 联合可行性（25×1.2>28）→
                             两阶段选择 → v2
* ``tcanet_recovery``     — 网关失效：E^aff,0 → 闭包逐轮扩张动画 → 三类
                             候选 → W_m → 验收 v3（含全量重建对比）
* ``tcanet_rebind``       — 支撑 agent 失效：重绑定捆绑 → B_r 首次被拒 →
                             排除重试 → 验收 v4

步骤 schema 与 ``viz/trace.py`` 完全一致（nodes/edges/steps + active/
focus/failed/changed + risk/threshold/qos/badges/compare），前端零改动。
"""
from __future__ import annotations

import asyncio

from src.tcanet.metrics import modification_stats
from src.tcanet.scenario import (
    E1_DEMAND_HIGH,
    build_world,
    rescue_task,
    run_coordination,
    run_formation,
    run_gateway_recovery,
    run_support_recovery,
    with_demand,
)
from src.tcanet.spec import Layer
from src.tcanet.subnet import SubnetState

TCANET_SCENARIOS = {
    "tcanet_formation": {
        "name": "TCANet · 子网构建（论文 Sec. IV-A）",
        "trigger": "上层确认 T_m",
        "expect": "端点确认→绑定→路径→转发表→W_m→v1",
    },
    "tcanet_coordination": {
        "name": "TCANet · 跨层联合决策（式 3-7）",
        "trigger": "e1 需求 15→25 Mbps",
        "expect": "25×1.2=30>28 联合不可行 → 传输模式切换 v2",
    },
    "tcanet_recovery": {
        "name": "TCANet · 弹性重构（式 8 + W_m）",
        "trigger": "中转网关 G2 失效",
        "expect": "闭包逐轮扩张 → 路径替换 → 验收 v3",
    },
    "tcanet_rebind": {
        "name": "TCANet · 支撑失效与 B_r 重试（Sec. IV-C）",
        "trigger": "支撑 agent physical-G4 失效",
        "expect": "重绑定捆绑 → 首次验证被拒 → 重试 v4",
    },
}

_APP_CN = {
    "drone": "无人机采集",
    "detect": "目标检测",
    "assess": "多模态评估",
    "dispatch": "救援调度",
}
_GW_X = {"G1": 140, "G2": 380, "G3": 620, "G4": 860}
_GW_Y = 160
_APP_Y = 260
_LAYER_Y = {Layer.TRANSPORT: 360, Layer.NETWORK: 468, Layer.PHYSICAL: 576}
_LAYER_KIND = {Layer.TRANSPORT: "trans", Layer.NETWORK: "net",
               Layer.PHYSICAL: "phy"}
_LAYER_SHORT = {Layer.TRANSPORT: "tAgent", Layer.NETWORK: "nAgent",
                Layer.PHYSICAL: "pAgent"}


def _edge_id(*parts: str) -> str:
    return "||".join(parts)


def _static_nodes(support_agents, bound_ids) -> list[dict]:
    nodes = [
        {
            "id": "controller",
            "label": "TCANet 控制器 · 任务级跨层协调",
            "short": "TCANet 控制器",
            "kind": "controller",
            "x": 500,
            "y": 70,
        }
    ]
    for gid, x in _GW_X.items():
        nodes.append(
            {
                "id": gid,
                "label": f"{gid} · 网关（转发/执行）",
                "short": gid,
                "kind": "gateway",
                "x": x,
                "y": _GW_Y,
            }
        )
    app_offsets = {"G4": [-55, 55]}
    counters: dict[str, int] = {}
    for agent_id, cn in _APP_CN.items():
        gw = _app_gateway(agent_id)
        slot = counters.get(gw, 0)
        counters[gw] = slot + 1
        offsets = app_offsets.get(gw, [0])
        offset = offsets[min(slot, len(offsets) - 1)]
        nodes.append(
            {
                "id": agent_id,
                "label": f"{cn}（{agent_id}）",
                "short": cn,
                "kind": "app",
                "gateway": gw,
                "x": _GW_X[gw] + offset,
                "y": _APP_Y,
            }
        )
    for agent_id, agent in support_agents.items():
        nodes.append(
            {
                "id": agent_id,
                "label": f"{_LAYER_SHORT[agent.layer]}@{agent.gateway_id}",
                "short": f"{_LAYER_SHORT[agent.layer]}·{agent.gateway_id}",
                "kind": _LAYER_KIND[agent.layer],
                "gateway": agent.gateway_id,
                "standby": agent_id not in bound_ids,
                "x": _GW_X[agent.gateway_id],
                "y": _LAYER_Y[agent.layer],
            }
        )
    return nodes


def _app_gateway(agent_id: str) -> str:
    return {
        "drone": "G1",
        "detect": "G4",
        "assess": "G4",
        "dispatch": "G3",
    }[agent_id]


def _path_edge_ids(subnet: SubnetState) -> list[str]:
    ids = []
    for dep_id, record in sorted(subnet.paths.items()):
        path = record.gateway_path
        for i in range(len(path) - 1):
            ids.append(_edge_id("path", dep_id, path[i], path[i + 1]))
    return ids


def _binding_edge_ids(subnet: SubnetState) -> list[str]:
    ids = []
    for dep_id, binding in sorted(subnet.bindings.records.items()):
        for role, agent_id in (
            ("t", binding.t_agent_id),
            ("n", binding.n_agent_id),
            ("p", binding.p_agent_id),
        ):
            if agent_id:
                ids.append(_edge_id("bind", dep_id, role, agent_id))
    return ids


def _biz_edge_ids(task) -> list[str]:
    return [_edge_id("biz", dep.dep_id) for dep in task.dag.dependencies]


def _control_edge_ids() -> list[str]:
    return [_edge_id("ctl", gw) for gw in _GW_X]


def _biz_edges(task) -> list[dict]:
    edges = []
    for dep in task.dag.dependencies:
        edges.append(
            {
                "id": _edge_id("biz", dep.dep_id),
                "source": dep.source,
                "target": dep.target,
                "kind": "biz",
            }
        )
    return edges


def _path_edges(subnets) -> list[dict]:
    edges = {}
    for subnet in subnets:
        for dep_id, record in subnet.paths.items():
            path = record.gateway_path
            for i in range(len(path) - 1):
                eid = _edge_id("path", dep_id, path[i], path[i + 1])
                edges[eid] = {
                    "id": eid,
                    "source": path[i],
                    "target": path[i + 1],
                    "kind": "path",
                }
    return list(edges.values())


def _binding_edges(subnets) -> list[dict]:
    edges = {}
    for subnet in subnets:
        for dep_id, binding in subnet.bindings.records.items():
            for role, agent_id in (
                ("t", binding.t_agent_id),
                ("n", binding.n_agent_id),
                ("p", binding.p_agent_id),
            ):
                if not agent_id:
                    continue
                eid = _edge_id("bind", dep_id, role, agent_id)
                edges[eid] = {
                    "id": eid,
                    "source": _dep_anchor(dep_id),
                    "target": agent_id,
                    "kind": "bind",
                }
    return list(edges.values())


def _dep_anchor(dep_id: str) -> str:
    """Binding edges hang off the dependency's source endpoint agent."""
    return _DEP_SOURCE[dep_id]


_DEP_SOURCE = {"e1": "drone", "e2": "detect", "e3": "assess", "e4": "detect"}


class _Trace:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        # Edges a step wants to highlight even though no accepted subnet
        # ever contained them (e.g. the B_r first attempt's rebinds).
        self.extra_edges: dict[str, dict] = {}

    def add(self, kind: str, title: str, detail: str, **kw) -> None:
        self.steps.append(
            {
                "id": len(self.steps),
                "kind": kind,
                "title": title,
                "detail": detail,
                "active_nodes": sorted(set(kw.get("active_nodes", ()))),
                "active_edges": sorted(set(kw.get("active_edges", ()))),
                "focus_nodes": sorted(set(kw.get("focus_nodes", ()))),
                "focus_edges": sorted(set(kw.get("focus_edges", ()))),
                "failed_nodes": sorted(set(kw.get("failed_nodes", ()))),
                "changed_nodes": sorted(set(kw.get("changed_nodes", ()))),
                "changed_edges": sorted(set(kw.get("changed_edges", ()))),
                "risk": kw.get("risk"),
                "threshold": kw.get("threshold"),
                "qos": kw.get("qos"),
                "badges": kw.get("badges") or [],
                "compare": kw.get("compare"),
            }
        )


class _Episode:
    """Runs the full scenario chain once and keeps every intermediate."""

    def __init__(self) -> None:
        self.world = build_world()
        self.task = rescue_task(self.world)
        self.formation = asyncio.run(run_formation(self.task, self.world))
        self.task_high = with_demand(self.task, "e1", E1_DEMAND_HIGH)
        self.coord = run_coordination(
            self.task_high, self.world, self.formation.subnet
        )
        self.gateway = run_gateway_recovery(
            self.task_high, self.world, self.coord.subnet
        )
        self.support = run_support_recovery(
            self.task_high, self.world, self.gateway.subnet
        )

    @property
    def subnets(self):
        return [
            self.formation.subnet,
            self.coord.subnet,
            self.gateway.subnet,
            self.support.subnet,
        ]

    def all_bound_ids(self) -> set[str]:
        bound: set[str] = set()
        for subnet in self.subnets:
            for binding in subnet.bindings.records.values():
                bound.update(a for a in binding.as_tuple() if a)
        return bound


def _baseline_state(episode: _Episode, subnet: SubnetState, task):
    return {
        "active_nodes": ["controller", *_GW_X, *_APP_CN],
        "active_edges": [
            *_control_edge_ids(),
            *_biz_edge_ids(task),
            *_path_edge_ids(subnet),
            *_binding_edge_ids(subnet),
        ],
    }


def _formation_steps(episode: _Episode, trace: _Trace) -> None:
    formation = episode.formation
    subnet = formation.subnet
    task = episode.task
    base = _baseline_state(episode, subnet, task)

    trace.add(
        "input",
        "① 任务输入 T_m = ⟨m, G_task, r_m, q^H, q^S⟩",
        f"救援任务 DAG：{len(task.dag.dependencies)} 条业务依赖，"
        f"q^H：吞吐≥{task.hard.min_throughput_mbps:g}Mbps / "
        f"时延≤{task.hard.max_delay_ms:g}ms / "
        f"丢包≤{task.hard.max_loss_rate:g}。",
        active_nodes=["controller", *_APP_CN],
        active_edges=_biz_edge_ids(task),
        focus_nodes=list(_APP_CN),
        focus_edges=_biz_edge_ids(task),
        badges=[["任务", task.dag.task_id], ["依赖", f"{len(task.dag.dependencies)} 条"],
                ["端点", f"{len(task.dag.endpoints)} 个"]],
    )
    trace.add(
        "member_confirm",
        "② 端点确认（网关本地成员 ACK）",
        "控制器按 η(a) 定位各端点所在网关，4 个端点全部在线 ACK；"
        "（若端点失效，需上层 orchestrator 重新授权替代端点。）",
        active_nodes=["controller", *_GW_X, *_APP_CN],
        active_edges=[*_control_edge_ids(), *_biz_edge_ids(task)],
        focus_nodes=[*_GW_X],
        focus_edges=_control_edge_ids(),
        badges=[["端点确认", "4/4 ACK"]],
    )
    trace.add(
        "binding",
        "③ 支撑 agent 绑定 Φ_m(e)=⟨t,n,p⟩",
        "每条依赖就近绑定 t/n/p 支撑 agent（条目可空，不引入数据面跳点）；"
        "e2 两端同驻 G4，t/n 留空。",
        active_nodes=["controller", *_GW_X, *_APP_CN],
        active_edges=[*_control_edge_ids(), *_biz_edge_ids(task),
                      *_binding_edge_ids(subnet)],
        focus_edges=_binding_edge_ids(subnet),
        badges=[["绑定记录", f"{len(subnet.bindings.records)} 条"]],
    )
    trace.add(
        "map",
        "④ 依赖 → 网关路径 π_m,e（CSPF）",
        "按最小时延求解路径并顺序保留共享资源：e1 走 G1→G2→G4，"
        "e2 本地交付，e3/e4 走 G4→G3。",
        active_nodes=["controller", *_GW_X, *_APP_CN],
        active_edges=[*_control_edge_ids(), *_biz_edge_ids(task),
                      *_path_edge_ids(subnet), *_binding_edge_ids(subnet)],
        focus_edges=_path_edge_ids(subnet),
        focus_nodes=[*_GW_X],
        badges=[["路径", f"{len(subnet.paths)} 条"],
                ["e1", "G1→G2→G4"]],
    )
    trace.add(
        "forwarding",
        "⑤ 转发表 FT^g_m 编译 + 执行计划 C+_m",
        f"编译 {len(subnet.forwarding)} 条网关转发规则，生成 "
        f"{len(formation.plan.actions)} 个可执行动作（install/bind），"
        "目标版本 v+1 = 1；执行前条件一致性复查通过。",
        active_nodes=["controller", *_GW_X, *_APP_CN],
        active_edges=[*_control_edge_ids(), *_biz_edge_ids(task),
                      *_path_edge_ids(subnet), *_binding_edge_ids(subnet)],
        focus_nodes=[*_GW_X],
        badges=[["转发规则", f"{len(subnet.forwarding)} 条"],
                ["可执行动作", f"{len(formation.plan.actions)} 个"]],
    )
    trace.add(
        "verify",
        "⑥ 验证窗口 W_m：安装 + 实测",
        "安装成功 → 旧状态无（首建）→ 4 条依赖观测到达 → 实测满足 q^H。",
        **base,
        qos=True,
        badges=[["安装", f"{len(formation.execution.outcomes)} 动作 ✓"],
                ["观测", "4/4 到达"]],
    )
    trace.add(
        "recovered",
        "⑦ 验收 → 版本推进 v1",
        f"窗口接受，S_m^(v1) 生效；形成时延 "
        f"{formation.formation_latency_ms:.1f} ms（确认 → 初始验收）。",
        **base,
        qos=True,
        badges=[["版本", "v0 → v1"],
                ["形成时延", f"{formation.formation_latency_ms:.1f} ms"],
                ["安装记录", "16 条"]],
    )


def _coordination_steps(episode: _Episode, trace: _Trace) -> None:
    coord = episode.coord
    before = episode.formation.subnet
    task = episode.task_high
    base = _baseline_state(episode, before, task)

    trace.add(
        "baseline",
        "① 运行态 S_m^(v1)",
        "v1 子网运行中：e1（15 Mbps）走 G1→G2→G4；link:L1 可用 28 Mbps"
        "（40 容量 − 12 保护负载），当前利用率 15/28 = 0.54。",
        **base,
        badges=[["版本", "v1"], ["e1 需求", "15 Mbps"],
                ["L1 利用率", "0.54"]],
    )
    trace.add(
        "event",
        "② 运行时事件：e1 需求 15 → 25 Mbps",
        "无人机升级高分辨率视频流。若叠加可靠传输（20% 开销）："
        "25 × 1.2 = 30 > 28 Mbps —— 联合违反式 3。",
        **base,
        focus_nodes=["drone", "detect"],
        focus_edges=[_edge_id("biz", "e1")],
        risk=25.0 / 28.0,
        threshold=0.85,
        badges=[["事件", "demand ×1.67"], ["投影利用率", "0.89 > 0.85"]],
    )
    initial_edges = [_edge_id("biz", dep) for dep in coord.closure.initial]
    trace.add(
        "closure0",
        "③ 受影响初集 E^aff_m,0（式 8）",
        "需求变化直接命中 e1；初集 = {e1}。",
        **base,
        focus_edges=initial_edges,
        focus_nodes=["drone", "detect"],
        badges=[["E^aff_m,0", "{e1}"]],
    )
    pulled = [dep for r in coord.closure.rounds for dep in r.added]
    trace.add(
        "closure1",
        "④ 闭包扩张：D^res 经共享资源拉入远距依赖",
        "e1 与 e2/e3/e4 共享 G4 接入资源（D^res），第 1 轮全部拉入；"
        "不动点 E^aff_m = {e1..e4}。资源核算仍覆盖全任务。",
        **base,
        focus_edges=_biz_edge_ids(task),
        focus_nodes=[*_APP_CN, "G4"],
        badges=[["第 1 轮", f"+{len(pulled)} 条"],
                ["不动点", "{e1,e2,e3,e4}"]],
    )
    sel = coord.attempts[-1].selection
    u_star = coord.attempts[-1].selected_label
    trace.add(
        "candidates",
        "⑤ 四层候选枚举 U^l_m（含 no-change）",
        f"共评估 {len(sel.evaluations)} 个联合决策；可靠模式组合被式 3 "
        "拦截（shared_resource:link:L1）。",
        **base,
        focus_nodes=["drone", "detect"],
        badges=[["评估组合", f"{len(sel.evaluations)}"],
                ["不可行示例", "25×1.2=30>28"]],
    )
    trace.add(
        "selection",
        "⑥ 两阶段字典序选择（式 6 → 式 7）",
        f"第一阶段 min V_m = {sel.stage1_min_v:.3f}；第二阶段 min R_m = "
        f"{sel.stage2_min_r}（路径/转发表/绑定零改动）。",
        **base,
        focus_nodes=["drone", "detect"],
        risk=25.0 * 0.95 / 28.0,
        threshold=0.85,
        badges=[["u*_m", u_star],
                ["V*", f"{sel.stage1_min_v:.3f}"], ["R*", sel.stage2_min_r]],
    )
    window = coord.attempts[-1].window
    trace.add(
        "verify",
        "⑦ 验证窗口 W_m：实测满足 q^H",
        "; ".join(
            f"t={e.at_ms:.0f}ms {e.detail}" for e in window.timeline[:3]
        ) + "；窗口接受。",
        **base,
        qos=True,
        badges=[["窗口", "接受"], ["u*_m 已安装", u_star]],
    )
    stats = modification_stats(before, coord.subnet)
    trace.add(
        "recovered",
        "⑧ 验收 → v2（参数变更零记录改动）",
        f"u* 仅切换传输模式：Mod_m = {stats.changed}/{stats.installed}"
        f" = {stats.ratio:.2f}；恢复时延 {coord.recovery_latency_ms:.1f} ms。",
        **_baseline_state(episode, coord.subnet, task),
        qos=True,
        badges=[["版本", "v1 → v2"],
                ["Mod_m", f"{stats.changed}/{stats.installed}"],
                ["恢复时延", f"{coord.recovery_latency_ms:.0f} ms"]],
    )


def _recovery_steps(episode: _Episode, trace: _Trace) -> None:
    gw = episode.gateway
    before = episode.coord.subnet
    task = episode.task_high
    base = _baseline_state(episode, before, task)

    trace.add(
        "baseline",
        "① 运行态 S_m^(v2)",
        "v2 子网运行中：e1（25 Mbps，轻量模式）走 G1→G2→G4。",
        **base,
        badges=[["版本", "v2"]],
    )
    # Only what this episode killed: the support episode runs later on the
    # same world, so filter rather than listing every offline agent.
    g2_agents = [
        agent_id
        for agent_id, agent in episode.world.support_agents.items()
        if agent.gateway_id == "G2"
    ]
    failed_set = ["G2", *g2_agents]
    dead_path = _path_edge_ids(before)
    alive_edges = [e for e in base["active_edges"] if "G2" not in e]
    trace.add(
        "event",
        "② 运行时事件：中转网关 G2 失效",
        "G2 宕机：链路 L1/L2 中断，其上支撑 agent 一并离线；e1 的路径"
        " G1→G2→G4 断裂。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=alive_edges,
        failed_nodes=failed_set,
        badges=[["事件", "gateway G2 down"], ["中断链路", "L1, L2"]],
    )
    initial_edges = [_edge_id("biz", dep) for dep in gw.closure.initial]
    trace.add(
        "closure0",
        "③ 受影响初集 E^aff_m,0",
        "路径遍历失效网关的依赖：初集 = {e1}（G2 上无任务端点，无需"
        "orchestrator 重新授权端点）。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=alive_edges,
        failed_nodes=failed_set,
        focus_edges=initial_edges,
        focus_nodes=["drone", "detect"],
        badges=[["E^aff_m,0", "{e1}"]],
    )
    trace.add(
        "closure1",
        "④ 闭包扩张（式 8 逐轮）",
        "D^res 共享接入资源把 e2/e3/e4 拉入（via e1 @ access:G4）；"
        "不动点 = {e1..e4}，但重配置范围仍由两阶段选择最小化。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=alive_edges,
        failed_nodes=failed_set,
        focus_edges=_biz_edge_ids(task),
        focus_nodes=[*_APP_CN, "G4"],
        badges=[["第 1 轮", "+e2/e3/e4"], ["不动点", "{e1..e4}"]],
    )
    sel = gw.attempts[-1].selection
    trace.add(
        "candidates",
        "⑤ 三类恢复候选（Sec. IV-B）",
        f"受影响闭包内枚举：参数变更（速率/模式/扩容）、不变路径重绑定、"
        f"路径替换（e1 备选 G1→G3→G4）；共评估 {len(sel.evaluations)} 组合。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=alive_edges,
        failed_nodes=failed_set,
        badges=[["评估组合", f"{len(sel.evaluations)}"],
                ["路径替换候选", "G1→G3→G4"]],
    )
    trace.add(
        "selection",
        "⑥ 两阶段选择 → u*_m = 路径替换",
        f"min V_m = {sel.stage1_min_v:.3f} 后 min R_m = "
        f"{sel.stage2_min_r}：仅重路由 e1（1 路径 + 3 转发记录），其余"
        "依赖不动。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=alive_edges,
        failed_nodes=failed_set,
        focus_edges=[_edge_id("path", "e1", "G1", "G3"),
                     _edge_id("path", "e1", "G3", "G4")],
        badges=[["u*_m", "network:REROUTE:e1:G1→G3→G4"],
                ["V*", f"{sel.stage1_min_v:.3f}"], ["R*", sel.stage2_min_r]],
    )
    new_path = _path_edge_ids(gw.subnet)
    trace.add(
        "adjust",
        "⑦ 执行：撤销旧规则 + 安装新路径",
        "撤销 e1:G2:1，安装 e1@G1（下一跳 G3）与 e1@G3；共享资源操作按"
        "网关串行化。",
        active_nodes=["controller", "G1", "G3", "G4", *_APP_CN],
        active_edges=[e for e in alive_edges if e not in dead_path] + new_path,
        failed_nodes=failed_set,
        changed_nodes=["G3"],
        changed_edges=[e for e in new_path if e not in _path_edge_ids(before)],
        badges=[["撤销", "e1:G2:1"], ["安装", "e1@G1, e1@G3"],
                ["串行化", "按网关分组"]],
    )
    stats = modification_stats(before, gw.subnet)
    trace.add(
        "recovered",
        "⑧ W_m 验证通过 → 验收 v3",
        f"4 条依赖观测到达且满足 q^H；Mod_m = {stats.changed}/"
        f"{stats.installed} = {stats.ratio:.2f}，恢复时延 "
        f"{gw.recovery_latency_ms:.1f} ms。",
        **{**_baseline_state(episode, gw.subnet, task),
           "failed_nodes": failed_set},
        qos=True,
        badges=[["版本", "v2 → v3"],
                ["Mod_m", f"{stats.changed}/{stats.installed}"],
                ["恢复时延", f"{gw.recovery_latency_ms:.0f} ms"],
                ["对比", f"全量重建 {stats.installed}/{stats.installed}"]],
    )


def _rebind_steps(episode: _Episode, trace: _Trace) -> None:
    support = episode.support
    before = episode.gateway.subnet
    task = episode.task_high
    base = _baseline_state(episode, before, task)
    # G2 died in the previous episode and stays dead in this one.
    dead = [
        "G2",
        *(
            agent_id
            for agent_id, agent in episode.world.support_agents.items()
            if agent.gateway_id == "G2"
        ),
        "physical-G4",
    ]

    trace.add(
        "baseline",
        "① 运行态 S_m^(v3)",
        "v3 子网运行中：e1 已改走 G1→G3→G4；e2/e3/e4 的 p 角色绑定在"
        " physical-G4。",
        **base,
        badges=[["版本", "v3"]],
    )
    trace.add(
        "event",
        "② 运行时事件：支撑 agent physical-G4 失效",
        "pAgent@G4 宕机（网关本身存活）：e2/e3/e4 的绑定 Φ 失效。",
        **base,
        failed_nodes=dead,
        focus_edges=[_edge_id("bind", dep, "p", "physical-G4")
                     for dep in ("e2", "e3", "e4")],
        badges=[["事件", "pAgent@G4 down"], ["受影响绑定", "3 条"]],
    )
    trace.add(
        "closure0",
        "③ 初集与闭包",
        "绑定失效支撑 agent 的依赖进入初集 {e2,e3,e4}；D^res 经 access:G4 "
        "第 1 轮拉入 e1，不动点 = {e1..e4}。",
        **base,
        failed_nodes=dead,
        focus_edges=_biz_edge_ids(task),
        badges=[["E^aff_m,0", "{e2,e3,e4}"], ["闭包", "{e1..e4}"]],
    )
    sel1 = support.attempts[0].selection
    trace.add(
        "candidates",
        "④ 重绑定捆绑候选（不变路径类）",
        f"每个失效 (e, p) 对有 3 个可用 pAgent（G1/G2/G3；G2 的 agent 已随"
        f"网关失效排除），捆绑成 3×3×3 组合；共评估 "
        f"{len(sel1.evaluations)} 个联合决策。",
        **base,
        failed_nodes=dead,
        badges=[["捆绑组合", "27"], ["评估", f"{len(sel1.evaluations)}"]],
    )
    first_binds = _first_attempt_rebind_edges(support, trace)
    first_targets = sorted({e["target"] for e in first_binds})
    first_desc = "、".join(first_targets)
    trace.add(
        "adjust",
        "⑤ B_r 第 1 次尝试：执行选中捆绑",
        f"u* = REBIND e2/e3/e4 → {first_desc}"
        f"（V={sel1.stage1_min_v:.3f}，R={sel1.stage2_min_r}）；执行安装成功。",
        **base,
        failed_nodes=dead,
        focus_nodes=first_targets,
        focus_edges=[e["id"] for e in first_binds],
        badges=[["B_r", f"1/3"], ["u*", f"REBIND ×3 → {first_targets[0]}"]],
    )
    trace.add(
        "verify",
        "⑥ W_m 首次验证被拒 → 排除该备选",
        "e2 实测时延超上界（delay_hard:e2）：窗口拒绝，"
        "排除已选 3 个 REBIND 动作，按最新态重评估。",
        **base,
        failed_nodes=dead,
        qos=False,
        badges=[["窗口", "拒绝"], ["违约", "delay_hard:e2"],
                ["排除", f"REBIND→{first_targets[0]} ×3"]],
    )
    sel2 = support.attempts[-1].selection
    second = support.subnet.bindings.binding("e2").p_agent_id
    new_binds = _binding_edge_ids(support.subnet)
    trace.add(
        "adjust2",
        "⑦ B_r 第 2 次尝试：重绑定至存活 agent",
        f"排除后重评估：u* = REBIND e2/e3/e4 → {second}"
        f"（V={sel2.stage1_min_v:.3f}，R={sel2.stage2_min_r}）。",
        **_baseline_state(episode, support.subnet, task),
        failed_nodes=dead,
        changed_nodes=[second],
        changed_edges=[e for e in new_binds
                       if e not in _binding_edge_ids(before)],
        badges=[["B_r", "2/3"], ["u*", f"REBIND ×3 → {second}"]],
    )
    stats = modification_stats(before, support.subnet)
    trace.add(
        "recovered",
        "⑧ W_m 验证通过 → 验收 v4",
        f"实测满足 q^H；Mod_m = {stats.changed}/{stats.installed}"
        f" = {stats.ratio:.2f}（仅 3 条绑定记录），恢复时延 "
        f"{support.recovery_latency_ms:.1f} ms。",
        **{**_baseline_state(episode, support.subnet, task),
           "failed_nodes": dead},
        qos=True,
        badges=[["版本", "v3 → v4"],
                ["Mod_m", f"{stats.changed}/{stats.installed}"],
                ["B_r", "2 次尝试"],
                ["恢复时延", f"{support.recovery_latency_ms:.0f} ms"]],
    )


def _first_attempt_rebind_edges(support, trace: _Trace) -> list[dict]:
    """Binding edges of the B_r first attempt, parsed from its label.

    The first attempt's rebinds are staged but rejected by the window, so no
    accepted subnet carries them; register them on the trace so the step can
    still highlight what was tried.
    """
    edges = []
    label = support.attempts[0].selected_label
    layer_role = {"transport": "t", "network": "n", "physical": "p"}
    for part in label.split(" + "):
        pieces = part.split(":")
        if len(pieces) != 4 or pieces[1] != "REBIND":
            continue
        layer, _action, dep_id, agent_id = pieces
        role = layer_role.get(layer, "p")
        eid = _edge_id("bind", dep_id, role, agent_id)
        edge = {
            "id": eid,
            "source": _dep_anchor(dep_id),
            "target": agent_id,
            "kind": "bind",
        }
        trace.extra_edges[eid] = edge
        edges.append(edge)
    return edges


_BUILDERS = {
    "tcanet_formation": _formation_steps,
    "tcanet_coordination": _coordination_steps,
    "tcanet_recovery": _recovery_steps,
    "tcanet_rebind": _rebind_steps,
}


def build_tcanet_trace(scenario: str) -> dict:
    """Run the scenario chain and emit a viz-compatible trace."""
    if scenario not in TCANET_SCENARIOS:
        scenario = "tcanet_formation"
    episode = _Episode()
    trace = _Trace()
    _BUILDERS[scenario](episode, trace)
    cfg = TCANET_SCENARIOS[scenario]

    nodes = _static_nodes(
        episode.world.support_agents, episode.all_bound_ids()
    )
    edges = (
        [
            {
                "id": _edge_id("ctl", gw),
                "source": "controller",
                "target": gw,
                "kind": "control",
            }
            for gw in _GW_X
        ]
        + _biz_edges(episode.task)
        + _path_edges(episode.subnets)
        + _binding_edges(episode.subnets)
        + list(trace.extra_edges.values())
    )
    return {
        "scenario": scenario,
        "name": cfg["name"],
        "trigger": cfg["trigger"],
        "threshold": 0.85,
        "nodes": nodes,
        "edges": edges,
        "steps": trace.steps,
    }


def list_tcanet_scenarios() -> list[dict]:
    return [
        {"key": k, "name": v["name"], "trigger": v["trigger"],
         "expect": v["expect"]}
        for k, v in TCANET_SCENARIOS.items()
    ]
