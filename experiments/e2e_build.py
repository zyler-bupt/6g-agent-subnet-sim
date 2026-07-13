from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import asdict
from pathlib import Path
from statistics import fmean, median
from typing import Any

from src import report
from src.controller.networking import AgentController
from src.core.models import to_jsonable
from src.e2e.build import build_task_subnet_e2e
from src.e2e.factory import build_rescue_netns_provider
from src.e2e.installers import (
    NetnsGatewayInstaller,
    SimulatedGatewayInstaller,
    rescue_netns_gateway_targets,
)
from src.e2e.verifiers import NetnsTaskSubnetVerifier, SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.topology import build_rescue_topology


DEFAULT_RESCUE_INTENT = "建立应急救援无人机、边缘识别、云端决策和现场反馈任务通信子网"


async def run_e2e_build_experiment(args: argparse.Namespace) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for run_id in range(1, args.rounds + 1):
        provider = _metric_provider(args)
        controller = AgentController(build_rescue_topology(provider))
        installer = _installer(args, run_id)
        verifier = _verifier(args, provider)
        _subnet, metrics, install_result, verify_result = await build_task_subnet_e2e(
            args.intent,
            controller,
            provider,
            installer=installer,
            verifier=verifier,
        )
        sample = {"scenario": args.scenario, "run_id": run_id, **asdict(metrics)}
        samples.append(sample)
        details.append(
            {
                "run_id": run_id,
                "install": asdict(install_result),
                "verify": asdict(verify_result),
            }
        )
    return {
        "scenario": args.scenario,
        "rounds": args.rounds,
        "measurement_mode": _measurement_mode(args.install_mode, args.verify_mode),
        "threshold_ms": 5000.0,
        "summary": _summarize(samples),
        "samples": samples,
        "details": details,
    }


def _measurement_mode(install_mode: str, verify_mode: str) -> str:
    if install_mode == "netns" and verify_mode == "netns":
        return "real-netns"
    if install_mode == "simulated" and verify_mode == "synthetic":
        return "simulated"
    return "mixed"


def _metric_provider(args: argparse.Namespace):
    if args.verify_mode == "netns":
        return build_rescue_netns_provider(
            term_namespace=args.term_namespace,
            edge_namespace=args.edge_namespace,
            cloud_namespace=args.cloud_namespace,
            term_ip=args.term_ip,
            edge_ip=args.edge_ip,
            cloud_ip=args.cloud_ip,
            iperf_port=args.iperf_port,
            ping_count=args.ping_count,
            ping_interval_s=args.ping_interval_s,
            iperf_seconds=args.iperf_seconds,
            command_timeout_s=args.command_timeout_s,
            sudo=args.sudo,
        )
    return SyntheticMetricProvider(base_app_rate_mbps=24.0)


def _installer(args: argparse.Namespace, run_id: int):
    if args.install_mode == "netns":
        return NetnsGatewayInstaller(
            rescue_netns_gateway_targets(
                term_namespace=args.term_namespace,
                edge_namespace=args.edge_namespace,
                cloud_namespace=args.cloud_namespace,
            ),
            sudo=args.sudo,
            command_timeout_s=args.command_timeout_s,
        )
    return SimulatedGatewayInstaller(
        control_rtt_ms=args.control_rtt_ms,
        rule_install_ms=args.rule_install_ms,
        ack_ms=args.ack_ms,
        jitter_ms=args.jitter_ms,
        seed=args.seed + run_id,
    )


