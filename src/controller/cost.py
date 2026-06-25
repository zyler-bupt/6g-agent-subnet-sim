from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CostModel:
    """Transparent, shared cost model for run-time adjustments.

    Both the minimal elastic adjustment and the full-rebuild baseline use this
    same model. The service interruption of each method therefore differs only
    because of how many operations the method *actually performs*, not because
    of any hand-picked constant. This keeps the "minimal vs full-rebuild"
    comparison honest and reproducible.

    Cost rationale:
    - local_tune: an Agent retunes its own parameters (bitrate, transport
      profile). The data plane stays up, only a brief reconfiguration per Agent.
    - session_setup: re-establishing an end-to-end session briefly interrupts
      that flow.
    - gateway_install: a gateway reloads its forwarding/whitelist rules.
    - member_confirm: a member is re-confirmed from scratch (only on rebuild).
    - agent_replace: a support Agent is swapped in (reserved for tier-2 of the
      minimal adjustment, implemented in a later item).
    """

    member_confirm_ms: float = 6.0
    gateway_install_ms: float = 12.0
    session_setup_ms: float = 9.0
    local_tune_ms: float = 4.0
    agent_replace_ms: float = 15.0

    def interruption_ms(self, operations: Mapping[str, int]) -> float:
        unit = {
            "member_confirm": self.member_confirm_ms,
            "gateway_install": self.gateway_install_ms,
            "session_setup": self.session_setup_ms,
            "local_tune": self.local_tune_ms,
            "agent_replace": self.agent_replace_ms,
        }
        return sum(unit[name] * count for name, count in operations.items() if count)
