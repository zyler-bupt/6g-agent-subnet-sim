from __future__ import annotations

import time
from itertools import count

from semantic_controller.schemas import (
    ApplicationState,
    NetworkState,
    SemanticContext,
    UserPreferences,
)


_EVENT_COUNTER = count(1)


def build_semantic_context(
    raw_user_input: str,
    *,
    session_id: str = "meeting-01",
    recent_dialogue: list[str] | None = None,
    application: ApplicationState | None = None,
    network: NetworkState | None = None,
    user_preferences: UserPreferences | None = None,
) -> SemanticContext:
    dialogue = list(recent_dialogue or [])
    if raw_user_input not in dialogue:
        dialogue.append(raw_user_input)
    dialogue = dialogue[-3:]

    return SemanticContext(
        event_id=f"evt-{next(_EVENT_COUNTER):04d}",
        timestamp=time.time(),
        session_id=session_id,
        raw_user_input=raw_user_input,
        recent_dialogue=dialogue,
        application=application or ApplicationState(),
        network=network or NetworkState(),
        user_preferences=user_preferences or UserPreferences(),
    )

