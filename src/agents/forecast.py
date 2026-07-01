from __future__ import annotations


def exponential_forecast(history: list[float], horizon: int, alpha: float = 0.55) -> tuple[float, ...]:
    """Forecast the next horizon values from a recent history window.

    The current real-testbed experiments use the latest 10 samples and request a
    5-step horizon. The function stays generic so the offline simulations can keep
    their existing shorter windows.
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if not history:
        raise ValueError("history must not be empty")
    estimate = history[0]
    for value in history[1:]:
        estimate = alpha * value + (1.0 - alpha) * estimate
    if len(history) > 1:
        trend = (history[-1] - history[max(0, len(history) - 4)]) / min(3, len(history) - 1)
    else:
        trend = 0.0
    return tuple(max(0.0, estimate + trend * (idx + 1)) for idx in range(horizon))
