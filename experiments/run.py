from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:  # plotting is optional for minimal simulation runs
    plt = None

from src import report
from src.controller.networking import AgentController
from src.core.models import ExperimentMetrics, TaskSpec, TaskSubnet, to_jsonable
from src.metrics.mock import MockMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology

EVENT_TYPE = "bearer_degradation"
EVENT_SEVERITY = 1.3
EVENT_TIME = 4.0


async def _fresh_world() -> tuple[MockMetricProvider, TaskSpec, AgentController, TaskSubnet, ExperimentMetrics]:
    """Build an independent, deterministically seeded copy of the scenario.

    Two worlds built this way see identical metrics and the identical injected
    event, so applying minimal-adjustment to one and full-rebuild to the other
    is a controlled comparison on exactly the same condition.
    """
    provider = MockMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, build_metrics = await controller.build_task_subnet(task)
    return provider, task, controller, subnet, build_metrics


async def run_rescue(output_dir: Path) -> tuple[ExperimentMetrics, ExperimentMetrics, dict]:
    # --- World A: minimal elastic adjustment -------------------------------
    prov_a, task, ctrl_a, subnet_a, build_metrics = await _fresh_world()
    prov_a.inject_event(task.task_id, EVENT_TYPE, severity=EVENT_SEVERITY)
    risk_before, risk_after_min, minimal = await ctrl_a.evaluate_and_adjust(subnet_a, EVENT_TIME)

    # --- World B: full-rebuild baseline on the identical event -------------
    prov_b, task_b, ctrl_b, subnet_b, _ = await _fresh_world()
    prov_b.inject_event(task_b.task_id, EVENT_TYPE, severity=EVENT_SEVERITY)
    preds_b = await ctrl_b.run_agent_loop(subnet_b, EVENT_TIME)
    risk_before_b = ctrl_b.risk_calculator.risk(task_b, preds_b)
    _, risk_after_full, full = await ctrl_b.rebuild_task_subnet(subnet_b, EVENT_TIME + 0.1)

    minimal_metrics = ExperimentMetrics(
        networking_success=build_metrics.networking_success,
        networking_latency_ms=build_metrics.networking_latency_ms,
        session_count=build_metrics.session_count,
        involved_gateway_count=build_metrics.involved_gateway_count,
        qos_satisfied=risk_after_min <= task.qos.risk_threshold,
        risk_before=risk_before,
        risk_after=risk_after_min,
        changed_agents=minimal.changed_agents,
        changed_edges=minimal.changed_edges,
        changed_gateways=minimal.changed_gateways,
        control_updates=minimal.changed_gateways,
        service_interruption_ms=minimal.service_interruption_ms,
    )
    full_metrics = ExperimentMetrics(
        networking_success=True,
        networking_latency_ms=build_metrics.networking_latency_ms,
        session_count=build_metrics.session_count,
        involved_gateway_count=full.changed_gateways,
        qos_satisfied=risk_after_full <= task.qos.risk_threshold,
        risk_before=risk_before_b,
        risk_after=risk_after_full,
        changed_agents=full.changed_agents,
        changed_edges=full.changed_edges,
        changed_gateways=full.changed_gateways,
        control_updates=full.changed_gateways,
        service_interruption_ms=full.service_interruption_ms,
    )

    context = {
        "task": task,
        "minimal_strategy": minimal.strategy,
        "minimal_ops": minimal.operations,
        "full_strategy": full.strategy,
        "full_ops": full.operations,
    }

    _write_csv(output_dir, minimal_metrics, full_metrics)
    _plot(output_dir, minimal_metrics, full_metrics)
    return minimal_metrics, full_metrics, context


def _op_summary(operations: dict[str, int]) -> str:
    label = {
        "local_tune": "局部调参",
        "session_setup": "会话重建",
        "gateway_install": "网关装载",
        "member_confirm": "成员确认",
        "agent_replace": "替换支撑Agent",
    }
    parts = [f"{label.get(name, name)}×{count}" for name, count in operations.items() if count]
    return "  ".join(parts) if parts else "无"


