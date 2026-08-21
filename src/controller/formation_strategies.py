from __future__ import annotations

import random
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Mapping, Sequence

from experiments.paper_protocol import EXPERIMENT_METHODS, stable_fingerprint
from src.core.models import BusinessEdge, TaskSubnet, to_jsonable
from src.e2e.models import VerifyResult
from src.simulation.latency_model import formation_latency_breakdown
from src.simulation.paper_scenarios import FormationScenarioSnapshot, PaperLink


@dataclass(frozen=True)
class ChurnEvent:
    time_ms: float
    link_id: str
    bandwidth_factor: float
    delay_factor: float
    queue_delay_ms: float
    loss_addition: float


@dataclass(frozen=True)
class FormationOperation:
    kind: str
    object_id: str
    start_ms: float
    finish_ms: float
    edge_id: str = ""
    gateway_id: str = ""
    control_messages: int = 0

    @property
    def duration_ms(self) -> float:
        return self.finish_ms - self.start_ms


@dataclass(frozen=True)
class FormationTrace:
    method_id: str
    stage_mode: str
    primitive_cost_fingerprint: str
    churn_fingerprint: str
    operations: tuple[FormationOperation, ...]

    @property
    def total_ms(self) -> float:
        return max((item.finish_ms for item in self.operations), default=0.0)

    @property
    def control_messages(self) -> int:
        return sum(item.control_messages for item in self.operations)


@dataclass(frozen=True)
class FormationOutcome:
    method_id: str
    trace: FormationTrace
    controller_processing_latency_ms: float
    formation_latency_ms: float
    # Explicit five-phase breakdown (see src/simulation/latency_model.py).
    # T_form = T_ctrl + T_dispatch + T_install + T_verify + T_activate.
    t_ctrl_ms: float = 0.0
    t_dispatch_ms: float = 0.0
    t_install_ms: float = 0.0
    t_verify_ms: float = 0.0
    t_activate_ms: float = 0.0
    deploy_mode: str = ""
    success: bool
    qos_satisfied: bool
    failure_reason: str
    rollback_count: int
    rollback_succeeded: bool
    stale_state_detected: bool
    stable_verification_attempted: bool
    path_constraints_satisfied: bool
    path_fingerprint: str
    rule_fingerprint: str
    verifier_fingerprint: str
    required_edge_count: int
    processed_edge_count: int
    total_flows: int
    total_rules: int
    total_gateways: int
    control_messages: int
    control_bytes: int
    task_received_at: float
    stable_verify_finished_at: float


@dataclass(frozen=True)
class FormationPrimitiveCosts:
    graph_analysis_ms: float
    supporting_binding_ms: float
    endpoint_resolution_ms: Mapping[str, float]
    path_computation_ms: Mapping[str, float]
    rule_generation_ms: Mapping[str, float]
    edge_verification_ms: Mapping[str, float]
    gateway_rtt_ms: Mapping[str, float]
    gateway_processing_ms: Mapping[str, float]
    gateway_serialization_per_rule_ms: Mapping[str, float]
    rule_stage_ms: Mapping[str, float]
    rule_verify_ms: Mapping[str, float]
    rule_activate_ms: Mapping[str, float]

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(asdict(self))


class _LogicalChurnVerifier:
    """Use the shared verifier, then enforce the paper churn state at stable verify."""

    def __init__(self, delegate, *, stable_qos_ok: bool) -> None:
        self.delegate = delegate
        self.stable_qos_ok = stable_qos_ok
        self.calls = 0

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        result = await self.delegate.verify(subnet)
        self.calls += 1
        if self.calls < 2 or not result.ok or self.stable_qos_ok:
            return result
        edge_results = list(result.edge_results)
        if edge_results:
            edge_results[0] = replace(
                edge_results[0],
                ok=False,
                error="paper state churn violated stable QoS",
            )
        return replace(
            result,
            ok=False,
            passed_edges=max(0, result.passed_edges - 1),
            edge_results=tuple(edge_results),
            mode="paper_churn_stable_verifier",
        )


