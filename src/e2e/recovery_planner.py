from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import Any, Protocol

import aiohttp

from src.controller.networking import AgentController
from src.core.models import AgentRole, TaskSubnet
from src.e2e.models import VerifyResult


SUPPORTED_RECOVERY_STRATEGIES = (
    "local_tuning",
    "support_agent_replace",
    "communication_reroute",
    "gateway_rule_reinstall",
    "link_repair",
    "full_rebuild",
)
SUPPORTED_DIAGNOSES = (
    "agent_offline",
    "link_degrade",
    "gateway_rule_loss",
    "unknown",
)
_COMPACT_DIAGNOSES = {
    "A": "agent_offline",
    "L": "link_degrade",
    "G": "gateway_rule_loss",
    "U": "unknown",
    "agent": "agent_offline",
    "link": "link_degrade",
    "route": "gateway_rule_loss",
    "unknown": "unknown",
}
_COMPACT_STRATEGIES = {
    "T": "local_tuning",
    "S": "support_agent_replace",
    "C": "communication_reroute",
    "G": "gateway_rule_reinstall",
    "L": "link_repair",
    "F": "full_rebuild",
    "tune": "local_tuning",
    "standby": "support_agent_replace",
    "reroute": "communication_reroute",
    "reinstall": "gateway_rule_reinstall",
    "repair": "link_repair",
    "rebuild": "full_rebuild",
}


@dataclass(frozen=True)
class AnomalyEvent:
    event_id: str
    task_id: str
    detected_at: float
    detection_source: str
    affected_session_ids: tuple[str, ...]
    observed_conflicts: tuple[str, ...]
    agent_state_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    gateway_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    business_edge_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RecoveryPlan:
    diagnosed_fault_type: str
    conflict_types: tuple[str, ...]
    affected_session_ids: tuple[str, ...]
    strategy: str
    target_agent_id: str | None
    affected_gateway_ids: tuple[str, ...]
    confidence: float
    reason: str


@dataclass(frozen=True)
class ModelProposal:
    raw_response: str = ""
    analysis_ms: float = 0.0
    timed_out: bool = False
    error: str = ""


@dataclass(frozen=True)
class PlannerWarmupResult:
    ok: bool
    elapsed_ms: float
    error: str = ""


class RecoveryPlanner(Protocol):
    async def propose(self, event: AnomalyEvent) -> ModelProposal:
        ...


