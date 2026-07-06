from __future__ import annotations

import asyncio

from src import report
from src.controller.networking import AgentController
from src.core.models import AgentLayer
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology

_LAYER_LABEL = {
    AgentLayer.APPLICATION: "应用层 aAgent",
    AgentLayer.TRANSPORT: "传输层 tAgent",
    AgentLayer.NETWORK: "网络层 nAgent",
    AgentLayer.PHYSICAL: "物理层 pAgent",
}

_EDGE_LABEL = {
    "biz": "业务协作",
    "uses_trans": "占用传输",
    "uses_net": "占用网络",
    "supports_delivery": "承载交付",
}


async def run() -> None:
    provider = SyntheticMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, metrics = await controller.build_task_subnet(task)
    predictions = await controller.run_agent_loop(subnet, timestamp=1.0)

    lines: list[str] = []
    lines.append(report.banner("应急救援任务 · 跨层组网过程"))

    lines.append(report.section("任务输入（上层下发）"))
    lines.append(
        report.kv_block(
            [
                ("任务编号", task.task_id),
                ("目标", task.goal),
                ("业务 Agent", "  ".join(task.app_agents)),
                ("业务协作关系", f"{len(task.biz_edges)} 条"),
            ]
        )
    )

    lines.append(report.section("组网结果 G_m（Controller 映射 + 网关确认）"))
    lines.append(
        report.kv_block(
            [
                ("状态", f"{subnet.state.value}  " + ("✓ 组网完成" if metrics.networking_success else "✗ 失败")),
                ("建网耗时", f"{metrics.networking_latency_ms:.2f} ms"),
                ("应用层成员", "  ".join(sorted(subnet.app_agents))),
                ("传输层支撑", "  ".join(sorted(subnet.trans_agents))),
                ("网络层支撑", "  ".join(sorted(subnet.net_agents))),
                ("物理层(占位)", "  ".join(sorted(subnet.phy_agents)) or "（暂不实装）"),
            ]
        )
    )

    lines.append(report.section("AgentCard 确认 ACK（网关先确认本地 Agent 可用）"))
    agent_ack_rows = [
        [
            ack.gateway_id,
            ack.agent_id,
            ack.layer.value if ack.layer else "",
            ack.purpose,
            f"{ack.ip}:{ack.port}" if ack.port is not None else ack.ip,
            "接受 ✓" if ack.accepted else "拒绝 ✗",
            ack.reason,
        ]
        for ack in subnet.agent_acks
    ]
    lines.append(report.table(["网关", "Agent", "层", "用途", "IP:端口", "结果", "原因"], agent_ack_rows))

    lines.append(report.section("端到端会话（每条业务边映射一条会话）"))
    session_rows = [
        [
            f"会话{i}",
            f"{s.source} → {s.target}",
            s.t_agent_id,
            s.n_agent_id,
            " → ".join(s.gateway_path or (s.source_gateway, s.target_gateway)),
        ]
        for i, s in enumerate(subnet.sessions, start=1)
    ]
    lines.append(report.table(["#", "业务流", "传输Agent", "网络Agent", "跨网关路径"], session_rows))

    lines.append(report.section("会话/路径支撑绑定（tAgent 管会话，nAgent 管路径）"))
    support_rows = []
    for session in subnet.sessions:
        session_support = subnet.session_supports[session.session_id]
        path_support = subnet.path_supports[session_support.path_support_id]
        support_rows.append(
            [
                session.session_id.rsplit("-", 1)[-1],
                session_support.support_id,
                session_support.t_agent_id,
                path_support.support_id,
                path_support.n_agent_id,
                " → ".join(path_support.gateway_path),
                _format_links(path_support.monitored_links),
            ]
        )
    lines.append(
        report.table(
            ["会话", "会话支撑ID", "tAgent", "路径支撑ID", "nAgent", "网关路径", "监测链路"],
            support_rows,
        )
    )

    lines.append(report.section("Controller 下发的网关任务级路由表（隔离转发）"))
    route_rows = []
    for gateway_id in sorted(subnet.involved_gateways):
        gateway = controller.gateways[gateway_id]
        for entry in gateway.installed_route_table():
            route_rows.append(
                [
                    gateway_id,
                    entry.session_id.rsplit("-", 1)[-1],
                    f"{entry.hop_index + 1}/{len(entry.gateway_path) or 1}",
                    f"{entry.match.src_agent} → {entry.match.dst_agent}",
                    entry.action.mode,
                    _route_action_target(entry),
                    f"{entry.t_agent_id} / {entry.n_agent_id}",
                    entry.path_support_id,
                ]
            )
    lines.append(
        report.table(
            ["网关", "会话", "跳", "业务流", "动作", "下一跳/本地投递", "支撑Agent", "路径支撑"],
            route_rows,
        )
    )

    lines.append(report.section("跨层边 E_m"))
    edge_rows = [
        [src, _EDGE_LABEL.get(kind, kind), dst] for src, dst, kind in sorted(subnet.edges, key=lambda e: (e[2], e[0]))
    ]
    lines.append(report.table(["源", "关系", "目标"], edge_rows))

    lines.append(report.section("各层 Agent 短时预测（horizon=3）"))
    pred_rows = [
        [_LAYER_LABEL.get(p.layer, p.layer), p.agent_id, p.metric, "→".join(f"{v:.2f}" for v in p.values)]
        for p in sorted(predictions, key=lambda x: (x.layer.value, x.agent_id))
    ]
    lines.append(report.table(["层", "Agent", "预测指标", "未来 H 步"], pred_rows))

    lines.append(report.section("网关确认"))
    ack_rows = [
        [
            ack.gateway_id,
            ack.operation,
            "接受 ✓" if ack.accepted else "拒绝 ✗",
            ack.session_count,
            ack.route_count,
            "  ".join(_short_session_id(item) for item in ack.installed_session_ids) or "-",
            ack.reason,
        ]
        for ack in subnet.gateway_acks
    ]
    lines.append(report.table(["网关", "操作", "结果", "会话数", "路由数", "会话", "原因"], ack_rows))

    print("\n".join(lines))


def _route_action_target(entry) -> str:
    action = entry.action
    if action.mode == "local_delivery":
        return f"{action.local_agent} @ {action.local_agent_ip}:{action.local_agent_port}"
    if action.mode == "forward_to_gateway":
        return f"{action.next_hop_gateway} @ {action.next_hop_gateway_ip}"
    return "deny"


def _format_links(links: tuple[tuple[str, str], ...]) -> str:
    if not links:
        return "本地"
    return "  ".join(f"{source}->{target}" for source, target in links)


def _short_session_id(session_id: str) -> str:
    return session_id.rsplit("-", 1)[-1]


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