class _ScheduleBuilder:
    def __init__(self) -> None:
        self.cursor_ms = 0.0
        self.operations: list[FormationOperation] = []

    def serial(
        self,
        kind: str,
        object_id: str,
        duration_ms: float,
        *,
        edge_id: str = "",
        gateway_id: str = "",
        control_messages: int = 0,
    ) -> None:
        start = self.cursor_ms
        self.cursor_ms += duration_ms
        self.operations.append(
            FormationOperation(
                kind=kind,
                object_id=object_id,
                start_ms=start,
                finish_ms=self.cursor_ms,
                edge_id=edge_id,
                gateway_id=gateway_id,
                control_messages=control_messages,
            )
        )

    def parallel(
        self,
        items: Iterable[tuple[str, str, float, str, str, int]],
    ) -> None:
        start = self.cursor_ms
        finishes = []
        for kind, object_id, duration_ms, edge_id, gateway_id, messages in items:
            finish = start + duration_ms
            finishes.append(finish)
            self.operations.append(
                FormationOperation(
                    kind=kind,
                    object_id=object_id,
                    start_ms=start,
                    finish_ms=finish,
                    edge_id=edge_id,
                    gateway_id=gateway_id,
                    control_messages=messages,
                )
            )
        if finishes:
            self.cursor_ms = max(finishes)


async def run_formation_method(
    snapshot: FormationScenarioSnapshot,
    method_id: str,
    *,
    churn_probability: float = 0.0,
    churn_trace: Sequence[ChurnEvent] | None = None,
) -> FormationOutcome:
    if method_id not in EXPERIMENT_METHODS["exp1"]:
        raise ValueError(f"unsupported Exp.1 method: {method_id}")
    if not 0.0 <= churn_probability <= 1.0:
        raise ValueError("churn_probability must be in [0, 1]")

    paths = _edge_paths(snapshot)
    costs = _primitive_costs(snapshot, paths)
    edges_on_gateway = _gateway_edges(snapshot.task.biz_edges, paths)
    path_lengths = {
        edge.edge_id: len(paths[edge.edge_id]) for edge in snapshot.task.biz_edges
    }
    latency_breakdown = formation_latency_breakdown(
        method_id=method_id,
        num_agents=len(snapshot.task.app_agents),
        num_edges=len(snapshot.task.biz_edges),
        gateway_ids=snapshot.topology.gateway_ids,
        edge_ids=[edge.edge_id for edge in snapshot.task.biz_edges],
        edges_on_gateway=edges_on_gateway,
        path_lengths=path_lengths,
        topology_seed_str=snapshot.topology.fingerprint,
    )
    shared_churn = tuple(churn_trace) if churn_trace is not None else _generate_churn(
        snapshot,
        costs,
        paths,
        churn_probability,
    )
    trace = _build_schedule(snapshot, method_id, costs, paths, shared_churn)
    stable_qos_ok = _qos_satisfied_at(
        snapshot,
        paths,
        shared_churn,
        trace.total_ms,
    )
    controller, base_verifier, _provider = snapshot.instantiate()
    verifier = _LogicalChurnVerifier(base_verifier, stable_qos_ok=stable_qos_ok)
    subnet, metrics = await controller.build_task_subnet(
        snapshot.task,
        verifier=verifier,
        run_id=snapshot.seed * 1000 + snapshot.event_id,
        seed=snapshot.seed,
    )
    if metrics.rollback_triggered:
        trace = _append_rollback(trace, snapshot, costs, paths)

    path_fingerprint = stable_fingerprint(
        {
            session.business_edge_id: list(session.gateway_path)
            for session in sorted(subnet.sessions, key=lambda item: item.business_edge_id)
        }
    )
    rule_fingerprint = stable_fingerprint(
        [
            to_jsonable(subnet.rules[rule_id])
            for rule_id in sorted(subnet.rules)
        ]
    )
    verifier_fingerprint = stable_fingerprint(
        {
            "base": type(base_verifier).__name__,
            "stable_churn_check": type(verifier).__name__,
            "qos": to_jsonable(snapshot.task.qos),
        }
    )
    path_constraints_satisfied = all(
        _base_path_satisfies_constraints(snapshot, edge, paths[edge.edge_id])
        for edge in snapshot.task.biz_edges
    )
    stable_verify_finished_ms = max(
        (
            item.finish_ms
            for item in trace.operations
            if item.kind == "stable_verify"
        ),
        default=trace.total_ms,
    )
    success = bool(metrics.networking_success and stable_qos_ok)
    return FormationOutcome(
        method_id=method_id,
        trace=trace,
        controller_processing_latency_ms=latency_breakdown.t_ctrl_ms,
        formation_latency_ms=latency_breakdown.t_form_ms,
        t_ctrl_ms=latency_breakdown.t_ctrl_ms,
        t_dispatch_ms=latency_breakdown.t_dispatch_ms,
        t_install_ms=latency_breakdown.t_install_ms,
        t_verify_ms=latency_breakdown.t_verify_ms,
        t_activate_ms=latency_breakdown.t_activate_ms,
        deploy_mode=latency_breakdown.deploy_mode,
        success=success,
        qos_satisfied=success,
        failure_reason=metrics.failure_reason,
        rollback_count=1 if metrics.rollback_triggered else 0,
        rollback_succeeded=(
            bool(metrics.rollback_success) if metrics.rollback_triggered else False
        ),
        stale_state_detected=not stable_qos_ok,
        stable_verification_attempted=verifier.calls >= 2,
        path_constraints_satisfied=path_constraints_satisfied,
        path_fingerprint=path_fingerprint,
        rule_fingerprint=rule_fingerprint,
        verifier_fingerprint=verifier_fingerprint,
        required_edge_count=len(snapshot.task.biz_edges),
        processed_edge_count=len(subnet.sessions),
        total_flows=len(subnet.sessions),
        total_rules=len(subnet.rules),
        total_gateways=len(subnet.involved_gateways),
        control_messages=trace.control_messages,
        control_bytes=metrics.control_bytes,
        task_received_at=0.0,
        stable_verify_finished_at=stable_verify_finished_ms / 1000.0,
    )


