from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.agents.app_agent import AppAgent
from src.agents.controls import FlowgenControl
from src.core.models import AgentAction, AgentCard, AgentLayer, AgentRole, AgentState
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task


def _card() -> AgentCard:
    return AgentCard(
        agent_id="agent-test",
        name="test",
        layer=AgentLayer.APPLICATION,
        role=AgentRole.BUSINESS,
        gateway_id="gw-test",
        subnet_id="subnet-test",
        node="node-test",
        endpoint="sim://test",
        capabilities=("video_capture",),
        state=AgentState({}),
    )


class RealAgentLoopTests(unittest.TestCase):
    def test_history_keeps_latest_ten_values(self) -> None:
        agent = AppAgent(_card(), SyntheticMetricProvider(), history_window=10)
        task = rescue_task()
        for step in range(12):
            agent.sense(task, float(step))
        self.assertEqual(len(agent.history["data_rate_mbps"]), 10)

    def test_horizon_five_prediction(self) -> None:
        agent = AppAgent(_card(), SyntheticMetricProvider(), horizon=5, history_window=10)
        task = rescue_task()
        for step in range(10):
            agent.sense(task, float(step))
        prediction = agent.predict(task)[0]
        self.assertEqual(prediction.horizon, 5)
        self.assertEqual(len(prediction.values), 5)

    def test_executor_reduces_flowgen_target_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "flowgen.json"
            executor = FlowgenControl(control_file=control, initial_target_mbps=10.0)
            agent = AppAgent(_card(), SyntheticMetricProvider(), action_executor=executor)
            agent.execute(
                rescue_task(),
                AgentAction(
                    agent_id="agent-test",
                    layer=AgentLayer.APPLICATION,
                    action_type="reduce_noncritical_quality",
                    params={"bitrate_multiplier": 0.5},
                ),
            )
            payload = json.loads(control.read_text(encoding="utf-8"))
            self.assertAlmostEqual(payload["target_mbps"], 5.0)

    def test_executor_writes_transport_and_network_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp) / "flowgen.json"
            executor = FlowgenControl(control_file=control, initial_target_mbps=10.0)
            task = rescue_task()
            from src.agents.net_agent import NetAgent
            from src.agents.trans_agent import TransAgent

            trans_card = _card()
            trans_card = AgentCard(
                **{
                    **trans_card.__dict__,
                    "agent_id": "tagent-test",
                    "layer": AgentLayer.TRANSPORT,
                    "role": AgentRole.SUPPORT,
                }
            )
            net_card = _card()
            net_card = AgentCard(
                **{
                    **net_card.__dict__,
                    "agent_id": "nagent-test",
                    "layer": AgentLayer.NETWORK,
                    "role": AgentRole.SUPPORT,
                }
            )
            TransAgent(trans_card, SyntheticMetricProvider(), action_executor=executor).execute(
                task,
                AgentAction(
                    agent_id="tagent-test",
                    layer=AgentLayer.TRANSPORT,
                    action_type="tune_transport_parameters",
                    params={"send_rate_multiplier": 0.8, "tcp_nodelay": True},
                ),
            )
            NetAgent(net_card, SyntheticMetricProvider(), action_executor=executor).execute(
                task,
                AgentAction(
                    agent_id="nagent-test",
                    layer=AgentLayer.NETWORK,
                    action_type="raise_monitoring_and_bearer_advice",
                    params={"bearer_advice": "prioritize_task_flow"},
                ),
            )
            payload = json.loads(control.read_text(encoding="utf-8"))
            self.assertAlmostEqual(payload["target_mbps"], 8.0)
            self.assertTrue(payload["tcp_nodelay"])
            self.assertEqual(payload["tcp_congestion"], "cubic")
            self.assertEqual(payload["ip_tos"], 0xB8)
            self.assertEqual(len(executor.network_advice_log), 1)


if __name__ == "__main__":
    unittest.main()
