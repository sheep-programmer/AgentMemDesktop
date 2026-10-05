"""检索管线的数据模型。

这些模型只服务于 ``retrieve`` 包与对话链路，不属于 ``02-DATA-MODEL.md`` 的持久化实体，
因此就近定义在本包内；跨模块传递仍然全部是 Pydantic 模型，不出现裸 dict。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from agentmem.types import (
    AgentMemModel,
    ChunkKind,
    ContentModel,
    Id,
    Insight,
    KnowledgeCard,
    SearchMode,
)

#: 调试面板与引用气泡里展示的片段长度
SNIPPET_CHARS = 200


def snippet_of(text: str, limit: int = SNIPPET_CHARS) -> str:
    """把正文压成片段，供列表展示与引用注解使用。

    只压掉行内的连续空白，**保留换行**：片段在界面上是按 Markdown 渲染的，把换行也
    压掉的话表格会退化成一行竖线、列表会变成一串短横线。
    """
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    collapsed = "\n".join(line for line in lines if line)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


class ScoredChunk(ContentModel):
    """一条检索命中，保留各路分数供检索调试面板与轨迹落库使用。

    ``score`` 是展示用的综合分，各路原始分单独保留：调检索效果时看的是
    「向量排第几、BM25 排第几、重排后变成第几」，只看综合分会丢掉这些信息。

    ``ordinal`` / ``char_start`` / ``char_end`` 是**可被修改的正文的定位信息**：
    相邻切片的重叠裁掉一段之后，正文与偏移必须一起改，否则前端拿
    ``char_start`` 去全文里找位置会整体错位（见 ``retrieve/overlap.py``）。
    """

    chunk_id: Id
    document_id: Id
    document_title: str = ""
    heading_path: str | None = None
    page: int | None = None
    ordinal: int | None = Field(default=None, description="文档内序号，用于识别相邻切片")
    char_start: int | None = None
    char_end: int | None = None
    content: str
    vec_score: float | None = None
    bm25_score: float | None = None
    rrf: float | None = None
    rerank_score: float | None = None
    legs: list[str] = Field(
        default_factory=list,
        description="这条切片是被哪几路召回的（vector:1 / fts:1 / graph …）；"
        "「为什么这条证据会出现」比「它排第几」更常被问到",
    )
    kind: ChunkKind = Field(
        default="body",
        description="body=正文切片；summary=文档概要。界面据此加徽章，也据此决定能不能定位到原文",
    )
    merged_from: list[Id] = Field(
        default_factory=list,
        description="这条证据由哪几条切片合并而来（同一节里连号的兄弟切片）。"
        "为空表示它就是原始切片本身",
    )

    @property
    def score(self) -> float:
        """综合分：重排分优先，其次 RRF，最后回退到各路原始分。"""
        for candidate in (self.rerank_score, self.rrf, self.vec_score, self.bm25_score):
            if candidate is not None:
                return candidate
        return 0.0

    def snippet(self, limit: int = SNIPPET_CHARS) -> str:
        """单行片段。"""
        return snippet_of(self.content, limit)


class QueryPlan(AgentMemModel):
    """检索前对问题的加工结果。

    ``rewritten`` 只在与原问题不同时才有值，这样前端可以在真正发生改写时才渲染
    「已改写为 …」；``hyde`` 是假想答案，**只喂给向量召回**，绝不进入最终回答的上下文。
    """

    original: str
    rewritten: str | None = None
    variants: list[str] = Field(default_factory=list, description="多查询扩展产物")
    hyde: str | None = None

    @property
    def primary(self) -> str:
        """主检索式：改写过就用改写结果。"""
        return self.rewritten or self.original

    @property
    def search_queries(self) -> list[str]:
        """参与全文召回的检索式（不含 HyDE 产物）。"""
        queries = [self.primary, *self.variants]
        return _dedupe(queries)

    @property
    def vector_queries(self) -> list[str]:
        """参与向量召回的文本。

        开了 HyDE 时用假想答案顶替原问题——原问题与文档措辞差异大时，
        假想答案更接近文档的写法，召回更准。
        """
        queries = [self.hyde] if self.hyde else [self.primary]
        queries.extend(self.variants)
        return _dedupe([item for item in queries if item])


class RetrievalResult(AgentMemModel):
    """一次检索的全部产物（不含生成）。"""

    space_id: Id
    plan: QueryPlan
    mode: SearchMode = "hybrid"
    chunks: list[ScoredChunk] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)
    cards: list[KnowledgeCard] = Field(default_factory=list)
    reranked: bool = False
    degraded: list[str] = Field(
        default_factory=list,
        description=(
            "本轮检索中**配置了却没用上**的环节：`vector`（查询向量没算出来，"
            "退化成纯全文）、`rerank`（重排失败，退回 RRF 顺序）。"
            "没配的环节不算降级。实测本地模型全挂时问答照常出结果、没有任何页面"
            "报错，用户拿到更差的回答却无从知晓——这个字段就是为了让界面说出来"
        ),
    )
    latency_ms: int = 0


class SearchOutcome(AgentMemModel):
    """``/spaces/{id}/search`` 的检索产物。"""

    query: str
    mode: SearchMode
    chunks: list[ScoredChunk] = Field(default_factory=list)
    reranked: bool = False
    latency_ms: int = 0


class DeltaChunk(AgentMemModel):
    """正文流式增量。

    与 ``types.DeltaEvent`` 同形，但**不裁剪首尾空白**：一个增量可能就是一个空格
    （英文词之间、Markdown 缩进、代码块对齐），``str_strip_whitespace`` 会把它
    变成空串，正文就在流式过程中被悄悄改了形。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    text: str


class ChatEvent(AgentMemModel):
    """一条待推送的对话事件：事件名 + 载荷模型。

    core 不认识 SSE（不得依赖 FastAPI），只产出「事件名 + Pydantic 载荷」，
    由 ``apps/api`` 的分帧层包装成 ``event:`` / ``data:`` 帧。
    """

    name: str
    payload: BaseModel


def _dedupe(items: list[str]) -> list[str]:
    """去重且保持顺序。"""
    seen: dict[str, None] = {}
    for item in items:
        if item:
            seen.setdefault(item, None)
    return list(seen)
