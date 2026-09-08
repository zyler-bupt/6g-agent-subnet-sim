from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from src.core.models import Message


@dataclass
class AsyncMessageBus:
    queues: dict[str, asyncio.Queue[Message]] = field(default_factory=lambda: defaultdict(asyncio.Queue))

    async def send(self, message: Message) -> None:
        await self.queues[message.target].put(message)

    async def receive(self, target: str, timeout: float | None = None) -> Message:
        if timeout is None:
            return await self.queues[target].get()
        return await asyncio.wait_for(self.queues[target].get(), timeout=timeout)

