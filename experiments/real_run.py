from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import report
from src.agents.controls import FlowgenControl
from src.agents.forecast import SUPPORTED_FORECAST_METHODS
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator
from src.core.models import AgentAction, AgentPrediction, TaskSubnet, to_jsonable
from src.metrics.netns import NetnsMetricProvider, NetnsTarget
from src.metrics.provider import MetricProvider
from src.metrics.trace import TraceMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology
from testbed.netem import HtbController

_OP_LABEL = {
    "local_tune": "局部调参",
    "session_setup": "会话重调",
    "gateway_install": "网关装载",
    "member_confirm": "成员确认",
    "agent_replace": "替换支撑Agent",
    "reroute": "跨子网改接",
}

_STRATEGY_LABEL = {
    "local_tuning": "tier1 局部调参",
    "support_agent_replace": "tier2 替换支撑Agent",
    "support_session_retune": "tier2 会话重调",
    "communication_reroute": "tier3 调整通信关系",
    "no_adjustment": "无需调整",
    "full_rebuild": "全量重建",
}


@dataclass
class RealLoopResult:
    name: str
    risk: float
    predictions: list[AgentPrediction]
    actions: list[AgentAction]
    sense_log: list[dict[str, Any]]
    histories: dict[str, dict[str, list[float]]]


async def run(args: argparse.Namespace) -> dict:
    task = rescue_task()
    provider = _build_provider(args)
    controller = AgentController(build_rescue_topology(provider))
    subnet, build_metrics = await controller.build_task_subnet(task)
    path_measurement_bindings = _apply_path_support_targets(
        provider,
        subnet,
        _build_link_target_profile(args),
        controller,
    )
    executor = FlowgenControl(
        control_file=Path(args.flowgen_control_file),
        initial_target_mbps=args.initial_target_mbps,
        default_tcp_congestion=args.tcp_congestion,
        bandwidth_controller=_build_htb_controller(args),
    )
    _configure_real_agents(
        controller,
        subnet,
        args.horizon,
        args.history_steps,
        executor,
        args.forecast_method,
    )

    baseline = await _sample_predict_and_act(
        controller,
        subnet,
        name="baseline",
        history_steps=args.history_steps,
        interval_s=args.sample_interval_s,
        start_timestamp=0.0,
    )

    event = None
    adjustment = None
    full_rebuild_baseline = None
    if args.netem_delay_ms is not None or args.netem_loss_percent is not None:
        _apply_netem(args)
        try:
            event = await _sample_predict_and_act(
                controller,
                subnet,
                name="event",
                history_steps=args.history_steps,
                interval_s=args.sample_interval_s,
                start_timestamp=args.history_steps * args.sample_interval_s,
            )
            adjustment = await _evaluate_adjustment(
                controller,
                subnet,
                timestamp=2 * args.history_steps * args.sample_interval_s,
            )
            if not args.skip_rebuild_baseline:
                full_rebuild_baseline = await _evaluate_rebuild_baseline_from_args(
                    args,
                    timestamp=2 * args.history_steps * args.sample_interval_s + 0.1,
                )
        finally:
            if args.clear_netem:
                _clear_netem(args)
    else:
        adjustment = await _evaluate_adjustment(
            controller,
            subnet,
            timestamp=args.history_steps * args.sample_interval_s,
        )
        if not args.skip_rebuild_baseline:
            full_rebuild_baseline = await _evaluate_rebuild_baseline_from_args(
                args,
                timestamp=args.history_steps * args.sample_interval_s + 0.1,
            )

    return {
        "task_id": task.task_id,
        "target_profile": args.target_profile,
        "measurement_targets": _describe_provider_targets(provider),
        "path_measurement_bindings": path_measurement_bindings,
        "gateway_route_tables": _gateway_route_tables(controller),
        "support_bindings": _support_bindings(subnet),
        "agent_confirm_acks": to_jsonable(subnet.agent_acks),
        "gateway_install_acks": to_jsonable(subnet.gateway_acks),
        "build_metrics": to_jsonable(build_metrics),
        "history_steps": args.history_steps,
        "horizon": args.horizon,
        "forecast_method": args.forecast_method,
        "flowgen_control_file": str(args.flowgen_control_file),
        "executor_state": executor.state,
        "network_advice": executor.network_advice_log,
        "baseline": _result_to_jsonable(baseline),
        "event": _result_to_jsonable(event) if event is not None else None,
        "adjustment": adjustment,
        "full_rebuild_baseline": full_rebuild_baseline,
    }


