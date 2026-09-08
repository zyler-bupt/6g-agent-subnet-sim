from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from src.core.models import GatewayRouteEntry, TaskSubnet
from src.e2e.models import GatewayCommandLog, GatewayInstallResult


class GatewayInstaller(Protocol):
    async def install_subnet(self, subnet: TaskSubnet) -> GatewayInstallResult:
        ...

    async def apply_delta(self, subnet: TaskSubnet, delta: Any) -> GatewayInstallResult:
        ...


@dataclass
class SimulatedGatewayInstaller:
    control_rtt_ms: float = 10.0
    rule_install_ms: float = 30.0
    ack_ms: float = 5.0
    jitter_ms: float = 5.0
    seed: int = 17
    fail_gateways: set[str] = field(default_factory=set)

    async def install_subnet(self, subnet: TaskSubnet) -> GatewayInstallResult:
        return await self._install(subnet, set(subnet.involved_gateways), None)

    async def apply_delta(self, subnet: TaskSubnet, delta: Any) -> GatewayInstallResult:
        gateway_ids, session_ids = _affected_scope(subnet, delta)
        return await self._install(subnet, gateway_ids, session_ids)

    async def _install(
        self,
        subnet: TaskSubnet,
        gateway_ids: set[str],
        session_ids: set[str] | None,
    ) -> GatewayInstallResult:
        if not gateway_ids:
            return GatewayInstallResult(
                ok=True,
                install_ms=0.0,
                updated_gateway_count=0,
                updated_rule_count=0,
                mode="simulated",
            )

        rng = random.Random(self.seed)
        gateway_times = {
            gateway_id: max(
                0.0,
                self.control_rtt_ms
                + self.rule_install_ms
                + self.ack_ms
                + rng.uniform(-self.jitter_ms, self.jitter_ms),
            )
            for gateway_id in sorted(gateway_ids)
        }
        started = perf_counter()
        await asyncio.gather(
            *(asyncio.sleep(value / 1000.0) for value in gateway_times.values())
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        errors = tuple(
            f"{gateway_id}: simulated install failure"
            for gateway_id in sorted(gateway_ids & self.fail_gateways)
        )
        route_count = _route_count(subnet, gateway_ids, session_ids)
        return GatewayInstallResult(
            ok=not errors,
            install_ms=elapsed_ms,
            updated_gateway_count=len(gateway_ids),
            updated_rule_count=route_count,
            errors=errors,
            mode="simulated",
            gateway_times_ms=gateway_times,
        )


@dataclass(frozen=True)
class NetnsGatewayTarget:
    namespace: str
    device: str
    router_ip: str


def rescue_netns_gateway_targets(
    *,
    term_namespace: str = "h-term",
    edge_namespace: str = "h-edge",
    cloud_namespace: str = "h-cloud",
) -> dict[str, NetnsGatewayTarget]:
    return {
        "gw-ue": NetnsGatewayTarget(term_namespace, "term0", "10.10.1.1"),
        "gw-mec": NetnsGatewayTarget(edge_namespace, "edge0", "10.10.2.1"),
        "gw-cloud": NetnsGatewayTarget(cloud_namespace, "cloud0", "10.10.3.1"),
    }


@dataclass
class NetnsGatewayInstaller:
    targets: dict[str, NetnsGatewayTarget]
    sudo: bool = False
    command_timeout_s: float = 8.0

    async def install_subnet(self, subnet: TaskSubnet) -> GatewayInstallResult:
        return await self._install(subnet, set(subnet.involved_gateways), None)

    async def apply_delta(self, subnet: TaskSubnet, delta: Any) -> GatewayInstallResult:
        gateway_ids, session_ids = _affected_scope(subnet, delta)
        return await self._install(subnet, gateway_ids, session_ids)

    async def _install(
        self,
        subnet: TaskSubnet,
        gateway_ids: set[str],
        session_ids: set[str] | None,
    ) -> GatewayInstallResult:
        if not gateway_ids:
            return GatewayInstallResult(
                ok=True,
                install_ms=0.0,
                updated_gateway_count=0,
                updated_rule_count=0,
                mode="netns-ip-route",
            )

        errors: list[str] = []
        command_groups: dict[str, list[tuple[list[str], bool]]] = {}
        for gateway_id in sorted(gateway_ids):
            target = self.targets.get(gateway_id)
            if target is None:
                errors.append(f"{gateway_id}: no netns gateway target configured")
                continue
            entries = [
                entry
                for entry in subnet.gateway_routes.get(gateway_id, [])
                if session_ids is None or entry.session_id in session_ids
            ]
            command_groups[gateway_id] = self._commands_for_entries(target, entries)

        started = perf_counter()
        grouped_logs = await asyncio.gather(
            *(
                self._run_gateway_commands(gateway_id, commands)
                for gateway_id, commands in command_groups.items()
            )
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        logs = tuple(log for group in grouped_logs for log in group)
        for log in logs:
            if log.returncode != 0:
                errors.append(
                    f"{log.gateway_id}: command failed ({log.returncode}): "
                    + " ".join(log.command)
                    + (f"; {log.stderr.strip()}" if log.stderr.strip() else "")
                )

        gateway_times: dict[str, float] = {}
        for log in logs:
            gateway_times[log.gateway_id] = gateway_times.get(log.gateway_id, 0.0) + log.elapsed_ms
        mutated = sum(
            1
            for gateway_id, commands in command_groups.items()
            for _command, is_mutation in commands
            if is_mutation
            and all(
                log.returncode == 0
                for log in logs
                if log.gateway_id == gateway_id and tuple(_command) == log.command
            )
        )
        validated = sum(
            1
            for _gateway_id, commands in command_groups.items()
            for _command, is_mutation in commands
            if not is_mutation
        )
        return GatewayInstallResult(
            ok=not errors,
            install_ms=elapsed_ms,
            updated_gateway_count=len(command_groups),
            updated_rule_count=mutated,
            errors=tuple(errors),
            mode="netns-ip-route",
            gateway_times_ms=gateway_times,
            command_logs=logs,
            validated_rule_count=validated,
        )

    def _commands_for_entries(
        self,
        target: NetnsGatewayTarget,
        entries: list[GatewayRouteEntry],
    ) -> list[tuple[list[str], bool]]:
        commands: list[tuple[list[str], bool]] = []
        seen: set[tuple[str, ...]] = set()
        for entry in entries:
            if entry.action.mode == "forward_to_gateway":
                destination = entry.action.next_hop_gateway_ip
                if not destination:
                    continue
                command = self._prefix(
                    "ip",
                    "-n",
                    target.namespace,
                    "route",
                    "replace",
                    f"{destination}/32",
                    "via",
                    target.router_ip,
                    "dev",
                    target.device,
                    "proto",
                    "static",
                )
                is_mutation = True
            elif entry.action.mode == "local_delivery" and entry.action.local_agent_ip:
                # Local-delivery addresses already belong to the endpoint. A
                # route lookup verifies that binding without replacing the
                # kernel's local-table route.
                command = self._prefix(
                    "ip",
                    "-n",
                    target.namespace,
                    "route",
                    "get",
                    entry.action.local_agent_ip,
                )
                is_mutation = False
            else:
                continue
            key = tuple(command)
            if key not in seen:
                seen.add(key)
                commands.append((command, is_mutation))
        return commands

    async def _run_gateway_commands(
        self,
        gateway_id: str,
        commands: list[tuple[list[str], bool]],
    ) -> list[GatewayCommandLog]:
        logs: list[GatewayCommandLog] = []
        for command, _is_mutation in commands:
            logs.append(await self._run_command(gateway_id, command))
        return logs

    async def _run_command(self, gateway_id: str, command: list[str]) -> GatewayCommandLog:
        started = perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.command_timeout_s,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return GatewayCommandLog(
                    gateway_id=gateway_id,
                    command=tuple(command),
                    elapsed_ms=(perf_counter() - started) * 1000.0,
                    returncode=124,
                    stderr="command timed out",
                )
            return GatewayCommandLog(
                gateway_id=gateway_id,
                command=tuple(command),
                elapsed_ms=(perf_counter() - started) * 1000.0,
                returncode=int(process.returncode or 0),
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        except OSError as error:
            return GatewayCommandLog(
                gateway_id=gateway_id,
                command=tuple(command),
                elapsed_ms=(perf_counter() - started) * 1000.0,
                returncode=127,
                stderr=str(error),
            )

    def _prefix(self, *command: str) -> list[str]:
        result = list(command)
        return ["sudo", *result] if self.sudo else result


def _affected_scope(subnet: TaskSubnet, delta: Any) -> tuple[set[str], set[str] | None]:
    explicit_gateways = set(getattr(delta, "changed_gateway_ids", ()) or ())
    explicit_sessions = set(getattr(delta, "changed_session_ids", ()) or ())
    changed_sessions = tuple(getattr(delta, "changed_sessions", ()) or ())
    session_ids = explicit_sessions | {session.session_id for session in changed_sessions}
    gateway_ids = explicit_gateways | {
        gateway_id
        for session in changed_sessions
        for gateway_id in (session.gateway_path or (session.source_gateway, session.target_gateway))
    }
    if not gateway_ids and int(getattr(delta, "changed_gateways", 0) or 0) > 0:
        gateway_ids = set(subnet.involved_gateways)
    return gateway_ids, session_ids or None


def _route_count(
    subnet: TaskSubnet,
    gateway_ids: set[str],
    session_ids: set[str] | None,
) -> int:
    return sum(
        1
        for gateway_id in gateway_ids
        for entry in subnet.gateway_routes.get(gateway_id, [])
        if session_ids is None or entry.session_id in session_ids
    )