def render_report(
    minimal: ExperimentMetrics,
    full: ExperimentMetrics,
    context: dict,
) -> str:
    task: TaskSpec = context["task"]
    qos = task.qos
    yes = "满足 ✓"
    no = "未满足 ✗"
    lines: list[str] = []
    lines.append(report.banner("应急救援任务 · 弹性最小调整 vs 全量重建  对比实验"))

    lines.append(report.section("任务"))
    lines.append(
        report.kv_block(
            [
                ("任务编号", task.task_id),
                ("目标", task.goal),
                ("业务链路", f"{len(task.biz_edges)} 条"),
                ("QoS 要求", f"时延≤{qos.max_latency_ms:.0f}ms  丢包≤{qos.max_loss_rate*100:.0f}%  带宽≥{qos.min_bandwidth_mbps:.0f}Mbps"),
            ]
        )
    )

    lines.append(report.section("组网结果（建网一次，两种策略共用）"))
    lines.append(
        report.kv_block(
            [
                ("状态", "networked ✓" if minimal.networking_success else "failed ✗"),
                ("建网耗时", f"{minimal.networking_latency_ms:.2f} ms"),
                ("端到端会话", f"{minimal.session_count} 条"),
                ("涉及网关", f"{minimal.involved_gateway_count} 个"),
            ]
        )
    )

    lines.append(report.section("注入事件"))
    lines.append(
        report.kv_block(
            [
                ("时刻", f"t = {EVENT_TIME:.1f}s"),
                ("事件", f"承载劣化 {EVENT_TYPE} (severity={EVENT_SEVERITY})"),
                (
                    "触发前风险 R",
                    f"{minimal.risk_before:.3f}   (阈值 τ = {qos.risk_threshold:.2f})   "
                    + ("⚠ 超阈值，需要运行期调整" if minimal.risk_before > qos.risk_threshold else "未超阈值"),
                ),
            ]
        )
    )

    lines.append(report.section("两种策略对比（数值均为实际执行后测得）"))
    lines.append(
        report.table(
            headers=["指标", "最小弹性调整", "全量重建"],
            rows=[
                ["调整策略", context["minimal_strategy"], context["full_strategy"]],
                [
                    "调整后风险 R",
                    f"{minimal.risk_after:.3f} {'✓' if minimal.qos_satisfied else '✗'}",
                    f"{full.risk_after:.3f} {'✓' if full.qos_satisfied else '✗'}",
                ],
                ["QoS", yes if minimal.qos_satisfied else no, yes if full.qos_satisfied else no],
                ["变更 Agent 数", minimal.changed_agents, full.changed_agents],
                ["变更通信关系数", minimal.changed_edges, full.changed_edges],
                ["触碰网关数", minimal.changed_gateways, full.changed_gateways],
                ["控制下发次数", minimal.control_updates, full.control_updates],
                ["业务中断 (ms)", f"{minimal.service_interruption_ms:.1f}", f"{full.service_interruption_ms:.1f}"],
                ["操作明细", _op_summary(context["minimal_ops"]), _op_summary(context["full_ops"])],
            ],
            aligns=["left", "left", "left"],
        )
    )

    lines.append(report.section("结论"))
    gw_saved = full.changed_gateways - minimal.changed_gateways
    int_saved = full.service_interruption_ms - minimal.service_interruption_ms
    lines.append("  最小弹性调整 相比 全量重建：")
    lines.append(
        report.bullet(
            f"触碰网关  {full.changed_gateways} → {minimal.changed_gateways}  "
            f"(少 {gw_saved} 个, {report.pct_change(full.changed_gateways, minimal.changed_gateways)})"
        )
    )
    lines.append(
        report.bullet(
            f"业务中断  {full.service_interruption_ms:.1f} → {minimal.service_interruption_ms:.1f} ms  "
            f"(省 {int_saved:.1f} ms, {report.pct_change(full.service_interruption_ms, minimal.service_interruption_ms)})"
        )
    )
    if minimal.qos_satisfied and full.qos_satisfied:
        lines.append(report.bullet("两者都把风险压回阈值内，QoS 均满足"))
    lines.append("")
    if minimal.qos_satisfied and int_saved > 0:
        lines.append("  ✓ 在满足 QoS 的前提下，最小调整以显著更小的控制开销与业务中断完成恢复。")
    return "\n".join(lines)


def _write_csv(output_dir: Path, minimal: ExperimentMetrics, full: ExperimentMetrics) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"method": "minimal_adjustment", **to_jsonable(minimal)},
        {"method": "full_rebuild", **to_jsonable(full)},
    ]
    csv_path = output_dir / "rescue_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def _plot(output_dir: Path, minimal: ExperimentMetrics, full: ExperimentMetrics) -> None:
    if plt is None:
        return
    labels = ["minimal_adjustment", "full_rebuild"]
    control = [minimal.control_updates, full.control_updates]
    interruption = [minimal.service_interruption_ms, full.service_interruption_ms]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.2))
    axes[0].bar(labels, control, color=["#3572a5", "#999999"])
    axes[0].set_title("Control updates")
    axes[0].tick_params(axis="x", rotation=15)
    axes[1].bar(labels, interruption, color=["#3572a5", "#999999"])
    axes[1].set_title("Interruption ms")
    axes[1].tick_params(axis="x", rotation=15)
    fig.tight_layout()
    fig.savefig(output_dir / "rescue_comparison.png", dpi=140)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="rescue", choices=["rescue"])
    parser.add_argument("--output-dir", default="results/async_sim")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    minimal, full, context = asyncio.run(run_rescue(output_dir))
    print(render_report(minimal, full, context))
    print()
    print(f"  结果已写入: {output_dir / 'rescue_summary.csv'}")
    if plt is not None:
        print(f"  对比图已写入: {output_dir / 'rescue_comparison.png'}")


if __name__ == "__main__":
    main()
