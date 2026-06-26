from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PingStats:
    loss_rate: float = 0.0
    rtt_min_ms: float = 0.0
    rtt_avg_ms: float = 0.0
    rtt_max_ms: float = 0.0
    jitter_ms: float = 0.0


@dataclass(frozen=True)
class IperfStats:
    throughput_mbps: float = 0.0
    sender_mbps: float = 0.0
    receiver_mbps: float = 0.0
    retransmits: int = 0


@dataclass(frozen=True)
class SsTcpInfo:
    srtt_ms: float = 0.0
    rtt_var_ms: float = 0.0
    rto_ms: float = 0.0
    snd_cwnd: float = 0.0
    delivery_rate_mbps: float = 0.0
    retransmission_rate: float = 0.0


def parse_ping(output: str) -> PingStats:
    packet_match = re.search(
        r"(?P<tx>\d+)\s+packets transmitted,\s+(?P<rx>\d+)\s+(?:packets )?received,.*?"
        r"(?P<loss>[\d.]+)%\s+packet loss",
        output,
        flags=re.DOTALL,
    )
    if packet_match is None:
        raise ValueError("ping output does not contain packet statistics")

    rtt_match = re.search(
        r"(?:rtt|round-trip).*?=\s*"
        r"(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<mdev>[\d.]+)\s*ms",
        output,
    )
    if rtt_match is None:
        return PingStats(loss_rate=float(packet_match.group("loss")) / 100.0)

    return PingStats(
        loss_rate=float(packet_match.group("loss")) / 100.0,
        rtt_min_ms=float(rtt_match.group("min")),
        rtt_avg_ms=float(rtt_match.group("avg")),
        rtt_max_ms=float(rtt_match.group("max")),
        jitter_ms=float(rtt_match.group("mdev")),
    )


def parse_iperf3_json(output: str) -> IperfStats:
    payload = json.loads(output)
    end = payload.get("end", {})
    sent = end.get("sum_sent", {})
    received = end.get("sum_received", {})

    sender_mbps = _bits_to_mbps(float(sent.get("bits_per_second", 0.0)))
    receiver_mbps = _bits_to_mbps(float(received.get("bits_per_second", 0.0)))
    retransmits = int(sent.get("retransmits", 0) or 0)
    throughput = receiver_mbps or sender_mbps
    return IperfStats(
        throughput_mbps=throughput,
        sender_mbps=sender_mbps,
        receiver_mbps=receiver_mbps,
        retransmits=retransmits,
    )


def parse_ss_ti(output: str) -> SsTcpInfo:
    rtt = _pair_after(output, "rtt")
    rto = _number_after(output, "rto")
    cwnd = _number_after(output, "cwnd")
    delivery_rate = _rate_after(output, "delivery_rate")
    retrans_rate = _retransmission_rate(output)
    return SsTcpInfo(
        srtt_ms=rtt[0],
        rtt_var_ms=rtt[1],
        rto_ms=rto,
        snd_cwnd=cwnd,
        delivery_rate_mbps=delivery_rate,
        retransmission_rate=retrans_rate,
    )


def _bits_to_mbps(bits_per_second: float) -> float:
    return bits_per_second / 1_000_000.0


def _number_after(text: str, key: str) -> float:
    match = re.search(rf"\b{re.escape(key)}:(?P<value>[\d.]+)", text)
    return float(match.group("value")) if match else 0.0


def _pair_after(text: str, key: str) -> tuple[float, float]:
    match = re.search(rf"\b{re.escape(key)}:(?P<a>[\d.]+)/(?P<b>[\d.]+)", text)
    if match is None:
        return 0.0, 0.0
    return float(match.group("a")), float(match.group("b"))


def _rate_after(text: str, key: str) -> float:
    match = re.search(rf"\b{re.escape(key)}\s+(?P<value>[\d.]+)(?P<unit>[KMG]?bps)", text)
    if match is None:
        return 0.0
    value = float(match.group("value"))
    unit = match.group("unit")
    if unit == "Kbps":
        return value / 1000.0
    if unit == "Mbps":
        return value
    if unit == "Gbps":
        return value * 1000.0
    return value / 1_000_000.0


def _retransmission_rate(text: str) -> float:
    bytes_sent = _number_after(text, "bytes_sent")
    bytes_retrans = _number_after(text, "bytes_retrans")
    if bytes_sent > 0:
        return min(1.0, bytes_retrans / bytes_sent)

    match = re.search(r"\bretrans:(?P<current>\d+)/(?P<total>\d+)", text)
    if match is None:
        return 0.0
    total = int(match.group("total"))
    if total <= 0:
        return 0.0
    return min(1.0, int(match.group("current")) / total)