def _build_provider(args: argparse.Namespace) -> MetricProvider:
    default_target = _netns_target(
        args,
        namespace=args.namespace,
        target_ip=args.target_ip,
        label="default measurement link",
    )
    provider: MetricProvider = NetnsMetricProvider(
        targets=_build_target_profile(args, default_target),
        default_target=default_target,
        app_rate_mbps=args.initial_target_mbps,
        cache_ttl_s=max(0.1, args.sample_interval_s * 0.8),
    )
    if args.trace_path and not args.no_trace:
        provider = TraceMetricProvider(
            args.trace_path,
            base_provider=provider,
            target_mean_mbps=args.initial_target_mbps,
            sample_interval_s=args.sample_interval_s,
        )
    return provider


def _build_htb_controller(args: argparse.Namespace) -> HtbController | None:
    if not args.htb_dev:
        return None
    event_active = args.netem_delay_ms is not None or args.netem_loss_percent is not None
    if event_active and args.htb_dev == args.event_dev and not args.htb_allow_replace_event_qdisc:
        raise ValueError(
            "htb_dev and event_dev both point at the same root qdisc. "
            "Use a different --htb-dev, omit --htb-dev, or pass "
            "--htb-allow-replace-event-qdisc if replacing the netem event is intentional."
        )
    return HtbController(
        namespace=args.htb_namespace,
        dev=args.htb_dev,
        total_mbps=args.htb_total_mbps,
        priority_mbps=args.htb_priority_mbps,
        default_mbps=args.htb_default_mbps,
        sudo=args.sudo,
        dry_run=args.htb_dry_run,
    )


def _netns_target(
    args: argparse.Namespace,
    *,
    namespace: str,
    target_ip: str,
    label: str,
) -> NetnsTarget:
    return NetnsTarget(
        iperf_port=args.iperf_port,
        namespace=namespace,
        target_ip=target_ip,
        ping_count=args.ping_count,
        ping_interval_s=args.ping_interval_s,
        iperf_seconds=args.iperf_seconds,
        command_timeout_s=args.command_timeout_s,
        sudo=args.sudo,
        label=label,
    )


def _build_target_profile(
    args: argparse.Namespace,
    default_target: NetnsTarget,
) -> dict[str, NetnsTarget]:
    if args.target_profile == "cloud":
        return {}

    term_to_edge = _netns_target(
        args,
        namespace=args.term_namespace,
        target_ip=args.edge_ip,
        label="terminal -> edge video stream",
    )
    edge_to_cloud = _netns_target(
        args,
        namespace=args.edge_namespace,
        target_ip=args.cloud_ip,
        label="edge -> cloud recognition result",
    )
    cloud_to_term = _netns_target(
        args,
        namespace=args.cloud_namespace,
        target_ip=args.term_ip,
        label="cloud -> terminal dispatch feedback",
    )
    end_to_end = _netns_target(
        args,
        namespace=args.term_namespace,
        target_ip=args.cloud_ip,
        label="terminal -> cloud end-to-end fallback",
    )

    return {
        "agent-drone-capture": term_to_edge,
        "agent-edge-recognition": edge_to_cloud,
        "agent-cloud-planning": cloud_to_term,
        "agent-terminal-feedback": end_to_end,
        "tagent-gw-ue": term_to_edge,
        "nagent-gw-ue": term_to_edge,
        "tagent-gw-mec": edge_to_cloud,
        "nagent-gw-mec": edge_to_cloud,
        "nagent-gw-mec-standby": edge_to_cloud,
        "tagent-gw-cloud": cloud_to_term,
        "nagent-gw-cloud": cloud_to_term,
        "nagent-gw-cloud-standby": cloud_to_term,
        "pagent-gw-ue": term_to_edge,
        "pagent-gw-mec": edge_to_cloud,
        "pagent-gw-cloud": cloud_to_term,
    }


