"""Generate a step-by-step visual trace of the networking process.

Each scenario (tier1/tier2/tier3) is replayed on the real Controller and turned
into an ordered list of "steps". Every step carries the cumulative graph state
(active nodes/edges) plus this-step emphasis (focus / failed / changed) and the
sidebar payload (risk, QoS, badges). The frontend just renders state -> colour.

Nothing here changes the mechanism: it only calls existing public methods
(build_task_subnet / run_agent_loop / evaluate_and_adjust / rebuild_task_subnet)
and snapshots intermediate state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from src.controller.networking import AgentController
from src.core.gateway import Gateway
from src.core.models import SessionSpec, TaskSubnet
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology

EVENT_TIME = 4.0

SCENARIOS = {
    "tier1": {
        "name": "承载劣化（预测风险触发）",
        "trigger": "预测风险 R 超阈值",
        "kind": "event",
        "event": ("bearer_degradation", 1.3),
        "expect": "tier1 局部调参",
    },
    "tier2": {
        "name": "边缘网络Agent失效（本地有备用）",
        "trigger": "F_m=1  nagent-gw-mec 离线",
        "kind": "failure",
        "failure": ("nagent-gw-mec", "gw-mec"),
        "expect": "tier2 替换支撑Agent",
    },
    "tier3": {
        "name": "终端网络Agent失效（本地无备用）",
        "trigger": "F_m=1  nagent-gw-ue 离线",
        "kind": "failure",
        "failure": ("nagent-gw-ue", "gw-ue"),
        "expect": "tier3 跨子网改接",
    },
}

_STRATEGY_LABEL = {
    "local_tuning": "tier1 局部调参",
    "support_agent_replace": "tier2 替换支撑Agent",
    "communication_reroute": "tier3 调整通信关系",
    "support_session_retune": "tier2 会话重调",
    "no_adjustment": "无需调整",
    "full_rebuild": "全量重建",
}
_OP_LABEL = {
    "local_tune": "局部调参",
    "session_setup": "会话重建",
    "gateway_install": "网关装载",
    "member_confirm": "成员确认",
    "agent_replace": "替换支撑Agent",
    "reroute": "跨子网改接",
}
_LAYER_LABEL = {"app": "应用层", "trans": "传输层", "net": "网络层", "phy": "物理层"}

_GATEWAY_X = {"gw-ue": 200, "gw-mec": 500, "gw-cloud": 800}
_GATEWAY_TITLE = {"gw-ue": "gw-ue · 终端子网", "gw-mec": "gw-mec · 边缘子网", "gw-cloud": "gw-cloud · 云子网"}
_SHORT = {
    "agent-drone-capture": "无人机采集",
    "agent-edge-recognition": "边缘识别",
    "agent-cloud-planning": "云端决策",
    "agent-terminal-feedback": "现场反馈",
}


def _short_label(card) -> str:
    aid = card.agent_id
    if aid in _SHORT:
        return _SHORT[aid]
    if card.layer.value == "trans":
        return "tAgent"
    if card.layer.value == "net":
        return "nAgent·备用" if aid.endswith("-standby") else "nAgent"
    if card.layer.value == "phy":
        return "pAgent·占位"
    return card.name


# --------------------------------------------------------------------------- #
# layout + static graph
# --------------------------------------------------------------------------- #
def _node_position(card, app_index: dict[str, int]) -> tuple[float, float]:
    gx = _GATEWAY_X[card.gateway_id]
    layer = card.layer.value
    if layer == "app":
        offset = app_index.get(card.agent_id, 0)
        return gx - 55 + offset * 110, 250
    if layer == "trans":
        return gx, 350
    if layer == "net":
        return (gx + 60, 448) if card.agent_id.endswith("-standby") else (gx - 25, 448)
    if layer == "phy":
        return gx, 548
    return gx, 300


def _op_summary(operations: dict) -> str:
    parts = [f"{_OP_LABEL.get(k, k)}×{v}" for k, v in operations.items() if v]
    return "  ".join(parts) if parts else "无"


def _strategy(name: str) -> str:
    return _STRATEGY_LABEL.get(name, name)


def _edge_id(src: str, dst: str, kind: str) -> str:
    return f"{src}||{dst}||{kind}"


def _build_static_graph(gateways: dict[str, Gateway]) -> tuple[list[dict], dict[str, int]]:
    nodes: list[dict] = [
        {"id": "controller", "label": "Agent Controller · 核心网控制域",
         "short": "Agent Controller · 核心网控制域", "kind": "controller", "x": 500, "y": 70}
    ]
    # app-agent ordering per gateway for horizontal offset
    app_index: dict[str, int] = {}
    for gateway in gateways.values():
        apps = [a for a in gateway.agents.values() if a.card.layer.value == "app"]
        for i, agent in enumerate(apps):
            app_index[agent.agent_id] = i

    for gid, gateway in gateways.items():
        nodes.append(
            {"id": gid, "label": _GATEWAY_TITLE[gid], "short": _GATEWAY_TITLE[gid],
             "kind": "gateway", "x": _GATEWAY_X[gid], "y": 160}
        )
        for agent in gateway.agents.values():
            card = agent.card
            x, y = _node_position(card, app_index)
            nodes.append(
                {
                    "id": card.agent_id,
                    "label": card.name,
                    "short": _short_label(card),
                    "kind": card.layer.value,
                    "gateway": gid,
                    "standby": card.agent_id.endswith("-standby"),
                    "x": x,
                    "y": y,
                }
            )
    return nodes, app_index


def _edges_from(subnet: TaskSubnet) -> dict[str, dict]:
    edges: dict[str, dict] = {}
    for src, dst, kind in subnet.edges:
        edges[_edge_id(src, dst, kind)] = {"id": _edge_id(src, dst, kind), "source": src, "target": dst, "kind": kind}
    # support-chain edges link app -> tAgent -> nAgent -> app via the controller's gateways.
    # The controller node is connected to every gateway for the control plane.
    return edges


def _control_edges(gateways: dict[str, Gateway]) -> dict[str, dict]:
    edges = {}
    for gid in gateways:
        eid = _edge_id("controller", gid, "control")
        edges[eid] = {"id": eid, "source": "controller", "target": gid, "kind": "control"}
    return edges


# --------------------------------------------------------------------------- #
# trace builder
# --------------------------------------------------------------------------- #
class _Trace:
    def __init__(self, nodes: list[dict], all_edges: dict[str, dict], task, scenario_key: str) -> None:
        self.nodes = nodes
        self.all_edges = all_edges
        self.task = task
        self.scenario_key = scenario_key
        self.steps: list[dict] = []

    def add(
        self,
        kind: str,
        title: str,
        detail: str,
        *,
        active_nodes=(),
        active_edges=(),
        focus_nodes=(),
        focus_edges=(),
        failed_nodes=(),
        changed_nodes=(),
        changed_edges=(),
        risk=None,
        threshold=None,
        qos=None,
        badges=None,
        compare=None,
    ) -> None:
        self.steps.append(
            {
                "id": len(self.steps),
                "kind": kind,
                "title": title,
                "detail": detail,
                "active_nodes": sorted(set(active_nodes)),
                "active_edges": sorted(set(active_edges)),
                "focus_nodes": sorted(set(focus_nodes)),
                "focus_edges": sorted(set(focus_edges)),
                "failed_nodes": sorted(set(failed_nodes)),
                "changed_nodes": sorted(set(changed_nodes)),
                "changed_edges": sorted(set(changed_edges)),
                "risk": risk,
                "threshold": threshold,
                "qos": qos,
                "badges": badges or [],
                "compare": compare,
            }
        )


def _kind_ids(nodes: list[dict], *kinds: str) -> list[str]:
    return [n["id"] for n in nodes if n["kind"] in kinds]


async def _predict_snapshot() -> tuple[list, float]:
    """Pre-event predictions/risk on a throwaway world (display only)."""
    provider = SyntheticMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, _ = await controller.build_task_subnet(task)
    preds = await controller.run_agent_loop(subnet, EVENT_TIME - 1.0)
    return preds, controller.risk_calculator.risk(task, preds)


async def _build(scenario_key: str) -> dict:
    cfg = SCENARIOS[scenario_key]
    threshold = rescue_task().qos.risk_threshold

    # ---- World A: minimal adjustment -------------------------------------
    provider = SyntheticMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    nodes, _ = _build_static_graph(controller.gateways)
    control_edges = _control_edges(controller.gateways)

    subnet, build_metrics = await controller.build_task_subnet(task)

    biz_edges = {_edge_id(e.source, e.target, "biz"): {
        "id": _edge_id(e.source, e.target, "biz"), "source": e.source, "target": e.target, "kind": "biz"}
        for e in task.biz_edges}
    pre_edges = _edges_from(subnet)
    all_edges = {**control_edges, **biz_edges, **pre_edges}

    app_ids = sorted(subnet.app_agents)
    gw_ids = sorted(subnet.involved_gateways)
    trans_ids = sorted(subnet.trans_agents)
    net_ids = sorted(subnet.net_agents)
    support_chain = [eid for eid, e in pre_edges.items() if e["kind"] != "biz"]
    biz_ids = list(biz_edges)

    trace = _Trace(nodes, all_edges, task, scenario_key)

    # Step 0: input
    trace.add(
        "input",
        "① 上层下发任务 (A^app, E^biz, Q)",
        f"业务 Agent {len(app_ids)} 个，业务协作关系 {len(task.biz_edges)} 条；"
        f"QoS：时延≤{task.qos.max_latency_ms:.0f}ms 丢包≤{task.qos.max_loss_rate*100:.0f}% 带宽≥{task.qos.min_bandwidth_mbps:.0f}Mbps。",
        active_nodes=["controller", *app_ids],
        active_edges=biz_ids,
        focus_nodes=app_ids,
        focus_edges=biz_ids,
        badges=[["任务", task.task_id]],
    )

    # Step 1: member confirm
    trace.add(
        "member_confirm",
        "② 网关确认本地成员",
        "Controller 按 A^app 定位子网 → 各网关确认本地 Agent 在线/能力/位置/端点。",
        active_nodes=["controller", *gw_ids, *app_ids],
        active_edges=[*biz_ids, *control_edges],
        focus_nodes=[*gw_ids, *app_ids],
        focus_edges=list(control_edges),
        badges=[["在线成员", f"{len(app_ids)} app / {len(gw_ids)} 网关"]],
    )

    # Step 2: map E^biz -> G_m
    trace.add(
        "map",
        "③ E^biz 映射为任务通信子网 G_m",
        "每条业务协作边关联所需传输(tAgent)/网络(nAgent)支撑，形成跨层支撑链：app→tAgent→nAgent→app。",
        active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
        active_edges=[*biz_ids, *control_edges, *support_chain],
        focus_nodes=[*trans_ids, *net_ids],
        focus_edges=support_chain,
        badges=[["跨层边", f"{len(pre_edges)} 条"]],
    )

    # Step 3: networked
    acks = "  ".join(f"{a.gateway_id}✓" for a in subnet.gateway_acks)
    trace.add(
        "networked",
        "④ 组网完成（端到端会话建立）",
        f"{len(subnet.sessions)} 条端到端会话建立，网关回 ack：{acks}。",
        active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
        active_edges=[*biz_ids, *control_edges, *support_chain],
        focus_edges=[*biz_ids, *support_chain],
        qos=True,
        badges=[["状态", "networked ✓"], ["建网耗时", f"{build_metrics.networking_latency_ms:.2f} ms"],
                ["会话", f"{len(subnet.sessions)} 条"]],
    )

    # Step 4: predict (pre-event). Computed on a throwaway world so world A stays
    # pristine for evaluate_and_adjust -> numbers match experiments/run.py exactly
    # (run_agent_loop applies action effects and would otherwise perturb world A).
    preds0, risk0 = await _predict_snapshot()
    sample = "; ".join(
        f"{_LAYER_LABEL.get(p.layer.value, p.layer.value)} {p.metric}={p.values[-1]:.2f}"
        for p in preds0[:3]
    )
    trace.add(
        "predict",
        "⑤ 四层 Agent 短时预测，计算风险 R",
        f"各层 Agent 感知→预测→动作；Controller 汇总算跨层风险 R。示例：{sample}。",
        active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
        active_edges=[*biz_ids, *control_edges, *support_chain],
        focus_nodes=[*app_ids, *trans_ids, *net_ids],
        risk=risk0,
        threshold=threshold,
        badges=[["风险 R", f"{risk0:.3f}"], ["阈值 τ", f"{threshold:.2f}"]],
    )

    # Step 5: inject event
    failed_id = None
    if cfg["kind"] == "event":
        provider.inject_event(task.task_id, cfg["event"][0], severity=cfg["event"][1])
        trace.add(
            "event",
            "⑥ 注入事件：承载劣化",
            f"链路承载下降（{cfg['event'][0]}, severity={cfg['event'][1]}），各 nAgent 预测可用承载下滑。",
            active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
            active_edges=[*biz_ids, *control_edges, *support_chain],
            focus_nodes=net_ids,
            badges=[["事件", "承载劣化"]],
        )
        failed_set = None
    else:
        failed_id, failed_gw = cfg["failure"]
        controller.gateways[failed_gw].fail_agent(failed_id)
        trace.add(
            "event",
            "⑥ 注入事件：支撑 Agent 失效 (F_m=1)",
            f"{failed_id} 离线。其所在子网{'有' if failed_gw != 'gw-ue' else '无'}本地备用 nAgent。",
            active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
            active_edges=[*biz_ids, *control_edges, *support_chain],
            failed_nodes=[failed_id],
            badges=[["事件", f"{failed_id} 离线"]],
        )
        failed_set = {failed_id}

    # Step 6: trigger + adjust
    pre_session_map = {s.session_id: s for s in subnet.sessions}
    risk_before, risk_after, adjustment = await controller.evaluate_and_adjust(subnet, EVENT_TIME, failed_set)

    trigger_reason = "预测风险 R 超阈值" if risk_before > threshold else "支撑 Agent 失效 F_m=1"
    trace.add(
        "trigger",
        "⑦ 触发运行期调整",
        f"触发依据：{trigger_reason}（R={risk_before:.3f}，τ={threshold:.2f}）。"
        "按『先局部调参 → 再替换支撑Agent → 最后调整通信关系』就近处理。",
        active_nodes=["controller", *gw_ids, *app_ids, *trans_ids, *net_ids],
        active_edges=[*biz_ids, *control_edges, *support_chain],
        failed_nodes=[failed_id] if failed_id else [],
        focus_nodes=["controller"],
        risk=risk_before,
        threshold=threshold,
        badges=[["触发", trigger_reason], ["风险 R", f"{risk_before:.3f}"]],
    )

    # post-adjust graph state
    post_edges = _edges_from(subnet)
    for eid, e in post_edges.items():
        all_edges.setdefault(eid, e)
    post_app = sorted(subnet.app_agents)
    post_trans = sorted(subnet.trans_agents)
    post_net = sorted(subnet.net_agents)
    post_support = [eid for eid, e in post_edges.items() if e["kind"] != "biz"]
    changed_edges = [eid for eid in post_edges if eid not in pre_edges]
    changed_nodes = sorted(set(post_net) - set(net_ids))  # newly used standby support agents

    trace.add(
        "adjust",
        f"⑧ 执行最小调整：{_strategy(adjustment.strategy)}",
        _adjust_detail(adjustment, failed_id),
        active_nodes=["controller", *gw_ids, *post_app, *post_trans, *post_net],
        active_edges=[*biz_ids, *control_edges, *post_support],
        failed_nodes=[failed_id] if failed_id else [],
        focus_nodes=changed_nodes or [*post_trans, *post_net],
        focus_edges=changed_edges,
        changed_nodes=changed_nodes,
        changed_edges=changed_edges,
        badges=[
            ["策略", _strategy(adjustment.strategy)],
            ["变更A/E/GW", f"{adjustment.changed_agents}/{adjustment.changed_edges}/{adjustment.changed_gateways}"],
            ["中断", f"{adjustment.service_interruption_ms:.0f} ms"],
            ["操作", _op_summary(adjustment.operations)],
        ],
    )

    # Step 8: recovered
    qos_ok = risk_after <= threshold
    trace.add(
        "recovered",
        "⑨ 风险回落，任务恢复",
        f"调整后风险 R={risk_after:.3f} ≤ τ={threshold:.2f}，QoS {'满足 ✓' if qos_ok else '未满足 ✗'}；"
        "未受影响的 Agent / 会话 / 网关保持不变。",
        active_nodes=["controller", *gw_ids, *post_app, *post_trans, *post_net],
        active_edges=[*biz_ids, *control_edges, *post_support],
        failed_nodes=[failed_id] if failed_id else [],
        focus_nodes=changed_nodes,
        risk=risk_after,
        threshold=threshold,
        qos=qos_ok,
        badges=[["风险 R", f"{risk_after:.3f}"], ["QoS", "满足 ✓" if qos_ok else "未满足 ✗"]],
    )

    # ---- World B: full-rebuild baseline ----------------------------------
    provider_b = SyntheticMetricProvider()
    task_b = rescue_task()
    controller_b = AgentController(build_rescue_topology(provider_b))
    subnet_b, build_b = await controller_b.build_task_subnet(task_b)
    if cfg["kind"] == "event":
        provider_b.inject_event(task_b.task_id, cfg["event"][0], severity=cfg["event"][1])
    else:
        controller_b.gateways[cfg["failure"][1]].fail_agent(cfg["failure"][0])
    preds_b = await controller_b.run_agent_loop(subnet_b, EVENT_TIME)
    risk_before_b = controller_b.risk_calculator.risk(task_b, preds_b)
    _, risk_after_b, full = await controller_b.rebuild_task_subnet(subnet_b, EVENT_TIME + 0.1)

    saved = full.service_interruption_ms - adjustment.service_interruption_ms
    drop = (saved / full.service_interruption_ms * 100.0) if full.service_interruption_ms else 0.0
    compare = {
        "minimal": {
            "strategy": _strategy(adjustment.strategy),
            "changed": f"{adjustment.changed_agents}/{adjustment.changed_edges}/{adjustment.changed_gateways}",
            "interruption": round(adjustment.service_interruption_ms, 1),
            "ops": _op_summary(adjustment.operations),
            "qos": qos_ok,
        },
        "full": {
            "strategy": _strategy("full_rebuild"),
            "changed": f"{full.changed_agents}/{full.changed_edges}/{full.changed_gateways}",
            "interruption": round(full.service_interruption_ms, 1),
            "ops": _op_summary(full.operations),
            "qos": risk_after_b <= threshold,
        },
        "saved_ms": round(saved, 1),
        "drop_pct": round(drop, 1),
    }
    trace.add(
        "compare",
        "⑩ 对比全量重建",
        f"同一事件下，全量重建需重新确认全部成员 + 重装全部网关 + 重建全部会话；"
        f"最小调整仅触碰受影响范围，业务中断 {adjustment.service_interruption_ms:.0f}ms vs {full.service_interruption_ms:.0f}ms，"
        f"降低约 {drop:.0f}%。",
        active_nodes=["controller", *gw_ids, *post_app, *post_trans, *post_net],
        active_edges=[*biz_ids, *control_edges, *post_support],
        failed_nodes=[failed_id] if failed_id else [],
        badges=[["中断降幅", f"-{drop:.0f}%"], ["省", f"{saved:.0f} ms"]],
        compare=compare,
    )

    return {
        "scenario": scenario_key,
        "name": cfg["name"],
        "trigger": cfg["trigger"],
        "threshold": threshold,
        "nodes": nodes,
        "edges": list(all_edges.values()),
        "steps": trace.steps,
    }


def _adjust_detail(adjustment, failed_id: str | None) -> str:
    if adjustment.strategy == "local_tuning":
        return "本地有备用且风险来自承载劣化：各层 Agent 仅局部调参（降码率/调传输参数/提监测），不动网关与通信关系。"
    if adjustment.strategy == "support_agent_replace":
        return f"{failed_id} 失效，本子网有空闲备用 nAgent：就地替换（tier2），仅触碰本子网会话与网关。"
    if adjustment.strategy == "communication_reroute":
        return f"{failed_id} 失效且本子网无备用：将该业务流的网络承载跨子网改接到邻近子网的备用 nAgent（tier3），最小范围重路由。"
    return "运行期调整。"


def build_scenario_trace(scenario: str) -> dict:
    if scenario not in SCENARIOS:
        scenario = "tier1"
    return asyncio.run(_build(scenario))


def list_scenarios() -> list[dict]:
    return [{"key": k, "name": v["name"], "trigger": v["trigger"], "expect": v["expect"]} for k, v in SCENARIOS.items()]
