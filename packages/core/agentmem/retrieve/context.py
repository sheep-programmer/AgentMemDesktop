"""上下文装配：把 core 的强类型模型映射成 Prompt 层的输入形状。

Prompt 层（``agentmem.prompts``）刻意零依赖，只认 ``_shapes`` 里那几个 TypedDict；
映射集中在本模块，业务代码里不出现裸 dict，Prompt 层也不必反过来依赖 types。

装配顺序来自 ``docs/00-VISION.md``（越靠前约束力越强）::

    L4 Persona → L3 Insights → L2 Cards → L1 Chunks（带引用 marker）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentmem.prompts import (
    CardRef,
    ChunkRef,
    InsightRef,
    PersonaSpec,
    Turn,
)
from agentmem.prompts import Message as PromptMessage
from agentmem.prompts.answer import build_answer_context
from agentmem.prompts.compaction import STANDARD_BUDGET, ContextBudget
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.base import MessageRole
from agentmem.types import (
    ContextUsageEvent,
    Insight,
    KnowledgeCard,
    Message,
    Persona,
    TraceRetrievedItem,
)

from .citations import CitationRegistry, marker_for
from .models import ScoredChunk

#: 进上下文的历史消息条数上限（通常是四轮问答），不是八轮问答
HISTORY_TURNS = 8

#: Prompt 角色 → Provider 角色。Prompt 层不允许出现 tool，这里做一次收窄。
_ROLE_MAP: dict[str, MessageRole] = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
}


def to_persona_spec(persona: Persona) -> PersonaSpec:
    """L4 画像 → Prompt 形状。"""
    style = persona.output_style
    return PersonaSpec(
        name=persona.name,
        domain=persona.domain,
        role_description=persona.role_description or None,
        principles=list(persona.principles),
        language=style.language,
        tone=style.tone or None,
        must_cite=style.must_cite,
        quality_bar=list(persona.quality_bar),
        glossary=dict(persona.glossary),
    )


def to_chunk_ref(chunk: ScoredChunk, marker: str) -> ChunkRef:
    """L1 命中 → 带引用编号的证据块。

    带上 ``score``（重排分优先，其次 RRF，最后回退各路原始分）：Prompt 层按相关性
    分配证据篇幅，分数是唯一的依据，不带过去就只能均分。
    """
    return ChunkRef(
        marker=marker,
        chunk_id=chunk.chunk_id,
        document_title=chunk.document_title or "未命名文档",
        heading_path=chunk.heading_path,
        page=chunk.page,
        content=chunk.content,
        score=chunk.score,
    )


def to_insight_ref(insight: Insight) -> InsightRef:
    """L3 经验 → Prompt 形状。"""
    return InsightRef(
        insight_id=insight.id,
        trigger=insight.trigger,
        guidance=insight.guidance,
        rationale=insight.rationale,
        confidence=insight.confidence,
    )


def to_card_ref(card: KnowledgeCard) -> CardRef:
    """L2 卡片 → Prompt 形状。"""
    return CardRef(
        card_id=card.id,
        kind=card.kind,
        title=card.title,
        body=card.body,
        confidence=card.confidence,
    )


def to_provider_messages(messages: Sequence[PromptMessage]) -> list[ProviderMessage]:
    """Prompt 层的 TypedDict 消息 → Provider 层入参。

    Provider 的 role 联合类型比 Prompt 层多一个 ``tool``，因此不能直接传，
    需要按映射表收窄一次。
    """
    return [
        ProviderMessage(role=_ROLE_MAP[message["role"]], content=message["content"])
        for message in messages
    ]


def to_turns(messages: Sequence[Message], *, limit: int = HISTORY_TURNS) -> list[Turn]:
    """对话记录 → 历史轮次。

    只保留 user / assistant（``system`` 每轮重新装配，``tool`` 不参与多轮上下文），
    并按最近 ``limit`` 轮截断。
    """
    turns: list[Turn] = []
    for message in messages:
        if message.role == "user":
            turns.append(Turn(role="user", content=message.content))
        elif message.role == "assistant":
            turns.append(Turn(role="assistant", content=message.content))
    return turns[-limit:] if limit > 0 else []


def assign_markers(chunks: Sequence[ScoredChunk]) -> tuple[list[ChunkRef], CitationRegistry]:
    """给每条命中分配 ``c1`` / ``c2`` … 并登记映射。

    marker 的分配顺序即证据在上下文里的顺序，也是前端引用列表的顺序。
    """
    registry = CitationRegistry()
    refs: list[ChunkRef] = []
    for index, chunk in enumerate(chunks, start=1):
        marker = marker_for(index)
        registry.add(marker, chunk)
        refs.append(to_chunk_ref(chunk, marker))
    return refs, registry


def to_retrieved_items(chunks: Sequence[ScoredChunk]) -> list[TraceRetrievedItem]:
    """命中 → 轨迹里的检索明细（各路分数都要落库）。

    这是 Phase 3 进化闭环的原料：「这条经验是从哪次检索、哪条证据蒸馏出来的」
    全靠这里记下来的分数还原。
    """
    return [
        TraceRetrievedItem(
            chunk_id=chunk.chunk_id,
            merged_from=list(chunk.merged_from),
            vec_score=chunk.vec_score,
            bm25_score=chunk.bm25_score,
            rrf=chunk.rrf,
            rerank_score=chunk.rerank_score,
            legs=list(chunk.legs),
        )
        for chunk in chunks
    ]


def assemble_answer_messages(
    *,
    persona: Persona,
    question: str,
    chunks: Sequence[ScoredChunk] = (),
    insights: Sequence[Insight] = (),
    cards: Sequence[KnowledgeCard] = (),
    history: Sequence[Message] = (),
    budget: ContextBudget | None = STANDARD_BUDGET,
    evidence_query: str | None = None,
) -> tuple[list[ProviderMessage], CitationRegistry]:
    """装配四层上下文并生成可直接喂给 LLM 的 messages。

    Returns:
        (messages, 引用注册表)。注册表供生成阶段把 ``[^c3]`` 解析回 chunk_id。
    """
    context = assemble_answer_context(
        persona=persona,
        question=question,
        chunks=chunks,
        insights=insights,
        cards=cards,
        history=history,
        budget=budget,
        evidence_query=evidence_query,
    )
    return context.messages, context.registry


@dataclass(frozen=True)
class AssembledContext:
    messages: list[ProviderMessage]
    registry: CitationRegistry
    insights: list[Insight]
    cards: list[KnowledgeCard]
    usage: ContextUsageEvent


def assemble_answer_context(
    *,
    persona: Persona,
    question: str,
    chunks: Sequence[ScoredChunk] = (),
    insights: Sequence[Insight] = (),
    cards: Sequence[KnowledgeCard] = (),
    history: Sequence[Message] = (),
    budget: ContextBudget | None = STANDARD_BUDGET,
    evidence_query: str | None = None,
) -> AssembledContext:
    refs, _ = assign_markers(chunks)
    context = build_answer_context(
        persona=to_persona_spec(persona),
        question=question,
        chunks=refs,
        insights=[to_insight_ref(item) for item in insights],
        cards=[to_card_ref(item) for item in cards],
        history=to_turns(history),
        budget=budget,
        evidence_query=evidence_query,
    )
    # A dropped chunk cannot be cited just because its number occurred in old history.
    # Original source text stays intact; excerpting never changes stored offsets or IDs.
    known = set(context.evidence_markers)
    registry = CitationRegistry()
    for chunk, ref in zip(chunks, refs, strict=True):
        if ref["marker"] in known:
            registry.add(ref["marker"], chunk)
    return AssembledContext(
        messages=to_provider_messages(context.messages),
        registry=registry,
        insights=[item for item in insights if item.id in context.insight_ids],
        cards=[item for item in cards if item.id in context.card_ids],
        usage=ContextUsageEvent(
            mode=budget.mode if budget else "standard",
            original_estimated_tokens=context.original_estimated_tokens,
            estimated_tokens=context.estimated_tokens,
            saved_estimated_tokens=max(
                0, context.original_estimated_tokens - context.estimated_tokens
            ),
            history_messages=len(context.history),
            evidence_count=len(known),
            insight_count=len(context.insight_ids),
            card_count=len(context.card_ids),
        ),
    )
