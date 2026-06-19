from __future__ import annotations

import json

from semantic_controller.controller import SemanticController
from semantic_controller.schemas import ApplicationState, NetworkState, UserPreferences


def main() -> None:
    controller = SemanticController()
    result = controller.handle_user_input(
        "当前画面太模糊了，希望清晰一些，但不要卡。",
        application=ApplicationState(
            resolution="720p",
            fps=30,
            target_bitrate_mbps=2.0,
            measured_rate_mbps=1.8,
            buffer_seconds=2.1,
        ),
        network=NetworkState(latest_bandwidth_mbps=6.2, rtt_ms=35, loss_percent=0.2),
        user_preferences=UserPreferences(prefer_quality=True, allow_quality_degradation=True),
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

