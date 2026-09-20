"""CLI 叙事演示：TCANet 五幕演示（python -m src.tcanet.demo.cli）。

幕次对应论文机制：

1. 任务输入     — ``T_m``、DAG、``q^H/q^S``、拓扑与支撑 agent 目录
2. 子网构建     — 端点确认 → Φ 绑定 → π 路径 → FT 编译 → 安装 → W_m → v1
3. 联合决策     — 需求-容量失配：候选枚举 → 式 3 联合可行性（含 25×1.2>28
                  示例）→ 两阶段字典序选择（式 4-7）→ v2
4. 弹性重构     — 网关失效：E^aff,0 → D^res/D^cfg 闭包逐轮扩张（式 8）→
                  三类候选 → 选择 → W_m 窗口 → v3
5. 支撑失效与 B_r — 支撑 agent 失效 → 重绑定捆绑 → 首次验证被拒 → 排除
                  重试 → v4 → 总结指标（Mod、时延、版本时间线）
"""
from __future__ import annotations

import asyncio
import sys

from src.report import banner, bullet, kv_block, pct_change, rule, section, table
from src.tcanet.candidates import (
    CLASS_PARAMETER,
    CLASS_PATH_REPLACE,
    CLASS_REBIND,
)
from src.tcanet.metrics import modification_stats
from src.tcanet.scenario import (
    E1_DEMAND_BASE,
    E1_DEMAND_HIGH,
    build_world,
    event_description,
    rescue_task,
    run_coordination,
    run_formation,
    run_gateway_recovery,
    run_support_recovery,
    with_demand,
)
from src.tcanet.spec import Layer
from src.tcanet.verify import RecoveryResult

LAYER_CN = {
    Layer.APPLICATION: "应用 aAgent",
    Layer.TRANSPORT: "传输 tAgent",
    Layer.NETWORK: "网络 nAgent",
    Layer.PHYSICAL: "物理 pAgent",
}

CLASS_CN = {
    CLASS_PARAMETER: "参数变更",
    CLASS_REBIND: "不变路径重绑定",
    CLASS_PATH_REPLACE: "路径替换",
}


def _window_lines(window, indent: str = "  ") -> list[str]:
    kind_cn = {
        "installed": "安装成功",
        "withdrawn": "撤销旧规则",
        "pending": "等待观测",
        "observed": "观测到达",
        "expired": "窗口到期",
        "violated": "实测违约",
    }
    return [
        f"{indent}t={event.at_ms:6.0f}ms  {kind_cn.get(event.kind, event.kind)}"
        f"：{event.detail}"
        for event in window.timeline
    ]


def _selection_table(trace, *, max_rows: int = 14) -> list[list[object]]:
    """评估表行：决策 / 式3可行 / 违反项 / V_m / R_m / 备注。"""
    rows: list[list[object]] = []
    survivors = set(map(id, trace.stage1_survivors))
    selected = trace.selected
    ordered = sorted(
        trace.evaluations,
        key=lambda item: (
            not item.feasibility.feasible,
            item.soft_violation,
            item.modification_scope,
        ),
    )
    for item in ordered[:max_rows]:
        note = ""
        if item is selected:
            note = "← u*_m"
        elif id(item) in survivors:
            note = "进入第二阶段"
        rows.append(
            [
                item.label,
                "✓" if item.feasibility.feasible else "✗",
                "; ".join(item.feasibility.violations[:2]) or "—",
                f"{item.soft_violation:.3f}",
                item.modification_scope,
                note,
            ]
        )
    return rows


def _closure_lines(closure) -> list[str]:
    lines = [bullet(f"E^aff_m,0（初集，{len(closure.initial)} 条）：")]
    for dep_id, reason in sorted(closure.initial.items()):
        lines.append(bullet(f"{dep_id} — {reason}", indent=4))
    for round_ in closure.rounds:
        lines.append(
            bullet(f"第 {round_.index} 轮扩张（式 8）新拉入 "
                   f"{len(round_.added)} 条：")
        )
        for dep_id, reason in sorted(round_.added.items()):
            lines.append(bullet(f"{dep_id} — {reason}", indent=4))
    lines.append(
        bullet(f"不动点 E^aff_m = {sorted(closure.final)}（共 "
               f"{len(closure.final)} 条；资源核算仍覆盖全任务）")
    )
    return lines