def _primitive_costs(
    snapshot: FormationScenarioSnapshot,
    paths: Mapping[str, tuple[str, ...]],
) -> FormationPrimitiveCosts:
    link_by_pair = _link_by_pair(snapshot)
    endpoint_resolution_ms: dict[str, float] = {}
    path_computation_ms: dict[str, float] = {}
    rule_generation_ms: dict[str, float] = {}
    edge_verification_ms: dict[str, float] = {}
    rule_stage_ms: dict[str, float] = {}
    rule_verify_ms: dict[str, float] = {}
    rule_activate_ms: dict[str, float] = {}
    for edge in snapshot.task.biz_edges:
        path = paths[edge.edge_id]
        path_links = [link_by_pair[frozenset(pair)] for pair in zip(path, path[1:])]
        path_delay_ms = sum(link.delay_ms for link in path_links)
        hops = max(1, len(path) - 1)
        endpoint_resolution_ms[edge.edge_id] = 0.18 + 0.025 * hops
        path_computation_ms[edge.edge_id] = (
            0.20 + 0.015 * len(snapshot.topology.links) + 0.025 * hops
        )
        rule_generation_ms[edge.edge_id] = 0.12 + 0.055 * len(path)
        edge_verification_ms[edge.edge_id] = 0.55 + 0.025 * path_delay_ms + 0.04 * hops
        rule_stage_ms[edge.edge_id] = 0.08 + 0.012 * len(path)
        rule_verify_ms[edge.edge_id] = 0.07 + 0.008 * len(path)
        rule_activate_ms[edge.edge_id] = 0.05 + 0.006 * len(path)
    gateway_rtt_ms: dict[str, float] = {}
    gateway_processing_ms: dict[str, float] = {}
    gateway_serialization_per_rule_ms: dict[str, float] = {}
    for gateway_id in snapshot.topology.gateway_ids:
        gateway_seed = stable_fingerprint(
            {"snapshot_event": snapshot.event_fingerprint, "gateway": gateway_id}
        )
        gateway_rtt_ms[gateway_id] = 5.0 + (int(gateway_seed[:16], 16) % 15001) / 1000.0
        gateway_processing_ms[gateway_id] = (
            1.0 + (int(gateway_seed[16:32], 16) % 4001) / 1000.0
        )
        gateway_serialization_per_rule_ms[gateway_id] = (
            0.02 + (int(gateway_seed[32:48], 16) % 61) / 1000.0
        )
    edge_count = len(snapshot.task.biz_edges)
    return FormationPrimitiveCosts(
        graph_analysis_ms=0.50 + 0.08 * len(snapshot.task.app_agents) + 0.06 * edge_count,
        supporting_binding_ms=0.30 + 0.05 * edge_count,
        endpoint_resolution_ms=endpoint_resolution_ms,
        path_computation_ms=path_computation_ms,
        rule_generation_ms=rule_generation_ms,
        edge_verification_ms=edge_verification_ms,
        gateway_rtt_ms=gateway_rtt_ms,
        gateway_processing_ms=gateway_processing_ms,
        gateway_serialization_per_rule_ms=gateway_serialization_per_rule_ms,
        rule_stage_ms=rule_stage_ms,
        rule_verify_ms=rule_verify_ms,
        rule_activate_ms=rule_activate_ms,
    )


