from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.agents.phy_agent import PhyAgent
from src.controller.networking import AgentController
from src.core.events import EventInjector
from src.core.models import TaskSubnet
from src.e2e.verifiers import SyntheticTaskSubnetVerifier
from src.metrics.collector import (
    AgentRemovalMetrics,
    write_event_log_jsonl,
    write_metrics_csv,
    write_metrics_json,
)
from src.metrics.synthetic import SyntheticMetricProvider
from src.sim.scenarios import rescue_task
from src.sim.topology import build_rescue_topology


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a versioned AGENT_REMOVE transaction and rollback case.",
    )
    parser.add_argument(
        "--output-dir",
        default="results/stage2",
        help="Directory for JSONL event logs, metrics, and rule snapshots.",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--agent-id",
        default="agent-terminal-feedback",
        help="Existing business Agent to remove from the rescue DAG.",
    )
    parser.add_argument(
        "--stage-failure-gateway",
        default="gw-cloud",
        help="Gateway that rejects the candidate version in the rollback run.",
    )
    return parser


def _world(seed: int) -> tuple[AgentController, SyntheticTaskSubnetVerifier]:
    # This demo verifies the mechanism, so keep nominal throughput above the
    # rescue task's highest-rate business edge. No result values are fabricated.
    provider = SyntheticMetricProvider(seed=seed, base_app_rate_mbps=24.0)
    controller = AgentController(build_rescue_topology(provider))
    verifier = SyntheticTaskSubnetVerifier(provider, probe_delay_ms=0.0)
    return controller, verifier


async def _successful_removal(
    output_dir: Path,
    *,
    seed: int,
    agent_id: str,
) -> AgentRemovalMetrics:
    controller, verifier = _world(seed)
    subnet, _metrics = await controller.build_task_subnet(rescue_task())
    task_state_before = _task_state_snapshot(controller, subnet)
    event = EventInjector(prefix="agent-remove-success").agent_remove(
        subnet.task.task_id,
        agent_id,
    )
    result = await controller.handle_runtime_event(
        event,
        run_id=1,
        seed=seed,
        verifier=verifier,
    )
    if not result.success:
        raise RuntimeError(f"successful removal demo failed: {result.failure_reason}")

    write_event_log_jsonl(
        output_dir / "agent_remove_success_events.jsonl",
        result.event_log,
    )
    write_metrics_json(
        output_dir / "agent_remove_success_metrics.json",
        result.metrics,
    )
    _write_json(
        output_dir / "agent_remove_success_rule_snapshots.json",
        {
            "event": asdict(event),
            "impact_scope": _scope_json(result.scope),
            "rule_delta": _delta_json(result.delta),
            "task_state_before": task_state_before,
            "task_state_after": _task_state_snapshot(controller, result.subnet),
            "before": result.before_snapshot,
            "after": result.after_snapshot,
        },
    )
    return result.metrics


async def _failed_stage_with_rollback(
    output_dir: Path,
    *,
    seed: int,
    agent_id: str,
    failure_gateway: str,
) -> AgentRemovalMetrics:
    controller, verifier = _world(seed)
    subnet, _metrics = await controller.build_task_subnet(rescue_task())
    task_state_before = _task_state_snapshot(controller, subnet)
    if failure_gateway not in controller.gateways:
        raise ValueError(f"unknown failure gateway: {failure_gateway}")
    controller.gateways[failure_gateway].reject_stage_versions.add(subnet.version + 1)
    event = EventInjector(prefix="agent-remove-rollback").agent_remove(
        subnet.task.task_id,
        agent_id,
    )
    result = await controller.handle_runtime_event(
        event,
        run_id=2,
        seed=seed,
        verifier=verifier,
    )
    if result.success or not result.metrics.rollback_success:
        raise RuntimeError(
            "rollback demo did not fail safely: "
            f"success={result.success}, rollback={result.metrics.rollback_success}, "
            f"reason={result.failure_reason}"
        )

    write_event_log_jsonl(
        output_dir / "agent_remove_stage_failure_events.jsonl",
        result.event_log,
    )
    write_metrics_json(
        output_dir / "agent_remove_stage_failure_metrics.json",
        result.metrics,
    )
    _write_json(
        output_dir / "agent_remove_stage_failure_rule_snapshots.json",
        {
            "event": asdict(event),
            "injected_failure_gateway": failure_gateway,
            "impact_scope": _scope_json(result.scope),
            "rule_delta": _delta_json(result.delta),
            "task_state_before": task_state_before,
            "task_state_after_rollback": _task_state_snapshot(
                controller,
                result.subnet,
            ),
            "before": result.before_snapshot,
            "after_rollback": result.after_snapshot,
        },
    )
    return result.metrics


