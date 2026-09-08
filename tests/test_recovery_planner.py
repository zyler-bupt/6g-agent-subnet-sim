from __future__ import annotations

import asyncio
import json
import unittest

from src.controller.networking import AgentController
from src.e2e.build import build_task_subnet_e2e
from src.e2e.installers import SimulatedGatewayInstaller
from src.e2e.recovery import SyntheticFaultActuator, measure_e2e_recovery
from src.e2e.recovery_planner import LLMRecoveryPlanner
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


class _EvidenceAwareClient:
    def __init__(self) -> None:
        self.user_payloads: list[dict[str, object]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        event = json.loads(messages[-1]["content"])
        self.user_payloads.append(event)
        reports = event["agent_state_reports"]
        offline = sorted(
            agent_id
            for agent_id, state in reports.items()
            if state["selected"] and not state["online"]
        )
        missing_gateways = sorted(
            gateway_id
            for gateway_id, state in event["gateway_state"].items()
            if state["missing_rule_count"] > 0
        )
        if offline:
            diagnosis_code = "A"
            strategy_code = "S"
        elif missing_gateways:
            diagnosis_code = "G"
            strategy_code = "G"
        else:
            diagnosis_code = "L"
            strategy_code = "L"
        return json.dumps({"d": diagnosis_code, "s": strategy_code, "c": 95})


class _SlowClient:
    async def complete_async(self, _messages: list[dict[str, str]]) -> str:
        await asyncio.sleep(0.05)
        return "{}"


class _InvalidClient:
    def complete(self, _messages: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "diagnosed_fault_type": "link_degrade",
                "conflict_types": ["business_qos_violation"],
                "affected_session_ids": [],
                "strategy": "execute_shell",
                "target_agent_id": None,
                "affected_gateway_ids": [],
                "confidence": 1.0,
                "reason": "run an unrestricted command",
            }
        )


class RecoveryPlannerTests(unittest.TestCase):
    @staticmethod
    async def _run_fault(fault_type: str, planner: LLMRecoveryPlanner):
        provider = SyntheticMetricProvider(base_app_rate_mbps=24.0)
        controller = AgentController(build_rescue_topology(provider))
        installer = SimulatedGatewayInstaller(
            control_rtt_ms=0.0,
            rule_install_ms=0.0,
            ack_ms=0.0,
            jitter_ms=0.0,
        )
        verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
        subnet, build, _install, _verify = await build_task_subnet_e2e(
            rescue_task(),
            controller,
            provider,
            installer=installer,
            verifier=verifier,
        )
        if not build.success:
            raise AssertionError(build.errors)
        return await measure_e2e_recovery(
            scenario="rescue",
            run_id=1,
            controller=controller,
            subnet=subnet,
            provider=provider,
            installer=installer,
            verifier=verifier,
            actuator=SyntheticFaultActuator(provider),
            fault_type=fault_type,
            healthy_windows=2,
            sample_interval_s=0.0,
            detection_timeout_s=1.0,
            recovery_timeout_s=1.0,
            decision_mode="llm",
            recovery_planner=planner,
        )

    def test_all_faults_use_validated_llm_plan_without_ground_truth_label(self) -> None:
        client = _EvidenceAwareClient()
        planner = LLMRecoveryPlanner(client=client, timeout_s=0.5)
        for fault_type in ("agent_offline", "gateway_rule_loss", "link_degrade"):
            with self.subTest(fault_type=fault_type):
                result = asyncio.run(self._run_fault(fault_type, planner))
                self.assertTrue(result.incremental_success, result.errors)
                self.assertEqual(result.decision_source, "llm")
                self.assertTrue(result.llm_plan_valid)
                self.assertTrue(result.llm_plan_adopted)
                self.assertFalse(result.model_timeout)
                self.assertGreaterEqual(result.e2e_recovery_ms, result.elastic_recovery_ms)

        self.assertEqual(len(client.user_payloads), 3)
        for payload in client.user_payloads:
            self.assertNotIn("fault_type", payload)
            self.assertIn("observed_conflicts", payload)
            self.assertIn("agent_state_reports", payload)
            self.assertIn("gateway_state", payload)
            self.assertIn("business_edge_results", payload)

    def test_model_timeout_falls_back_within_bounded_analysis_time(self) -> None:
        planner = LLMRecoveryPlanner(client=_SlowClient(), timeout_s=0.005)
        result = asyncio.run(self._run_fault("gateway_rule_loss", planner))
        self.assertTrue(result.incremental_success, result.errors)
        self.assertTrue(result.model_timeout)
        self.assertEqual(result.decision_source, "rule_fallback")
        self.assertEqual(result.fallback_reason, "model_timeout")
        self.assertFalse(result.llm_plan_valid)
        self.assertFalse(result.llm_plan_adopted)
        self.assertLess(result.model_analysis_ms, 40.0)
        self.assertEqual(result.applied_strategy, "gateway_rule_reinstall")

    def test_unexecutable_model_strategy_is_rejected_without_retry(self) -> None:
        planner = LLMRecoveryPlanner(client=_InvalidClient(), timeout_s=0.5)
        result = asyncio.run(self._run_fault("link_degrade", planner))
        self.assertTrue(result.incremental_success, result.errors)
        self.assertEqual(result.model_strategy, "execute_shell")
        self.assertEqual(result.decision_source, "rule_fallback")
        self.assertIn("invalid_model_plan", result.fallback_reason)
        self.assertEqual(result.applied_strategy, "link_repair")
        self.assertFalse(result.llm_plan_valid)


if __name__ == "__main__":
    unittest.main()
