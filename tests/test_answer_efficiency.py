"""问答链路的省时省 token：证据精简、卡片去重、推理强度提示、问题向量与重排分缓存。

这些改动都不该改变「答得对不对」，所以每条测试除了钉住「省掉了什么」，
也钉住「该留的还留着」：最高分恒在、没把握时不裁、缺一条正文就不丢概要、
被截断的证据不算覆盖卡片、服务端不认推理参数时照常成功。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from agentmem.config import Settings
from agentmem.prompts._shapes import CardRef, ChunkRef, PersonaSpec
from agentmem.prompts.answer import build_answer_context
from agentmem.providers.adapters.openai_compatible import OpenAICompatibleProvider
from agentmem.providers.base import ChatResult, Message, RankedDoc, ToolSpec
from agentmem.providers.registry import (
    EmbeddingRoute,
    LLMRoute,
    LruCache,
    RerankRoute,
    UsageRecorder,
)
from agentmem.retrieve import QueryRewriter, RetrievalPipeline, ScoredChunk
from agentmem.retrieve.models import QueryPlan
from agentmem.retrieve.pipeline import _near_top
from agentmem.retrieve.relevance import drop_covered_summaries, drop_low_relevance
from agentmem.store import Database
from agentmem.types import ProviderConfig, RetrievalSettings, RoleName

# ---------------------------------------------------------------------------
# 证据精简
# ---------------------------------------------------------------------------


def _scored(
    chunk_id: str,
    score: float | None,
    *,
    document_id: str = "doc-1",
    kind: str = "body",
    ordinal: int | None = 0,
    content: str = "正文",
    merged_from: list[str] | None = None,
) -> ScoredChunk:
    return ScoredChunk.model_validate(
        {
            "chunk_id": chunk_id,
            "document_id": document_id,
            "content": content,
            "rerank_score": score,
            "kind": kind,
            "ordinal": ordinal,
            "merged_from": merged_from or [],
        }
    )


def test_low_relevance_evidence_is_dropped_but_order_is_kept() -> None:
    """重排分不到最高分十分之一的条目不送给回答模型，其余保持原顺序。"""
    chunks = [
        _scored("a", 0.99),
        _scored("noise-1", 0.0),
        _scored("b", 0.15),
        _scored("noise-2", 0.05),
    ]
    kept = drop_low_relevance(chunks, floor=0.1)
    assert [chunk.chunk_id for chunk in kept] == ["a", "b"]


def test_low_relevance_floor_keeps_everything_when_reranker_is_unsure() -> None:
    """最高分都不到 0.5 时（宽泛的概览题），相对比例只是噪声，一条不裁。"""
    chunks = [_scored("a", 0.007), _scored("b", 0.0006), _scored("c", 0.002)]
    assert drop_low_relevance(chunks, floor=0.1) == chunks


@pytest.mark.parametrize(
    "scores",
    [
        [0.9, None, 0.01],  # 有条目没打上分
        [4.2, -3.1, -7.5],  # 原始 logits：比例没有意义
    ],
)
def test_low_relevance_floor_ignores_scores_that_are_not_probabilities(
    scores: list[float | None],
) -> None:
    chunks = [_scored(f"c{index}", score) for index, score in enumerate(scores)]
    assert drop_low_relevance(chunks, floor=0.1) == chunks


def test_low_relevance_floor_zero_disables_it() -> None:
    chunks = [_scored("a", 0.99), _scored("b", 0.0)]
    assert drop_low_relevance(chunks, floor=0.0) == chunks


def test_summary_is_dropped_when_the_whole_short_document_is_present() -> None:
    """短文档的正文全部在场时，概要切片只是同一件事的第二种说法（还定位不到原文）。"""
    chunks = [
        _scored("summary", 0.99, kind="summary", ordinal=1, content="长江大学于2003年5月组建"),
        _scored("body", 0.15, ordinal=0, content="2003年5月，校庆是5月22日"),
    ]
    kept = drop_covered_summaries(chunks)
    assert [chunk.chunk_id for chunk in kept] == ["body"]


def test_summary_is_kept_when_any_body_chunk_is_missing() -> None:
    chunks = [
        _scored("summary", 0.9, kind="summary", ordinal=3),
        _scored("b0", 0.8, ordinal=0),
        _scored("b2", 0.7, ordinal=2),
    ]
    assert drop_covered_summaries(chunks) == chunks


def test_merged_siblings_count_as_covering_their_whole_span() -> None:
    """兄弟切片合并后的条目覆盖从 ordinal 起 len(merged_from) 个序号。"""
    chunks = [
        _scored("b0", 0.9, ordinal=0, merged_from=["b0", "b1"]),
        _scored("b2", 0.8, ordinal=2),
        _scored("summary", 0.7, kind="summary", ordinal=3),
    ]
    assert [chunk.chunk_id for chunk in drop_covered_summaries(chunks)] == ["b0", "b2"]


def test_summary_is_kept_for_long_documents() -> None:
    """长文档的正文可能被证据预算截断，届时概要里的汇总未必还在。"""
    chunks = [
        _scored("b0", 0.9, ordinal=0, content="长" * 900),
        _scored("b1", 0.8, ordinal=1, content="文" * 900),
        _scored("summary", 0.7, kind="summary", ordinal=2),
    ]
    assert drop_covered_summaries(chunks) == chunks


def test_summary_of_another_document_is_untouched() -> None:
    chunks = [
        _scored("b0", 0.9, ordinal=0, document_id="doc-a"),
        _scored("summary", 0.7, kind="summary", ordinal=1, document_id="doc-b"),
    ]
    assert drop_covered_summaries(chunks) == chunks


def test_cards_far_below_the_best_match_are_not_recalled() -> None:
    """卡片按向量近邻召回；比最相似那张低出 0.2 以上的基本是另一个话题。"""
    hits = [("a", 0.66), ("b", 0.395), ("c", 0.391), ("d", 0.55)]
    assert _near_top(hits, 0.2) == ["a", "d"]
    assert _near_top([], 0.2) == []


async def test_retrieve_prunes_evidence_but_search_does_not(
    database: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """问答链路精简证据；检索调试接口照旧返回完整 top-N，调参要看的正是那些噪声。"""
    pipeline = RetrievalPipeline(
        space_id=database.space_id or "space",
        database=database,
        registry=_NoProviders(),  # type: ignore[arg-type]
        settings=settings,
        retrieval=RetrievalSettings(),
    )
    ranked = [
        _scored("hit", 0.98, ordinal=0, document_id="doc-a"),
        _scored("summary", 0.97, kind="summary", ordinal=1, document_id="doc-a"),
        _scored("noise", 0.001, ordinal=7, document_id="doc-b"),
    ]

    async def fixed(*_args: Any, **_kwargs: Any) -> tuple[list[ScoredChunk], bool]:
        return list(ranked), True

    async def no_embedding(_plan: QueryPlan) -> Any:
        return _EmptyEmbedded()

    monkeypatch.setattr(pipeline, "_search_with", fixed)
    monkeypatch.setattr(pipeline, "_embed_queries", no_embedding)

    result = await pipeline.retrieve("问题", use_insights=False, use_cards=False)
    assert [chunk.chunk_id for chunk in result.chunks] == ["hit"]

    outcome = await pipeline.search("问题")
    assert [chunk.chunk_id for chunk in outcome.chunks] == ["hit", "summary", "noise"]


class _EmptyEmbedded:
    """没有任何查询向量（等价于没绑定 embedding 角色）。"""

    primary = None

    def vector_legs(self) -> list[list[float]]:
        return []


class _NoProviders:
    """一个角色都没绑定的注册表替身。"""

    def role_provider_id(self, role: RoleName) -> str | None:
        del role
        return None


# ---------------------------------------------------------------------------
# 卡片去重
# ---------------------------------------------------------------------------

_PERSONA = PersonaSpec(name="专家", domain="药化")
_THRESHOLDS = (
    "共价结合效率 kinact/KI 应大于 1000 M⁻¹s⁻¹。谷胱甘肽（GSH）半衰期需大于 24 小时，"
    "低于此值提示弹头过度活泼。对 KRAS WT 的选择性倍数应大于 100x。"
)


def _card(card_id: str, title: str, body: str) -> CardRef:
    return CardRef(card_id=card_id, kind="fact", title=title, body=body, confidence=0.8)


def _evidence(content: str) -> ChunkRef:
    return ChunkRef(marker="c1", chunk_id="k1", document_title="kras-test.md", content=content)


def test_card_repeating_rendered_evidence_is_not_sent_twice() -> None:
    """卡片正文已经整段出现在本轮证据里：留下能被引用的证据，卡片不再重复。"""
    context = build_answer_context(
        persona=_PERSONA,
        question="KRAS G12C 共价抑制剂的判定阈值？",
        chunks=[_evidence("## 3. 关键判定阈值\n\n" + _THRESHOLDS)],
        cards=[
            _card("dup", "关键判定阈值", _THRESHOLDS),
            _card("other", "Switch II 口袋", "突变引入的半胱氨酸位于 Switch II 口袋附近。"),
        ],
    )
    assert context.card_ids == ["other"]
    assert context.messages[-1]["content"].count("kinact/KI 应大于 1000") == 1


def test_near_identical_cards_are_rendered_once() -> None:
    """抽取常把同一结论写成两张标题略异的卡片。"""
    context = build_answer_context(
        persona=_PERSONA,
        question="判定阈值？",
        cards=[
            _card("first", "KRAS G12C 共价抑制剂关键判定阈值", _THRESHOLDS),
            _card("second", "KRAS G12C 共价抑制剂的关键判定阈值", _THRESHOLDS + "\n"),
        ],
    )
    assert context.card_ids == ["first"]


def test_card_is_kept_when_the_matching_evidence_was_truncated() -> None:
    """比对的是渲染后的证据：被预算截掉的那部分只留在卡片里时，卡片照送。"""
    filler = "这是一段与阈值无关的背景说明，用来把证据撑长。" * 200
    context = build_answer_context(
        persona=_PERSONA,
        question="判定阈值？",
        chunks=[_evidence(filler + _THRESHOLDS + filler)],
        cards=[_card("dup", "关键判定阈值", _THRESHOLDS)],
    )
    assert "kinact/KI 应大于 1000" in context.messages[-1]["content"]
    assert context.card_ids == ["dup"]


# ---------------------------------------------------------------------------
# 推理强度提示
# ---------------------------------------------------------------------------


def _llm_config(**extra: object) -> ProviderConfig:
    return ProviderConfig.model_validate(
        {
            "id": "reasoner",
            "kind": "llm",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:1/v1",
            "model": "flash",
            "extra": extra,
        }
    )


class _FakeCompletions:
    """记录每次请求参数；``reject`` 为真时对带 reasoning_effort 的请求回 400。"""

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.reject and "reasoning_effort" in kwargs:
            request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
            raise openai.BadRequestError(
                "Input should be 'none', 'low', 'medium' or 'high' (reasoning_effort)",
                response=httpx.Response(400, request=request),
                body=None,
            )
        return _completion()


def _completion() -> SimpleNamespace:
    """形状与 SDK 的 ChatCompletion 一致的最小响应。"""
    message = SimpleNamespace(content='{"query": "改写后的问题", "changed": true}', tool_calls=None)
    choice = SimpleNamespace(finish_reason="stop", message=message)
    return SimpleNamespace(model="flash", usage=None, choices=[choice])


def _provider_with(fake: _FakeCompletions, **extra: object) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(_llm_config(**extra))
    client = type("Client", (), {"chat": type("Chat", (), {"completions": fake})()})()
    provider._client = client
    return provider


async def test_reasoning_effort_is_sent_when_requested() -> None:
    fake = _FakeCompletions()
    provider = _provider_with(fake)
    await provider.chat([Message(role="user", content="改写")], reasoning_effort="none")
    await provider.chat([Message(role="user", content="回答")])
    assert fake.calls[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in fake.calls[1], "没要求、也没配置时不该擅自带上"


async def test_provider_extra_sets_the_default_reasoning_effort() -> None:
    """正式回答不在代码里关推理；由 provider 的 ``extra.reasoning_effort`` 交给用户决定。"""
    fake = _FakeCompletions()
    provider = _provider_with(fake, reasoning_effort="low")
    await provider.chat([Message(role="user", content="回答")])
    await provider.chat([Message(role="user", content="改写")], reasoning_effort="none")
    assert fake.calls[0]["reasoning_effort"] == "low"
    assert fake.calls[1]["reasoning_effort"] == "none", "显式提示优先于配置"


async def test_rejected_reasoning_effort_is_dropped_and_remembered() -> None:
    """服务端不认这个参数时去掉重来，且之后不再带——不让每次调用都先挨一次 400。"""
    fake = _FakeCompletions(reject=True)
    provider = _provider_with(fake)
    result = await provider.chat([Message(role="user", content="改写")], reasoning_effort="none")
    assert result.content
    await provider.chat([Message(role="user", content="再改写")], reasoning_effort="none")
    assert ["reasoning_effort" in call for call in fake.calls] == [True, False, False]


async def test_streams_honour_the_reasoning_hint_too() -> None:
    fake = _FakeCompletions(reject=True)
    provider = _provider_with(fake, reasoning_effort="none", stream_usage=False)

    async def empty_stream(**kwargs: Any) -> Any:
        fake.calls.append(kwargs)
        if "reasoning_effort" in kwargs:
            request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
            raise openai.BadRequestError(
                "Unrecognized request argument supplied: reasoning_effort",
                response=httpx.Response(400, request=request),
                body=None,
            )

        async def frames() -> Any:
            empty: tuple[Any, ...] = ()
            for item in empty:
                yield item

        return frames()

    fake.create = empty_stream  # type: ignore[method-assign]
    chunks = [chunk async for chunk in provider.stream([Message(role="user", content="答")])]
    assert chunks == []
    assert ["reasoning_effort" in call for call in fake.calls] == [True, False]


class _PlainLLM:
    """签名里没有 reasoning_effort 的第三方 / 旧适配器。"""

    name = "plain"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        del messages, tools, temperature, max_tokens
        self.calls += 1
        return ChatResult(
            content='{"query": "改写", "changed": true}',
            model="plain",
            provider_id=self.name,
            latency_ms=0,
        )

    def stream(self, messages: list[Message], **kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


async def test_route_only_passes_the_hint_to_adapters_that_understand_it() -> None:
    plain = _PlainLLM()
    route = LLMRoute("fast", [plain], UsageRecorder(None))
    result = await route.chat([Message(role="user", content="改写")], reasoning_effort="none")
    assert result.content and plain.calls == 1


async def test_rewriter_asks_for_no_reasoning() -> None:
    """指代补全是机械任务：显式要 ``none``，不让推理模型先想几秒再答。"""
    fake = _FakeCompletions()
    provider = _provider_with(fake)

    class Registry:
        def llm(self, role: RoleName = "chat", *, purpose: str | None = None) -> LLMRoute:
            del role, purpose
            return LLMRoute("fast", [provider], UsageRecorder(None))

    rewritten = await QueryRewriter(Registry()).contextualize(
        question="那它的半衰期呢？",
        history=[{"role": "user", "content": "CMPD-005 的口服生物利用度是多少？"}],
    )
    assert rewritten == "改写后的问题"
    assert fake.calls[0]["reasoning_effort"] == "none"


# ---------------------------------------------------------------------------
# 问题向量与重排分缓存
# ---------------------------------------------------------------------------


class _CountingEmbedder:
    name = "embed"
    model_name = "bge"
    dimension = 2
    concurrent_safe = True

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed(self, texts: list[str], *, kind: str = "doc") -> list[list[float]]:
        del kind
        self.batches.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


async def test_query_vectors_are_reused_but_document_vectors_are_not() -> None:
    """「重新生成」与评测 A/B 两臂会把同一个问题再嵌入一遍；摄取的正文不进缓存。"""
    embedder = _CountingEmbedder()
    cache: LruCache[Any] = LruCache(8)
    route = EmbeddingRoute("embedding", [embedder], UsageRecorder(None), query_cache=cache)

    first = await route.embed(["问题甲"], kind="query")
    again = await route.embed(["问题甲"], kind="query")
    mixed = await route.embed(["问题甲", "问题乙"], kind="query")
    await route.embed(["正文"], kind="doc")
    await route.embed(["正文"], kind="doc")

    assert first == again == [[3.0, 1.0]]
    assert mixed == [[3.0, 1.0], [3.0, 1.0]]
    assert embedder.batches == [["问题甲"], ["问题乙"], ["正文"], ["正文"]]


class _CountingReranker:
    name = "rerank"
    model_name = "bge-reranker"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]:
        del query
        self.batches.append(list(docs))
        ranked = [
            RankedDoc(index=index, score=len(doc) / 10, text=doc) for index, doc in enumerate(docs)
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_n]


async def test_rerank_scores_are_cached_per_candidate() -> None:
    """同一问题再问一遍不再打分；新增的候选只给新增的那几条打分。结果与不缓存一致。"""
    reranker = _CountingReranker()
    route = RerankRoute(
        "rerank",
        [reranker],
        UsageRecorder(None),
        score_cache=LruCache(64),
    )
    docs = ["短", "长一点", "最长的一条"]

    first = await route.rerank("问题", docs, top_n=2)
    again = await route.rerank("问题", docs, top_n=2)
    widened = await route.rerank("问题", [*docs, "新入库的一条很长的正文"], top_n=2)

    assert [item.index for item in first] == [2, 1]
    assert [(item.index, item.score) for item in again] == [
        (item.index, item.score) for item in first
    ]
    assert [item.index for item in widened] == [3, 2]
    assert reranker.batches == [docs, ["新入库的一条很长的正文"]]


async def test_rerank_cache_is_keyed_by_query() -> None:
    reranker = _CountingReranker()
    route = RerankRoute(
        "rerank",
        [reranker],
        UsageRecorder(None),
        score_cache=LruCache(64),
    )
    await route.rerank("问题一", ["候选"], top_n=1)
    await route.rerank("问题二", ["候选"], top_n=1)
    assert len(reranker.batches) == 2


def test_lru_cache_evicts_the_least_recently_used() -> None:
    cache: LruCache[int] = LruCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # a 变成最近使用
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1 and cache.get("c") == 3
    assert len(cache) == 2
