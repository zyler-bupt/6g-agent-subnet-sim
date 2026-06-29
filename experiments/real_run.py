from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from src.agents.controls import FlowgenControl
from src.controller.networking import AgentController
from src.controller.risk import RiskCalculator
from src.core.models import AgentPrediction, AgentAction, TaskSubnet, to_jsonable
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
    target = NetnsTarget(
        namespace=args.namespace,
        target_ip=args.target_ip,
        iperf_port=args.iperf_port,
        ping_count=args.ping_count,
        ping_interval_s=args.ping_interval_s,
        iperf_seconds=args.iperf_seconds,
        command_timeout_s=args.command_timeout_s,
        sudo=args.sudo,
    )
    provider: MetricProvider = NetnsMetricProvider(
        targets={},
        default_target=target,
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
    agents = controller._agents_for_subnet(subnet)
    for step in range(history_steps):
        timestamp = start_timestamp + step * interval_s
        for agent in agents:
            agent.sense(subnet.task, timestamp)
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
    return RealLoopResult(name=name, risk=risk, predictions=predictions, actions=actions)


def _result_to_jsonable(result: RealLoopResult) -> dict:
    return {
        "name": result.name,
        "risk": result.risk,
        "predictions": [to_jsonable(item) for item in result.predictions],
        "actions": [to_jsonable(item) for item in result.actions],
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
    parser.add_argument("--namespace", default="h-term")
    parser.add_argument("--target-ip", default="10.10.3.2")
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
    args = parser.parse_args()

    result = asyncio.run(run(args))
    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
