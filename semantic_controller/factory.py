from __future__ import annotations

import os

from semantic_controller.controller import SemanticController
from semantic_controller.embedding import (
    GoalPrototypeRetriever,
    HashingEmbeddingEncoder,
    OpenAICompatibleEmbeddingEncoder,
)
from semantic_controller.planner import ConstrainedRulePlanner, OpenManusTaskPlanner
from semantic_controller.qwen import OpenAICompatibleChatClient, QwenGoalRecognizer
from semantic_controller.recognizer import RetrievalAugmentedRuleRecognizer


def build_semantic_controller_from_env() -> SemanticController:
    retriever = GoalPrototypeRetriever(encoder=_build_embedding_encoder())
    qwen_base_url = os.environ.get("QWEN_BASE_URL")
    if not qwen_base_url:
        return SemanticController(
            goal_recognizer=RetrievalAugmentedRuleRecognizer(retriever=retriever),
            task_planner=ConstrainedRulePlanner(),
        )

    client = OpenAICompatibleChatClient(
        base_url=qwen_base_url,
        model=os.environ.get("QWEN_MODEL", "Qwen2.5-7B-Instruct"),
        api_key=os.environ.get("QWEN_API_KEY"),
        timeout=float(os.environ.get("QWEN_TIMEOUT_SECONDS", "30")),
    )
    recognizer = QwenGoalRecognizer(client=client, retriever=retriever)
    if os.environ.get("PLANNER_MODE", "rules").lower() == "openmanus":
        planner = OpenManusTaskPlanner(client=client)
    else:
        planner = ConstrainedRulePlanner()
    return SemanticController(goal_recognizer=recognizer, task_planner=planner)


def _build_embedding_encoder():
    base_url = os.environ.get("EMBEDDING_BASE_URL")
    if base_url:
        return OpenAICompatibleEmbeddingEncoder(
            base_url=base_url,
            model_name=os.environ.get("EMBEDDING_MODEL", "Qwen3-Embedding-0.6B"),
            api_key=os.environ.get("EMBEDDING_API_KEY"),
            timeout=float(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "15")),
        )
    return HashingEmbeddingEncoder(
        dimension=int(os.environ.get("EMBEDDING_DIMENSION", "256"))
    )
