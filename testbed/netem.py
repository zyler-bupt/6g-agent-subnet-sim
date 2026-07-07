#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass


def tc_command(namespace: str, *args: str, sudo: bool = False) -> list[str]:
    command = ["ip", "netns", "exec", namespace, "tc", *args]
    if sudo:
        return ["sudo", *command]
    return command


def run_command(command: list[str], *, dry_run: bool = False) -> subprocess.CompletedProcess[str] | None:
    if dry_run:
        print(" ".join(command))
        return None
    return subprocess.run(command, check=True, capture_output=True, text=True)


@dataclass(frozen=True)
class NetemController:
    namespace: str = "h-router"
    dev: str = "rt-cloud0"
    sudo: bool = False
    dry_run: bool = False

    def replace(
        self,
        *,
        delay_ms: float | None = None,
        loss_percent: float | None = None,
        jitter_ms: float | None = None,
        rate_mbps: float | None = None,
    ) -> list[list[str]]:
        commands = self.commands_for_replace(
            delay_ms=delay_ms,
            loss_percent=loss_percent,
            jitter_ms=jitter_ms,
            rate_mbps=rate_mbps,
        )
        self._run(commands)
        return commands

    def commands_for_replace(
        self,
        *,
        delay_ms: float | None = None,
        loss_percent: float | None = None,
        jitter_ms: float | None = None,
        rate_mbps: float | None = None,
    ) -> list[list[str]]:
        netem = ["netem"]
        if delay_ms is not None:
            if jitter_ms is not None:
                netem.extend(["delay", f"{delay_ms:g}ms", f"{jitter_ms:g}ms"])
            else:
                netem.extend(["delay", f"{delay_ms:g}ms"])
        if loss_percent is not None:
            netem.extend(["loss", f"{loss_percent:g}%"])
        if rate_mbps is not None:
            netem.extend(["rate", f"{rate_mbps:g}mbit"])
        return [tc_command(self.namespace, "qdisc", "replace", "dev", self.dev, "root", *netem, sudo=self.sudo)]

    def clear(self) -> list[list[str]]:
        commands = [tc_command(self.namespace, "qdisc", "del", "dev", self.dev, "root", sudo=self.sudo)]
        self._run(commands)
        return commands

    def _run(self, commands: list[list[str]]) -> None:
        for command in commands:
            run_command(command, dry_run=self.dry_run)


@dataclass(frozen=True)
class HtbController:
    namespace: str = "h-router"
    dev: str = "rt-cloud0"
    total_mbps: float = 100.0
    priority_mbps: float = 80.0
    default_mbps: float = 20.0
    sudo: bool = False
    dry_run: bool = False

    def prioritize_task_flow(self, ip_tos: int) -> list[list[str]]:
        commands = self.commands_for_prioritize(ip_tos)
        for command in commands:
            run_command(command, dry_run=self.dry_run)
        return commands

    def clear(self) -> list[list[str]]:
        commands = [tc_command(self.namespace, "qdisc", "del", "dev", self.dev, "root", sudo=self.sudo)]
        for command in commands:
            run_command(command, dry_run=self.dry_run)
        return commands

    def commands_for_prioritize(self, ip_tos: int) -> list[list[str]]:
        tos = ip_tos & 0xFC
        total = _rate(self.total_mbps)
        priority = _rate(self.priority_mbps)
        default = _rate(self.default_mbps)
        return [
            tc_command(
                self.namespace,
                "qdisc",
                "replace",
                "dev",
                self.dev,
                "root",
                "handle",
                "1:",
                "htb",
                "default",
                "20",
                sudo=self.sudo,
            ),
            tc_command(
                self.namespace,
                "class",
                "replace",
                "dev",
                self.dev,
                "parent",
                "1:",
                "classid",
                "1:1",
                "htb",
                "rate",
                total,
                "ceil",
                total,
                sudo=self.sudo,
            ),
            tc_command(
                self.namespace,
                "class",
                "replace",
                "dev",
                self.dev,
                "parent",
                "1:1",
                "classid",
                "1:10",
                "htb",
                "rate",
                priority,
                "ceil",
                total,
                "prio",
                "0",
                sudo=self.sudo,
            ),
            tc_command(
                self.namespace,
                "class",
                "replace",
                "dev",
                self.dev,
                "parent",
                "1:1",
                "classid",
                "1:20",
                "htb",
                "rate",
                default,
                "ceil",
                total,
                "prio",
                "1",
                sudo=self.sudo,
            ),
            tc_command(
                self.namespace,
                "filter",
                "replace",
                "dev",
                self.dev,
                "protocol",
                "ip",
                "parent",
                "1:",
                "prio",
                "1",
                "u32",
                "match",
                "ip",
                "tos",
                f"0x{tos:02x}",
                "0xfc",
                "flowid",
                "1:10",
                sudo=self.sudo,
            ),
        ]


def _rate(mbps: float) -> str:
    return f"{mbps:g}mbit"


def main() -> None:
    parser = argparse.ArgumentParser(description="Control tc netem/htb in the real netns testbed.")
    parser.add_argument("--namespace", default="h-router")
    parser.add_argument("--dev", default="rt-cloud0")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    netem = subparsers.add_parser("netem")
    netem.add_argument("--delay-ms", type=float)
    netem.add_argument("--loss-percent", type=float)
    netem.add_argument("--jitter-ms", type=float)
    netem.add_argument("--rate-mbps", type=float)

    subparsers.add_parser("clear")

    htb = subparsers.add_parser("htb")
    htb.add_argument("--ip-tos", type=lambda value: int(value, 0), default=0xB8)
    htb.add_argument("--total-mbps", type=float, default=100.0)
    htb.add_argument("--priority-mbps", type=float, default=80.0)
    htb.add_argument("--default-mbps", type=float, default=20.0)

    args = parser.parse_args()
    if args.mode == "netem":
        NetemController(args.namespace, args.dev, args.sudo, args.dry_run).replace(
            delay_ms=args.delay_ms,
            loss_percent=args.loss_percent,
            jitter_ms=args.jitter_ms,
            rate_mbps=args.rate_mbps,
        )
    elif args.mode == "clear":
        NetemController(args.namespace, args.dev, args.sudo, args.dry_run).clear()
    elif args.mode == "htb":
        HtbController(
            namespace=args.namespace,
            dev=args.dev,
            total_mbps=args.total_mbps,
            priority_mbps=args.priority_mbps,
            default_mbps=args.default_mbps,
            sudo=args.sudo,
            dry_run=args.dry_run,
        ).prioritize_task_flow(args.ip_tos)


if __name__ == "__main__":
    main()
