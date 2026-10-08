"""Process table and agent launch specs for the prototype."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.tcanet.prototype.addressing import AddressPlan, endpoint_ns, gateway_ns
from src.tcanet.spec import World

SUPPORT_ROLES = ("transport", "network", "physical")


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    gateway: str
    ns: str


def agent_specs(world: World, plan: AddressPlan) -> list[AgentSpec]:
    specs = [
        AgentSpec(f"app-{agent_id}", "application", endpoint.gateway_id, endpoint_ns(agent_id))
        for agent_id, endpoint in sorted(plan.endpoints.items())
    ]
    specs += [
        AgentSpec(f"{role}-{gateway}", role, gateway, gateway_ns(gateway))
        for gateway in world.graph.gateways
        for role in SUPPORT_ROLES
    ]
    return specs


def agent_argv(spec: AgentSpec, *, sim: bool, scenario: str, python: str = sys.executable) -> list[str]:
    argv = [python, "-m", "src.tcanet.prototype.agents", "--role", spec.role,
            "--agent-id", spec.agent_id, "--gateway", spec.gateway, "--scenario", scenario]
    if sim:
        return argv + ["--sim"]
    return ["ip", "netns", "exec", spec.ns, *argv]


class ProcessTable:
    """Named background processes tracked through ``<dir>/<name>.pid``."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _pidfile(self, name: str) -> Path:
        return self.directory / f"{name}.pid"

    def start(self, name: str, argv: list[str], *, log_path: Path, cwd: Path | None = None,
              env: dict[str, str] | None = None) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, cwd=cwd,
                                    env=env, start_new_session=True)
        self._pidfile(name).write_text(str(proc.pid))
        return proc.pid

    def pid(self, name: str) -> int | None:
        try:
            return int(self._pidfile(name).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def alive(self, name: str) -> bool:
        pid = self.pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return not _zombie(pid)

    def kill(self, name: str, sig: int = signal.SIGKILL) -> bool:
        pid = self.pid(name)
        if pid is None:
            return False
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.pid"))

    def stop_all(self) -> None:
        for name in self.names():
            self.kill(name, signal.SIGTERM)
            self._pidfile(name).unlink(missing_ok=True)


def _zombie(pid: int) -> bool:
    """A killed process nobody reaped yet (e.g. under a container's PID 1)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False  # no procfs (macOS) or already gone
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"