def _build_link_target_profile(args: argparse.Namespace) -> dict[tuple[str, str], NetnsTarget]:
    gateway_targets = {
        "gw-ue": (
            args.term_namespace,
            args.term_ip,
            "terminal",
        ),
        "gw-mec": (
            args.edge_namespace,
            args.edge_ip,
            "edge",
        ),
        "gw-cloud": (
            args.cloud_namespace,
            args.cloud_ip,
            "cloud",
        ),
    }
    link_targets: dict[tuple[str, str], NetnsTarget] = {}
    for source_gateway, (namespace, _, source_label) in gateway_targets.items():
        for target_gateway, (_, target_ip, target_label) in gateway_targets.items():
            if source_gateway == target_gateway:
                continue
            link_targets[(source_gateway, target_gateway)] = _netns_target(
                args,
                namespace=namespace,
                target_ip=target_ip,
                label=f"{source_label} -> {target_label} path support",
            )
    return link_targets


def _apply_path_support_targets(
    provider: MetricProvider,
    subnet: TaskSubnet,
    link_targets: dict[tuple[str, str], NetnsTarget],
    controller: AgentController,
) -> dict[str, dict[str, Any]]:
    netns_provider = _netns_provider(provider)
    if netns_provider is None:
        return {}

    bindings: dict[str, dict[str, Any]] = {}
    for path_support in subnet.path_supports.values():
        agent = controller._agent_by_id(path_support.n_agent_id)
        preferred_gateway = agent.card.gateway_id if agent is not None else ""
        selected_link = _select_measurement_link(path_support.monitored_links, preferred_gateway, link_targets)
        if selected_link is None:
            continue
        target = link_targets[selected_link]
        netns_provider.targets[path_support.n_agent_id] = target
        bindings[path_support.n_agent_id] = {
            "path_support_id": path_support.support_id,
            "path_id": path_support.path_id,
            "gateway_path": list(path_support.gateway_path),
            "selected_link": list(selected_link),
            "measurement_target": _target_to_jsonable(target),
        }
    return bindings


def _select_measurement_link(
    monitored_links: tuple[tuple[str, str], ...],
    preferred_source_gateway: str,
    link_targets: dict[tuple[str, str], NetnsTarget],
) -> tuple[str, str] | None:
    preferred = [
        link
        for link in monitored_links
        if link[0] == preferred_source_gateway and link in link_targets
    ]
    if preferred:
        return preferred[0]
    for link in monitored_links:
        if link in link_targets:
            return link
    return None


def _netns_provider(provider: MetricProvider) -> NetnsMetricProvider | None:
    if isinstance(provider, NetnsMetricProvider):
        return provider
    base_provider = getattr(provider, "base_provider", None)
    if isinstance(base_provider, NetnsMetricProvider):
        return base_provider
    return None


def _target_to_jsonable(target: NetnsTarget) -> dict[str, int | float | str | bool]:
    return {
        "label": target.label,
        "namespace": target.namespace,
        "target_ip": target.target_ip,
        "iperf_port": target.iperf_port,
        "ping_count": target.ping_count,
        "ping_interval_s": target.ping_interval_s,
        "iperf_seconds": target.iperf_seconds,
        "sudo": target.sudo,
    }


def _configure_real_agents(
    controller: AgentController,
    subnet: TaskSubnet,
    horizon: int,
    history_steps: int,
    executor: FlowgenControl | None,
    forecast_method: str,
) -> None:
    for agent in controller._agents_for_subnet(subnet):
        agent.horizon = horizon
        agent.history_window = history_steps
        agent.forecast_method = forecast_method
        agent.action_executor = executor


