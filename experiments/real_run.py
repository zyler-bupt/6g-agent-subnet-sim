from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.controls import FlowgenControl
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator
from src.core.models import AgentAction, AgentPrediction, TaskSubnet, to_jsonable
from src.metrics.netns import NetnsMetricProvider, NetnsTarget
from src.metrics.provider import MetricProvider
from src.metrics.trace import TraceMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


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
    executor = FlowgenControl(
        control_file=Path(args.flowgen_control_file),
        initial_target_mbps=args.initial_target_mbps,
        default_tcp_congestion=args.tcp_congestion,
    )
    _configure_real_agents(controller, subnet, args.horizon, args.history_steps, executor)

    baseline = await _sample_predict_and_act(
        controller,
        subnet,
        name="baseline",
        history_steps=args.history_steps,
        interval_s=args.sample_interval_s,
        start_timestamp=0.0,
    )

    event = None
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
        finally:
            if args.clear_netem:
                _clear_netem(args)

    return {
        "task_id": task.task_id,
        "target_profile": args.target_profile,
        "measurement_targets": _describe_provider_targets(provider),
        "gateway_route_tables": _gateway_route_tables(controller),
        "support_bindings": _support_bindings(subnet),
        "agent_confirm_acks": to_jsonable(subnet.agent_acks),
        "gateway_install_acks": to_jsonable(subnet.gateway_acks),
        "build_metrics": to_jsonable(build_metrics),
        "history_steps": args.history_steps,
        "horizon": args.horizon,
        "flowgen_control_file": str(args.flowgen_control_file),
        "executor_state": executor.state,
        "network_advice": executor.network_advice_log,
        "baseline": _result_to_jsonable(baseline),
        "event": _result_to_jsonable(event) if event is not None else None,
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
        "pagent-stub-ue": term_to_edge,
    }


def _configure_real_agents(
    controller: AgentController,
    subnet: TaskSubnet,
    horizon: int,
    history_steps: int,
    executor: FlowgenControl,
) -> None:
    for agent in controller._agents_for_subnet(subnet):
        agent.horizon = horizon
        agent.history_window = history_steps
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
        "measurement_targets": result["measurement_targets"],
        "gateway_route_tables": result["gateway_route_tables"],
        "support_bindings": result["support_bindings"],
        "agent_confirm_acks": result["agent_confirm_acks"],
        "gateway_install_acks": result["gateway_install_acks"],
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
    parser.add_argument("--sample-interval-s", type=float, default=1.0)
    parser.add_argument("--initial-target-mbps", type=float, default=16.0)
    parser.add_argument("--tcp-congestion", default="cubic")
    parser.add_argument("--flowgen-control-file", default="/tmp/6g-agent-testbed/flowgen-control.json")
    parser.add_argument("--trace-path", default="third_party/SANet/data/example_band_n1/traffic.npy")
    parser.add_argument("--no-trace", action="store_true")
    parser.add_argument("--event-namespace", default="h-router")
    parser.add_argument("--event-dev", default="rt-cloud0")
    parser.add_argument("--netem-delay-ms", type=float)
    parser.add_argument("--netem-loss-percent", type=float)
    parser.add_argument("--clear-netem", action="store_true", default=True)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args))
    if args.summary_only:
        result = _summary_payload(to_jsonable(result))
    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
