from __future__ import annotations

from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.topology import build_rescue_topology


def main() -> None:
    provider = SyntheticMetricProvider()
    gateways = build_rescue_topology(provider)
    print("6G Agent rescue topology")
    for gateway in gateways.values():
        print(f"- {gateway.gateway_id} subnet={gateway.subnet_id} node={gateway.node}")
        for agent in sorted(gateway.agents.values(), key=lambda item: item.agent_id):
            card = agent.card
            sample = provider.snapshot("topology-preview", card.agent_id, 0.0)
            print(
                f"  {card.agent_id} layer={card.layer.value} role={card.role.value} "
                f"caps={','.join(card.capabilities)} bw={sample.available_bandwidth_mbps:.1f}Mbps "
                f"lat={sample.latency_ms:.1f}ms loss={sample.loss_rate:.3f}"
            )


if __name__ == "__main__":
    main()
