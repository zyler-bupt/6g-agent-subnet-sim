from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.request import Request, urlopen

from pydantic import ValidationError

from semantic_controller.embedding import GoalPrototypeRetriever, build_embedding_text
from semantic_controller.recognizer import RuleBasedGoalRecognizer
from semantic_controller.schemas import (
    GoalCandidate,
    GoalSpec,
    SemanticContext,
    SemanticEmbedding,
)


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return the assistant message text."""


@dataclass
class OpenAICompatibleChatClient:
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float = 30.0
    temperature: float = 0.0
    top_p: float = 0.8
    max_tokens: int = 1024

    def complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        request = Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return str(body["choices"][0]["message"]["content"])

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


@dataclass
class QwenGoalRecognizer:
    client: ChatClient
    retriever: GoalPrototypeRetriever = field(default_factory=GoalPrototypeRetriever)
    fallback: RuleBasedGoalRecognizer = field(default_factory=RuleBasedGoalRecognizer)

    def recognize(self, context: SemanticContext) -> GoalSpec:
        goal, _, _ = self.recognize_with_evidence(context)
        return goal

    def recognize_with_evidence(
        self,
        context: SemanticContext,
    ) -> tuple[GoalSpec, SemanticEmbedding, list[GoalCandidate]]:
        embedding, candidates = self.retriever.retrieve(context)
        messages = self._messages(context, candidates)
        try:
            raw = self.client.complete(messages)
            goal = _parse_goal(raw)
        except (OSError, KeyError, ValueError, ValidationError, json.JSONDecodeError) as first_error:
            try:
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": locals().get("raw", "")},
                    {
                        "role": "user",
                        "content": (
                            "上一输出未通过GoalSpec校验。只返回修复后的JSON对象，不要解释。"
                            f" 校验错误：{first_error}"
                        ),
                    },
                ]
                goal = _parse_goal(self.client.complete(repair_messages))
            except (OSError, KeyError, ValueError, ValidationError, json.JSONDecodeError):
                goal = self.fallback.recognize(context)
        return goal, embedding, candidates

    @staticmethod
    def _messages(
        context: SemanticContext,
        candidates: list[GoalCandidate],
    ) -> list[dict[str, str]]:
        system = """你是SANet双Agent Controller的语义目标识别模块。
当前系统只支持application-agent和network-agent。
你只能选择以下Goal ID：IMPROVE_VIDEO_QUALITY、REDUCE_VIDEO_STALL、GUARANTEE_SMOOTH_STREAMING、SAVE_NETWORK_BANDWIDTH、CHECK_NETWORK_FEASIBILITY、UNKNOWN。
required_information只能包含future_app_rate和future_network_bandwidth。
不得创建pAgent、CSI、频谱或无线资源任务。
信息不足时goal_id必须为UNKNOWN，并设置need_clarification=true和clarification_question。
只输出符合GoalSpec的JSON对象。"""
        candidate_text = "\n".join(
            f"{item.goal_id.value}: {item.similarity:.4f}" for item in candidates
        )
        user = f"""结构化上下文：
{build_embedding_text(context)}

候选目标：
{candidate_text}

GoalSpec字段：goal_id、goal_description、confidence、constraints、required_information、need_clarification、clarification_question。"""
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


def _parse_goal(raw: str) -> GoalSpec:
    payload = _extract_json_object(raw)
    return GoalSpec.model_validate(payload)


def _extract_json_object(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response does not contain a JSON object")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("GoalSpec response must be a JSON object")
    return value
