"""Publish/subscribe bus over a Unix domain socket (JSON lines).

A Unix socket lives in the filesystem, so processes in every network
namespace reach the same broker without any extra networking.  Frames:

* ``{"op": "sub", "topics": [prefix, ...]}`` — subscribe by topic prefix;
* ``{"op": "pub", "msg": {"topic", "src", "ts", "payload"}}`` — publish.

The broker forwards each message to every other client with a matching
prefix and appends it to the run's ``events.jsonl`` record.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

_LIMIT = 1 << 20


def message(topic: str, src: str, payload: dict, ts: float | None = None) -> dict:
    return {
        "topic": topic,
        "src": src,
        "ts": time.time() if ts is None else ts,
        "payload": payload,
    }


def _encode(frame: dict) -> bytes:
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode()


def _matches(topic: str, prefixes: set[str]) -> bool:
    return any(topic.startswith(prefix) for prefix in prefixes)


class Broker:
    def __init__(self, path: Path, record_path: Path | None = None) -> None:
        self.path = Path(path)
        self.record_path = record_path
        self._clients: dict[asyncio.StreamWriter, set[str]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._record = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(
            self._serve, path=str(self.path), limit=_LIMIT
        )
        os.chmod(self.path, 0o666)
        if self.record_path is not None:
            self._record = open(self.record_path, "a", encoding="utf-8")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self._record is not None:
            self._record.close()
            self._record = None
        if self.path.exists():
            self.path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients[writer] = set()
        try:
            while line := await reader.readline():
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if frame.get("op") == "sub":
                    self._clients.setdefault(writer, set()).update(frame.get("topics", []))
                elif frame.get("op") == "pub" and isinstance(frame.get("msg"), dict):
                    self._fanout(writer, frame["msg"])
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._clients.pop(writer, None)
            writer.close()

    def _fanout(self, sender: asyncio.StreamWriter, msg: dict) -> None:
        data = _encode(msg)
        if self._record is not None:
            self._record.write(data.decode())
            self._record.flush()
        topic = str(msg.get("topic", ""))
        for writer, prefixes in list(self._clients.items()):
            if writer is sender or not _matches(topic, prefixes):
                continue
            if writer.is_closing():
                self._clients.pop(writer, None)
                continue
            writer.write(data)


class BusClient:
    def __init__(self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.name = name
        self._reader = reader
        self._writer = writer
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._pump_task = asyncio.create_task(self._pump())

    @classmethod
    async def connect(
        cls,
        path: Path,
        name: str,
        topics: tuple[str, ...] = (),
        *,
        retries: int = 100,
        delay: float = 0.05,
    ) -> "BusClient":
        for attempt in range(retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(path), limit=_LIMIT)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay)
        client = cls(name, reader, writer)
        if topics:
            await client.subscribe(*topics)
        return client

    async def subscribe(self, *prefixes: str) -> None:
        await self._send({"op": "sub", "topics": list(prefixes)})

    async def publish(self, topic: str, payload: dict) -> dict:
        msg = message(topic, self.name, payload)
        await self._send({"op": "pub", "msg": msg})
        return msg

    async def _send(self, frame: dict) -> None:
        async with self._lock:
            self._writer.write(_encode(frame))
            await self._writer.drain()

    async def _pump(self) -> None:
        try:
            while line := await self._reader.readline():
                try:
                    await self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._queue.put_nowait(None)

    async def recv(self, timeout: float | None = None) -> dict | None:
        """Next message; ``None`` when the broker closed.  Raises
        ``asyncio.TimeoutError`` if ``timeout`` elapses first."""
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout)

    async def close(self) -> None:
        self._pump_task.cancel()
        self._writer.close()


async def serve(path: Path, record_path: Path | None) -> None:
    """Run a broker until SIGTERM/SIGINT."""
    broker = Broker(path, record_path)
    await broker.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await broker.stop()


def main() -> None:
    from src.tcanet.prototype import config

    config.run_dir().mkdir(parents=True, exist_ok=True)
    asyncio.run(serve(config.bus_path(), config.events_path()))


if __name__ == "__main__":
    main()