async def _run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    success = await _successful_removal(
        output_dir,
        seed=args.seed,
        agent_id=args.agent_id,
    )
    rollback = await _failed_stage_with_rollback(
        output_dir,
        seed=args.seed,
        agent_id=args.agent_id,
        failure_gateway=args.stage_failure_gateway,
    )
    write_metrics_csv(output_dir / "agent_removal_metrics.csv", (success, rollback))
    _write_json(
        output_dir / "run_summary.json",
        {
            "success_case": {
                "success": success.success,
                "old_version": success.old_version,
                "new_version": success.new_version,
                "deleted_rules": success.deleted_rules,
                "residual_rules": success.residual_rules,
                "elastic_latency_ms": success.elastic_latency_ms,
            },
            "rollback_case": {
                "success": rollback.success,
                "rollback_triggered": rollback.rollback_triggered,
                "rollback_success": rollback.rollback_success,
                "old_version": rollback.old_version,
                "candidate_version": rollback.new_version,
                "failure_reason": rollback.failure_reason,
                "rollback_latency_ms": rollback.rollback_latency_ms,
            },
        },
    )
    print(f"Stage-2 artifacts written to {output_dir.resolve()}")


def _scope_json(scope) -> dict[str, list[str]]:
    return {
        "affected_agents": sorted(scope.affected_agents),
        "affected_business_edges": sorted(scope.affected_business_edges),
        "affected_sessions": sorted(scope.affected_sessions),
        "affected_routes": sorted(scope.affected_routes),
        "affected_physical_resources": sorted(scope.affected_physical_resources),
        "affected_gateways": sorted(scope.affected_gateways),
        "affected_rules": sorted(scope.affected_rules),
        "unaffected_business_edges": sorted(scope.unaffected_business_edges),
    }


def _delta_json(delta) -> dict[str, list[str]]:
    return {
        "additions": sorted(rule.rule_id for rule in delta.additions),
        "updates": sorted(rule.rule_id for rule in delta.updates),
        "deletions": sorted(rule.rule_id for rule in delta.deletions),
    }


def _task_state_snapshot(
    controller: AgentController,
    subnet: TaskSubnet,
) -> dict[str, Any]:
    sessions = {
        session.session_id: {
            "business_edge_id": session.business_edge_id,
            "source": session.source,
            "target": session.target,
            "route_id": f"{session.session_id}:route",
            "gateway_path": list(session.gateway_path),
            "t_agent_id": session.t_agent_id,
            "n_agent_id": session.n_agent_id,
            "p_agent_ids": list(session.p_agent_ids),
        }
        for session in subnet.sessions
    }
    edge_reachability: dict[str, bool] = {}
    for edge_id, edge in subnet.business_edges.items():
        session = next(
            (
                item
                for item in subnet.sessions
                if item.business_edge_id == edge_id
            ),
            None,
        )
        edge_reachability[edge_id] = bool(
            session
            and all(
                controller.gateways[gateway_id].flow_allowed(
                    subnet.task.task_id,
                    edge.source,
                    edge.target,
                )
                for gateway_id in session.gateway_path
            )
        )

    physical_agent_resources: dict[str, list[str]] = {}
    for gateway in controller.gateways.values():
        for agent in gateway.agents.values():
            if isinstance(agent, PhyAgent):
                physical_agent_resources[agent.agent_id] = sorted(
                    binding_id
                    for binding_id, binding in agent.resource_bindings.items()
                    if binding.task_id == subnet.task.task_id and binding.active
                )

    return {
        "task_id": subnet.task.task_id,
        "version": subnet.version,
        "status": subnet.status,
        "business_agents": sorted(subnet.business_agents),
        "business_edges": {
            edge_id: {
                "source": edge.source,
                "target": edge.target,
                "flow_type": edge.flow_type,
            }
            for edge_id, edge in sorted(subnet.business_edges.items())
        },
        "sessions": sessions,
        "routes": {
            route_id: asdict(route)
            for route_id, route in sorted(subnet.routes.items())
        },
        "rules": sorted(subnet.rules),
        "physical_bindings": {
            binding_id: asdict(binding)
            for binding_id, binding in sorted(subnet.physical_bindings.items())
        },
        "physical_agent_resources": physical_agent_resources,
        "monitored_edge_ids": sorted(subnet.monitored_edge_ids),
        "edge_reachability": edge_reachability,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
