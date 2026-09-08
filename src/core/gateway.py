from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Iterable

from src.agents.base import BaseAgent
from src.core.models import AgentCard, GatewayAck, GatewayRouteEntry, SessionSpec, TaskSpec


@dataclass
class Gateway:
    gateway_id: str
    subnet_id: str
    node: str
    gateway_ip: str = ""
    gateway_port: int = 7000
    agents: dict[str, BaseAgent] = field(default_factory=dict)
    installed_sessions: dict[str, SessionSpec] = field(default_factory=dict)
    stable_rules: dict[str, GatewayRouteEntry] = field(default_factory=dict)
    staged_rules: dict[str, GatewayRouteEntry] = field(default_factory=dict)
    staged_sessions: dict[str, SessionSpec] = field(default_factory=dict)
    stable_versions: dict[str, int] = field(default_factory=dict)
    staged_versions: dict[str, int] = field(default_factory=dict)
    update_count: int = 0
    online: bool = True
    reject_stage_versions: set[int] = field(default_factory=set)
    reject_activate_versions: set[int] = field(default_factory=set)
    _rollback_rules: dict[str, dict[str, GatewayRouteEntry]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _rollback_sessions: dict[str, dict[str, SessionSpec]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _rollback_versions: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    @property
    def route_table(self) -> dict[str, GatewayRouteEntry]:
        """Compatibility view; stable_rules is the only active rule store."""

        return self.stable_rules

    @property
    def stable_version(self) -> int:
        return max(self.stable_versions.values(), default=0)

    @property
    def staged_version(self) -> int | None:
        return max(self.staged_versions.values()) if self.staged_versions else None

    def register(self, agent: BaseAgent) -> None:
        self.agents[agent.agent_id] = agent

    def fail_agent(self, agent_id: str) -> bool:
        """Simulate a local Agent going offline (F_m event). Returns success."""
        agent = self.agents.get(agent_id)
        if agent is None:
            return False
        agent.card = replace(agent.card, status="offline")
        return True

    async def confirm_member(self, agent_id: str) -> AgentCard | None:
        await asyncio.sleep(0)
        agent = self.agents.get(agent_id)
        if agent is None or not agent.card.online:
            return None
        return agent.card

    async def confirm_support(self, capability: str) -> list[AgentCard]:
        await asyncio.sleep(0)
        return [
            agent.card
            for agent in self.agents.values()
            if agent.card.online and capability in agent.card.capabilities
        ]

    async def install_subnet(
        self,
        task: TaskSpec,
        sessions: list[SessionSpec],
        route_entries: list[GatewayRouteEntry],
    ) -> GatewayAck:
        await asyncio.sleep(0)
        version = _configuration_version(route_entries, self.stable_versions.get(task.task_id, 0) or 1)
        if version < self.stable_versions.get(task.task_id, 0):
            return self._reject(task.task_id, "install", "stale_version", version=version)
        rejected = self._validate_route_entries(task.task_id, route_entries, operation="install")
        if rejected is not None:
            return rejected
        self._remove_stable_task_rules(task.task_id)
        self._remove_stable_task_sessions(task.task_id)
        installed_session_ids: list[str] = []
        for session in sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
                installed_session_ids.append(session.session_id)
        installed_route_ids: list[str] = []
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                key = entry_key(entry)
                self.stable_rules[key] = entry
                installed_route_ids.append(key)
        self.stable_versions[task.task_id] = version
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task.task_id,
            accepted=True,
            operation="install",
            installed_session_ids=tuple(sorted(installed_session_ids)),
            installed_route_ids=tuple(sorted(installed_route_ids)),
            session_count=len(installed_session_ids),
            route_count=len(installed_route_ids),
            version=version,
        )

    async def apply_update(
        self,
        task_id: str,
        changed_sessions: list[SessionSpec],
        route_entries: list[GatewayRouteEntry],
    ) -> GatewayAck:
        await asyncio.sleep(0)
        self.update_count += 1
        version = _configuration_version(
            route_entries,
            self.stable_versions.get(task_id, 0) or 1,
        )
        if version < self.stable_versions.get(task_id, 0):
            return self._reject(task_id, "update", "stale_version", version=version)
        rejected = self._validate_route_entries(task_id, route_entries, operation="update")
        if rejected is not None:
            return rejected
        installed_session_ids: list[str] = []
        for session in changed_sessions:
            if self.gateway_id in _session_gateways(session):
                self.installed_sessions[session.session_id] = session
                installed_session_ids.append(session.session_id)
        installed_route_ids: list[str] = []
        for entry in route_entries:
            if entry.gateway_id == self.gateway_id:
                key = entry_key(entry)
                self.stable_rules[key] = entry
                installed_route_ids.append(key)
        self.stable_versions[task_id] = max(self.stable_versions.get(task_id, 0), version)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="update",
            installed_session_ids=tuple(sorted(installed_session_ids)),
            installed_route_ids=tuple(sorted(installed_route_ids)),
            session_count=len(installed_session_ids),
            route_count=len(installed_route_ids),
            version=version,
        )

    async def stage_delta(
        self,
        task_id: str,
        version: int,
        additions: Iterable[GatewayRouteEntry],
        updates: Iterable[GatewayRouteEntry],
        deletions: Iterable[GatewayRouteEntry],
    ) -> GatewayAck:
        additions = tuple(additions)
        updates = tuple(updates)
        deletions = tuple(deletions)
        current_version = self.stable_versions.get(task_id, 0)
        if not self.online:
            return self._reject(task_id, "stage", "gateway_offline", version=version)
        if version <= current_version:
            return self._reject(task_id, "stage", "stale_version", version=version)
        existing_stage = self.staged_versions.get(task_id)
        if existing_stage is not None and existing_stage != version:
            return self._reject(
                task_id,
                "stage",
                f"another_version_staged:{existing_stage}",
                version=version,
            )
        if version in self.reject_stage_versions:
            return self._reject(task_id, "stage", "injected_stage_failure", version=version)

        changed = additions + updates
        rejected = self._validate_route_entries(
            task_id,
            list(changed),
            operation="stage",
        )
        if rejected is not None:
            return replace(rejected, version=version)
        for entry in changed:
            if entry.version != version:
                return self._reject(
                    task_id,
                    "stage",
                    f"rule_version_mismatch:{entry.rule_id}",
                    version=version,
                )
        for entry in deletions:
            if entry.task_id != task_id or entry.gateway_id != self.gateway_id:
                return self._reject(
                    task_id,
                    "stage",
                    f"invalid_deletion:{entry.rule_id}",
                    version=version,
                )

        stable = self.get_stable_rules(task_id)
        candidate = dict(stable)
        for entry in additions:
            if entry.rule_id in candidate:
                return self._reject(
                    task_id,
                    "stage",
                    f"addition_already_exists:{entry.rule_id}",
                    version=version,
                )
            candidate[entry.rule_id] = entry
        for entry in updates:
            if entry.rule_id not in candidate:
                return self._reject(
                    task_id,
                    "stage",
                    f"update_missing_rule:{entry.rule_id}",
                    version=version,
                )
            candidate[entry.rule_id] = entry
        for entry in deletions:
            if entry.rule_id not in candidate:
                return self._reject(
                    task_id,
                    "stage",
                    f"delete_missing_rule:{entry.rule_id}",
                    version=version,
                )
            candidate.pop(entry.rule_id)

        if task_id not in self._rollback_rules:
            self._rollback_rules[task_id] = deepcopy(stable)
            self._rollback_sessions[task_id] = deepcopy(self._task_sessions(task_id))
            self._rollback_versions[task_id] = current_version
        self._remove_staged_task_rules(task_id)
        self.staged_rules.update(candidate)
        self.staged_versions[task_id] = version
        changed_ids = tuple(
            sorted(entry.rule_id for entry in additions + updates + deletions)
        )
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="stage",
            installed_route_ids=changed_ids,
            route_count=len(changed_ids),
            version=version,
        )

    async def stage_sessions(
        self,
        task_id: str,
        version: int,
        sessions: Iterable[SessionSpec],
    ) -> GatewayAck:
        if self.staged_versions.get(task_id) != version:
            return self._reject(task_id, "stage_sessions", "rule_stage_missing", version=version)
        self._remove_staged_task_sessions(task_id)
        installed: list[str] = []
        for session in sessions:
            if session.task_id == task_id and self.gateway_id in _session_gateways(session):
                self.staged_sessions[session.session_id] = session
                installed.append(session.session_id)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="stage_sessions",
            installed_session_ids=tuple(sorted(installed)),
            session_count=len(installed),
            version=version,
        )

    async def validate_staged(self, task_id: str, version: int) -> GatewayAck:
        if self.staged_versions.get(task_id) != version:
            return self._reject(task_id, "validate", "staged_version_missing", version=version)
        rules = list(self.get_staged_rules(task_id).values())
        rejected = self._validate_route_entries(task_id, rules, operation="validate")
        if rejected is not None:
            return replace(rejected, version=version)
        if len({rule.rule_id for rule in rules}) != len(rules):
            return self._reject(task_id, "validate", "duplicate_rule_id", version=version)
        seen_matches: set[tuple[object, ...]] = set()
        for rule in rules:
            match_key = (
                rule.match.src_agent,
                rule.match.dst_agent,
                rule.match.flow_type,
                rule.match.protocol,
                rule.match.dst_port,
            )
            if match_key in seen_matches:
                return self._reject(
                    task_id,
                    "validate",
                    f"conflicting_rule_match:{rule.rule_id}",
                    version=version,
                )
            seen_matches.add(match_key)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="validate",
            installed_route_ids=tuple(sorted(rule.rule_id for rule in rules)),
            route_count=len(rules),
            version=version,
        )

    async def activate(self, task_id: str, version: int) -> GatewayAck:
        if self.staged_versions.get(task_id) != version:
            return self._reject(task_id, "activate", "stale_or_missing_stage", version=version)
        if version <= self.stable_versions.get(task_id, 0):
            return self._reject(task_id, "activate", "stale_version", version=version)
        if version in self.reject_activate_versions:
            return self._reject(task_id, "activate", "injected_activate_failure", version=version)

        candidate_rules = self.get_staged_rules(task_id)
        candidate_sessions = self._staged_task_sessions(task_id)
        self._remove_stable_task_rules(task_id)
        self.stable_rules.update(candidate_rules)
        self._remove_stable_task_sessions(task_id)
        self.installed_sessions.update(candidate_sessions)
        self.stable_versions[task_id] = version
        self._remove_staged_task_rules(task_id)
        self._remove_staged_task_sessions(task_id)
        self.staged_versions.pop(task_id, None)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="activate",
            installed_session_ids=tuple(sorted(candidate_sessions)),
            installed_route_ids=tuple(sorted(candidate_rules)),
            session_count=len(candidate_sessions),
            route_count=len(candidate_rules),
            version=version,
        )

    async def rollback(self, task_id: str, version: int) -> GatewayAck:
        stable_version = self.stable_versions.get(task_id, 0)
        staged_version = self.staged_versions.get(task_id)
        if stable_version > version or (
            staged_version is not None and staged_version != version
        ):
            return self._reject(
                task_id,
                "rollback",
                "stale_or_mismatched_rollback",
                version=version,
            )
        previous_rules = self._rollback_rules.get(task_id)
        previous_sessions = self._rollback_sessions.get(task_id)
        previous_version = self._rollback_versions.get(task_id)
        if stable_version == version and previous_rules is None:
            return self._reject(
                task_id,
                "rollback",
                "rollback_window_closed",
                version=version,
            )
        if stable_version == version and previous_rules is not None:
            self._remove_stable_task_rules(task_id)
            self.stable_rules.update(deepcopy(previous_rules))
            self._remove_stable_task_sessions(task_id)
            self.installed_sessions.update(deepcopy(previous_sessions or {}))
            if previous_version is None or previous_version == 0:
                self.stable_versions.pop(task_id, None)
            else:
                self.stable_versions[task_id] = previous_version
        self._remove_staged_task_rules(task_id)
        self._remove_staged_task_sessions(task_id)
        self.staged_versions.pop(task_id, None)
        self._rollback_rules.pop(task_id, None)
        self._rollback_sessions.pop(task_id, None)
        self._rollback_versions.pop(task_id, None)
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=True,
            operation="rollback",
            route_count=len(self.get_stable_rules(task_id)),
            session_count=len(self._task_sessions(task_id)),
            version=self.stable_versions.get(task_id, 0),
        )

    def finalize(self, task_id: str, version: int) -> None:
        if self.stable_versions.get(task_id) != version:
            raise ValueError(
                f"cannot finalize {task_id} version {version} at {self.gateway_id}"
            )
        self._rollback_rules.pop(task_id, None)
        self._rollback_sessions.pop(task_id, None)
        self._rollback_versions.pop(task_id, None)

    def get_stable_rules(self, task_id: str) -> dict[str, GatewayRouteEntry]:
        return {
            rule_id: rule
            for rule_id, rule in self.stable_rules.items()
            if rule.task_id == task_id
        }

    def get_staged_rules(self, task_id: str) -> dict[str, GatewayRouteEntry]:
        return {
            rule_id: rule
            for rule_id, rule in self.staged_rules.items()
            if rule.task_id == task_id
        }

    def get_stable_sessions(self, task_id: str) -> dict[str, SessionSpec]:
        return self._task_sessions(task_id)

    def get_staged_sessions(self, task_id: str) -> dict[str, SessionSpec]:
        return self._staged_task_sessions(task_id)

    def get_stable_version(self, task_id: str) -> int:
        return self.stable_versions.get(task_id, 0)

    def get_staged_version(self, task_id: str) -> int | None:
        return self.staged_versions.get(task_id)

    def flow_allowed(
        self,
        task_id: str,
        src_agent: str,
        dst_agent: str,
        *,
        staged: bool = False,
    ) -> bool:
        rules = self.get_staged_rules(task_id) if staged else self.get_stable_rules(task_id)
        return any(
            rule.match.src_agent == src_agent
            and rule.match.dst_agent == dst_agent
            and rule.action.allow
            for rule in rules.values()
        )

    def installed_route_table(self) -> list[GatewayRouteEntry]:
        return sorted(
            self.stable_rules.values(),
            key=lambda item: (item.task_id, item.session_id, item.match.dst_agent, item.action.mode),
        )

    def _validate_route_entries(
        self,
        task_id: str,
        route_entries: list[GatewayRouteEntry],
        *,
        operation: str,
    ) -> GatewayAck | None:
        if not self.online:
            return self._reject(task_id, operation, "gateway_offline")
        for entry in route_entries:
            if entry.gateway_id != self.gateway_id:
                return self._reject(task_id, operation, f"route_for_other_gateway:{entry.gateway_id}")
            action = entry.action
            if action.mode == "local_delivery":
                agent_id = action.local_agent or ""
                agent = self.agents.get(agent_id)
                if agent is None or not agent.card.online:
                    return self._reject(task_id, operation, f"local_agent_unavailable:{agent_id}")
                if not action.local_agent_ip:
                    return self._reject(task_id, operation, f"local_agent_ip_missing:{agent_id}")
            elif action.mode == "forward_to_gateway":
                if not action.next_hop_gateway or not action.next_hop_gateway_ip:
                    return self._reject(task_id, operation, f"next_hop_missing:{entry.session_id}")
            elif action.mode != "deny":
                return self._reject(task_id, operation, f"unsupported_route_action:{action.mode}")
            if entry.task_id != task_id:
                return self._reject(
                    task_id,
                    operation,
                    f"route_for_other_task:{entry.task_id}",
                )
        return None

    def _reject(
        self,
        task_id: str,
        operation: str,
        reason: str,
        *,
        version: int = 0,
    ) -> GatewayAck:
        return GatewayAck(
            gateway_id=self.gateway_id,
            task_id=task_id,
            accepted=False,
            reason=reason,
            operation=operation,
            version=version,
        )

    def _remove_stable_task_rules(self, task_id: str) -> None:
        for rule_id, rule in list(self.stable_rules.items()):
            if rule.task_id == task_id:
                self.stable_rules.pop(rule_id)

    def _remove_staged_task_rules(self, task_id: str) -> None:
        for rule_id, rule in list(self.staged_rules.items()):
            if rule.task_id == task_id:
                self.staged_rules.pop(rule_id)

    def _remove_stable_task_sessions(self, task_id: str) -> None:
        for session_id, session in list(self.installed_sessions.items()):
            if session.task_id == task_id:
                self.installed_sessions.pop(session_id)

    def _remove_staged_task_sessions(self, task_id: str) -> None:
        for session_id, session in list(self.staged_sessions.items()):
            if session.task_id == task_id:
                self.staged_sessions.pop(session_id)

    def _task_sessions(self, task_id: str) -> dict[str, SessionSpec]:
        return {
            session_id: session
            for session_id, session in self.installed_sessions.items()
            if session.task_id == task_id
        }

    def _staged_task_sessions(self, task_id: str) -> dict[str, SessionSpec]:
        return {
            session_id: session
            for session_id, session in self.staged_sessions.items()
            if session.task_id == task_id
        }


def entry_key(entry: GatewayRouteEntry) -> str:
    return entry.rule_id


def _session_gateways(session: SessionSpec) -> tuple[str, ...]:
    return session.gateway_path or (session.source_gateway, session.target_gateway)


def _configuration_version(
    entries: Iterable[GatewayRouteEntry],
    default: int,
) -> int:
    versions = {entry.version for entry in entries}
    if not versions:
        return default
    if len(versions) != 1:
        raise ValueError(f"configuration contains mixed rule versions: {sorted(versions)}")
    return next(iter(versions))