def _build_schedule(
    snapshot: FormationScenarioSnapshot,
    method_id: str,
    costs: FormationPrimitiveCosts,
    paths: Mapping[str, tuple[str, ...]],
    churn: Sequence[ChurnEvent],
) -> FormationTrace:
    builder = _ScheduleBuilder()
    edges = tuple(snapshot.task.biz_edges)
    gateway_edges = _gateway_edges(edges, paths)
    # Every formation method receives the same Task DAG and Agent placement.
    # Parsing the DAG and resolving the supporting-Agent mapping are therefore
    # common controller costs, not Proposed-only work.
    builder.serial("dag_analysis", snapshot.task.task_id, costs.graph_analysis_ms)
    builder.serial(
        "supporting_agent_binding",
        snapshot.task.task_id,
        costs.supporting_binding_ms,
        control_messages=2 * len(snapshot.task.app_agents),
    )
    if method_id in {"proposed", "proposed_without_batch"}:
        builder.parallel(
            (
                "path_compute",
                edge.edge_id,
                costs.path_computation_ms[edge.edge_id],
                edge.edge_id,
                "",
                0,
            )
            for edge in edges
        )
        for edge in edges:
            builder.serial(
                "rule_generate",
                edge.edge_id,
                costs.rule_generation_ms[edge.edge_id],
                edge_id=edge.edge_id,
            )
        stage_items = [
            (
                "gateway_dispatch_install_ack",
                gateway_id,
                _gateway_exchange_cost(costs, gateway_id, edge_ids, "stage"),
                "",
                gateway_id,
                2,
            )
            for gateway_id, edge_ids in sorted(gateway_edges.items())
        ]
        if method_id == "proposed":
            builder.parallel(stage_items)
        else:
            for item in stage_items:
                builder.serial(
                    item[0], item[1], item[2], gateway_id=item[4], control_messages=item[5]
                )
        builder.parallel(
            () if method_id == "proposed_without_batch" else (
                (
                    "gateway_verification_report_ack",
                    gateway_id,
                    _gateway_exchange_cost(costs, gateway_id, edge_ids, "verify"),
                    "",
                    gateway_id,
                    2,
                )
                for gateway_id, edge_ids in sorted(gateway_edges.items())
            )
        )
        if method_id == "proposed_without_batch":
            for gateway_id, edge_ids in sorted(gateway_edges.items()):
                builder.serial(
                    "gateway_verification_report_ack",
                    gateway_id,
                    _gateway_exchange_cost(costs, gateway_id, edge_ids, "verify"),
                    gateway_id=gateway_id,
                    control_messages=2,
                )
        activation_items = [
            (
                "gateway_activate_ack",
                gateway_id,
                _gateway_exchange_cost(costs, gateway_id, edge_ids, "activate"),
                "",
                gateway_id,
                2,
            )
            for gateway_id, edge_ids in sorted(gateway_edges.items())
        ]
        if method_id == "proposed":
            builder.parallel(activation_items)
            stage_mode = "parallel_gateway_batch"
        else:
            for item in activation_items:
                builder.serial(
                    item[0], item[1], item[2], gateway_id=item[4], control_messages=item[5]
                )
            stage_mode = "sequential_gateway"
    elif method_id == "cspf":
        for edge in edges:
            builder.serial(
                "path_compute",
                edge.edge_id,
                costs.path_computation_ms[edge.edge_id],
                edge_id=edge.edge_id,
            )
            builder.serial(
                "rule_generate",
                edge.edge_id,
                costs.rule_generation_ms[edge.edge_id],
                edge_id=edge.edge_id,
            )
            builder.parallel(
                (
                    "gateway_dispatch_install_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "stage"),
                    edge.edge_id,
                    gateway_id,
                    2,
                )
                for gateway_id in paths[edge.edge_id]
            )
            builder.parallel(
                (
                    "gateway_verification_report_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "verify"),
                    edge.edge_id,
                    gateway_id,
                    2,
                )
                for gateway_id in paths[edge.edge_id]
            )
            builder.serial(
                "edge_verify",
                edge.edge_id,
                costs.edge_verification_ms[edge.edge_id],
                edge_id=edge.edge_id,
                control_messages=2,
            )
            builder.parallel(
                (
                    "gateway_activate_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "activate"),
                    edge.edge_id,
                    gateway_id,
                    2,
                )
                for gateway_id in paths[edge.edge_id]
            )
        stage_mode = "per_edge_cspf"
    else:
        for edge in edges:
            builder.serial(
                "endpoint_resolution",
                edge.edge_id,
                costs.endpoint_resolution_ms[edge.edge_id],
                edge_id=edge.edge_id,
                control_messages=2,
            )
            builder.serial(
                "path_compute",
                edge.edge_id,
                costs.path_computation_ms[edge.edge_id],
                edge_id=edge.edge_id,
            )
            builder.serial(
                "rule_generate",
                edge.edge_id,
                costs.rule_generation_ms[edge.edge_id],
                edge_id=edge.edge_id,
            )
            for gateway_id in paths[edge.edge_id]:
                builder.serial(
                    "gateway_dispatch_install_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "stage"),
                    edge_id=edge.edge_id,
                    gateway_id=gateway_id,
                    control_messages=2,
                )
            for gateway_id in paths[edge.edge_id]:
                builder.serial(
                    "gateway_verification_report_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "verify"),
                    edge_id=edge.edge_id,
                    gateway_id=gateway_id,
                    control_messages=2,
                )
            builder.serial(
                "edge_verify",
                edge.edge_id,
                costs.edge_verification_ms[edge.edge_id],
                edge_id=edge.edge_id,
                control_messages=2,
            )
            for gateway_id in paths[edge.edge_id]:
                builder.serial(
                    "gateway_activate_ack",
                    f"{edge.edge_id}:{gateway_id}",
                    _gateway_exchange_cost(costs, gateway_id, (edge.edge_id,), "activate"),
                    edge_id=edge.edge_id,
                    gateway_id=gateway_id,
                    control_messages=2,
                )
        stage_mode = "sequential_agent_procedure"

    builder.parallel(
        (
            "gateway_post_activation_stable_report_ack",
            gateway_id,
            _gateway_exchange_cost(costs, gateway_id, edge_ids, "stable_report"),
            "",
            gateway_id,
            2,
        )
        for gateway_id, edge_ids in sorted(gateway_edges.items())
    )
    builder.parallel(
        (
            "stable_verify",
            edge.edge_id,
            costs.edge_verification_ms[edge.edge_id],
            edge.edge_id,
            "",
            2,
        )
        for edge in edges
    )
    return FormationTrace(
        method_id=method_id,
        stage_mode=stage_mode,
        primitive_cost_fingerprint=costs.fingerprint,
        churn_fingerprint=stable_fingerprint(
            {
                "event": snapshot.event_fingerprint,
                "events": [asdict(item) for item in churn],
            }
        ),
        operations=tuple(builder.operations),
    )


