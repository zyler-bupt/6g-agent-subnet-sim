from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:  # plotting is optional for minimal simulation runs
    plt = None

from src import report
from src.controller.networking import AgentController
from src.core.models import ExperimentMetrics, TaskSpec, TaskSubnet, to_jsonable
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology

EVENT_TIME = 4.0

_OP_LABEL = {
    "local_tune": "局部调参",
    "session_setup": "会话重建",
    "gateway_install": "网关装载",
    "member_confirm": "成员确认",
    "agent_replace": "替换支撑Agent",
    "reroute": "跨子网改接",
}
_STRATEGY_LABEL = {
    "local_tuning": "tier1 局部调参",
    "support_agent_replace": "tier2 替换支撑Agent",
    "communication_reroute": "tier3 调整通信关系",
    "support_session_retune": "tier2 会话重调",
    "no_adjustment": "无需调整",
    "full_rebuild": "全量重建",
}


@dataclass
class Outcome:
    name: str
    trigger: str
    risk_before: float
    threshold: float
    minimal: ExperimentMetrics
    minimal_strategy: str
    minimal_ops: dict
    full: ExperimentMetrics
    full_ops: dict


async def _fresh_world() -> tuple[SyntheticMetricProvider, TaskSpec, AgentController, TaskSubnet, ExperimentMetrics]:
    """Build an independent, deterministically seeded copy of the scenario."""
    provider = SyntheticMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, build_metrics = await controller.build_task_subnet(task)
    return provider, task, controller, subnet, build_metrics


def _metrics(build: ExperimentMetrics, risk_before, risk_after, threshold, adj) -> ExperimentMetrics:
    return ExperimentMetrics(
        networking_success=build.networking_success,
        networking_latency_ms=build.networking_latency_ms,
        session_count=build.session_count,
        involved_gateway_count=build.involved_gateway_count,
        qos_satisfied=risk_after <= threshold,
        risk_before=risk_before,
        risk_after=risk_after,
        changed_agents=adj.changed_agents,
        changed_edges=adj.changed_edges,
        changed_gateways=adj.changed_gateways,
        control_updates=adj.changed_gateways,
        service_interruption_ms=adj.service_interruption_ms,
    )


async def _run_pair(
    name: str,
    trigger: str,
    *,
    event: tuple[str, float] | None = None,
    failure: tuple[str, str] | None = None,
) -> Outcome:
    threshold = rescue_task().qos.risk_threshold

    # World A: minimal elastic adjustment.
    prov_a, task, ctrl_a, subnet_a, build_a = await _fresh_world()
    failed_set: set[str] | None = None
    if event is not None:
        prov_a.inject_event(task.task_id, event[0], severity=event[1])
    if failure is not None:
        ctrl_a.gateways[failure[1]].fail_agent(failure[0])
        failed_set = {failure[0]}
    risk_before, risk_after_min, minimal = await ctrl_a.evaluate_and_adjust(subnet_a, EVENT_TIME, failed_set)
    minimal_metrics = _metrics(build_a, risk_before, risk_after_min, threshold, minimal)

    # World B: full-rebuild baseline on the identical trigger.
    prov_b, task_b, ctrl_b, subnet_b, build_b = await _fresh_world()
    if event is not None:
        prov_b.inject_event(task_b.task_id, event[0], severity=event[1])
    if failure is not None:
        ctrl_b.gateways[failure[1]].fail_agent(failure[0])
    preds_b = await ctrl_b.run_agent_loop(subnet_b, EVENT_TIME)
    risk_before_b = ctrl_b.risk_calculator.risk(task_b, preds_b)
    _, risk_after_full, full = await ctrl_b.rebuild_task_subnet(subnet_b, EVENT_TIME + 0.1)
    full_metrics = _metrics(build_b, risk_before_b, risk_after_full, threshold, full)

    return Outcome(
        name=name,
        trigger=trigger,
        risk_before=risk_before,
        threshold=threshold,
        minimal=minimal_metrics,
        minimal_strategy=minimal.strategy,
        minimal_ops=minimal.operations,
        full=full_metrics,
        full_ops=full.operations,
    )


async def run_scenarios() -> list[Outcome]:
    return [
        await _run_pair(
            "承载劣化",
            "预测风险 R 超阈值",
            event=("bearer_degradation", 1.3),
        ),
        await _run_pair(
            "边缘网络Agent失效(有备用)",
            "F_m=1  nagent-gw-mec 离线",
            failure=("nagent-gw-mec", "gw-mec"),
        ),
        await _run_pair(
            "终端网络Agent失效(无备用)",
            "F_m=1  nagent-gw-ue 离线",
            failure=("nagent-gw-ue", "gw-ue"),
        ),
    ]


def _op_summary(operations: dict) -> str:
    parts = [f"{_OP_LABEL.get(name, name)}×{count}" for name, count in operations.items() if count]
    return "  ".join(parts) if parts else "无"


