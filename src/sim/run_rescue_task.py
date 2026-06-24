from __future__ import annotations

import asyncio

from src.controller.networking import AgentController
from src.core.models import to_jsonable
from src.metrics.mock import MockMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


async def run() -> None:
    provider = MockMetricProvider()
    task = rescue_task()
    controller = AgentController(build_rescue_topology(provider))
    subnet, metrics = await controller.build_task_subnet(task)
    predictions = await controller.run_agent_loop(subnet, timestamp=1.0)
    print("task:", task.task_id)
    print("state:", subnet.state.value)
    print("G_m members:")
    print("  app:", sorted(subnet.app_agents))
    print("  trans:", sorted(subnet.trans_agents))
    print("  net:", sorted(subnet.net_agents))
    print("  phy_stub:", sorted(subnet.phy_agents))
    print("G_m edges:")
    for edge in sorted(subnet.edges):
        print(f"  {edge[0]} -[{edge[2]}]-> {edge[1]}")
    print("sessions:")
    for session in subnet.sessions:
        print(
            f"  {session.session_id}: {session.source}->{session.target} "
            f"t={session.t_agent_id} n={session.n_agent_id} "
            f"gw={session.source_gateway}->{session.target_gateway}"
        )
    print("gateway ack:", [to_jsonable(ack) for ack in subnet.gateway_acks])
    print("predictions:", [to_jsonable(item) for item in predictions])
    print("metrics:", to_jsonable(metrics))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