async def _sample_predict_and_act(
    controller: AgentController,
    subnet: TaskSubnet,
    *,
    name: str,
    history_steps: int,
    interval_s: float,
    start_timestamp: float,
) -> RealLoopResult:
    agents = _ordered_agents(controller, subnet)
    sense_log: list[dict[str, Any]] = []
    for step in range(history_steps):
        timestamp = start_timestamp + step * interval_s
        for agent in agents:
            state = agent.sense(subnet.task, timestamp)
            sense_log.append(
                {
                    "step": step,
                    "timestamp": timestamp,
                    "agent_id": agent.agent_id,
                    "agent_name": agent.card.name,
                    "layer": agent.card.layer.value,
                    "measurement_target": _describe_agent_target(agent),
                    "values": dict(state.values),
                }
            )
        if interval_s > 0:
            await asyncio.sleep(interval_s)

    predictions: list[AgentPrediction] = []
    actions: list[AgentAction] = []
    for agent in agents:
        agent_predictions = agent.predict(subnet.task)
        predictions.extend(agent_predictions)
        action = agent.select_action(subnet.task, agent_predictions)
        actions.append(agent.apply(subnet.task, action))
    subnet.predictions = {f"{item.agent_id}:{item.metric}": item for item in predictions}
    subnet.actions = actions
    risk = RiskCalculator().risk(subnet.task, predictions)
    return RealLoopResult(
        name=name,
        risk=risk,
        predictions=predictions,
        actions=actions,
        sense_log=sense_log,
        histories=_agent_histories(agents),
    )


def _result_to_jsonable(result: RealLoopResult) -> dict:
    return {
        "name": result.name,
        "risk": result.risk,
        "sense_log": result.sense_log,
        "histories": result.histories,
        "predictions": [to_jsonable(item) for item in result.predictions],
        "actions": [to_jsonable(item) for item in result.actions],
        "demo_summary": _demo_summary(result),
    }


async def _evaluate_adjustment(
    controller: AgentController,
    subnet: TaskSubnet,
    timestamp: float,
) -> dict[str, Any]:
    risk_before, risk_after, adjustment = await controller.evaluate_and_adjust(subnet, timestamp)
    return _adjustment_to_jsonable(risk_before, risk_after, adjustment)


async def _evaluate_rebuild_baseline_from_args(
    args: argparse.Namespace,
    timestamp: float,
) -> dict[str, Any]:
    task = rescue_task()
    provider = _build_provider(args)
    controller = AgentController(build_rescue_topology(provider))
    subnet, build_metrics = await controller.build_task_subnet(task)
    path_measurement_bindings = _apply_path_support_targets(
        provider,
        subnet,
        _build_link_target_profile(args),
        controller,
    )
    _configure_real_agents(
        controller,
        subnet,
        args.horizon,
        args.history_steps,
        executor=None,
        forecast_method=args.forecast_method,
    )
    summary = await _evaluate_rebuild_baseline(controller, subnet, timestamp)
    summary["build_metrics"] = to_jsonable(build_metrics)
    summary["path_measurement_bindings"] = path_measurement_bindings
    return summary


async def _evaluate_rebuild_baseline(
    controller: AgentController,
    subnet: TaskSubnet,
    timestamp: float,
) -> dict[str, Any]:
    predictions = await controller.run_agent_loop(subnet, timestamp)
    risk_before = controller.risk_calculator.risk(subnet.task, predictions)
    _rebuilt, risk_after, adjustment = await controller.rebuild_task_subnet(subnet, timestamp + 0.1)
    return _adjustment_to_jsonable(risk_before, risk_after, adjustment)


def _adjustment_to_jsonable(
    risk_before: float,
    risk_after: float,
    adjustment,
) -> dict[str, Any]:
    return {
        "risk_before": risk_before,
        "risk_after": risk_after,
        "strategy": adjustment.strategy,
        "changed_agents": adjustment.changed_agents,
        "changed_edges": adjustment.changed_edges,
        "changed_gateways": adjustment.changed_gateways,
        "estimated_interruption_ms": adjustment.estimated_interruption_ms,
        "service_interruption_ms": adjustment.service_interruption_ms,
        "operations": dict(adjustment.operations),
        "actions": [to_jsonable(action) for action in adjustment.actions],
        "changed_sessions": [to_jsonable(session) for session in adjustment.changed_sessions],
    }


def _ordered_agents(controller: AgentController, subnet: TaskSubnet) -> list:
    return sorted(
        controller._agents_for_subnet(subnet),
        key=lambda agent: (agent.card.layer.value, agent.agent_id),
    )


def _agent_histories(agents: list) -> dict[str, dict[str, list[float]]]:
    return {
        agent.agent_id: {
            metric: [round(value, 6) for value in values]
            for metric, values in sorted(agent.history.items())
        }
        for agent in agents
    }