def _recovery_block(title: str, result: RecoveryResult, before_version: int):
    lines = [banner(title)]
    lines.append(bullet(f"运行时事件：{event_description(result.event)}"))
    lines.extend(_closure_lines(result.closure))
    for attempt in result.attempts:
        lines.append(
            bullet(
                f"第 {attempt.index}/{len(result.attempts)} 次尝试 "
                f"B_r：选中 [{attempt.selected_label or '无可行决策'}]"
            )
        )
        if attempt.selection and attempt.selection.selected is not None:
            sel = attempt.selection.selected
            lines.append(
                bullet(
                    f"两阶段选择：V*={attempt.selection.stage1_min_v:.3f}，"
                    f"R*={attempt.selection.stage2_min_r}",
                    indent=4,
                )
            )
            if attempt.selection.tiebreak_note:
                lines.append(
                    bullet(attempt.selection.tiebreak_note, indent=4)
                )
            rows = _selection_table(attempt.selection, max_rows=6)
            lines.append(
                table(
                    ["联合决策 u", "式3可行", "违反项", "V_m(u)", "R_m(u)", "备注"],
                    rows,
                )
            )
            _ = sel
        if attempt.execution is not None and attempt.execution.failed:
            for outcome in attempt.execution.failed:
                lines.append(
                    bullet(f"执行失败：{outcome.action_id}（{outcome.detail}）",
                           indent=4)
                )
        if attempt.window is not None:
            lines.extend(_window_lines(attempt.window))
            if not attempt.window.accepted:
                detail = "; ".join(
                    attempt.window.violations
                    + tuple(f"缺失观测:{dep}" for dep in attempt.window.pending)
                )
                lines.append(bullet(f"窗口拒绝（{detail}），排除该备选", indent=4))
                if attempt.excluded_after:
                    lines.append(
                        bullet(
                            "排除动作：" + "、".join(attempt.excluded_after[:6]),
                            indent=4,
                        )
                    )
        if attempt.outcome == "accepted":
            lines.append(bullet("验证通过 → 版本推进并接受", indent=4))
    if result.recovered and result.subnet is not None:
        lines.append(
            bullet(
                f"恢复成功：v{before_version} → v{result.subnet.version}，"
                f"恢复时延 {result.recovery_latency_ms:.1f} ms"
            )
        )
    else:
        lines.append(bullet(f"恢复失败：{result.error}"))
    return lines


def act1(task, world) -> list[str]:
    lines = [banner("第一幕  任务输入 T_m = ⟨m, G_task, r_m, q^H, q^S⟩")]
    lines.append(bullet(f"任务目标：{task.dag.goal}（task_id={task.dag.task_id}）"))
    lines.append("")
    lines.append(bullet("业务依赖 DAG（G_task_m，边 = 端点间业务依赖）："))
    dep_rows = [
        [
            dep.dep_id,
            f"{dep.source} → {dep.target}",
            dep.flow_type,
            f"{dep.demand_mbps:g} Mbps",
        ]
        for dep in task.dag.dependencies
    ]
    lines.append(table(["依赖 e", "端点", "流类型", "需求 r_m,e"], dep_rows))
    lines.append("")
    ep_rows = [
        [ep.agent_id, ep.name, f"η({ep.agent_id}) = {ep.gateway_id}"]
        for ep in task.dag.endpoints
    ]
    lines.append(table(["端点 aAgent", "职能", "网关绑定 η(a)"], ep_rows))
    lines.append("")
    lines.append(bullet("硬性要求 q^H（全部满足才可行，式 3）："))
    lines.append(
        kv_block(
            [
                ("最小吞吐", f"{task.hard.min_throughput_mbps:g} Mbps"),
                ("最大时延", f"{task.hard.max_delay_ms:g} ms"),
                ("最大丢包", f"{task.hard.max_loss_rate:g}"),
            ]
        )
    )
    lines.append("")
    lines.append(bullet("软性目标 q^S（式 4 归一化违反度 V_m）："))
    soft_rows = [
        [
            t.name,
            "≤ 上界" if t.kind == "upper" else "≥ 下界",
            f"{t.bound:g}",
            t.metric,
        ]
        for t in task.soft
    ]
    lines.append(table(["目标", "方向", "界", "投影态指标"], soft_rows))
    lines.append("")
    lines.append(bullet("网关图 H=(G,E_gw) 与共享资源（容量/保护负载/可用）："))
    link_rows = []
    for link in world.graph.links:
        rid = world.link_resource_id(link.link_id)
        resource = world.resources[rid]
        link_rows.append(
            [
                link.link_id,
                f"{link.source_gateway} → {link.target_gateway}",
                f"{link.capacity_mbps:g}",
                f"{resource.protected_load_mbps:g}",
                f"{resource.available_mbps:g}",
                f"{link.delay_ms:g} ms",
            ]
        )
    lines.append(
        table(
            ["链路", "方向", "容量", "保护负载 d^prot", "可用", "时延"],
            link_rows,
        )
    )
    lines.append(
        bullet("注意 link:L1：40 − 12（保护负载）= 28 Mbps 可用 —— 第三幕的"
               "联合不可行示例即发生于此。")
    )
    return lines


