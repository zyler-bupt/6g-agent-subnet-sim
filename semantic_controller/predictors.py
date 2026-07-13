from __future__ import annotations

from dataclasses import dataclass

from src.agents.forecast import forecast_history

from semantic_controller.schemas import AgentID, PredictionResult


def forecast_values(history: list[float], horizon: int, method: str = "trend") -> list[float]:
    if method != "trend":
        return list(forecast_history(history, horizon, method=method))
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if not history:
        raise ValueError("history must not be empty")
    if len(history) == 1:
        return [max(0.0, history[0]) for _ in range(horizon)]

    recent = history[-5:]
    base = sum(recent) / len(recent)
    trend = (recent[-1] - recent[0]) / max(len(recent) - 1, 1)
    return [max(0.0, base + trend * (idx + 1)) for idx in range(horizon)]


@dataclass
class ApplicationPredictor:
    default_horizon: int = 5
    forecast_method: str = "adaptive"

    def predict(self, history_mbps: list[float], horizon: int | None = None) -> PredictionResult:
        resolved_horizon = horizon or self.default_horizon
        return PredictionResult(
            agent_id=AgentID.APPLICATION_AGENT,
            metric="app_rate_mbps",
            horizon=resolved_horizon,
            values=forecast_values(
                history_mbps,
                resolved_horizon,
                method=self.forecast_method,
            ),
        )


@dataclass
class NetworkPredictor:
    default_horizon: int = 5
    forecast_method: str = "adaptive"

    def predict(self, history_mbps: list[float], horizon: int | None = None) -> PredictionResult:
        resolved_horizon = horizon or self.default_horizon
        return PredictionResult(
            agent_id=AgentID.NETWORK_AGENT,
            metric="network_bandwidth_mbps",
            horizon=resolved_horizon,
            values=forecast_values(
                history_mbps,
                resolved_horizon,
                method=self.forecast_method,
            ),
        )
