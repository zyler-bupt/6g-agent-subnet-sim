from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Any, Callable


class RuntimeEventType(str, Enum):
    TASK_BUILD = "TASK_BUILD"
    AGENT_REMOVE = "AGENT_REMOVE"
    CROSS_LAYER_CONFLICT = "CROSS_LAYER_CONFLICT"
    AGENT_FAILURE = "AGENT_FAILURE"
    LINK_DEGRADATION = "LINK_DEGRADATION"
    LINK_FAILURE = "LINK_FAILURE"
    PHYSICAL_CAPACITY_DROP = "PHYSICAL_CAPACITY_DROP"
    GATEWAY_FAILURE = "GATEWAY_FAILURE"


class EventStage(str, Enum):
    EVENT_OCCURRED = "EVENT_OCCURRED"
    EVENT_RECEIVED = "EVENT_RECEIVED"
    SCOPE_STARTED = "SCOPE_STARTED"
    SCOPE_FINISHED = "SCOPE_FINISHED"
    DELTA_COMPILED = "DELTA_COMPILED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_FINISHED = "STAGE_FINISHED"
    VERIFY_STARTED = "VERIFY_STARTED"
    VERIFY_FINISHED = "VERIFY_FINISHED"
    ACTIVATE_STARTED = "ACTIVATE_STARTED"
    ACTIVATE_FINISHED = "ACTIVATE_FINISHED"
    POST_ACTIVATE_VERIFY_STARTED = "POST_ACTIVATE_VERIFY_STARTED"
    POST_ACTIVATE_VERIFY_FINISHED = "POST_ACTIVATE_VERIFY_FINISHED"
    ROLLBACK_STARTED = "ROLLBACK_STARTED"
    ROLLBACK_FINISHED = "ROLLBACK_FINISHED"


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    task_id: str
    event_type: str
    occurred_at: float
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def agent_id(self) -> str:
        return str(self.payload.get("agent_id", ""))

    @property
    def agent_ids(self) -> tuple[str, ...]:
        values = self.payload.get("agent_ids")
        if values is None:
            value = self.agent_id
            return (value,) if value else ()
        if isinstance(values, str):
            values = (values,)
        return tuple(dict.fromkeys(str(value) for value in values if str(value)))


@dataclass(frozen=True)
class EventLogRecord:
    run_id: int
    event_id: str
    task_id: str
    old_version: int
    new_version: int
    timestamp: float
    component: str
    event_type: str
    event_stage: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventInjector:
    """Creates reproducible event identifiers and records occurrence immediately."""

    clock: Callable[[], float] = perf_counter
    prefix: str = "runtime"
    _sequence: int = field(default=0, init=False)

    def agent_remove(self, task_id: str, agent_id: str) -> RuntimeEvent:
        return self.agent_remove_many(task_id, (agent_id,))

    def agent_remove_many(
        self,
        task_id: str,
        agent_ids: tuple[str, ...] | list[str],
    ) -> RuntimeEvent:
        if not task_id:
            raise ValueError("task_id must not be empty")
        normalized = tuple(dict.fromkeys(str(value) for value in agent_ids if str(value)))
        if not normalized:
            raise ValueError("agent_ids must not be empty")
        self._sequence += 1
        occurred_at = self.clock()
        return RuntimeEvent(
            event_id=f"{self.prefix}-{self._sequence:06d}",
            task_id=task_id,
            event_type=RuntimeEventType.AGENT_REMOVE.value,
            occurred_at=occurred_at,
            payload={"agent_id": normalized[0], "agent_ids": list(normalized)},
        )

    def cross_layer_conflict(
        self,
        task_id: str,
        *,
        scenario: str,
        pressure: float,
        scenario_fingerprint: str,
    ) -> RuntimeEvent:
        if not task_id:
            raise ValueError("task_id must not be empty")
        self._sequence += 1
        return RuntimeEvent(
            event_id=f"{self.prefix}-{self._sequence:06d}",
            task_id=task_id,
            event_type=RuntimeEventType.CROSS_LAYER_CONFLICT.value,
            occurred_at=self.clock(),
            payload={
                "scenario": scenario,
                "pressure": float(pressure),
                "scenario_fingerprint": scenario_fingerprint,
            },
        )

    def fault(
        self,
        task_id: str,
        event_type: RuntimeEventType | str,
        *,
        payload: dict[str, Any],
        occurred_at: float | None = None,
    ) -> RuntimeEvent:
        """Create a fault event at the instant the simulated fault takes effect.

        ``occurred_at`` is accepted for deterministic logical-time simulations;
        callers must pass the timestamp used to mutate the fault state, not a
        later handler timestamp.
        """

        if not task_id:
            raise ValueError("task_id must not be empty")
        value = event_type.value if isinstance(event_type, RuntimeEventType) else str(event_type)
        allowed = {
            RuntimeEventType.AGENT_FAILURE.value,
            RuntimeEventType.LINK_DEGRADATION.value,
            RuntimeEventType.LINK_FAILURE.value,
            RuntimeEventType.PHYSICAL_CAPACITY_DROP.value,
            RuntimeEventType.GATEWAY_FAILURE.value,
        }
        if value not in allowed:
            raise ValueError(f"unsupported fault event type: {value}")
        self._sequence += 1
        effective_at = self.clock() if occurred_at is None else float(occurred_at)
        return RuntimeEvent(
            event_id=f"{self.prefix}-{self._sequence:06d}",
            task_id=task_id,
            event_type=value,
            occurred_at=effective_at,
            payload=dict(payload),
        )

    def agent_failure(
        self,
        task_id: str,
        agent_id: str,
        *,
        occurred_at: float | None = None,
        **payload: Any,
    ) -> RuntimeEvent:
        return self.fault(
            task_id,
            RuntimeEventType.AGENT_FAILURE,
            payload={"agent_id": agent_id, **payload},
            occurred_at=occurred_at,
        )

    def link_failure(
        self,
        task_id: str,
        source_gateway: str,
        target_gateway: str,
        *,
        degradation_factor: float = 0.0,
        occurred_at: float | None = None,
        **payload: Any,
    ) -> RuntimeEvent:
        event_type = (
            RuntimeEventType.LINK_FAILURE
            if degradation_factor <= 0.0
            else RuntimeEventType.LINK_DEGRADATION
        )
        return self.fault(
            task_id,
            event_type,
            payload={
                "source_gateway": source_gateway,
                "target_gateway": target_gateway,
                "degradation_factor": float(degradation_factor),
                **payload,
            },
            occurred_at=occurred_at,
        )

    def physical_capacity_drop(
        self,
        task_id: str,
        physical_agent_id: str,
        *,
        capacity_factor: float,
        occurred_at: float | None = None,
        **payload: Any,
    ) -> RuntimeEvent:
        return self.fault(
            task_id,
            RuntimeEventType.PHYSICAL_CAPACITY_DROP,
            payload={
                "physical_agent_id": physical_agent_id,
                "capacity_factor": float(capacity_factor),
                **payload,
            },
            occurred_at=occurred_at,
        )
