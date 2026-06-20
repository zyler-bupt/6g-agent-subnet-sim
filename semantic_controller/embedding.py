from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Protocol
from urllib.request import Request, urlopen

from semantic_controller.schemas import GoalCandidate, GoalID, SemanticContext, SemanticEmbedding


GOAL_PROTOTYPES: dict[GoalID, tuple[str, ...]] = {
    GoalID.IMPROVE_VIDEO_QUALITY: (
        "提高当前视频清晰度",
        "画面太模糊了",
        "我想看得更清楚",
        "调到更高分辨率",
    ),
    GoalID.REDUCE_VIDEO_STALL: (
        "视频太卡了",
        "减少卡顿和掉帧",
        "降低播放延迟",
        "不要再卡顿",
    ),
    GoalID.GUARANTEE_SMOOTH_STREAMING: (
        "保证会议流畅稳定",
        "会议过程不要中断",
        "让视频传输稳定一些",
        "保证流畅播放",
    ),
    GoalID.SAVE_NETWORK_BANDWIDTH: (
        "网络紧张需要节省带宽",
        "降低视频流量",
        "减少当前带宽占用",
        "使用更低码率",
    ),
    GoalID.CHECK_NETWORK_FEASIBILITY: (
        "检查网络能否支持视频",
        "当前带宽够不够",
        "判断网络是否能够承载",
        "检查提高画质是否可行",
    ),
}


class EmbeddingEncoder(Protocol):
    model_name: str

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode texts into equal-length normalized vectors."""


@dataclass
class HashingEmbeddingEncoder:
    """Dependency-free baseline used when no embedding service is configured."""

    dimension: int = 256
    model_name: str = "hashing-char-ngram-v1"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> list[float]:
        compact = "".join(text.lower().split())
        tokens = list(compact)
        tokens.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
        vector = [0.0] * self.dimension
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)


@dataclass
class OpenAICompatibleEmbeddingEncoder:
    base_url: str
    model_name: str = "Qwen3-Embedding-0.6B"
    api_key: str | None = None
    timeout: float = 15.0

    def encode(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model_name, "input": texts}
        request = Request(
            f"{self.base_url.rstrip('/')}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        ordered = sorted(body["data"], key=lambda item: item["index"])
        return [_normalize([float(value) for value in item["embedding"]]) for item in ordered]

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


@dataclass
class GoalPrototypeRetriever:
    encoder: EmbeddingEncoder = field(default_factory=HashingEmbeddingEncoder)
    prototypes: dict[GoalID, tuple[str, ...]] = field(default_factory=lambda: GOAL_PROTOTYPES)
    _prototype_vectors: dict[GoalID, list[list[float]]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        indexed_examples = [
            (goal_id, example)
            for goal_id, examples in self.prototypes.items()
            for example in examples
        ]
        vectors = self.encoder.encode([example for _, example in indexed_examples])
        self._prototype_vectors = {goal_id: [] for goal_id in self.prototypes}
        for (goal_id, _), vector in zip(indexed_examples, vectors):
            self._prototype_vectors[goal_id].append(vector)

    def retrieve(
        self,
        context: SemanticContext,
        *,
        top_k: int = 3,
    ) -> tuple[SemanticEmbedding, list[GoalCandidate]]:
        text = build_embedding_text(context)
        vector, intent_vector = self.encoder.encode([text, context.raw_user_input])
        candidates = []
        for goal_id, prototype_vectors in self._prototype_vectors.items():
            similarity = max(_dot(intent_vector, prototype) for prototype in prototype_vectors)
            candidates.append(GoalCandidate(goal_id=goal_id, similarity=similarity))
        candidates.sort(key=lambda item: item.similarity, reverse=True)
        return (
            SemanticEmbedding(
                model=self.encoder.model_name,
                dimension=len(vector),
                values=vector,
            ),
            candidates[:top_k],
        )


def build_embedding_text(context: SemanticContext) -> str:
    preferences = []
    if context.user_preferences.prefer_quality:
        preferences.append("优先画质")
    if context.user_preferences.prefer_low_latency:
        preferences.append("优先低时延")
    if context.user_preferences.allow_quality_degradation:
        preferences.append("允许降低画质")
    return "\n".join(
        [
            f"业务类型：{context.application.type}",
            f"用户输入：{context.raw_user_input}",
            f"当前分辨率：{context.application.resolution}",
            f"当前帧率：{context.application.fps}fps",
            f"当前应用数据率：{context.application.measured_rate_mbps}Mbps",
            f"当前网络带宽：{context.network.latest_bandwidth_mbps}Mbps",
            f"用户偏好：{'、'.join(preferences) if preferences else '未指定'}",
        ]
    )


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _dot(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    return sum(a * b for a, b in zip(left, right))