def _describe_agent_target(agent) -> dict[str, Any]:
    describe = getattr(agent.metric_provider, "describe_target", None)
    if describe is None:
        return {}
    return describe(agent.agent_id)


def _describe_provider_targets(provider: MetricProvider) -> dict[str, Any]:
    describe = getattr(provider, "describe_targets", None)
    if describe is None:
        return {}
    return describe()


def _demo_summary(result: RealLoopResult) -> dict[str, Any]:
    sensed_agents = sorted({item["agent_id"] for item in result.sense_log})
    links = sorted(
        {
            item.get("measurement_target", {}).get("label", "")
            for item in result.sense_log
            if item.get("measurement_target", {}).get("label")
        }
    )
    return {
        "sensed_agent_count": len(sensed_agents),
        "sensed_agents": sensed_agents,
        "measurement_links": links,
        "prediction_count": len(result.predictions),
        "action_count": len(result.actions),
        "actions": [
            {
                "agent_id": action.agent_id,
                "layer": action.layer.value,
                "action_type": action.action_type,
            }
            for action in result.actions
        ],
    }


def _summary_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "task_id": result["task_id"],
        "target_profile": result["target_profile"],
        "history_steps": result["history_steps"],
        "horizon": result["horizon"],
        "forecast_method": result.get("forecast_method", "adaptive"),
        "measurement_targets": result["measurement_targets"],
        "path_measurement_bindings": result["path_measurement_bindings"],
        "gateway_route_tables": result["gateway_route_tables"],
        "support_bindings": result["support_bindings"],
        "agent_confirm_acks": result["agent_confirm_acks"],
        "gateway_install_acks": result["gateway_install_acks"],
        "adjustment": result["adjustment"],
        "full_rebuild_baseline": result["full_rebuild_baseline"],
        "baseline": _compact_loop_result(result["baseline"]),
    }
    if result.get("event") is not None:
        payload["event"] = _compact_loop_result(result["event"])
    return payload


def _compact_loop_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "risk": result["risk"],
        "demo_summary": result["demo_summary"],
        "latest_sense": _latest_sense_by_agent(result["sense_log"]),
        "histories": result["histories"],
        "predictions": result["predictions"],
        "actions": result["actions"],
    }