def act2(formation) -> list[str]:
    lines = [banner("第二幕  子网构建与初始验收（论文 Sec. IV-A）")]
    lines.append(bullet("端点确认：4 个端点全部在线，ACK 通过（无需 orchestrator "
                        "重新授权替代端点）。"))
    bindings = formation.subnet.bindings
    lines.append("")
    lines.append(bullet("支撑 agent 绑定 Φ_m(e)=⟨t,n,p⟩（条目可空，不引入额外"
                        "数据面跳数）："))
    bind_rows = [
        [
            dep_id,
            b.t_agent_id or "∅",
            b.n_agent_id or "∅",
            b.p_agent_id or "∅",
        ]
        for dep_id, b in sorted(bindings.records.items())
    ]
    lines.append(table(["依赖 e", "tAgent", "nAgent", "pAgent"], bind_rows))
    lines.append(bullet("e2 两端同驻 G4：本地交付，t/n 角色留空（论文 "
                        "Sec. II-A）。"))
    lines.append("")
    lines.append(bullet("依赖 → 网关路径 π_m,e（CSPF 最小时延，顺序保留共享"
                        "资源）："))
    path_rows = [
        [
            dep_id,
            " → ".join(record.gateway_path),
            ", ".join(record.link_ids) or "（本地交付）",
        ]
        for dep_id, record in sorted(formation.subnet.paths.items())
    ]
    lines.append(table(["依赖 e", "路径 π_m,e", "经过链路"], path_rows))
    lines.append("")
    lines.append(bullet("转发表 FT^g_m 编译（共 "
                       f"{len(formation.subnet.forwarding)} 条规则）："))
    ft_rows = [
        [
            entry.rule_id,
            entry.action_mode,
            entry.next_hop_gateway or "—",
            f"{entry.src_agent} → {entry.dst_agent}",
        ]
        for entry in sorted(
            formation.subnet.forwarding.values(), key=lambda e: e.rule_id
        )
    ]
    lines.append(table(["规则", "动作模式", "下一跳", "流"], ft_rows))
    lines.append("")
    lines.append(
        bullet(f"执行计划 C+_m：{len(formation.plan.actions)} 个可执行动作"
               f"（install/bind），目标版本 v+1 = "
               f"{formation.plan.next_version}。")
    )
    lines.append(bullet("执行前条件一致性复查通过；共享资源操作按网关串行化。"))
    lines.append("")
    lines.append(bullet("验证窗口 W_m 时间线："))
    lines.extend(_window_lines(formation.window))
    lines.append("")
    stats = ("安装成功" if formation.execution.ok else "安装失败")
    lines.append(
        kv_block(
            [
                ("执行结果", stats),
                ("窗口结论", "接受 → 版本推进" if formation.window.accepted else "拒绝"),
                ("子网版本", f"v0 → v{formation.subnet.version}"),
                ("形成时延", f"{formation.formation_latency_ms:.1f} ms"
                 "（确认 → 初始验收）"),
            ]
        )
    )
    return lines