def _append_rollback(
    trace: FormationTrace,
    snapshot: FormationScenarioSnapshot,
    costs: FormationPrimitiveCosts,
    paths: Mapping[str, tuple[str, ...]],
) -> FormationTrace:
    builder = _ScheduleBuilder()
    builder.cursor_ms = trace.total_ms
    builder.operations.extend(trace.operations)
    gateway_edges = _gateway_edges(snapshot.task.biz_edges, paths)
    builder.parallel(
        (
            "gateway_rollback",
            gateway_id,
            costs.gateway_rtt_ms[gateway_id]
            + costs.gateway_processing_ms[gateway_id]
            + 0.04 * len(edge_ids),
            "",
            gateway_id,
            2,
        )
        for gateway_id, edge_ids in sorted(gateway_edges.items())
    )
    return replace(trace, operations=tuple(builder.operations))


def _generate_churn(
    snapshot: FormationScenarioSnapshot,
    costs: FormationPrimitiveCosts,
    paths: Mapping[str, tuple[str, ...]],
    probability: float,
) -> tuple[ChurnEvent, ...]:
    if probability <= 0.0:
        return ()
    active_link_ids = sorted(
        {
            _link_for_pair(snapshot, left, right).link_id
            for path in paths.values()
            for left, right in zip(path, path[1:])
        }
    )
    if not active_link_ids:
        return ()
    horizon_ms = _churn_horizon(snapshot, costs, paths)
    opportunities = max(12, 3 * len(snapshot.task.biz_edges))
    rng = random.Random(
        int(
            stable_fingerprint(
                {
                    "event": snapshot.event_fingerprint,
                    "churn_probability": probability,
                }
            )[:16],
            16,
        )
    )
    events = []
    for index in range(opportunities):
        if rng.random() >= probability:
            continue
        events.append(
            ChurnEvent(
                time_ms=(index + 1) * horizon_ms / (opportunities + 1),
                link_id=rng.choice(active_link_ids),
                bandwidth_factor=rng.uniform(0.85, 0.98),
                delay_factor=rng.uniform(1.05, 1.25),
                queue_delay_ms=rng.uniform(0.0, 5.0),
                loss_addition=rng.uniform(0.0, 0.003),
            )
        )
    return tuple(events)


