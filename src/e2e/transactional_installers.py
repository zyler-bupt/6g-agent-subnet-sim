from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable

from src.core.gateway import Gateway
from src.core.models import GatewayAck, SessionSpec, to_jsonable
from src.core.rules import RuleDelta


@dataclass(frozen=True)
class TransactionalInstallResult:
    accepted: bool
    gateway_id: str
    operation: str
    version: int
    reason: str = "ok"
    control_messages: int = 0
    control_bytes: int = 0
    rule_ack: GatewayAck | None = None
    session_ack: GatewayAck | None = None


@runtime_checkable
class TransactionalInstaller(Protocol):
    mode: str

    async def stage(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
        delta: RuleDelta,
        sessions: tuple[SessionSpec, ...],
    ) -> TransactionalInstallResult:
        ...

    async def validate(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ...

    async def activate(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ...

    async def rollback(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ...

    def finalize(self, gateway: Gateway, task_id: str, version: int) -> None:
        ...


class InMemoryTransactionalInstaller:
    mode = "in_memory_transactional"

    async def stage(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
        delta: RuleDelta,
        sessions: tuple[SessionSpec, ...],
    ) -> TransactionalInstallResult:
        request_bytes = _payload_bytes(
            {
                "task_id": task_id,
                "version": version,
                "additions": delta.additions,
                "updates": delta.updates,
                "deletions": delta.deletions,
                "sessions": sessions,
            }
        )
        rule_ack = await gateway.stage_delta(
            task_id,
            version,
            delta.additions,
            delta.updates,
            delta.deletions,
        )
        if not rule_ack.accepted:
            return TransactionalInstallResult(
                accepted=False,
                gateway_id=gateway.gateway_id,
                operation="stage",
                version=version,
                reason=rule_ack.reason,
                control_messages=2,
                control_bytes=request_bytes + _payload_bytes(rule_ack),
                rule_ack=rule_ack,
            )
        session_ack = await gateway.stage_sessions(task_id, version, sessions)
        return TransactionalInstallResult(
            accepted=session_ack.accepted,
            gateway_id=gateway.gateway_id,
            operation="stage",
            version=version,
            reason=session_ack.reason,
            control_messages=4,
            control_bytes=(
                request_bytes
                + _payload_bytes(rule_ack)
                + _payload_bytes({"task_id": task_id, "version": version, "sessions": sessions})
                + _payload_bytes(session_ack)
            ),
            rule_ack=rule_ack,
            session_ack=session_ack,
        )

    async def validate(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ack = await gateway.validate_staged(task_id, version)
        return _ack_result(ack, "validate")

    async def activate(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ack = await gateway.activate(task_id, version)
        return _ack_result(ack, "activate")

    async def rollback(
        self,
        gateway: Gateway,
        task_id: str,
        version: int,
    ) -> TransactionalInstallResult:
        ack = await gateway.rollback(task_id, version)
        return _ack_result(ack, "rollback")

    def finalize(self, gateway: Gateway, task_id: str, version: int) -> None:
        gateway.finalize(task_id, version)


class NetnsTransactionalInstaller:
    """Interface-compatible reservation for a real netns two-phase backend.

    The existing NetnsGatewayInstaller performs immediate route changes and
    cannot safely claim transactional stage/activate semantics.  These methods
    therefore fail explicitly until a shadow-table/atomic-swap backend is added.
    """

    mode = "netns_transactional_unimplemented"

    async def stage(self, *args, **kwargs) -> TransactionalInstallResult:
        raise NotImplementedError(
            "netns transactional stage is not implemented; use the in-memory backend"
        )

    async def validate(self, *args, **kwargs) -> TransactionalInstallResult:
        raise NotImplementedError("netns transactional validation is not implemented")

    async def activate(self, *args, **kwargs) -> TransactionalInstallResult:
        raise NotImplementedError("netns atomic activation is not implemented")

    async def rollback(self, *args, **kwargs) -> TransactionalInstallResult:
        raise NotImplementedError("netns transactional rollback is not implemented")

    def finalize(self, *args, **kwargs) -> None:
        raise NotImplementedError("netns transactional finalize is not implemented")


def _ack_result(ack: GatewayAck, operation: str) -> TransactionalInstallResult:
    request = {"task_id": ack.task_id, "version": ack.version, "operation": operation}
    return TransactionalInstallResult(
        accepted=ack.accepted,
        gateway_id=ack.gateway_id,
        operation=operation,
        version=ack.version,
        reason=ack.reason,
        control_messages=2,
        control_bytes=_payload_bytes(request) + _payload_bytes(ack),
        rule_ack=ack,
    )


def _payload_bytes(value: object) -> int:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    return len(
        json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
