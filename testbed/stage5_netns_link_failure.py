#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


NAMESPACES = (
    "s5-agent-src",
    "s5-gw1",
    "s5-gw2",
    "s5-gw3",
    "s5-gw4",
    "s5-agent-dst",
)


class NetnsCase:
    def __init__(self, output_dir: Path, probe_interval_ms: float, healthy_windows: int) -> None:
        self.output_dir = output_dir
        self.probe_interval_ms = probe_interval_ms
        self.healthy_windows = healthy_windows
        self.commands: list[dict[str, Any]] = []
        self.probes: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        self._preflight()
        self.cleanup()
        try:
            self._setup()
            before_route = self._run(
                "route_before", "ip", "netns", "exec", "s5-gw1", "ip", "route", "get", "10.55.6.2"
            ).stdout.strip()
            self._require_probe("BEFORE_FAILURE", expected=True)
            throughput_before = self._iperf("BEFORE_FAILURE")

            # Timestamp is captured immediately before the kernel link-state
            # mutation, not after an injection helper returns.
            fault_effective_at = time.monotonic()
            self._run(
                "link_failure",
                "ip",
                "netns",
                "exec",
                "s5-gw1",
                "ip",
                "link",
                "set",
                "s5g12",
                "down",
            )
            consecutive_failures = 0
            detection_at = 0.0
            while consecutive_failures < 3:
                healthy = self._probe("DETECTION")
                consecutive_failures = 0 if healthy else consecutive_failures + 1
                if consecutive_failures < 3:
                    time.sleep(self.probe_interval_ms / 1000.0)
            detection_at = time.monotonic()

            self._run(
                "install_forward_backup",
                "ip",
                "netns",
                "exec",
                "s5-gw1",
                "ip",
                "route",
                "replace",
                "10.55.6.0/30",
                "via",
                "10.55.4.2",
                "dev",
                "s5g13",
            )
            self._run(
                "install_reverse_backup",
                "ip",
                "netns",
                "exec",
                "s5-gw4",
                "ip",
                "route",
                "replace",
                "10.55.1.0/30",
                "via",
                "10.55.5.1",
                "dev",
                "s5g43",
            )
            route_install_at = time.monotonic()
            first_successful_probe_at = 0.0
            healthy = 0
            while healthy < self.healthy_windows:
                ok = self._probe("RECOVERY")
                if ok and first_successful_probe_at == 0.0:
                    first_successful_probe_at = time.monotonic()
                healthy = healthy + 1 if ok else 0
                if healthy < self.healthy_windows:
                    time.sleep(self.probe_interval_ms / 1000.0)
            stable_recovery_at = time.monotonic()
            after_route = self._run(
                "route_after", "ip", "netns", "exec", "s5-gw1", "ip", "route", "get", "10.55.6.2"
            ).stdout.strip()
            throughput_after = self._iperf("AFTER_RECOVERY")
            result = {
                "status": "PASS",
                "result_mode": "real_linux_netns_mechanism_validation",
                "transaction_atomicity": "not_claimed",
                "fault": "Link Failure -> Route Switching -> QoS Recovery",
                "primary_path": ["s5-gw1", "s5-gw2", "s5-gw4"],
                "backup_path": ["s5-gw1", "s5-gw3", "s5-gw4"],
                "fault_effective_at": fault_effective_at,
                "detection_at": detection_at,
                "route_install_at": route_install_at,
                "first_successful_probe_at": first_successful_probe_at,
                "stable_recovery_at": stable_recovery_at,
                "detection_latency_ms": (detection_at - fault_effective_at) * 1000.0,
                "repair_latency_ms": (stable_recovery_at - detection_at) * 1000.0,
                "recovery_latency_ms": (stable_recovery_at - fault_effective_at) * 1000.0,
                "route_before": before_route,
                "route_after": after_route,
                "throughput_before_mbps": throughput_before,
                "throughput_after_mbps": throughput_after,
                "probe_interval_ms": self.probe_interval_ms,
                "stable_health_windows": self.healthy_windows,
            }
            self._write(result)
            return result
        finally:
            self.cleanup()

    def _preflight(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError("netns mechanism validation requires root/CAP_NET_ADMIN")
        for command in ("ip", "ping", "iperf3"):
            if shutil.which(command) is None:
                raise FileNotFoundError(f"required command is unavailable: {command}")
        self._run("netns_preflight", "ip", "netns", "list")

    def _setup(self) -> None:
        for namespace in NAMESPACES:
            self._run("namespace_add", "ip", "netns", "add", namespace)
            self._run("loopback_up", "ip", "-n", namespace, "link", "set", "lo", "up")
        links = (
            ("s5-agent-src", "s5src", "10.55.1.2/30", "s5-gw1", "s5g1s", "10.55.1.1/30"),
            ("s5-gw1", "s5g12", "10.55.2.1/30", "s5-gw2", "s5g21", "10.55.2.2/30"),
            ("s5-gw2", "s5g24", "10.55.3.1/30", "s5-gw4", "s5g42", "10.55.3.2/30"),
            ("s5-gw1", "s5g13", "10.55.4.1/30", "s5-gw3", "s5g31", "10.55.4.2/30"),
            ("s5-gw3", "s5g34", "10.55.5.1/30", "s5-gw4", "s5g43", "10.55.5.2/30"),
            ("s5-gw4", "s5g4d", "10.55.6.1/30", "s5-agent-dst", "s5dst", "10.55.6.2/30"),
        )
        for left_ns, left_dev, left_ip, right_ns, right_dev, right_ip in links:
            self._run("veth_add", "ip", "link", "add", left_dev, "type", "veth", "peer", "name", right_dev)
            self._run("veth_move", "ip", "link", "set", left_dev, "netns", left_ns)
            self._run("veth_move", "ip", "link", "set", right_dev, "netns", right_ns)
            for namespace, device, address in (
                (left_ns, left_dev, left_ip),
                (right_ns, right_dev, right_ip),
            ):
                self._run("address_add", "ip", "-n", namespace, "addr", "add", address, "dev", device)
                self._run("link_up", "ip", "-n", namespace, "link", "set", device, "up")
        for gateway in ("s5-gw1", "s5-gw2", "s5-gw3", "s5-gw4"):
            self._run(
                "forwarding_enable",
                "ip",
                "netns",
                "exec",
                gateway,
                "sysctl",
                "-q",
                "-w",
                "net.ipv4.ip_forward=1",
            )
        routes = (
            ("s5-agent-src", "default", "10.55.1.1", "s5src"),
            ("s5-agent-dst", "default", "10.55.6.1", "s5dst"),
            ("s5-gw1", "10.55.6.0/30", "10.55.2.2", "s5g12"),
            ("s5-gw2", "10.55.6.0/30", "10.55.3.2", "s5g24"),
            ("s5-gw2", "10.55.1.0/30", "10.55.2.1", "s5g21"),
            ("s5-gw4", "10.55.1.0/30", "10.55.3.1", "s5g42"),
            ("s5-gw3", "10.55.6.0/30", "10.55.5.2", "s5g34"),
            ("s5-gw3", "10.55.1.0/30", "10.55.4.1", "s5g31"),
        )
        for namespace, destination, gateway, device in routes:
            self._run(
                "route_add",
                "ip",
                "-n",
                namespace,
                "route",
                "replace",
                destination,
                "via",
                gateway,
                "dev",
                device,
            )

    def _probe(self, phase: str) -> bool:
        started = time.monotonic()
        result = self._run(
            "ping_probe",
            "ip",
            "netns",
            "exec",
            "s5-agent-src",
            "ping",
            "-n",
            "-c",
            "1",
            "-W",
            "0.2",
            "10.55.6.2",
            check=False,
        )
        healthy = result.returncode == 0
        self.probes.append(
            {
                "timestamp": started,
                "phase": phase,
                "reachable": healthy,
                "returncode": result.returncode,
                "elapsed_ms": (time.monotonic() - started) * 1000.0,
            }
        )
        return healthy

    def _require_probe(self, phase: str, expected: bool) -> None:
        actual = self._probe(phase)
        if actual != expected:
            raise RuntimeError(f"probe {phase} expected reachable={expected}, observed={actual}")

    def _iperf(self, phase: str) -> float:
        server = subprocess.Popen(
            ["ip", "netns", "exec", "s5-agent-dst", "iperf3", "-s", "-1", "-J"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.05)
        try:
            result = self._run(
                f"iperf_{phase.lower()}",
                "ip",
                "netns",
                "exec",
                "s5-agent-src",
                "iperf3",
                "-c",
                "10.55.6.2",
                "-t",
                "1",
                "-J",
            )
            payload = json.loads(result.stdout)
            bits = float(payload["end"]["sum_received"]["bits_per_second"])
            return bits / 1_000_000.0
        finally:
            if server.poll() is None:
                server.terminate()
            server.communicate(timeout=2)

    def _run(self, label: str, *command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        self.commands.append(
            {
                "timestamp": started,
                "label": label,
                "command": list(command),
                "returncode": result.returncode,
                "elapsed_ms": (time.monotonic() - started) * 1000.0,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed ({label}, rc={result.returncode}): {' '.join(command)}: {result.stderr.strip()}"
            )
        return result

    def cleanup(self) -> None:
        if shutil.which("ip") is None:
            return
        listing = subprocess.run(["ip", "netns", "list"], text=True, capture_output=True)
        existing = {line.split()[0] for line in listing.stdout.splitlines() if line.strip()}
        for namespace in NAMESPACES:
            if namespace in existing:
                subprocess.run(["ip", "netns", "del", namespace], check=False)

    def _write(self, result: dict[str, Any]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        result_path = self.output_dir / "link_failure_case.json"
        command_path = self.output_dir / "commands.jsonl"
        probe_path = self.output_dir / "probe_samples.csv"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with command_path.open("w", encoding="utf-8", newline="\n") as handle:
            for item in self.commands:
                handle.write(json.dumps(item, sort_keys=True) + "\n")
        with probe_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.probes[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(self.probes)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            for path in (result_path, command_path, probe_path):
                os.chown(path, int(sudo_uid), int(sudo_gid))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real Linux netns Stage-5 link-failure mechanism validation")
    parser.add_argument("--output-dir", default="results/exp4/netns")
    parser.add_argument("--probe-interval-ms", type=float, default=50.0)
    parser.add_argument("--healthy-windows", type=int, default=3)
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = NetnsCase(
        Path(args.output_dir),
        probe_interval_ms=args.probe_interval_ms,
        healthy_windows=args.healthy_windows,
    ).run()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
