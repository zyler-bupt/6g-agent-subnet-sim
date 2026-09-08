from __future__ import annotations

import argparse
import json
import subprocess
import time

from src.core.models import to_jsonable
from src.metrics.netns import NetnsMetricProvider, NetnsTarget


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe one real netns MetricSnapshot.")
    parser.add_argument("--namespace", default="h-term")
    parser.add_argument("--target-ip", default="10.10.3.2")
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--ping-count", type=int, default=5)
    parser.add_argument("--ping-interval-s", type=float, default=0.2)
    parser.add_argument("--iperf-seconds", type=int, default=1)
    parser.add_argument("--app-rate-mbps", type=float, default=16.0)
    parser.add_argument("--task-id", default="task-rescue-001")
    parser.add_argument("--agent-id", default="nagent-gw-ue")
    parser.add_argument("--sudo", action="store_true", help="prefix netns commands with sudo")
    parser.add_argument("--event-namespace", default="h-router")
    parser.add_argument("--event-dev", default="rt-cloud0")
    parser.add_argument("--netem-delay-ms", type=float)
    parser.add_argument("--netem-loss-percent", type=float)
    parser.add_argument("--clear-netem", action="store_true")
    args = parser.parse_args()

    target = NetnsTarget(
        namespace=args.namespace,
        target_ip=args.target_ip,
        iperf_port=args.iperf_port,
        ping_count=args.ping_count,
        ping_interval_s=args.ping_interval_s,
        iperf_seconds=args.iperf_seconds,
        sudo=args.sudo,
    )
    provider = NetnsMetricProvider(
        targets={args.agent_id: target},
        default_target=target,
        app_rate_mbps=args.app_rate_mbps,
        cache_ttl_s=0.0,
    )
    if args.netem_delay_ms is not None or args.netem_loss_percent is not None:
        _apply_netem(args)
    snapshot = provider.snapshot(args.task_id, args.agent_id, time.time())
    print(json.dumps(to_jsonable(snapshot), ensure_ascii=False, indent=2, sort_keys=True))
    if args.clear_netem:
        _clear_netem(args)


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


if __name__ == "__main__":
    main()
