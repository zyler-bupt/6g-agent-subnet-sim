#!/usr/bin/env python3

import argparse
import json
import socket
import threading
import time
from pathlib import Path
from typing import Optional

TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)


def run_server(host, port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    print(f"flowgen server listening on {host}:{port}", flush=True)
    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=_drain_connection, args=(conn, addr), daemon=True)
        thread.start()


def run_client(
    host,
    port,
    target_mbps,
    duration_s,
    control_file,
    tcp_congestion,
    tcp_nodelay,
    ip_tos,
):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if tcp_congestion:
        sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, tcp_congestion.encode("ascii"))
    if tcp_nodelay:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if ip_tos is not None:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, ip_tos)
    sock.connect((host, port))

    chunk = b"x" * 16_384
    deadline = time.monotonic() + duration_s if duration_s > 0 else None
    bytes_sent = 0
    last_report = time.monotonic()
    config_mtime = 0.0
    current_mbps = target_mbps

    while deadline is None or time.monotonic() < deadline:
        if control_file is not None:
            current_mbps, config_mtime = _reload_control(
                sock,
                control_file,
                config_mtime,
                current_mbps,
            )

        start = time.monotonic()
        sock.sendall(chunk)
        bytes_sent += len(chunk)
        _pace(start, len(chunk), current_mbps)

        now = time.monotonic()
        if now - last_report >= 1.0:
            mbps = (bytes_sent * 8 / (now - last_report)) / 1_000_000
            print(json.dumps({"send_rate_mbps": mbps, "target_mbps": current_mbps}), flush=True)
            bytes_sent = 0
            last_report = now


def _drain_connection(conn, addr):
    print(f"flowgen accepted {addr[0]}:{addr[1]}", flush=True)
    with conn:
        while conn.recv(65_536):
            pass


def _pace(start, bytes_sent, target_mbps):
    if target_mbps <= 0:
        return
    target_seconds = bytes_sent * 8 / (target_mbps * 1_000_000)
    elapsed = time.monotonic() - start
    if target_seconds > elapsed:
        time.sleep(target_seconds - elapsed)


def _reload_control(
    sock,
    control_file,
    last_mtime,
    current_mbps,
):
    try:
        stat = control_file.stat()
    except FileNotFoundError:
        return current_mbps, last_mtime
    if stat.st_mtime <= last_mtime:
        return current_mbps, last_mtime

    payload = json.loads(control_file.read_text(encoding="utf-8"))
    if "tcp_congestion" in payload:
        sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, str(payload["tcp_congestion"]).encode("ascii"))
    if "tcp_nodelay" in payload:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1 if payload["tcp_nodelay"] else 0)
    if "ip_tos" in payload:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(payload["ip_tos"]))
    return float(payload.get("target_mbps", current_mbps)), stat.st_mtime


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode")

    server = subparsers.add_parser("server")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=9000)

    client = subparsers.add_parser("client")
    client.add_argument("--host", required=True)
    client.add_argument("--port", type=int, default=9000)
    client.add_argument("--target-mbps", type=float, default=10.0)
    client.add_argument("--duration-s", type=float, default=0.0, help="0 means run until interrupted")
    client.add_argument("--control-file", type=Path)
    client.add_argument("--tcp-congestion")
    client.add_argument("--tcp-nodelay", action="store_true")
    client.add_argument("--ip-tos", type=lambda value: int(value, 0))

    args = parser.parse_args()
    if args.mode == "server":
        run_server(args.host, args.port)
    elif args.mode == "client":
        run_client(
            args.host,
            args.port,
            args.target_mbps,
            args.duration_s,
            args.control_file,
            args.tcp_congestion,
            args.tcp_nodelay,
            args.ip_tos,
        )
    else:
        parser.error("mode is required: choose server or client")


if __name__ == "__main__":
    main()
