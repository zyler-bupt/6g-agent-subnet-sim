"""In-process bus harness for prototype tests (fast time scale)."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from src.tcanet.prototype.bus import Broker, BusClient

TIME_SCALE = "0.2"


class BusHarness:
    """Temporary run dir + broker; use as ``async with BusHarness() as h``."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))
        self.sock = self.dir / "bus.sock"
        self.broker = Broker(self.sock, self.dir / "events.jsonl")
        self._env: dict[str, str | None] = {}

    async def __aenter__(self) -> "BusHarness":
        for key, value in (("TCANET_RUN_DIR", str(self.dir)), ("TCANET_TIME_SCALE", TIME_SCALE)):
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        await self.broker.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.broker.stop()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    async def client(self, name: str, *topics: str) -> BusClient:
        client = await BusClient.connect(self.sock, name, topics)
        await asyncio.sleep(0.02)
        return client


async def wait_for(client: BusClient, predicate, timeout: float = 3.0) -> dict:
    """First message satisfying ``predicate`` within ``timeout`` seconds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("expected bus message did not arrive")
        msg = await client.recv(timeout=remaining)
        if msg is not None and predicate(msg):
            return msg
