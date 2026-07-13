from __future__ import annotations

from statistics import fmean


SUPPORTED_FORECAST_METHODS = ("ewma", "holt", "kalman", "adaptive")


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


def forecast_history(
    history: list[float],
    horizon: int,
    *,
    method: str = "adaptive",
) -> tuple[float, ...]:
    """Forecast with a small-data method suitable for online network metrics."""
    resolved = method.lower().strip()
    if resolved == "ewma":
        return exponential_forecast(history, horizon)
    if resolved == "holt":
        return holt_forecast(history, horizon)
    if resolved == "kalman":
        return kalman_forecast(history, horizon)
    if resolved == "adaptive":
        return adaptive_forecast(history, horizon)
    raise ValueError(
        f"unsupported forecast method {method!r}; "
        f"expected one of {', '.join(SUPPORTED_FORECAST_METHODS)}"
    )


def holt_forecast(
    history: list[float],
    horizon: int,
    *,
    alpha: float = 0.55,
    beta: float = 0.25,
) -> tuple[float, ...]:
    """Holt linear trend forecast for short monotonic changes."""
    values = _validate(history, horizon)
    if len(values) == 1:
        return tuple(max(0.0, values[0]) for _ in range(horizon))

    level = values[0]
    trend = values[1] - values[0]
    for value in values[1:]:
        previous_level = level
        level = alpha * value + (1.0 - alpha) * (level + trend)
        trend = beta * (level - previous_level) + (1.0 - beta) * trend
    return tuple(max(0.0, level + trend * (idx + 1)) for idx in range(horizon))


def kalman_forecast(
    history: list[float],
    horizon: int,
    *,
    process_noise: float = 0.02,
    velocity_gain: float = 0.2,
) -> tuple[float, ...]:
    """Lightweight alpha-beta Kalman-style filter for noisy short histories."""
    values = _validate(history, horizon)
    if len(values) == 1:
        return tuple(max(0.0, values[0]) for _ in range(horizon))

    measurement_noise = max(_variance(values), 1e-4)
    estimate = values[0]
    velocity = values[1] - values[0]
    covariance = 1.0
    for observed in values[1:]:
        predicted = estimate + velocity
        covariance += process_noise
        gain = covariance / (covariance + measurement_noise)
        residual = observed - predicted
        estimate = predicted + gain * residual
        velocity += velocity_gain * gain * residual
        covariance *= 1.0 - gain
    return tuple(max(0.0, estimate + velocity * (idx + 1)) for idx in range(horizon))


def adaptive_forecast(history: list[float], horizon: int) -> tuple[float, ...]:
    """Choose EWMA, Holt, or Kalman based on the observed short history."""
    values = _validate(history, horizon)
    if len(values) < 4:
        return exponential_forecast(values, horizon)

    scale = max(abs(fmean(values)), 1e-6)
    first_difference = [values[idx] - values[idx - 1] for idx in range(1, len(values))]
    volatility = fmean(abs(item) for item in first_difference) / scale
    trend_strength = abs(values[-1] - values[0]) / scale

    if volatility > 0.18:
        return kalman_forecast(values, horizon)
    if trend_strength > 0.12:
        return holt_forecast(values, horizon)
    return exponential_forecast(values, horizon)


def _validate(history: list[float], horizon: int) -> list[float]:
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if not history:
        raise ValueError("history must not be empty")
    return [float(value) for value in history]


def _variance(values: list[float]) -> float:
    mean = fmean(values)
    return fmean((value - mean) ** 2 for value in values)
