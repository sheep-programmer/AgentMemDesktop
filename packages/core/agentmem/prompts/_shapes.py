"""Prompt 层使用的轻量输入结构。

刻意不复用 `agentmem.types` 里的 Pydantic 模型：Prompt 层要保持零依赖、可独立测试，
调用方在 retrieve / evolve 里做一次简单映射即可。
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class ChunkRef(TypedDict):
    """一条检索到的原文证据。`marker` 是分配给它的引用编号（如 "c3"）。

    ``score`` 是检索侧给出的相关性得分（重排分优先），供证据预算分配按相关性
    分篇幅用。它是可选的：不是所有调用方都有分数，缺省时预算分配退化为均分。
    """

    marker: str
    chunk_id: str
    document_title: str
    heading_path: NotRequired[str | None]
    page: NotRequired[int | None]
    content: str
    score: NotRequired[float]


class CardRef(TypedDict):
    """一张知识卡片（L2）。"""

    card_id: str
    kind: str
    title: str
    body: str
    confidence: float


class InsightRef(TypedDict):
    """一条经验（L3）。三段式：场景 / 做法 / 依据。"""

    insight_id: str
    trigger: str
    guidance: str
    rationale: NotRequired[str | None]
    confidence: float


class PersonaSpec(TypedDict):
    """专家人格（L4），来自 space.yaml。"""

    name: str
    domain: str
    role_description: NotRequired[str | None]
    principles: NotRequired[list[str]]
    language: NotRequired[str]
    tone: NotRequired[str | None]
    must_cite: NotRequired[bool]
    quality_bar: NotRequired[list[str]]
    glossary: NotRequired[dict[str, str]]


class Turn(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str
