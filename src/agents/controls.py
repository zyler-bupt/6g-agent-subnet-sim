from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FlowgenControl:
    """Low-level control surface for the real TCP flow generator."""

    control_file: Path
    initial_target_mbps: float = 16.0
    default_tcp_congestion: str = "cubic"
    default_ip_tos: int = 0xB8
    network_advice_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.control_file = Path(self.control_file)
        self._target_mbps = self.initial_target_mbps
        self._state: dict[str, Any] = {"target_mbps": self._target_mbps}
        self._write_state()

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def scale_target_rate(self, multiplier: float) -> dict[str, Any]:
        self._target_mbps = max(0.1, self._target_mbps * multiplier)
        self._state["target_mbps"] = self._target_mbps
        self._write_state()
        return self.state

    def tune_transport(
        self,
        *,
        send_rate_multiplier: float = 1.0,
        tcp_nodelay: bool = True,
        tcp_congestion: str | None = None,
    ) -> dict[str, Any]:
        self._target_mbps = max(0.1, self._target_mbps * send_rate_multiplier)
        self._state["target_mbps"] = self._target_mbps
        self._state["tcp_nodelay"] = tcp_nodelay
        self._state["tcp_congestion"] = tcp_congestion or self.default_tcp_congestion
        self._write_state()
        return self.state

    def prioritize_task_flow(self, task_id: str, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        advice = {
            "task_id": task_id,
            "agent_id": agent_id,
            "params": dict(params),
            "ip_tos": self.default_ip_tos,
        }
        self.network_advice_log.append(advice)
        self._state["ip_tos"] = self.default_ip_tos
        self._write_state()
        return self.state

    def _write_state(self) -> None:
        self.control_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.control_file.with_suffix(self.control_file.suffix + ".tmp")
        temp.write_text(json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.control_file)
