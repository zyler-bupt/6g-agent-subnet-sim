from __future__ import annotations

import argparse
import asyncio
import csv
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:  # plotting is optional for minimal simulation runs
    plt = None

from src.controller.networking import AgentController
from src.core.models import ExperimentMetrics, to_jsonable
from src.metrics.mock import MockMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


async def run_rescue(output_dir: Path) -> dict[str, float | int | str | bool]:
    provider = MockMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, metrics = await controller.build_task_subnet(task)
    provider.inject_event(task.task_id, "bearer_degradation", severity=1.3)
    risk_before, risk_after, adjustment = await controller.evaluate_and_adjust(subnet, timestamp=4.0)

    minimal = ExperimentMetrics(
        networking_success=metrics.networking_success,
        networking_latency_ms=metrics.networking_latency_ms,
        session_count=metrics.session_count,
        involved_gateway_count=metrics.involved_gateway_count,
        qos_satisfied=risk_after <= task.qos.risk_threshold,
        risk_before=risk_before,
        risk_after=risk_after,
        changed_agents=adjustment.changed_agents,
        changed_edges=adjustment.changed_edges,
        changed_gateways=adjustment.changed_gateways,
        control_updates=adjustment.changed_gateways,
        service_interruption_ms=adjustment.service_interruption_ms,
    )
    full_rebuild = ExperimentMetrics(
        networking_success=True,
        networking_latency_ms=metrics.networking_latency_ms * 1.8,
        session_count=metrics.session_count,
        involved_gateway_count=metrics.involved_gateway_count,
        qos_satisfied=True,
        risk_before=risk_before,
        risk_after=max(0.0, risk_after * 0.8),
        changed_agents=len(subnet.app_agents | subnet.trans_agents | subnet.net_agents),
        changed_edges=len(subnet.edges),
        changed_gateways=len(subnet.involved_gateways),
        control_updates=len(subnet.involved_gateways),
        service_interruption_ms=65.0,
    )
    rows = [
        {"method": "minimal_adjustment", **to_jsonable(minimal)},
        {"method": "full_rebuild", **to_jsonable(full_rebuild)},
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "rescue_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    _plot(rows, output_dir / "rescue_comparison.png")
    return rows[0]


def _plot(rows: list[dict], output_path: Path) -> None:
    if plt is None:
        return
    labels = [row["method"] for row in rows]
    control = [row["control_updates"] for row in rows]
    interruption = [row["service_interruption_ms"] for row in rows]
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.2))
    axes[0].bar(labels, control, color=["#3572a5", "#999999"])
    axes[0].set_title("Control updates")
    axes[0].tick_params(axis="x", rotation=15)
    axes[1].bar(labels, interruption, color=["#3572a5", "#999999"])
    axes[1].set_title("Interruption ms")
    axes[1].tick_params(axis="x", rotation=15)
    fig.tight_layout()
    fig.savefig(output_path, dpi=140)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="rescue", choices=["rescue"])
    parser.add_argument("--output-dir", default="results/async_sim")
    args = parser.parse_args()
    result = asyncio.run(run_rescue(Path(args.output_dir)))
    print(to_jsonable(result))


if __name__ == "__main__":
    main()
