from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from src.core.models import SessionSpec, TaskSubnet
from src.e2e.models import EdgeVerifyResult, VerifyResult
from src.metrics.parsers import parse_ping, parse_ss_ti


@dataclass(frozen=True)
class NetnsFlowTarget:
    session_id: str
    source_namespace: str
    target_namespace: str
    target_ip: str
    port: int
    target_mbps: float


@dataclass
class NetnsFlowgenManager:
    gateway_namespaces: dict[str, str]
    gateway_ips: dict[str, str]
    sudo: bool = False
    port_base: int = 9400
    startup_s: float = 0.5
    rate_headroom: float = 1.2
    flowgen_script: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "testbed" / "flowgen.py"
    )
    _processes: list[subprocess.Popen] = field(default_factory=list, init=False)
    targets: dict[str, NetnsFlowTarget] = field(default_factory=dict, init=False)

    async def start(self, subnet: TaskSubnet) -> dict[str, NetnsFlowTarget]:
        if self._processes:
            raise RuntimeError("flowgen manager is already running")
        self.targets = self._targets_for(subnet)
        try:
            for target in self.targets.values():
                self._processes.append(
                    self._popen(
                        target.target_namespace,
                        "server",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        str(target.port),
                    )
                )
            await asyncio.sleep(self.startup_s)
            self._raise_if_exited("flowgen server")

            for target in self.targets.values():
                self._processes.append(
                    self._popen(
                        target.source_namespace,
                        "client",
                        "--host",
                        target.target_ip,
                        "--port",
                        str(target.port),
                        "--target-mbps",
                        f"{target.target_mbps:g}",
                        "--duration-s",
                        "0",
                        "--tcp-nodelay",
                    )
                )
            await asyncio.sleep(self.startup_s)
            self._raise_if_exited("flowgen client")
            return dict(self.targets)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        processes = list(reversed(self._processes))
        self._processes.clear()
        for process in processes:
            if process.poll() is None:
                try:
                    self._signal_process_group(process, signal.SIGTERM)
                except (OSError, RuntimeError):
                    pass
        if processes:
            await asyncio.sleep(0.1)
        for process in processes:
            if process.poll() is None:
                try:
                    self._signal_process_group(process, signal.SIGKILL)
                except (OSError, RuntimeError):
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            if process.stderr is not None:
                process.stderr.close()

    def _signal_process_group(
        self,
        process: subprocess.Popen,
        sig: signal.Signals,
    ) -> None:
        if not self.sudo:
            os.killpg(process.pid, sig)
            return
        completed = subprocess.run(
            [
                "sudo",
                "-n",
                "/bin/kill",
                f"-{sig.name.removeprefix('SIG')}",
                "--",
                f"-{process.pid}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0 and process.poll() is None:
            raise RuntimeError(
                f"failed to signal flowgen process group {process.pid}: "
                f"{completed.stderr.strip()}"
            )

    def _targets_for(self, subnet: TaskSubnet) -> dict[str, NetnsFlowTarget]:
        targets: dict[str, NetnsFlowTarget] = {}
        for index, session in enumerate(subnet.sessions, start=1):
            try:
                source_namespace = self.gateway_namespaces[session.source_gateway]
                target_namespace = self.gateway_namespaces[session.target_gateway]
                target_ip = self.gateway_ips[session.target_gateway]
            except KeyError as error:
                raise ValueError(f"missing flowgen gateway mapping: {error.args[0]}") from error
            targets[session.session_id] = NetnsFlowTarget(
                session_id=session.session_id,
                source_namespace=source_namespace,
                target_namespace=target_namespace,
                target_ip=target_ip,
                port=self.port_base + index,
                target_mbps=session.data_rate_mbps * self.rate_headroom,
            )
        return targets

    def _popen(self, namespace: str, *args: str) -> subprocess.Popen:
        command = [
            "ip",
            "netns",
            "exec",
            namespace,
            "python3",
            str(self.flowgen_script),
            *args,
        ]
        if self.sudo:
            command[0:0] = ["sudo", "-n"]
        return subprocess.Popen(
            command,
            # Keep the parent's controlling TTY so sudo can use the ticket
            # established by `sudo -v`, while a separate process group still
            # lets stop() terminate the complete sudo/ip/python process tree.
            stdin=None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )

    def _raise_if_exited(self, label: str) -> None:
        failures: list[str] = []
        for process in self._processes:
            if process.poll() is None:
                continue
            stderr = ""
            if process.stderr is not None:
                stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
            command = " ".join(str(item) for item in process.args)
            failures.append(
                f"exit={process.returncode}; command={command}"
                + (f"; stderr={stderr}" if stderr else "")
            )
        if failures:
            raise RuntimeError(f"{label} exited during startup: " + " | ".join(failures))


@dataclass
class NetnsFlowHealthVerifier:
    targets: dict[str, NetnsFlowTarget]
    sudo: bool = False
    ping_timeout_s: float = 1.0
    command_timeout_s: float = 3.0
    mode: str = "netns-flowgen"

    async def verify(self, subnet: TaskSubnet) -> VerifyResult:
        started = perf_counter()
        edge_results = tuple(
            await asyncio.gather(
                *(self._verify_session(subnet, session) for session in subnet.sessions)
            )
        )
        elapsed_ms = (perf_counter() - started) * 1000.0
        passed = sum(1 for item in edge_results if item.ok)
        return VerifyResult(
            ok=passed == len(edge_results) and bool(edge_results),
            verify_ms=elapsed_ms,
            checked_edges=len(edge_results),
            passed_edges=passed,
            edge_results=edge_results,
            mode=self.mode,
        )

    async def _verify_session(
        self,
        subnet: TaskSubnet,
        session: SessionSpec,
    ) -> EdgeVerifyResult:
        target = self.targets[session.session_id]
        try:
            ping_output, ss_output = await asyncio.gather(
                self._run(
                    target.source_namespace,
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    f"{self.ping_timeout_s:g}",
                    target.target_ip,
                    True,
                ),
                self._run(
                    target.source_namespace,
                    "ss",
                    "-tin",
                    "dst",
                    target.target_ip,
                    False,
                ),
            )
            ping = parse_ping(ping_output)
            tcp = parse_ss_ti(ss_output)
            available = tcp.delivery_rate_mbps
            throughput = min(target.target_mbps, available) if available > 0 else 0.0
            latency = ping.rtt_avg_ms or tcp.srtt_ms
            loss = max(ping.loss_rate, tcp.retransmission_rate)
            reachable = ping.loss_rate < 1.0 and throughput > 0.0
            ok = (
                reachable
                and latency <= session.latency_budget_ms
                and loss <= subnet.task.qos.max_loss_rate
                and available >= session.data_rate_mbps
                and throughput >= session.data_rate_mbps
            )
            return EdgeVerifyResult(
                session_id=session.session_id,
                source=session.source,
                target=session.target,
                ok=ok,
                reachable=reachable,
                latency_ms=latency,
                max_latency_ms=session.latency_budget_ms,
                loss_rate=loss,
                max_loss_rate=subnet.task.qos.max_loss_rate,
                available_bandwidth_mbps=available,
                min_bandwidth_mbps=session.data_rate_mbps,
                throughput_mbps=throughput,
            )
        except Exception as error:
            return EdgeVerifyResult(
                session_id=session.session_id,
                source=session.source,
                target=session.target,
                ok=False,
                reachable=False,
                max_latency_ms=session.latency_budget_ms,
                max_loss_rate=subnet.task.qos.max_loss_rate,
                min_bandwidth_mbps=session.data_rate_mbps,
                error=str(error),
            )

    async def _run(
        self,
        namespace: str,
        command: str,
        *args: str | bool,
    ) -> str:
        allow_failure = bool(args[-1])
        command_args = [str(item) for item in args[:-1]]
        full_command = ["ip", "netns", "exec", namespace, command, *command_args]
        if self.sudo:
            full_command.insert(0, "sudo")
        process = await asyncio.create_subprocess_exec(
            *full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout_s,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("command timed out: " + " ".join(full_command))
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0 and not allow_failure:
            raise RuntimeError(
                "command failed: "
                + " ".join(full_command)
                + f"; exit={process.returncode}; stderr={stderr_text.strip()}"
            )
        return stdout_text


def rescue_flow_gateway_namespaces(
    *,
    term_namespace: str = "h-term",
    edge_namespace: str = "h-edge",
    cloud_namespace: str = "h-cloud",
) -> dict[str, str]:
    return {
        "gw-ue": term_namespace,
        "gw-mec": edge_namespace,
        "gw-cloud": cloud_namespace,
    }


def rescue_flow_gateway_ips(
    *,
    term_ip: str = "10.10.1.2",
    edge_ip: str = "10.10.2.2",
    cloud_ip: str = "10.10.3.2",
) -> dict[str, str]:
    return {
        "gw-ue": term_ip,
        "gw-mec": edge_ip,
        "gw-cloud": cloud_ip,
    }