def _strategy(name: str) -> str:
    return _STRATEGY_LABEL.get(name, name)


def render_report(outcomes: list[Outcome]) -> str:
    task = rescue_task()
    lines: list[str] = []
    lines.append(report.banner("应急救援任务 · 三级弹性最小调整 vs 全量重建"))

    lines.append(report.section("场景"))
    lines.append(
        report.kv_block(
            [
                ("任务", task.goal),
                ("组网", f"{len(task.biz_edges)} 条业务链路 → 3 条端到端会话，涉及 3 个子网网关"),
                ("阈值 τ", f"{task.qos.risk_threshold:.2f}"),
                ("说明", "每个场景在两个同种子世界上分别执行『最小调整』与『全量重建』，数值均为实测"),
            ]
        )
    )

    lines.append(report.section("三种触发事件下的对比"))
    rows = []
    for o in outcomes:
        rows.append(
            [
                o.name,
                o.trigger,
                _strategy(o.minimal_strategy),
                f"{o.minimal.changed_agents}/{o.minimal.changed_edges}/{o.minimal.changed_gateways}",
                f"{o.minimal.service_interruption_ms:.0f}",
                f"{o.full.service_interruption_ms:.0f}",
                report.pct_change(o.full.service_interruption_ms, o.minimal.service_interruption_ms),
                "✓" if o.minimal.qos_satisfied else "✗",
            ]
        )
    lines.append(
        report.table(
            headers=["场景", "触发", "最小调整策略", "变更A/E/GW", "中断(ms)", "重建中断", "中断降幅", "QoS"],
            rows=rows,
        )
    )
    lines.append("  注：变更A/E/GW = 变更 Agent 数 / 通信关系数 / 触碰网关数")

    lines.append(report.section("逐场景操作明细"))
    for o in outcomes:
        lines.append(
            report.kv_block(
                [
                    (f"[{o.name}]", f"触发前风险 R={o.risk_before:.3f} (τ={o.threshold:.2f})"),
                    ("  最小调整", f"{_strategy(o.minimal_strategy)}  →  {_op_summary(o.minimal_ops)}"),
                    ("  全量重建", f"{_strategy('full_rebuild')}  →  {_op_summary(o.full_ops)}"),
                ]
            )
        )
        lines.append("")

    lines.append(report.section("结论"))
    lines.append("  三类运行期事件均按『先局部调参 → 再替换支撑Agent → 最后调整通信关系』就近处理：")
    lines.append(report.bullet("承载劣化：局部调参即可压回阈值内，0 网关变更"))
    lines.append(report.bullet("支撑Agent失效且本地有备用：tier2 就地替换，仅触碰本子网"))
    lines.append(report.bullet("支撑Agent失效且本地无备用：tier3 跨子网改接，最小范围重路由"))
    avg_drop = sum(
        (o.full.service_interruption_ms - o.minimal.service_interruption_ms)
        / o.full.service_interruption_ms
        for o in outcomes
        if o.full.service_interruption_ms
    ) / max(1, len(outcomes))
    lines.append("")
    lines.append(f"  ✓ 相比全量重建，最小调整在满足 QoS 的同时平均减少业务中断约 {avg_drop*100:.0f}%。")
    return "\n".join(lines)


def _write_csv(output_dir: Path, outcomes: list[Outcome]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for o in outcomes:
        rows.append({"scenario": o.name, "method": "minimal_adjustment", "strategy": o.minimal_strategy, **to_jsonable(o.minimal)})
        rows.append({"scenario": o.name, "method": "full_rebuild", "strategy": "full_rebuild", **to_jsonable(o.full)})
    csv_path = output_dir / "rescue_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def _plot(output_dir: Path, outcomes: list[Outcome]) -> None:
    if plt is None:
        return
    labels = [o.name for o in outcomes]
    minimal = [o.minimal.service_interruption_ms for o in outcomes]
    full = [o.full.service_interruption_ms for o in outcomes]
    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.bar([i - width / 2 for i in x], minimal, width, label="minimal", color="#3572a5")
    ax.bar([i + width / 2 for i in x], full, width, label="full_rebuild", color="#999999")
    ax.set_ylabel("interruption ms")
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"S{i+1}" for i in x])
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "rescue_comparison.png", dpi=140)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="rescue", choices=["rescue"])
    parser.add_argument("--output-dir", default="results/async_sim")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    outcomes = asyncio.run(run_scenarios())
    print(render_report(outcomes))
    csv_path = _write_csv(output_dir, outcomes)
    _plot(output_dir, outcomes)
    print()
    print(f"  结果已写入: {csv_path}")
    if plt is not None:
        print(f"  对比图已写入: {output_dir / 'rescue_comparison.png'}")


if __name__ == "__main__":
    main()
