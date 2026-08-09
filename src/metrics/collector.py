from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable, Iterable

from src.core.events import EventLogRecord, EventStage, RuntimeEvent
from src.core.rules import RuleDelta

if TYPE_CHECKING:
    from src.controller.impact import ImpactScope


@dataclass(frozen=True)
class AgentRemovalMetrics:
    run_id: int
    seed: int
    task_id: str
    event_id: str
    event_type: str
    old_version: int
    new_version: int
    num_agents_before: int
    num_agents_after: int
    num_edges_before: int
    num_edges_after: int
    num_gateways: int
    event_occurred_at: float
    event_received_at: float
    scope_finished_at: float
    delta_compiled_at: float
    stage_finished_at: float
    verification_finished_at: float
    activation_finished_at: float
    rollback_finished_at: float
    scope_latency_ms: float
    delta_compile_latency_ms: float
    stage_latency_ms: float
    verification_latency_ms: float
    activation_latency_ms: float
    rollback_latency_ms: float
    elastic_latency_ms: float
    affected_agents: int
    affected_edges: int
    affected_sessions: int
    affected_routes: int
    affected_physical_resources: int
    affected_gateways: int
    added_rules: int
    updated_rules: int
    deleted_rules: int
    residual_rules: int
    unaffected_edges_total: int
    unaffected_edges_interrupted: int
    unaffected_service_interruption_ms: float
    success: bool
    rollback_triggered: bool
    rollback_success: bool
    failure_reason: str


class MetricsCollector:
    def __init__(
        self,
        *,
        run_id: int,
        seed: int,
        event: RuntimeEvent,
        old_version: int,
        new_version: int,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.run_id = run_id
        self.seed = seed
        self.event = event
        self.old_version = old_version
        self.new_version = new_version
        self.clock = clock
        self.records: list[EventLogRecord] = []
        self.log(
            EventStage.EVENT_OCCURRED,
            component="EventInjector",
            timestamp=event.occurred_at,
            details=dict(event.payload),
        )

    def log(
        self,
        stage: EventStage | str,
        *,
        component: str,
        details: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> EventLogRecord:
        stage_value = stage.value if isinstance(stage, EventStage) else str(stage)
        record = EventLogRecord(
            run_id=self.run_id,
            event_id=self.event.event_id,
            task_id=self.event.task_id,
            old_version=self.old_version,
            new_version=self.new_version,
            timestamp=self.clock() if timestamp is None else timestamp,
            component=component,
            event_type=self.event.event_type,
            event_stage=stage_value,
            details=dict(details or {}),
        )
        if self.records and record.timestamp < self.records[-1].timestamp:
            raise ValueError(
                f"non-monotonic event log timestamp at {record.event_stage}: "
                f"{record.timestamp} < {self.records[-1].timestamp}"
            )
        self.records.append(record)
        return record

    def stage_timestamp(self, stage: EventStage | str, *, last: bool = False) -> float:
        value = stage.value if isinstance(stage, EventStage) else str(stage)
        matches = [record.timestamp for record in self.records if record.event_stage == value]
        if not matches:
            return 0.0
        return matches[-1] if last else matches[0]

    def build_agent_removal_metrics(
        self,
        *,
        num_agents_before: int,
        num_agents_after: int,
        num_edges_before: int,
        num_edges_after: int,
        num_gateways: int,
        scope: ImpactScope,
        delta: RuleDelta,
        delta_compile_started_at: float,
        residual_rules: int,
        unaffected_edges_interrupted: int,
        unaffected_service_interruption_ms: float,
        success: bool,
        rollback_triggered: bool,
        rollback_success: bool,
        failure_reason: str,
    ) -> AgentRemovalMetrics:
        occurred = self.event.occurred_at
        received = self.stage_timestamp(EventStage.EVENT_RECEIVED)
        scope_started = self.stage_timestamp(EventStage.SCOPE_STARTED)
        scope_finished = self.stage_timestamp(EventStage.SCOPE_FINISHED)
        delta_compiled = self.stage_timestamp(EventStage.DELTA_COMPILED)
        stage_started = self.stage_timestamp(EventStage.STAGE_STARTED)
        stage_finished = self.stage_timestamp(EventStage.STAGE_FINISHED)
        verify_started = self.stage_timestamp(EventStage.VERIFY_STARTED)
        verify_finished = self.stage_timestamp(EventStage.VERIFY_FINISHED)
        activate_started = self.stage_timestamp(EventStage.ACTIVATE_STARTED)
        activate_finished = self.stage_timestamp(EventStage.ACTIVATE_FINISHED)
        rollback_finished = self.stage_timestamp(EventStage.ROLLBACK_FINISHED, last=True)

        return AgentRemovalMetrics(
            run_id=self.run_id,
            seed=self.seed,
            task_id=self.event.task_id,
            event_id=self.event.event_id,
            event_type=self.event.event_type,
            old_version=self.old_version,
            new_version=self.new_version,
            num_agents_before=num_agents_before,
            num_agents_after=num_agents_after,
            num_edges_before=num_edges_before,
            num_edges_after=num_edges_after,
            num_gateways=num_gateways,
            event_occurred_at=occurred,
            event_received_at=received,
            scope_finished_at=scope_finished,
            delta_compiled_at=delta_compiled,
            stage_finished_at=stage_finished,
            verification_finished_at=verify_finished,
            activation_finished_at=activate_finished,
            rollback_finished_at=rollback_finished,
            scope_latency_ms=_duration_ms(scope_started, scope_finished),
            delta_compile_latency_ms=_duration_ms(delta_compile_started_at, delta_compiled),
            stage_latency_ms=_duration_ms(stage_started, stage_finished),
            verification_latency_ms=_duration_ms(verify_started, verify_finished),
            activation_latency_ms=_duration_ms(activate_started, activate_finished),
            rollback_latency_ms=(
                _duration_ms(occurred, rollback_finished) if rollback_triggered else 0.0
            ),
            elastic_latency_ms=(
                _duration_ms(occurred, activate_finished) if success else 0.0
            ),
            affected_agents=len(scope.affected_agents),
            affected_edges=len(scope.affected_business_edges),
            affected_sessions=len(scope.affected_sessions),
            affected_routes=len(scope.affected_routes),
            affected_physical_resources=len(scope.affected_physical_resources),
            affected_gateways=len(scope.affected_gateways),
            added_rules=len(delta.additions),
            updated_rules=len(delta.updates),
            deleted_rules=len(delta.deletions),
            residual_rules=residual_rules,
            unaffected_edges_total=len(scope.unaffected_business_edges),
            unaffected_edges_interrupted=unaffected_edges_interrupted,
            unaffected_service_interruption_ms=unaffected_service_interruption_ms,
            success=success,
            rollback_triggered=rollback_triggered,
            rollback_success=rollback_success,
            failure_reason=failure_reason,
        )


def write_event_log_jsonl(path: str | Path, records: Iterable[EventLogRecord]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")


def write_metrics_json(path: str | Path, metrics: AgentRemovalMetrics) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(metrics), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_metrics_csv(path: str | Path, metrics: Iterable[AgentRemovalMetrics]) -> None:
    rows = [asdict(item) for item in metrics]
    if not rows:
        raise ValueError("metrics must not be empty")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _duration_ms(start: float, finish: float) -> float:
    if start <= 0.0 or finish <= 0.0:
        return 0.0
    return max(0.0, (finish - start) * 1000.0)
