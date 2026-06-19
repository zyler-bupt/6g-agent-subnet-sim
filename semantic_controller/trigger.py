from __future__ import annotations

from semantic_controller.ontology import CONTROL_INTENT_TERMS, GOAL_KEYWORDS, NON_CONTROL_TERMS
from semantic_controller.schemas import ApplicationState, SemanticTriggerResult


def detect_semantic_trigger(
    raw_user_input: str,
    application: ApplicationState | None = None,
) -> SemanticTriggerResult:
    text = raw_user_input.strip()
    app = application or ApplicationState()

    matched_terms: list[str] = []
    for terms in GOAL_KEYWORDS.values():
        matched_terms.extend(term for term in terms if term in text)

    if not matched_terms:
        return SemanticTriggerResult(
            triggered=False,
            confidence=0.0,
            reason="no network-relevant semantic keyword matched",
        )

    if app.type != "video_conference":
        return SemanticTriggerResult(
            triggered=False,
            confidence=0.2,
            matched_terms=matched_terms,
            reason="current application is not a supported video conference session",
        )

    if any(term in text for term in NON_CONTROL_TERMS):
        return SemanticTriggerResult(
            triggered=False,
            confidence=0.15,
            matched_terms=matched_terms,
            reason="input discusses text or concepts rather than controlling the active session",
        )

    has_control_intent = any(term in text for term in CONTROL_INTENT_TERMS)
    confidence = 0.88 if has_control_intent else 0.62
    has_quality_term = any(
        term in text for term in ("清晰", "清楚", "模糊", "画质", "分辨率", "高清", "看不清")
    )
    trigger_type = "video_quality" if has_quality_term else "generic"
    if not has_quality_term and any(term in text for term in ("卡", "卡顿", "延迟", "流畅", "稳定")):
        trigger_type = "smooth_streaming"
    if any(term in text for term in ("省流量", "节省带宽", "网络紧张")):
        trigger_type = "bandwidth_saving"

    return SemanticTriggerResult(
        triggered=has_control_intent,
        trigger_type=trigger_type if has_control_intent else "ambiguous",
        confidence=confidence,
        matched_terms=matched_terms,
        reason=(
            "matched semantic network-control intent"
            if has_control_intent
            else "matched keywords but no explicit control intent"
        ),
    )