def _churn_horizon(
    snapshot: FormationScenarioSnapshot,
    costs: FormationPrimitiveCosts,
    paths: Mapping[str, tuple[str, ...]],
) -> float:
    total = costs.graph_analysis_ms + costs.supporting_binding_ms
    for edge in snapshot.task.biz_edges:
        total += (
            costs.endpoint_resolution_ms[edge.edge_id]
            + costs.path_computation_ms[edge.edge_id]
            + costs.rule_generation_ms[edge.edge_id]
            + costs.edge_verification_ms[edge.edge_id]
        )
        for gateway_id in paths[edge.edge_id]:
            total += 3.0 * (
                costs.gateway_rtt_ms[gateway_id]
                + costs.gateway_processing_ms[gateway_id]
                + costs.gateway_serialization_per_rule_ms[gateway_id]
            )
            total += (
                costs.rule_stage_ms[edge.edge_id]
                + costs.rule_verify_ms[edge.edge_id]
                + costs.rule_activate_ms[edge.edge_id]
            )
    for gateway_id, edge_ids in _gateway_edges(snapshot.task.biz_edges, paths).items():
        total += _gateway_exchange_cost(costs, gateway_id, edge_ids, "stable_report")
    total += max(costs.edge_verification_ms.values(), default=0.0)
    return total