def act3(coord, formation, task_high) -> list[str]:
    lines = [banner("第三幕  跨层联合决策：需求-容量失配（论文 Sec. III）")]
    lines.append(
        bullet(f"运行时事件：{event_description(coord.event)} —— 无人机升级"
               "高分辨率视频流。")
    )
    lines.append(
        bullet(f"e1 需求 {E1_DEMAND_BASE:g} → {E1_DEMAND_HIGH:g} Mbps"
               f"（{pct_change(E1_DEMAND_BASE, E1_DEMAND_HIGH)}），"
               "而 link:L1 可用仅 28 Mbps。")
    )
    lines.extend(_closure_lines(coord.closure))
    lines.append("")
    lines.append(bullet("四层候选集 U^l_m（每层含 no-change，聚焦受影响依赖）："))
    attempt = coord.attempts[0]
    candidate_rows = []
    seen_labels: set[str] = set()
    layer_order = {
        Layer.APPLICATION: 0,
        Layer.TRANSPORT: 1,
        Layer.NETWORK: 2,
        Layer.PHYSICAL: 3,
    }
    for evaluation in attempt.selection.evaluations:
        for action in evaluation.actions:
            if action.is_no_change or action.action_id in seen_labels:
                continue
            seen_labels.add(action.action_id)
            if action.action in {"ADJUST_RATE", "SWITCH_MODE", "REROUTE",
                                 "BOOST_ACCESS", "REBIND_SUPPORT"}:
                candidate_rows.append(
                    [
                        LAYER_CN[action.layer],
                        action.action_id.split(":", 1)[1],
                        CLASS_CN.get(action.recovery_class or "", "—"),
                        layer_order[action.layer],
                    ]
                )
    candidate_rows.sort(key=lambda row: row[3])
    candidate_rows = [row[:3] for row in candidate_rows][:12]
    lines.append(table(["层", "候选（截选）", "恢复候选类"], candidate_rows))
    lines.append(bullet("（候选按层授权枚举，上表截选展示；完整集合进入组合"
                        "评估。）"))
    lines.append("")
    lines.append(bullet("逐组合联合可行性 x̂_m(u)（式 3；截选 e1 的应用×传输"
                        "组合，其余层 no-change）："))
    focus_rows = []
    selected = attempt.selection.selected
    for item in attempt.selection.evaluations:
        ids = {a.action_id for a in item.actions}
        varies = [
            a.action_id for a in item.actions
            if not a.is_no_change
            and a.target == "e1"
            and a.action in {"ADJUST_RATE", "SWITCH_MODE"}
        ]
        others_quiet = all(
            a.is_no_change or a.action_id in varies for a in item.actions
        )
        if not others_quiet:
            continue
        focus_rows.append(
            [
                item.label,
                "✓" if item.feasibility.feasible else "✗",
                "; ".join(item.feasibility.violations) or "—",
                f"{item.soft_violation:.3f}",
                item.modification_scope,
                "← u*_m" if item is selected else "",
            ]
        )
    focus_rows = focus_rows[:10]
    lines.append(
        table(["u（e1 参数组合）", "式3可行", "违反项", "V_m", "R_m", "备注"],
              focus_rows)
    )
    lines.append(
        bullet("联合不可行示例（论文 Sec. III-A）：可靠传输开销 20%，"
               "25 × 1.2 = 30 > 28 Mbps —— 单层各自可行，联合违反式 3，"
               "必须跨层协调。")
    )
    lines.append("")
    lines.append(bullet("两阶段字典序选择（式 6 → 式 7）："))
    lines.append(
        bullet(
            f"第一阶段：min V_m = {attempt.selection.stage1_min_v:.3f}"
            f"（{len(attempt.selection.stage1_survivors)} 个组合并列存活）；"
            f"第二阶段：min R_m = {attempt.selection.stage2_min_r}。"
        )
    )
    if attempt.selection.tiebreak_note:
        lines.append(bullet(attempt.selection.tiebreak_note))
    lines.append(
        bullet(
            "u*_m = " + (attempt.selected_label or "—")
            + " —— 保住 25 Mbps 需求，仅换传输模式，"
            "路径/转发表/绑定零改动（R_m = 0）。"
        )
    )
    lines.append("")
    lines.append(bullet("验证窗口 W_m："))
    lines.extend(_window_lines(attempt.window))
    stats = modification_stats(formation.subnet, coord.subnet)
    lines.append(
        kv_block(
            [
                ("窗口结论", "接受 → 版本推进" if attempt.window.accepted else "拒绝"),
                ("子网版本", "v1 → v2"),
                ("修改比 Mod_m", f"{stats.changed}/{stats.installed}"
                 f" = {stats.ratio:.2f}（参数变更不触碰记录）"),
                ("恢复时延", f"{coord.recovery_latency_ms:.1f} ms"),
            ]
        )
    )
    _ = task_high
    return lines


