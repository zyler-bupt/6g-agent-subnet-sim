"""Data-plane seam for Apply and Rollback (paper Alg. 1, lines 15 and 19).

The controller hands every staged configuration to a ``Dataplane``.  The
default ``NullDataplane`` only revalidates preconditions (the behaviour of
the in-process simulator); the live prototype installs the forwarding,
binding and layer actions on real executors and undoes them on rollback.
"""
from __future__ import annotations

from typing import Protocol

from src.tcanet.executor import ExecutionRecord, StagedDecision, execute_staged
from src.tcanet.spec import World


class Dataplane(Protocol):
    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord: ...

    async def rollback(self, staged: StagedDecision, world: World) -> None: ...


class NullDataplane:
    """In-process executor: precondition revalidation, no device state."""

    def __init__(self) -> None:
        self.rollbacks: list[int] = []

    async def apply(self, staged: StagedDecision, world: World) -> ExecutionRecord:
        return await execute_staged(staged, world)

    async def rollback(self, staged: StagedDecision, world: World) -> None:
        self.rollbacks.append(staged.subnet.version)