def _latest_sense_by_agent(sense_log: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in sense_log:
        latest[item["agent_id"]] = {
            "layer": item["layer"],
            "measurement_target": item["measurement_target"],
            "values": item["values"],
        }
    return latest


def _render_real_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(report.banner("真实测试床闭环演示"))
    lines.append(report.section("运行配置"))
    lines.append(
        report.kv_block(
            [
                ("任务", result["task_id"]),
                ("测量 profile", result["target_profile"]),
                ("历史/预测窗口", f"{result['history_steps']} 步历史 → {result['horizon']} 步预测"),
                ("预测方法", result.get("forecast_method", "adaptive")),
                ("控制文件", result["flowgen_control_file"]),
            ]
        )
    )

    bindings = result.get("path_measurement_bindings") or {}
    if bindings:
        lines.append(report.section("nAgent 真实测量链路绑定"))
        rows = []
        for n_agent_id, binding in sorted(bindings.items()):
            target = binding.get("measurement_target", {})
            rows.append(
                [
                    n_agent_id,
                    " → ".join(binding.get("gateway_path", [])),
                    "->".join(binding.get("selected_link", [])),
                    target.get("label", ""),
                    f"{target.get('namespace', '')}:{target.get('target_ip', '')}",
                ]
            )
        lines.append(
            report.table(
                headers=["nAgent", "网关路径", "实测链路", "测量说明", "netns:目标IP"],
                rows=rows,
            )
        )

    lines.append(report.section("事件前后真实感知"))
    lines.append(_loop_snapshot_table("baseline", result["baseline"]))
    if result.get("event") is not None:
        lines.append("")
        lines.append(_loop_snapshot_table("event", result["event"]))

    adjustment = result.get("adjustment")
    if adjustment is not None:
        lines.append(report.section("Controller 最小弹性调整"))
        lines.append(_adjustment_block("最小调整", adjustment))

    full = result.get("full_rebuild_baseline")
    if adjustment is not None and full is not None:
        lines.append(report.section("最小调整 vs 全量重建"))
        minimal_ms = float(adjustment.get("estimated_interruption_ms", adjustment["service_interruption_ms"]))
        full_ms = float(full.get("estimated_interruption_ms", full["service_interruption_ms"]))
        lines.append(
            report.table(
                headers=["方法", "策略", "风险", "变更A/E/GW", "估计中断(ms)", "操作"],
                rows=[
                    [
                        "最小调整",
                        _strategy(adjustment["strategy"]),
                        f"{adjustment['risk_before']:.3f}→{adjustment['risk_after']:.3f}",
                        _change_counts(adjustment),
                        f"{minimal_ms:.0f}",
                        _op_summary(adjustment["operations"]),
                    ],
                    [
                        "全量重建",
                        _strategy(full["strategy"]),
                        f"{full['risk_before']:.3f}→{full['risk_after']:.3f}",
                        _change_counts(full),
                        f"{full_ms:.0f}",
                        _op_summary(full["operations"]),
                    ],
                ],
            )
        )
        lines.append(report.bullet(f"CostModel 估计中断对比：{report.pct_change(full_ms, minimal_ms)}"))
    return "\n".join(lines)


def _loop_snapshot_table(name: str, result: dict[str, Any]) -> str:
    latest = _latest_sense_by_agent(result["sense_log"])
    rows = []
    for agent_id, item in sorted(latest.items()):
        values = item["values"]
        target = item.get("measurement_target", {})
        rows.append(
            [
                name,
                item["layer"],
                agent_id,
                target.get("label", ""),
                _first_metric(values, "latency_ms", "rtt_ms"),
                _first_metric(values, "loss_rate", "retransmission_rate"),
                _first_metric(
                    values,
                    "available_bandwidth_mbps",
                    "throughput_mbps",
                    "send_rate_mbps",
                    "data_rate_mbps",
                ),
                _first_metric(values, "utilization"),
            ]
        )
    return report.table(
        headers=["阶段", "层", "Agent", "测量链路", "时延/RTT", "丢包/重传", "带宽/速率", "利用率"],
        rows=rows,
    )


def _first_metric(values: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in values:
            value = values[key]
            if isinstance(value, float):
                return f"{value:.3f}"
            return str(value)
    return "-"


def _adjustment_block(label: str, adjustment: dict[str, Any]) -> str:
    actions = [
        f"{item['agent_id']}:{item['action_type']}"
        for item in adjustment.get("actions", [])
        if item.get("action_type")
    ]
    return report.kv_block(
        [
            (label, _strategy(adjustment["strategy"])),
            ("风险", f"{adjustment['risk_before']:.3f} → {adjustment['risk_after']:.3f}"),
            ("变更A/E/GW", _change_counts(adjustment)),
            ("估计中断成本", f"{adjustment.get('estimated_interruption_ms', adjustment['service_interruption_ms']):.0f} ms（非实测恢复）"),
            ("操作", _op_summary(adjustment["operations"])),
            ("动作", "  ".join(actions) if actions else "无"),
        ]
    )


def _change_counts(adjustment: dict[str, Any]) -> str:
    return f"{adjustment['changed_agents']}/{adjustment['changed_edges']}/{adjustment['changed_gateways']}"


def _op_summary(operations: dict[str, int]) -> str:
    parts = [f"{_OP_LABEL.get(name, name)}×{count}" for name, count in operations.items() if count]
    return "  ".join(parts) if parts else "无"


def _strategy(name: str) -> str:
    return _STRATEGY_LABEL.get(name, name)


def _gateway_route_tables(controller: AgentController) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    for gateway_id, gateway in sorted(controller.gateways.items()):
        entries = []
        for entry in gateway.installed_route_table():
            item = to_jsonable(entry)
            if entry.action.mode == "forward_to_gateway":
                item["isolation"] = "remote_agent_ip_hidden"
            elif entry.action.mode == "local_delivery":
                item["isolation"] = "local_agent_ip_visible"
            entries.append(item)
        if entries:
            tables[gateway_id] = entries
    return tables


def _support_bindings(subnet: TaskSubnet) -> dict[str, Any]:
    return {
        "session_supports": to_jsonable(subnet.session_supports),
        "path_supports": to_jsonable(subnet.path_supports),
    }


def _apply_netem(args: argparse.Namespace) -> None:
    netem = ["netem"]
    if args.netem_delay_ms is not None:
        netem.extend(["delay", f"{args.netem_delay_ms:g}ms"])
    if args.netem_loss_percent is not None:
        netem.extend(["loss", f"{args.netem_loss_percent:g}%"])
    _run_tc(args, "replace", *netem)


def _clear_netem(args: argparse.Namespace) -> None:
    completed = _run_tc(args, "del", check=False)
    if completed.returncode != 0 and "No such file or directory" not in completed.stderr:
        raise RuntimeError(completed.stderr)


def _run_tc(args: argparse.Namespace, operation: str, *qdisc_args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = [
        "ip",
        "netns",
        "exec",
        args.event_namespace,
        "tc",
        "qdisc",
        operation,
        "dev",
        args.event_dev,
        "root",
        *qdisc_args,
    ]
    if args.sudo:
        command = ["sudo", *command]
    return subprocess.run(command, check=check, capture_output=True, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real netns Agent loop.")
    parser.add_argument(
        "--target-profile",
        choices=("rescue", "cloud"),
        default="rescue",
        help="rescue maps Agents to term->edge, edge->cloud and cloud->term links; cloud keeps the old single-link probe.",
    )
    parser.add_argument("--namespace", default="h-term")
    parser.add_argument("--target-ip", default="10.10.3.2")
    parser.add_argument("--term-namespace", default="h-term")
    parser.add_argument("--edge-namespace", default="h-edge")
    parser.add_argument("--cloud-namespace", default="h-cloud")
    parser.add_argument("--term-ip", default="10.10.1.2")
    parser.add_argument("--edge-ip", default="10.10.2.2")
    parser.add_argument("--cloud-ip", default="10.10.3.2")
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--ping-count", type=int, default=5)
    parser.add_argument("--ping-interval-s", type=float, default=0.2)
    parser.add_argument("--iperf-seconds", type=int, default=1)
    parser.add_argument("--command-timeout-s", type=float, default=8.0)
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--history-steps", type=int, default=10)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument(
        "--forecast-method",
        choices=SUPPORTED_FORECAST_METHODS,
        default="adaptive",
        help="Small-data forecast method for Agent predictions.",
    )
    parser.add_argument("--sample-interval-s", type=float, default=1.0)
    parser.add_argument("--initial-target-mbps", type=float, default=16.0)
    parser.add_argument("--tcp-congestion", default="cubic")
    parser.add_argument("--flowgen-control-file", default="/tmp/6g-agent-testbed/flowgen-control.json")
    parser.add_argument(
        "--trace-path",
        default=None,
        help="Optional NumPy trace path. Fetch SANet separately or provide your own trace.",
    )
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--event-namespace", default="h-router")
    parser.add_argument("--event-dev", default="rt-cloud0")
    parser.add_argument("--netem-delay-ms", type=float)
    parser.add_argument("--netem-loss-percent", type=float)
    parser.add_argument("--clear-netem", action="store_true", default=True)
    parser.add_argument("--htb-namespace", default="h-router")
    parser.add_argument("--htb-dev", help="Router device where nAgent installs tc htb for priority DSCP traffic.")
    parser.add_argument("--htb-total-mbps", type=float, default=100.0)
    parser.add_argument("--htb-priority-mbps", type=float, default=80.0)
    parser.add_argument("--htb-default-mbps", type=float, default=20.0)
    parser.add_argument("--htb-dry-run", action="store_true")
    parser.add_argument(
        "--htb-allow-replace-event-qdisc",
        action="store_true",
        help="Allow htb to replace a netem root qdisc on the same device. Usually keep this off.",
    )
    parser.add_argument(
        "--skip-rebuild-baseline",
        action="store_true",
        help="Skip the independent full-rebuild baseline comparison to shorten real testbed runs.",
    )
    parser.add_argument("--report", action="store_true", help="Print a compact Chinese demo report instead of JSON.")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    if args.report:
        print(_render_real_report(to_jsonable(result)))
        return
    if args.summary_only:
        result = _summary_payload(to_jsonable(result))
    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