def _qos_satisfied_at(
    snapshot: FormationScenarioSnapshot,
    paths: Mapping[str, tuple[str, ...]],
    churn: Sequence[ChurnEvent],
    timestamp_ms: float,
) -> bool:
    link_state = {
        link.link_id: {
            "delay_ms": link.delay_ms,
            "bandwidth_mbps": link.bandwidth_mbps,
            "loss_rate": link.loss_percent / 100.0,
        }
        for link in snapshot.topology.links
    }
    for event in sorted(churn, key=lambda item: item.time_ms):
        if event.time_ms > timestamp_ms:
            break
        state = link_state.get(event.link_id)
        if state is None:
            raise ValueError(f"churn event references unknown link: {event.link_id}")
        state["delay_ms"] = (
            state["delay_ms"] * event.delay_factor + event.queue_delay_ms
        )
        state["bandwidth_mbps"] *= event.bandwidth_factor
        state["loss_rate"] = min(1.0, state["loss_rate"] + event.loss_addition)

    for edge in snapshot.task.biz_edges:
        path_links = [
            _link_for_pair(snapshot, left, right)
            for left, right in zip(paths[edge.edge_id], paths[edge.edge_id][1:])
        ]
        delay_ms = sum(link_state[link.link_id]["delay_ms"] for link in path_links)
        available_mbps = min(
            (link_state[link.link_id]["bandwidth_mbps"] for link in path_links),
            default=float("inf"),
        )
        reliability = 1.0
        for link in path_links:
            reliability *= 1.0 - link_state[link.link_id]["loss_rate"]
        loss_rate = 1.0 - reliability
        if (
            delay_ms > edge.latency_budget_ms
            or available_mbps < edge.data_rate_mbps
            or loss_rate > (edge.max_loss_rate or snapshot.task.qos.max_loss_rate)
        ):
            return False
    return True


def _base_path_satisfies_constraints(
    snapshot: FormationScenarioSnapshot,
    edge: BusinessEdge,
    path: tuple[str, ...],
) -> bool:
    path_links = [
        _link_for_pair(snapshot, left, right)
        for left, right in zip(path, path[1:])
    ]
    return (
        bool(path)
        and sum(link.delay_ms for link in path_links) <= edge.latency_budget_ms
        and min(
            (link.bandwidth_mbps for link in path_links),
            default=float("inf"),
        )
        >= edge.data_rate_mbps
    )


def _edge_paths(
    snapshot: FormationScenarioSnapshot,
) -> dict[str, tuple[str, ...]]:
    return {
        edge.edge_id: snapshot.topology.shortest_path(
            snapshot.agent_gateway_mapping[edge.source],
            snapshot.agent_gateway_mapping[edge.target],
        )
        for edge in snapshot.task.biz_edges
    }


def _gateway_edges(
    edges: Iterable[BusinessEdge],
    paths: Mapping[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for edge in edges:
        for gateway_id in paths[edge.edge_id]:
            values.setdefault(gateway_id, []).append(edge.edge_id)
    return {
        gateway_id: tuple(edge_ids)
        for gateway_id, edge_ids in values.items()
    }


def _gateway_exchange_cost(
    costs: FormationPrimitiveCosts,
    gateway_id: str,
    edge_ids: Sequence[str],
    phase: str,
) -> float:
    per_rule = {
        "stage": costs.rule_stage_ms,
        "verify": costs.rule_verify_ms,
        "activate": costs.rule_activate_ms,
        "stable_report": costs.rule_verify_ms,
    }[phase]
    return (
        costs.gateway_rtt_ms[gateway_id]
        + costs.gateway_processing_ms[gateway_id]
        + costs.gateway_serialization_per_rule_ms[gateway_id] * len(edge_ids)
        + sum(per_rule[edge_id] for edge_id in edge_ids)
    )


def _controller_critical_work_ms(trace: FormationTrace) -> float:
    controller_kinds = {
        "dag_analysis",
        "supporting_agent_binding",
        "endpoint_resolution",
        "path_compute",
        "rule_generate",
    }
    parallel_stages: dict[float, float] = {}
    for operation in trace.operations:
        if operation.kind not in controller_kinds:
            continue
        parallel_stages[operation.start_ms] = max(
            parallel_stages.get(operation.start_ms, 0.0),
            operation.duration_ms,
        )
    return sum(parallel_stages.values())


def _link_by_pair(
    snapshot: FormationScenarioSnapshot,
) -> dict[frozenset[str], PaperLink]:
    return {
        frozenset((link.source, link.target)): link
        for link in snapshot.topology.links
    }


def _link_for_pair(
    snapshot: FormationScenarioSnapshot,
    left: str,
    right: str,
) -> PaperLink:
    try:
        return _link_by_pair(snapshot)[frozenset((left, right))]
    except KeyError as error:
        raise ValueError(f"path uses absent mesh link: {left}->{right}") from error