@dataclass
class OpenAICompatibleRecoveryClient:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_s: float = 1.0
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 32

    async def complete_async(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                body_text = await response.text()
                if response.status >= 400:
                    raise OSError(
                        f"LLM server returned HTTP {response.status}: {body_text[:500]}"
                    )
        body = json.loads(body_text)
        return str(body["choices"][0]["message"]["content"])


@dataclass
class LLMRecoveryPlanner:
    client: Any
    timeout_s: float = 1.0

    async def propose(self, event: AnomalyEvent) -> ModelProposal:
        started = perf_counter()
        try:
            raw = await asyncio.wait_for(
                self._complete(self.client, self._messages(event)),
                timeout=self.timeout_s,
            )
            return ModelProposal(
                raw_response=raw,
                analysis_ms=(perf_counter() - started) * 1000.0,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return ModelProposal(
                analysis_ms=(perf_counter() - started) * 1000.0,
                timed_out=True,
                error=f"model analysis exceeded {self.timeout_s:.3f}s",
            )
        except Exception as error:
            return ModelProposal(
                analysis_ms=(perf_counter() - started) * 1000.0,
                error=f"{type(error).__name__}: {error}",
            )

    async def warmup(self, timeout_s: float = 30.0) -> PlannerWarmupResult:
        started = perf_counter()
        client = self.client
        if isinstance(client, OpenAICompatibleRecoveryClient):
            client = replace(
                client,
                timeout_s=timeout_s,
                max_tokens=min(client.max_tokens, 16),
            )
        messages = [
            {
                "role": "system",
                "content": "Return one JSON object and no explanation.",
            },
            {
                "role": "user",
                "content": '{"status":"ready"}',
            },
        ]
        try:
            await asyncio.wait_for(
                self._complete(client, messages),
                timeout=timeout_s,
            )
            return PlannerWarmupResult(
                ok=True,
                elapsed_ms=(perf_counter() - started) * 1000.0,
            )
        except Exception as error:
            return PlannerWarmupResult(
                ok=False,
                elapsed_ms=(perf_counter() - started) * 1000.0,
                error=f"{type(error).__name__}: {error}",
            )

    @staticmethod
    async def _complete(client: Any, messages: list[dict[str, str]]) -> str:
        async_complete = getattr(client, "complete_async", None)
        if callable(async_complete):
            return str(await async_complete(messages))
        sync_complete = getattr(client, "complete", None)
        if not callable(sync_complete):
            raise TypeError("recovery planner client has no completion method")
        # Synchronous clients are supported for deterministic test doubles only.
        # Production uses OpenAICompatibleRecoveryClient so timeout cancellation
        # remains enforceable without a background thread.
        return str(sync_complete(messages))

    @staticmethod
    def _messages(event: AnomalyEvent) -> list[dict[str, str]]:
        system = """Choose recovery from only the evidence. d is agent, link,
route, or unknown. s is tune, standby, reroute, reinstall, repair, or rebuild.
agent_report_offline means d=agent; use s=standby when a local standby exists,
otherwise reroute. gateway_route_missing means d=route and s=reinstall. A
business_qos_violation without those conflicts means d=link and s=repair; this
testbed has no redundant path. Use rebuild only for unknown or no targeted
action. c is confidence 0..100. Return one JSON object with exactly keys d, s,
c and no prose or extra fields."""
        payload = asdict(event)
        # A monotonic timestamp is irrelevant to diagnosis and needlessly varies prompts.
        payload.pop("detected_at", None)
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]


def build_anomaly_event(
    controller: AgentController,
    subnet: TaskSubnet,
    probe: VerifyResult | None,
    *,
    detected_at: float,
    run_id: int,
    detection_source: str = "auto",
) -> AnomalyEvent:
    selected_ids = subnet.trans_agents | subnet.net_agents | subnet.phy_agents
    all_support_reports: dict[str, dict[str, Any]] = {}
    for gateway_id in sorted(subnet.involved_gateways):
        gateway = controller.gateways[gateway_id]
        for agent_id, agent in sorted(gateway.agents.items()):
            card = agent.card
            if card.role != AgentRole.SUPPORT:
                continue
            all_support_reports[agent_id] = {
                "online": card.online,
                "layer": card.layer.value,
                "gateway_id": card.gateway_id,
                "selected": agent_id in selected_ids,
            }

    missing = controller.missing_gateway_routes(subnet)
    conflicts: list[str] = []
    affected_sessions: set[str] = set()

    offline_ids = {
        agent_id
        for agent_id, report in all_support_reports.items()
        if report["selected"] and not report["online"]
    }
    if offline_ids:
        conflicts.append("agent_report_offline")
        affected_sessions.update(
            session.session_id
            for session in subnet.sessions
            if session.t_agent_id in offline_ids or session.n_agent_id in offline_ids
        )

    if missing:
        conflicts.append("gateway_route_missing")
        affected_sessions.update(
            entry.session_id for entries in missing.values() for entry in entries
        )

    all_failed_edges = [
        item
        for item in (probe.edge_results if probe else ())
        if not item.ok
    ]
    failed_edges = [
        item
        for item in all_failed_edges
        if not affected_sessions or item.session_id in affected_sessions
    ]
    structural_conflict = bool(offline_ids or missing)
    if failed_edges and not structural_conflict:
        conflicts.append("business_qos_violation")
    if failed_edges:
        affected_sessions.update(item.session_id for item in failed_edges)

    reports: dict[str, dict[str, Any]] = {}
    offline_layers = {
        str(all_support_reports[agent_id]["layer"])
        for agent_id in offline_ids
    }
    for agent_id, state in all_support_reports.items():
        if agent_id in offline_ids or (
            offline_layers
            and state["online"]
            and not state["selected"]
            and state["layer"] in offline_layers
        ):
            reports[agent_id] = state

    if offline_ids:
        relevant_gateways = {
            str(all_support_reports[agent_id]["gateway_id"])
            for agent_id in offline_ids
        }
    elif missing:
        relevant_gateways = set(missing)
    else:
        relevant_gateways = {
            gateway_id
            for session in subnet.sessions
            if session.session_id in affected_sessions
            for gateway_id in (
                session.gateway_path
                or (session.source_gateway, session.target_gateway)
            )
        }
    gateway_state: dict[str, dict[str, Any]] = {}
    for gateway_id in sorted(relevant_gateways):
        gateway = controller.gateways[gateway_id]
        absent = missing.get(gateway_id, [])
        gateway_state[gateway_id] = {
            "online": gateway.online,
            "missing_rule_count": len(absent),
            "missing_session_ids": sorted({entry.session_id for entry in absent}),
        }

    edge_results = tuple(
        _compact_edge_result(item, include_metrics=not structural_conflict)
        for item in failed_edges
    )

    resolved_source = detection_source
    if detection_source == "auto":
        if offline_ids:
            resolved_source = "agent_state_monitor"
        elif missing:
            resolved_source = "gateway_route_audit"
        elif edge_results:
            resolved_source = "business_qos_monitor"
        else:
            resolved_source = "external_detector"

    return AnomalyEvent(
        event_id=f"{subnet.task.task_id}-recovery-{run_id}",
        task_id=subnet.task.task_id,
        detected_at=detected_at,
        detection_source=resolved_source,
        affected_session_ids=tuple(sorted(affected_sessions)),
        observed_conflicts=tuple(conflicts or ("state_report_conflict",)),
        agent_state_reports=reports,
        gateway_state=gateway_state,
        business_edge_results=edge_results,
    )


def _compact_edge_result(item: Any, *, include_metrics: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "session_id": item.session_id,
        "source": item.source,
        "target": item.target,
        "ok": item.ok,
        "reachable": item.reachable,
    }
    if include_metrics:
        for name in (
            "latency_ms",
            "max_latency_ms",
            "loss_rate",
            "max_loss_rate",
            "throughput_mbps",
            "min_bandwidth_mbps",
        ):
            value = getattr(item, name)
            if value is not None:
                result[name] = round(float(value), 3)
    if item.error:
        result["error"] = item.error[:200]
    return result


def deterministic_recovery_plan(event: AnomalyEvent) -> RecoveryPlan:
    diagnosis = diagnose_anomaly(event)
    strategy = "full_rebuild"
    target_agent_id: str | None = None
    affected_gateways = _evidence_gateway_ids(event, diagnosis)

    if diagnosis == "agent_offline":
        offline = _offline_selected_agents(event)
        target_agent_id = sorted(offline)[0] if offline else None
        strategy = _agent_recovery_strategy(event, target_agent_id)
    elif diagnosis == "gateway_rule_loss":
        strategy = "gateway_rule_reinstall"
    elif diagnosis == "link_degrade":
        strategy = "link_repair"

    return RecoveryPlan(
        diagnosed_fault_type=diagnosis,
        conflict_types=event.observed_conflicts,
        affected_session_ids=event.affected_session_ids,
        strategy=strategy,
        target_agent_id=target_agent_id,
        affected_gateway_ids=affected_gateways,
        confidence=1.0,
        reason="deterministic bounded recovery selected from observed state",
    )


def validate_recovery_plan(
    raw: str,
    event: AnomalyEvent,
    *,
    confidence_threshold: float = 0.7,
) -> RecoveryPlan:
    payload = _extract_json_object(raw)
    if set(payload) == {"d", "s", "c"}:
        payload = _expand_compact_plan(payload, event)
    required = {
        "diagnosed_fault_type",
        "conflict_types",
        "affected_session_ids",
        "strategy",
        "target_agent_id",
        "affected_gateway_ids",
        "confidence",
        "reason",
    }
    if set(payload) != required:
        missing = sorted(required - set(payload))
        extra = sorted(set(payload) - required)
        raise ValueError(f"plan fields mismatch; missing={missing}, extra={extra}")

    diagnosis = _require_choice(payload["diagnosed_fault_type"], SUPPORTED_DIAGNOSES, "diagnosed_fault_type")
    strategy = _require_choice(payload["strategy"], SUPPORTED_RECOVERY_STRATEGIES, "strategy")
    conflict_types = _string_tuple(payload["conflict_types"], "conflict_types")
    session_ids = _string_tuple(payload["affected_session_ids"], "affected_session_ids")
    gateway_ids = _string_tuple(payload["affected_gateway_ids"], "affected_gateway_ids")
    target_agent_id = payload["target_agent_id"]
    if target_agent_id is not None and not isinstance(target_agent_id, str):
        raise ValueError("target_agent_id must be a string or null")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if confidence < confidence_threshold:
        raise ValueError(
            f"confidence {confidence:.3f} is below threshold {confidence_threshold:.3f}"
        )
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("reason must be a non-empty string up to 500 characters")

    unknown_conflicts = set(conflict_types) - set(event.observed_conflicts)
    if not conflict_types or unknown_conflicts:
        raise ValueError(f"conflict_types are not supported by evidence: {sorted(unknown_conflicts)}")
    unknown_sessions = set(session_ids) - set(event.affected_session_ids)
    if event.affected_session_ids and (not session_ids or unknown_sessions):
        raise ValueError(f"affected_session_ids are not supported by evidence: {sorted(unknown_sessions)}")
    unknown_gateways = set(gateway_ids) - set(event.gateway_state)
    if unknown_gateways:
        raise ValueError(f"affected_gateway_ids are unknown: {sorted(unknown_gateways)}")

    expected_diagnosis = diagnose_anomaly(event)
    if diagnosis != expected_diagnosis:
        raise ValueError(
            f"diagnosis {diagnosis} conflicts with observed evidence {expected_diagnosis}"
        )
    available = available_strategies(event)
    if strategy not in available:
        raise ValueError(f"strategy {strategy} is not executable; available={sorted(available)}")

    evidence_gateways = set(_evidence_gateway_ids(event, diagnosis))
    if evidence_gateways and not set(gateway_ids).intersection(evidence_gateways):
        raise ValueError("affected_gateway_ids do not include an evidenced gateway")

    offline = _offline_selected_agents(event)
    if diagnosis == "agent_offline":
        if target_agent_id not in offline:
            raise ValueError("target_agent_id must identify an offline selected support Agent")
    elif target_agent_id is not None:
        raise ValueError("target_agent_id must be null when no Agent is offline")

    return RecoveryPlan(
        diagnosed_fault_type=diagnosis,
        conflict_types=conflict_types,
        affected_session_ids=session_ids,
        strategy=strategy,
        target_agent_id=target_agent_id,
        affected_gateway_ids=gateway_ids,
        confidence=confidence,
        reason=reason.strip(),
    )


def diagnose_anomaly(event: AnomalyEvent) -> str:
    if _offline_selected_agents(event):
        return "agent_offline"
    if any(int(state.get("missing_rule_count", 0)) > 0 for state in event.gateway_state.values()):
        return "gateway_rule_loss"
    if any(not bool(item.get("ok")) for item in event.business_edge_results):
        return "link_degrade"
    return "unknown"


def available_strategies(event: AnomalyEvent) -> set[str]:
    diagnosis = diagnose_anomaly(event)
    if diagnosis == "agent_offline":
        offline = sorted(_offline_selected_agents(event))
        strategy = _agent_recovery_strategy(event, offline[0] if offline else None)
        return {strategy}
    if diagnosis == "gateway_rule_loss":
        return {"gateway_rule_reinstall"}
    if diagnosis == "link_degrade":
        return {"link_repair"}
    return {"full_rebuild"}


def model_strategy_from_raw(raw: str) -> str:
    try:
        payload = _extract_json_object(raw)
        value = payload.get("strategy", "")
        if isinstance(value, str) and value:
            return value
        compact = payload.get("s")
        return _COMPACT_STRATEGIES.get(compact, "") if isinstance(compact, str) else ""
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""


def _expand_compact_plan(
    payload: dict[str, Any],
    event: AnomalyEvent,
) -> dict[str, Any]:
    diagnosis_code = payload["d"]
    strategy_code = payload["s"]
    confidence_value = payload["c"]
    if not isinstance(diagnosis_code, str) or diagnosis_code not in _COMPACT_DIAGNOSES:
        raise ValueError("compact diagnosis d is invalid")
    if not isinstance(strategy_code, str) or strategy_code not in _COMPACT_STRATEGIES:
        raise ValueError("compact strategy s is invalid")
    if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
        raise ValueError("compact confidence c must be numeric")
    confidence = float(confidence_value)
    if not 0.0 <= confidence <= 100.0:
        raise ValueError("compact confidence c must be between 0 and 100")

    bounded = deterministic_recovery_plan(event)
    return {
        "diagnosed_fault_type": _COMPACT_DIAGNOSES[diagnosis_code],
        "conflict_types": list(event.observed_conflicts),
        "affected_session_ids": list(event.affected_session_ids),
        "strategy": _COMPACT_STRATEGIES[strategy_code],
        "target_agent_id": bounded.target_agent_id,
        "affected_gateway_ids": list(bounded.affected_gateway_ids),
        "confidence": confidence / 100.0,
        "reason": "LLM selected bounded diagnosis and recovery strategy",
    }


def _agent_recovery_strategy(event: AnomalyEvent, target_agent_id: str | None) -> str:
    target = event.agent_state_reports.get(target_agent_id or "", {})
    target_gateway = target.get("gateway_id")
    target_layer = target.get("layer")
    local_standby = any(
        report.get("online")
        and not report.get("selected")
        and report.get("gateway_id") == target_gateway
        and report.get("layer") == target_layer
        for report in event.agent_state_reports.values()
    )
    if local_standby:
        return "support_agent_replace"
    remote_standby = any(
        report.get("online")
        and not report.get("selected")
        and report.get("layer") == target_layer
        for report in event.agent_state_reports.values()
    )
    return "communication_reroute" if remote_standby else "full_rebuild"


def _offline_selected_agents(event: AnomalyEvent) -> set[str]:
    return {
        agent_id
        for agent_id, report in event.agent_state_reports.items()
        if report.get("selected") and not report.get("online")
    }


def _evidence_gateway_ids(event: AnomalyEvent, diagnosis: str) -> tuple[str, ...]:
    if diagnosis == "agent_offline":
        return tuple(
            sorted(
                {
                    str(event.agent_state_reports[agent_id]["gateway_id"])
                    for agent_id in _offline_selected_agents(event)
                }
            )
        )
    if diagnosis == "gateway_rule_loss":
        return tuple(
            gateway_id
            for gateway_id, state in sorted(event.gateway_state.items())
            if int(state.get("missing_rule_count", 0)) > 0
        )
    affected = set(event.affected_session_ids)
    gateways: set[str] = set()
    for agent_id, report in event.agent_state_reports.items():
        if not report.get("selected"):
            continue
        if any(agent_id in (item.get("source"), item.get("target")) for item in event.business_edge_results if item.get("session_id") in affected):
            gateways.add(str(report["gateway_id"]))
    return tuple(sorted(gateways or event.gateway_state.keys()))


def _extract_json_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise TypeError("model response must be text")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model response does not contain a JSON object")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("recovery plan must be a JSON object")
    return payload


def _require_choice(value: Any, choices: tuple[str, ...], field_name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{field_name} must be one of {choices}")
    return value


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} contains duplicate values")
    return tuple(value)
