from __future__ import annotations

import argparse
import asyncio
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median
from time import perf_counter
from typing import Any

from src import report
from src.controller.networking import AgentController
from src.core.models import TaskState, to_jsonable
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


@dataclass(frozen=True)
class NetworkingLatencySample:
    round: int
    success: bool
    subnet_state: str
    controller_build_ms: float
    networking_latency_ms: float
    wall_latency_ms: float
    session_count: int
    involved_gateway_count: int
    route_count: int
    agent_ack_count: int
    gateway_ack_count: int


async def measure_networking_latency(
    *,
    rounds: int = 20,
    warmup: int = 3,
) -> dict[str, Any]:
    if rounds <= 0:
        raise ValueError("rounds must be positive")
    if warmup < 0:
        raise ValueError("warmup must not be negative")

    samples: list[NetworkingLatencySample] = []
    for index in range(warmup + rounds):
        sample = await _run_one(index - warmup + 1)
        if index >= warmup:
            samples.append(sample)

    return {
        "scenario": "rescue",
        "rounds": rounds,
        "warmup": warmup,
        "summary": _summary(samples),
        "samples": [sample.__dict__ for sample in samples],
    }


async def _run_one(round_index: int) -> NetworkingLatencySample:
    provider = SyntheticMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    started = perf_counter()
    subnet, metrics = await controller.build_task_subnet(task)
    wall_latency_ms = (perf_counter() - started) * 1000
    return NetworkingLatencySample(
        round=round_index,
        success=metrics.networking_success and subnet.state == TaskState.NETWORKED,
        subnet_state=subnet.state.value,
        controller_build_ms=metrics.controller_build_ms,
        networking_latency_ms=metrics.networking_latency_ms,
        wall_latency_ms=wall_latency_ms,
        session_count=metrics.session_count,
        involved_gateway_count=metrics.involved_gateway_count,
        route_count=sum(len(items) for items in subnet.gateway_routes.values()),
        agent_ack_count=len(subnet.agent_acks),
        gateway_ack_count=len(subnet.gateway_acks),
    )


def _summary(samples: list[NetworkingLatencySample]) -> dict[str, float | int | bool]:
    measured = [sample.controller_build_ms for sample in samples]
    wall = [sample.wall_latency_ms for sample in samples]
    return {
        "success": all(sample.success for sample in samples),
        "sample_count": len(samples),
        "controller_build_avg_ms": fmean(measured),
        "controller_build_median_ms": median(measured),
        "controller_build_min_ms": min(measured),
        "controller_build_max_ms": max(measured),
        "controller_build_p95_ms": _percentile(measured, 95),
        "wall_avg_ms": fmean(wall),
        "wall_median_ms": median(wall),
        "wall_p95_ms": _percentile(wall, 95),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
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
    samples = result["samples"]
    lines: list[str] = []
    lines.append(report.banner("Controller 侧任务子网构建时延测量"))
    lines.append(report.section("口径"))
    lines.append(
        report.kv_block(
            [
                ("测量对象", "controller.build_task_subnet(task)"),
                ("指标名", "controller_build_ms（networking_latency_ms 为兼容别名）"),
                ("包含", "成员/支撑 Agent 选择、session/path 与规则表生成、进程内 Gateway ACK"),
                ("不包含", "真实控制消息、数据面规则装载、ping/iperf 业务验证、语义解析"),
                ("结论边界", "该结果不是端到端组网时延，不能直接与 5 秒指标比较"),
                ("场景", result["scenario"]),
                ("轮次", f"{result['rounds']} 次，warmup={result['warmup']}"),
            ]
        )
    )
    lines.append(report.section("统计结果"))
    lines.append(
        report.kv_block(
            [
                ("成功", "是" if summary["success"] else "否"),
                ("平均 Controller 构建时延", f"{summary['controller_build_avg_ms']:.3f} ms"),
                ("中位数", f"{summary['controller_build_median_ms']:.3f} ms"),
                ("P95", f"{summary['controller_build_p95_ms']:.3f} ms"),
                ("最小/最大", f"{summary['controller_build_min_ms']:.3f} / {summary['controller_build_max_ms']:.3f} ms"),
                ("外层 wall-clock P95", f"{summary['wall_p95_ms']:.3f} ms"),
            ]
        )
    )
    lines.append(report.section("前 10 轮明细"))
    lines.append(
        report.table(
            headers=["轮次", "成功", "状态", "controller(ms)", "sessions", "gateways", "routes", "acks"],
            rows=[
                [
                    item["round"],
                    "Y" if item["success"] else "N",
                    item["subnet_state"],
                    f"{item['controller_build_ms']:.3f}",
                    item["session_count"],
                    item["involved_gateway_count"],
                    item["route_count"],
                    f"{item['agent_ack_count']}/{item['gateway_ack_count']}",
                ]
                for item in samples[:10]
            ],
        )
    )
    return "\n".join(lines)


def write_csv(path: str | Path, samples: list[dict[str, Any]]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(samples[0].keys()))
        writer.writeheader()
        writer.writerows(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure controller-only task-subnet build latency.")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--json-output")
    parser.add_argument("--csv-output")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(
        measure_networking_latency(
            rounds=args.rounds,
            warmup=args.warmup,
        )
    )
    if args.csv_output:
        write_csv(args.csv_output, result["samples"])
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.report:
        print(render_report(to_jsonable(result)))
    else:
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
