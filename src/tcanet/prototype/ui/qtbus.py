"""Bridge the asyncio bus into Qt: a worker thread emits one signal per message."""
from __future__ import annotations

import asyncio
import threading

from PySide6 import QtCore

from src.tcanet.prototype import config
from src.tcanet.prototype.bus import BusClient


class BusBridge(QtCore.QObject):
    message = QtCore.Signal(dict)
    connected = QtCore.Signal(bool)

    def __init__(self, name: str, topics: tuple[str, ...]) -> None:
        super().__init__()
        self.name = name
        self.topics = topics
        self.publish_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def publish(self, topic: str, payload: dict) -> None:
        if self._loop is not None and self.publish_queue is not None:
            self._loop.call_soon_threadsafe(self.publish_queue.put_nowait, (topic, payload))

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.publish_queue = asyncio.Queue()
        while True:
            try:
                client = await BusClient.connect(config.bus_path(), self.name, self.topics, retries=1)
            except (FileNotFoundError, ConnectionRefusedError):
                self.connected.emit(False)
                await asyncio.sleep(1.0)
                continue
            self.connected.emit(True)
            sender = asyncio.create_task(self._send(client))
            while (msg := await client.recv()) is not None:
                self.message.emit(msg)
            sender.cancel()
            self.connected.emit(False)
            await asyncio.sleep(1.0)

    async def _send(self, client: BusClient) -> None:
        while True:
            topic, payload = await self.publish_queue.get()
            await client.publish(topic, payload)