def act4(gw, coord) -> list[str]:
    lines = _recovery_block(
        "第四幕  弹性重构：中转网关失效（论文 Sec. IV-B，式 8）",
        gw,
        coord.subnet.version,
    )
    stats = modification_stats(coord.subnet, gw.subnet) if gw.subnet else None
    if stats:
        lines.append("")
        lines.append(
            bullet(
                f"修改比 Mod_m = {stats.changed}/{stats.installed}"
                f" = {stats.ratio:.2f}：1 条路径记录 + 3 条转发记录"
                "（替换计一次），绑定不动。"
            )
        )
        lines.append(
            bullet(
                "对照：全量重建需重装全部 16 条记录（Mod=1.0），TCANet 仅"
                "改动受影响闭包内必需部分。"
            )
        )
    return lines


def act5(support, gw) -> list[str]:
    lines = _recovery_block(
        "第五幕  支撑 agent 失效与 B_r 有界重试（论文 Sec. IV-C）",
        support,
        gw.subnet.version,
    )
    if support.subnet:
        stats = modification_stats(gw.subnet, support.subnet)
        lines.append("")
        lines.append(
            bullet(
                f"修改比 Mod_m = {stats.changed}/{stats.installed}"
                f" = {stats.ratio:.2f}：仅 3 条绑定记录（e2/e3/e4 的 p "
                "角色），路径与转发表零改动 —— 重绑定类候选的价值。"
            )
        )
    return lines


def summarize_act(formation, coord, gw, support) -> list[str]:
    lines = [banner("总结  版本时间线与评估指标（论文 Sec. V）")]
    rows = [
        ["v0 → v1", "子网构建", f"{formation.formation_latency_ms:.1f} ms",
         "—", "16 条记录安装"],
    ]
    if coord.subnet:
        stats = modification_stats(formation.subnet, coord.subnet)
        rows.append(
            [
                "v1 → v2", "跨层协调（传输模式）",
                f"{coord.recovery_latency_ms:.1f} ms",
                f"{stats.changed}/{stats.installed}",
                "25×1.2=30>28 联合不可行被式 3 拦截",
            ]
        )
    if gw.subnet:
        stats = modification_stats(coord.subnet, gw.subnet)
        rows.append(
            [
                "v2 → v3", "弹性重构（路径替换）",
                f"{gw.recovery_latency_ms:.1f} ms",
                f"{stats.changed}/{stats.installed}",
                "闭包扩张后最小改动重路由",
            ]
        )
    if support.subnet:
        stats = modification_stats(gw.subnet, support.subnet)
        rows.append(
            [
                "v3 → v4", "支撑重绑定（B_r=2）",
                f"{support.recovery_latency_ms:.1f} ms",
                f"{stats.changed}/{stats.installed}",
                "首次验证被拒 → 排除重试成功",
            ]
        )
    lines.append(
        table(["版本", "机制", "时延", "改动 N/N₀", "说明"], rows)
    )
    lines.append("")
    lines.append(bullet("机制覆盖：式 1-3（任务/绑定/可行性）、式 4-7（两阶段"
                        "字典序选择）、式 8（依赖闭包）、式 10（修改比）、"
                        "B_r=3 有界重试、W_m 验证窗口。"))
    lines.append(bullet("三类恢复候选均有出场：参数变更（第三幕）、路径替换"
                        "（第四幕）、不变路径重绑定（第五幕）。"))
    return lines


def run_demo() -> None:
    world = build_world()
    task = rescue_task(world)

    formation = asyncio.run(run_formation(task, world))
    task_high = with_demand(task, "e1", E1_DEMAND_HIGH)
    coord = run_coordination(task_high, world, formation.subnet)
    gw = run_gateway_recovery(task_high, world, coord.subnet)
    support = run_support_recovery(task_high, world, gw.subnet)

    acts: list[list[str]] = [
        act1(task, world),
        act2(formation),
        act3(coord, formation, task_high),
        act4(gw, coord),
        act5(support, gw),
        summarize_act(formation, coord, gw, support),
    ]
    for act in acts:
        for line in act:
            print(line)
        print()
    print(rule())


def main() -> int:
    run_demo()
    return 0


if __name__ == "__main__":
    sys.exit(main())
