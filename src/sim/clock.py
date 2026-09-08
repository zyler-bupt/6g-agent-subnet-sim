from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SimClock:
    now: float = 0.0
    step_seconds: float = 1.0

    def tick(self) -> float:
        self.now += self.step_seconds
        return self.now

