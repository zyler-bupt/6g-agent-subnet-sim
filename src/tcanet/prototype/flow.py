"""Measured task traffic: sequenced, timestamped UDP streams.

Each packet carries a session id, a sequence number and the sender's
``time.time_ns()``.  Sender and receiver share one host clock, so the sink
measures true one-way delay, loss (sequence gaps) and goodput per period —
and keeps reporting zero goodput through an outage, where a TCP-controlled
tool would abort.  Transport modes follow the TransAgent profiles: the
``reliable`` mode duplicates every fifth packet (+20 % overhead, the copy
masks a lost original); ``lightweight`` sends 5 % fewer packets.
"""
from __future__ import annotations

import asyncio
import random
import socket
import statistics
import struct
import time
from typing import Awaitable, Callable

from src.tcanet.prototype.config import PACKET_BYTES

HEADER = struct.Struct("!4sIQQB")
MAGIC = b"TCAF"
MODES = ("standard", "reliable", "lightweight")


def encode(session: int, seq: int, send_ns: int, size: int, redundant: bool = False) -> bytes:
    head = HEADER.pack(MAGIC, session, seq, send_ns, 1 if redundant else 0)
    return head + b"\x00" * max(0, size - len(head))


def decode(data: bytes) -> tuple[int, int, int] | None:
    """``(session, seq, send_ns)`` or ``None`` for a foreign datagram."""
    if len(data) < HEADER.size:
        return None
    magic, session, seq, send_ns, _flags = HEADER.unpack_from(data)
    if magic != MAGIC:
        return None
    return session, seq, send_ns


class SinkStats:
    """Per-period goodput, loss and one-way delay of one stream."""

    def __init__(self) -> None:
        self._session: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._high = -1
        self._window_start = -1
        self._seen: set[int] = set()
        self._bytes = 0
        self._unique = 0
        self._owd_ms: list[float] = []

    def add(self, session: int, seq: int, send_ns: int, recv_ns: int, nbytes: int) -> None:
        if session != self._session:
            self._session = session
            self._reset()
        if seq in self._seen:
            return
        self._seen.add(seq)
        self._unique += 1
        self._bytes += nbytes
        self._owd_ms.append((recv_ns - send_ns) / 1e6)
        if seq > self._high:
            self._high = seq

    def snapshot(self, period_s: float) -> dict:
        expected = self._high - self._window_start
        loss = None if expected <= 0 else max(0.0, min(1.0, 1.0 - self._unique / expected))
        sample = {
            "rx_mbps": self._bytes * 8 / period_s / 1e6,
            "loss": loss,
            "owd_ms": statistics.median(self._owd_ms) if self._owd_ms else None,
            "packets": self._unique,
        }
        self._window_start = self._high
        self._bytes = 0
        self._unique = 0
        self._owd_ms = []
        if len(self._seen) > 200_000:
            self._seen = {seq for seq in self._seen if seq > self._high - 10_000}
        return sample


class FlowSender:
    def __init__(
        self,
        dst_ip: str,
        port: int,
        rate_mbps: float,
        *,
        mode: str = "standard",
        packet_bytes: int = PACKET_BYTES,
        tick_s: float = 0.005,
    ) -> None:
        self.dst_ip = dst_ip
        self.port = port
        self.packet_bytes = packet_bytes
        self.tick_s = tick_s
        self.session = random.getrandbits(32)
        self.seq = 0
        self.rate_mbps = 0.0
        self.mode = "standard"
        self.set_rate(rate_mbps)
        self.set_mode(mode)

    def set_rate(self, rate_mbps: float) -> None:
        self.rate_mbps = max(0.0, float(rate_mbps))

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown transport mode {mode!r}")
        self.mode = mode

    def packets_per_second(self) -> float:
        pps = self.rate_mbps * 1e6 / (8 * self.packet_bytes)
        return pps * 0.95 if self.mode == "lightweight" else pps

    async def run(self, stop: asyncio.Event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        credit = 0.0
        last = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                pps = self.packets_per_second()
                credit = min(credit + (now - last) * pps, pps * 0.05 + 1.0)
                last = now
                while credit >= 1.0:
                    credit -= 1.0
                    self._send_one(sock)
                await asyncio.sleep(self.tick_s)
        finally:
            sock.close()

    def _send_one(self, sock: socket.socket) -> None:
        target = (self.dst_ip, self.port)
        try:
            sock.sendto(encode(self.session, self.seq, time.time_ns(), self.packet_bytes), target)
            if self.mode == "reliable" and self.seq % 5 == 0:
                sock.sendto(
                    encode(self.session, self.seq, time.time_ns(), self.packet_bytes, True),
                    target,
                )
        except OSError:
            pass  # unreachable/full buffer: the sink sees the gap as loss
        self.seq += 1


SampleCallback = Callable[[dict], Awaitable[None] | None]


class FlowSink:
    def __init__(self, port: int, on_sample: SampleCallback, *, period_s: float, bind_ip: str = "0.0.0.0") -> None:
        self.port = port
        self.on_sample = on_sample
        self.period_s = period_s
        self.bind_ip = bind_ip
        self.stats = SinkStats()

    async def run(self, stop: asyncio.Event) -> None:
        stats = self.stats

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr) -> None:
                decoded = decode(data)
                if decoded is not None:
                    session, seq, send_ns = decoded
                    stats.add(session, seq, send_ns, time.time_ns(), len(data))

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            _Protocol, local_addr=(self.bind_ip, self.port)
        )
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.period_s)
                except asyncio.TimeoutError:
                    pass
                result = self.on_sample(stats.snapshot(self.period_s))
                if asyncio.iscoroutine(result):
                    await result
        finally:
            transport.close()