def _verifier(args: argparse.Namespace, provider):
    if args.verify_mode == "netns":
        return NetnsTaskSubnetVerifier(provider)
    return SyntheticTaskSubnetVerifier(provider, probe_delay_ms=args.synthetic_probe_delay_ms)


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    e2e = [float(item["e2e_build_ms"]) for item in samples]
    controller = [float(item["controller_build_ms"]) for item in samples]
    return {
        "success": all(bool(item["success"]) for item in samples),
        "sample_count": len(samples),
        "controller_build_avg_ms": fmean(controller),
        "controller_build_p95_ms": _percentile(controller, 95),
        "e2e_build_avg_ms": fmean(e2e),
        "e2e_build_median_ms": median(e2e),
        "e2e_build_p95_ms": _percentile(e2e, 95),
        "e2e_build_min_ms": min(e2e),
        "e2e_build_max_ms": max(e2e),
        "meets_5s_p95": _percentile(e2e, 95) <= 5000.0,
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def render_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [report.banner("端到端任务通信子网组网时延")]
    lines.append(report.section("测量口径"))
    lines.append(
        report.kv_block(
            [
                ("起点", "接收用户组网意图"),
                ("终点", "真实/模拟规则装载成功，且全部关键业务边通过 QoS 验证"),
                ("控制器时延", "controller_build_ms（原 networking_latency_ms，仅进程内逻辑与 ACK）"),
                ("端到端时延", "semantic + mapping + controller + install + verify"),
                ("底层网络", "假设 netns/veth/基础路由与网关运行环境已预部署"),
            ]
        )
    )
    lines.append(report.section("统计结果"))
    lines.append(
        report.kv_block(
            [
                ("成功", "是" if summary["success"] else "否"),
                ("Controller 平均/P95", f"{summary['controller_build_avg_ms']:.3f} / {summary['controller_build_p95_ms']:.3f} ms"),
                ("E2E 平均/P95", f"{summary['e2e_build_avg_ms']:.3f} / {summary['e2e_build_p95_ms']:.3f} ms"),
                (
                    "5 秒指标",
                    ("满足" if summary["meets_5s_p95"] else "不满足")
                    + ("（真实 netns 测量）" if result["measurement_mode"] == "real-netns" else "（模拟/混合配置，仅供回归）"),
                ),
            ]
        )
    )
    lines.append(report.section("分项明细"))
    lines.append(
        report.table(
            headers=["run", "semantic", "mapping", "controller", "install", "verify", "e2e", "边", "成功"],
            rows=[
                [
                    item["run_id"],
                    f"{item['semantic_ms']:.3f}",
                    f"{item['task_mapping_ms']:.3f}",
                    f"{item['controller_build_ms']:.3f}",
                    f"{item['gateway_install_ms']:.3f}",
                    f"{item['business_verify_ms']:.3f}",
                    f"{item['e2e_build_ms']:.3f}",
                    f"{item['passed_edges']}/{item['checked_edges']}",
                    "Y" if item["success"] else "N",
                ]
                for item in result["samples"][:20]
            ],
        )
    )
    errors = [error for item in result["samples"] for error in item.get("errors", ())]
    if errors:
        lines.append(report.section("错误"))
        lines.extend(report.bullet(error) for error in errors[:10])
    return "\n".join(lines)


def write_csv(path: str | Path, samples: list[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scenario",
        "run_id",
        "semantic_ms",
        "task_mapping_ms",
        "controller_build_ms",
        "gateway_install_ms",
        "business_verify_ms",
        "e2e_build_ms",
        "success",
        "session_count",
        "involved_gateway_count",
        "checked_edges",
        "passed_edges",
        "install_mode",
        "verify_mode",
        "controller_success",
        "install_success",
        "verify_success",
        "updated_rule_count",
        "errors",
    ]
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sample in samples:
            writer.writerow({**sample, "errors": " | ".join(sample.get("errors", ()))})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure end-to-end task-subnet build latency.")
    parser.add_argument("--scenario", choices=("rescue",), default="rescue")
    parser.add_argument("--intent", default=DEFAULT_RESCUE_INTENT)
    parser.add_argument("--semantic-mode", choices=("rule",), default="rule")
    parser.add_argument("--install-mode", choices=("simulated", "netns"), default="simulated")
    parser.add_argument("--verify-mode", choices=("synthetic", "netns"), default="synthetic")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--control-rtt-ms", type=float, default=10.0)
    parser.add_argument("--rule-install-ms", type=float, default=30.0)
    parser.add_argument("--ack-ms", type=float, default=5.0)
    parser.add_argument("--jitter-ms", type=float, default=5.0)
    parser.add_argument("--synthetic-probe-delay-ms", type=float, default=10.0)
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--term-namespace", default="h-term")
    parser.add_argument("--edge-namespace", default="h-edge")
    parser.add_argument("--cloud-namespace", default="h-cloud")
    parser.add_argument("--term-ip", default="10.10.1.2")
    parser.add_argument("--edge-ip", default="10.10.2.2")
    parser.add_argument("--cloud-ip", default="10.10.3.2")
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--ping-count", type=int, default=3)
    parser.add_argument("--ping-interval-s", type=float, default=0.2)
    parser.add_argument("--iperf-seconds", type=int, default=1)
    parser.add_argument("--command-timeout-s", type=float, default=8.0)
    parser.add_argument("--json-output")
    parser.add_argument("--csv-output")
    parser.add_argument("--report", action="store_true", default=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.rounds <= 0:
        raise SystemExit("--rounds must be positive")
    result = asyncio.run(run_e2e_build_experiment(args))
    if args.csv_output:
        write_csv(args.csv_output, result["samples"])
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(render_report(to_jsonable(result)))


if __name__ == "__main__":
    main()
