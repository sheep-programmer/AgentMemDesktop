"""检索管线：RRF 融合、引用解析、上下文装配、降级行为。

覆盖 `docs/01-ARCHITECTURE.md` §5 的每一段：并行召回、融合、重排（含缺省与失败降级）、
L3 / L2 召回（Phase 3 之前它们都是空集，必须照样能跑）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentmem.config import ModelsConfig, Settings
from agentmem.errors import ProviderNotConfiguredError
from agentmem.ingest import IngestPipeline
from agentmem.prompts import PersonaSpec
from agentmem.prompts._shapes import Turn
from agentmem.providers.base import ChatChunk, ChatResult, Message, ToolSpec
from agentmem.providers.registry import LLMRoute, ProviderRegistry, UsageRecorder
from agentmem.retrieve import (
    RRF_K,
    CitationEmitter,
    CitationRegistry,
    CitationStreamParser,
    GenerationRegistry,
    QueryPlan,
    QueryRewriter,
    RetrievalPipeline,
    ScoredChunk,
    assign_markers,
    document_filter,
    looks_like_verbatim_request,
    marker_for,
    needs_contextualize,
    reciprocal_rank_fusion,
    text_similarity,
)
from agentmem.retrieve.context import to_turns
from agentmem.store import Database
from agentmem.types import (
    ChunkCreate,
    Document,
    DocumentCreate,
    EntityCreate,
    Insight,
    InsightCreate,
    InsightStatus,
    MessageRole,
    ProviderConfig,
    RetrievalSettings,
    RoleBindings,
    RoleName,
    Space,
    SpaceCreate,
    VectorRecord,
)
from agentmem.types import (
    Message as MessageModel,
)

EMBED_DIM = 8

#: 指向死端口的 base_url，用来验证 provider 挂掉时的降级
DEAD_BASE_URL = "http://127.0.0.1:1/v1"

SAMPLE_A = (
    "# 第3章 脱壳\n\n加固 APK 的脱壳流程：先定位 DexClassLoader 调用点，再 dump 内存中的 dex。"
)
SAMPLE_B = "# 附录 加固厂商\n\n常见的加固厂商包括梆梆、爱加密、腾讯乐固。"


# ---------------------------------------------------------------------------
# RRF 融合
# ---------------------------------------------------------------------------


def test_rrf_sums_reciprocal_ranks() -> None:
    """分数等于各路 1/(k+rank) 之和，且名次从 1 开始。"""
    fused = reciprocal_rank_fusion({"vec": ["x", "y"], "fts": ["y", "z"]}, k=60)
    scores = {hit.id: hit.score for hit in fused}
    assert scores["x"] == pytest.approx(1 / 61)
    assert scores["y"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["z"] == pytest.approx(1 / 62)


def test_rrf_orders_by_score_and_breaks_ties_by_first_seen() -> None:
    """按融合分降序；同分时先出现的排前面（排序稳定，结果可复现）。"""
    fused = reciprocal_rank_fusion({"vec": ["x", "y"], "fts": ["y", "x"]}, k=60)
    assert [hit.id for hit in fused] == ["x", "y"]
    assert fused[0].score == pytest.approx(fused[1].score)


def test_rrf_records_each_source_rank() -> None:
    """保留各路名次，便于调试面板展示「向量第几、BM25 第几」。"""
    fused = reciprocal_rank_fusion({"vec": ["a", "b"], "fts": ["b", "a"]})
    by_id = {hit.id: hit for hit in fused}
    assert by_id["a"].ranks == {"vec": 1, "fts": 2}
    assert by_id["b"].ranks == {"vec": 2, "fts": 1}


def test_rrf_ignores_duplicate_ids_within_one_source() -> None:
    """同一路里重复出现只计首次名次，避免刷分。"""
    fused = reciprocal_rank_fusion({"vec": ["a", "a", "a"]})
    assert len(fused) == 1
    assert fused[0].id == "a"
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[0].ranks == {"vec": 1}


def test_rrf_larger_k_flattens_head_advantage() -> None:
    """k 越大，头名优势越小。"""
    small = reciprocal_rank_fusion({"vec": ["a", "b"]}, k=1)
    large = reciprocal_rank_fusion({"vec": ["a", "b"]}, k=1000)
    assert small[0].score - small[1].score > large[0].score - large[1].score
    assert RRF_K == 60


def test_rrf_weights_and_empty_input() -> None:
    """权重按来源生效；空输入返回空列表。"""
    weighted = reciprocal_rank_fusion({"vec": ["a"], "fts": ["b"]}, weights={"vec": 2.0})
    scores = {hit.id: hit.score for hit in weighted}
    assert scores["a"] == pytest.approx(2 / 61)
    assert scores["b"] == pytest.approx(1 / 61)
    assert reciprocal_rank_fusion({}) == []


def test_rrf_rejects_non_positive_k() -> None:
    """k 必须为正，否则会除零。"""
    with pytest.raises(ValueError):
        reciprocal_rank_fusion({"vec": ["a"]}, k=0)


# ---------------------------------------------------------------------------
# 引用解析
# ---------------------------------------------------------------------------


def test_marker_format() -> None:
    """标记形如 c1 / c12。"""
    assert marker_for(1) == "c1"
    assert marker_for(12) == "c12"


def test_citation_parser_single_chunk() -> None:
    """一片里出现完整标记时正文与标记分开输出。"""
    feed = CitationStreamParser().feed("脱壳要点[^c1]。")
    assert feed.text == "脱壳要点。"
    assert feed.markers == ["c1"]


def test_citation_parser_survives_chunk_boundary() -> None:
    """标记被切成多片到达也不漏检、不重复发。"""
    parser = CitationStreamParser()
    first = parser.feed("答案[^c")
    second = parser.feed("3]。继续")
    assert first.text == "答案"
    assert first.markers == []
    assert second.text == "。继续"
    assert second.markers == ["c3"]


def test_citation_parser_reports_positions() -> None:
    """标记的位置必须在剥离后的正文坐标里准确。

    这一定位是「带引用的句子 / 总句子」唯一的依据：正文里没有标记，
    只有位置能说明哪一句有出处。
    """
    parser = CitationStreamParser()
    first = parser.feed("结论一[^c1]。结论二")
    second = parser.feed("[^c2]。")

    assert first.text == "结论一。结论二"
    assert first.offsets == [3], "第一个标记在第 3 个字符处被剥离"
    assert second.text == "。"
    assert second.offsets == [0], "位置是相对本次输出的，调用方加上已输出字数即可"


def test_citation_parser_positions_survive_char_by_char() -> None:
    """逐字符到达时位置同样准确。"""
    parser = CitationStreamParser()
    text = ""
    absolute: list[int] = []
    for char in "甲[^c1]乙":
        base = len(text)  # 调用方（对话链路）就是这样换算绝对位置的
        feed = parser.feed(char)
        text += feed.text
        absolute.extend(base + offset for offset in feed.offsets)

    assert text == "甲乙"
    assert absolute == [1]


def test_citation_parser_handles_worst_case_split() -> None:
    """逐字符到达（最坏情况）也必须完整识别。"""
    parser = CitationStreamParser()
    text = ""
    markers: list[str] = []
    for char in "结论[^c12]与[^c3]":
        feed = parser.feed(char)
        text += feed.text
        markers.extend(feed.markers)
    assert markers == ["c12", "c3"]
    assert text == "结论与"


def test_citation_parser_keeps_plain_brackets() -> None:
    """普通方括号不是标记，必须原样输出。"""
    parser = CitationStreamParser()
    feed = parser.feed("见[1]与[^1]与[注]")
    assert feed.text == "见[1]与[^1]与[注]"
    assert feed.markers == []


def test_citation_parser_flushes_incomplete_marker() -> None:
    """流结束时未闭合的标记按普通正文输出，不做臆测补全。"""
    parser = CitationStreamParser()
    assert parser.feed("半截[^c").text == "半截"
    assert parser.pending == "[^c"
    assert parser.flush() == "[^c"
    assert parser.pending == ""
    assert parser.flush() == ""


def test_citation_parser_ignores_empty_chunk() -> None:
    """空增量不产生任何输出。"""
    feed = CitationStreamParser().feed("")
    assert (feed.text, feed.markers) == ("", [])


def test_citation_emitter_drops_hallucinated_markers() -> None:
    """模型编出来的编号要丢弃，不能写进 citations。"""
    registry = CitationRegistry()
    registry.add("c1", _chunk("chunk-1"))
    emitter = CitationEmitter(registry)

    accepted = emitter.accept(["c1", "c9", "c1"])
    assert [item.marker for item in accepted] == ["c1"]
    assert emitter.emitted == ["c1"]
    assert len(registry) == 1


def test_citation_registry_maps_marker_to_chunk() -> None:
    """注册表把标记映射回 chunk / 文档 / 页码，并带片段。"""
    registry = CitationRegistry()
    registry.add("c1", _chunk("chunk-1", page=3, content="加固 APK 的脱壳流程"))
    citation = registry.get("c1")
    assert citation is not None
    assert citation.chunk_id == "chunk-1"
    assert citation.document_id == "doc-1"
    assert citation.page == 3
    assert citation.snippet is not None and citation.snippet.startswith("加固 APK")
    assert registry.get("c2") is None
    assert registry.known() == frozenset({"c1"})
    assert [item.marker for item in registry.ordered()] == ["c1"]


def test_assign_markers_follows_evidence_order() -> None:
    """marker 按证据顺序分配，先出现的先编号。"""
    refs, registry = assign_markers([_chunk("c-a"), _chunk("c-b")])
    assert [ref["marker"] for ref in refs] == ["c1", "c2"]
    assert refs[0]["chunk_id"] == "c-a"
    assert [item.chunk_id for item in registry.ordered()] == ["c-a", "c-b"]


def test_document_filter_escapes_values() -> None:
    """拼进 LanceDB 过滤表达式的值必须被转义。"""
    assert document_filter(None) is None
    assert document_filter([]) is None
    assert document_filter(["doc-1"]) == "document_id IN ('doc-1')"
    assert document_filter(["a'b"]) == "document_id IN ('a''b')"


def test_to_turns_keeps_recent_dialogue() -> None:
    """历史只保留 user / assistant，并按最近若干轮截断。"""
    messages = [
        _message("system", "系统提示"),
        _message("user", "问题一"),
        _message("assistant", "回答一"),
        _message("user", "问题二"),
    ]
    turns = to_turns(messages, limit=2)
    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("assistant", "回答一"),
        ("user", "问题二"),
    ]


# ---------------------------------------------------------------------------
# 查询改写
# ---------------------------------------------------------------------------


class _FakeLLM:
    """最小 LLM：按顺序吐出预设文本，用来驱动改写解析。"""

    name = "fake-llm"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        del tools, temperature, max_tokens
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else ""
        return ChatResult(content=text, model="fake", provider_id=self.name, latency_ms=0)

    async def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]:
        """不产出任何分片的流：空的 for 让它成为异步生成器，且不留死代码。"""
        del messages, kw
        empty: tuple[ChatChunk, ...] = ()
        for chunk in empty:  # pragma: no cover - 永远不会迭代到
            yield chunk


class _FakeRegistry:
    """只提供 ``llm()`` 的注册表替身，满足 :class:`LLMRoleSource`。"""

    def __init__(self, llm: _FakeLLM) -> None:
        self.route = LLMRoute("fast", [llm], UsageRecorder(None))

    def llm(self, role: RoleName = "chat", *, purpose: str | None = None) -> LLMRoute:
        del role, purpose
        return self.route


class _BrokenRegistry:
    """每次取角色都抛错，用于验证改写失败时的降级。"""

    def llm(self, role: RoleName = "chat", *, purpose: str | None = None) -> LLMRoute:
        del purpose
        raise ProviderNotConfiguredError(role)


async def test_rewriter_contextualizes_reference() -> None:
    """多轮追问被补全成可独立检索的问题。"""
    llm = _FakeLLM(['{"query": "Frida 的 spawn 模式如何绕过反调试", "changed": true}'])
    rewriter = QueryRewriter(_FakeRegistry(llm))
    rewritten = await rewriter.contextualize(
        question="它呢？", history=[{"role": "user", "content": "Frida 怎么用？"}]
    )
    assert rewritten == "Frida 的 spawn 模式如何绕过反调试"
    assert llm.calls, "应当真的发起了改写请求"


async def test_rewriter_skips_without_history() -> None:
    """没有历史就不该发请求——省一次模型调用。"""
    llm = _FakeLLM(['{"query": "改写后", "changed": true}'])
    rewriter = QueryRewriter(_FakeRegistry(llm))
    assert await rewriter.contextualize(question="脱壳", history=[]) is None
    assert llm.calls == []


async def test_rewriter_treats_unchanged_and_invalid_json_as_none() -> None:
    """模型说没变、或返回的不是 JSON，都退回原问题。"""
    history: list[Any] = [{"role": "user", "content": "前情"}]
    unchanged = QueryRewriter(_FakeRegistry(_FakeLLM(['{"query": "脱壳", "changed": false}'])))
    assert await unchanged.contextualize(question="脱壳", history=history) is None

    invalid = QueryRewriter(_FakeRegistry(_FakeLLM(["我觉得不用改写"])))
    assert await invalid.contextualize(question="脱壳", history=history) is None

    same = QueryRewriter(_FakeRegistry(_FakeLLM(['{"query": "脱壳", "changed": true}'])))
    assert await same.contextualize(question="脱壳", history=history) is None


async def test_rewriter_degrades_when_provider_unavailable() -> None:
    """provider 取不到时改写失败但不抛错，检索链继续。"""
    rewriter = QueryRewriter(_BrokenRegistry())
    history: list[Any] = [{"role": "user", "content": "前情"}]
    assert await rewriter.contextualize(question="脱壳", history=history) is None
    assert await rewriter.expand(persona=_persona(), question="脱壳") == []
    assert await rewriter.hyde(persona=_persona(), question="脱壳") is None


async def test_rewriter_expand_and_hyde() -> None:
    """扩展去重去空、HyDE 返回纯文本。"""
    llm = _FakeLLM(
        [
            '{"queries": ["判断加固类型", "常见脱壳工具", "判断加固类型", ""]}',
            "假想的答案段落。",
        ]
    )
    rewriter = QueryRewriter(_FakeRegistry(llm))
    variants = await rewriter.expand(persona=_persona(), question="怎么脱壳")
    assert variants == ["判断加固类型", "常见脱壳工具"]
    assert await rewriter.hyde(persona=_persona(), question="怎么脱壳") == "假想的答案段落。"


def test_query_plan_derives_search_texts() -> None:
    """改写产物取代原问题；HyDE 只进向量召回，不进全文召回。"""
    plan = QueryPlan(
        original="它呢？", rewritten="Frida spawn", variants=["Frida hook"], hyde="假想答案"
    )
    assert plan.primary == "Frida spawn"
    assert plan.search_queries == ["Frida spawn", "Frida hook"]
    assert plan.vector_queries == ["假想答案", "Frida hook"]

    plain = QueryPlan(original="脱壳")
    assert plain.primary == "脱壳"
    assert plain.search_queries == ["脱壳"]
    assert plain.vector_queries == ["脱壳"]


# ---------------------------------------------------------------------------
# 检索管线
# ---------------------------------------------------------------------------


async def test_search_hybrid_fuses_vector_and_fts(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """混合检索命中两路证据，重排生效，并带回各路分数。"""
    registry = _registry(database, mock_server=mock_server, rerank_base=mock_server)
    space, documents = await _seed(
        database, settings, registry, {"脱壳笔记": SAMPLE_A, "厂商附录": SAMPLE_B}
    )
    pipeline = _pipeline(database, registry, settings, space_id=space.id)

    outcome = await pipeline.search("脱壳流程")
    assert outcome.mode == "hybrid"
    assert outcome.reranked is True
    assert outcome.chunks, "应当检到证据"

    for chunk in outcome.chunks:
        assert chunk.rrf is not None
        assert chunk.vec_score is not None or chunk.bm25_score is not None
    assert outcome.chunks[0].rerank_score is not None
    assert outcome.chunks[0].document_id in {doc.id for doc in documents}


async def test_search_skips_rerank_when_role_missing(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """没配 rerank provider 时直接取 RRF 前 N 条，不报错。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)

    outcome = await pipeline.search("脱壳")
    assert outcome.reranked is False
    assert outcome.chunks
    assert all(chunk.rerank_score is None for chunk in outcome.chunks)
    assert outcome.chunks[0].rrf is not None


async def test_search_degrades_when_rerank_provider_fails(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """rerank 服务连不上时回退 RRF 顺序，检索本身不失败。"""
    registry = _registry(database, mock_server=mock_server, rerank_base=DEAD_BASE_URL)
    space, _ = await _seed(database, settings, registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, registry, settings, space_id=space.id)

    outcome = await pipeline.search("脱壳")
    assert outcome.reranked is False
    assert outcome.chunks
    assert outcome.chunks[0].rrf is not None
    assert all(chunk.rerank_score is None for chunk in outcome.chunks)


async def _rerank_candidates(
    database: Database,
    settings: Settings,
    mock_server: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    params: RetrievalSettings | None = None,
) -> list[str]:
    """灌三份互为近重复的转载，返回真正被送去重排打分的候选正文。"""
    registry = _registry(database, mock_server=mock_server, rerank_base=mock_server)
    space, _ = await _seed(
        database,
        settings,
        registry,
        {
            "脱壳笔记": SAMPLE_A,
            "某论坛转载": f"{SAMPLE_A}（转载自某论坛）",
            "某公众号转载": f"{SAMPLE_A}（转载自某公众号）",
        },
    )
    pipeline = _pipeline(database, registry, settings, space_id=space.id, params=params)

    scored: list[list[str]] = []
    original = pipeline._rerank

    async def spy(
        query: str, candidates: list[ScoredChunk], top_n: int
    ) -> list[ScoredChunk] | None:
        scored.append([chunk.content for chunk in candidates])
        return await original(query, candidates, top_n)

    monkeypatch.setattr(pipeline, "_rerank", spy)
    outcome = await pipeline.search("脱壳流程")

    assert outcome.chunks, "应当检到证据"
    assert scored, "这一问应当走到重排"
    return scored[0]


async def test_rerank_is_not_asked_to_score_near_duplicates(
    database: Database, settings: Settings, mock_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一段话被转载三份时，只有一条进重排：近重复在打分之前就被压掉。

    重排是整条链路里最贵的一步，而这些条目在 `_diversify` 里用同一个阈值一样会被丢掉，
    先给它们打分纯属白花算力。
    """
    contents = await _rerank_candidates(database, settings, mock_server, monkeypatch)
    threshold = RetrievalSettings().dedup_threshold
    duplicates = [
        (left, right)
        for index, left in enumerate(contents)
        for right in contents[index + 1 :]
        if text_similarity(left, right) > threshold
    ]
    assert not duplicates, f"近重复被送去打分了：{duplicates}"


async def test_rerank_sees_every_candidate_when_diversity_is_off(
    database: Database, settings: Settings, mock_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 diversity 就不做任何提前丢弃：那时 `_diversify` 也不去重，提前丢会改变结果。"""
    contents = await _rerank_candidates(
        database,
        settings,
        mock_server,
        monkeypatch,
        params=RetrievalSettings(diversity=False),
    )
    threshold = RetrievalSettings().dedup_threshold
    assert sum(text_similarity(SAMPLE_A, content) > threshold for content in contents) == 3


async def test_search_modes_keep_their_own_scores(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """纯向量 / 纯全文模式下，展示的分数就是该路自己的分数。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)

    vector = await pipeline.search("脱壳", mode="vector")
    assert vector.chunks
    assert all(chunk.vec_score is not None for chunk in vector.chunks)
    assert all(chunk.bm25_score is None for chunk in vector.chunks)
    assert vector.chunks[0].score == vector.chunks[0].vec_score

    fts = await pipeline.search("脱壳", mode="fts")
    assert fts.chunks
    assert all(chunk.bm25_score is not None for chunk in fts.chunks)
    assert all(chunk.vec_score is None for chunk in fts.chunks)
    assert fts.chunks[0].score == fts.chunks[0].bm25_score


async def test_search_without_embedding_role_falls_back_to_fts(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """未绑定 embedding 角色时退化为纯全文检索。"""
    embedded = _registry(database, mock_server=mock_server)
    space, _ = await _seed(database, settings, embedded, {"脱壳笔记": SAMPLE_A})

    registry = _registry(database, mock_server=mock_server, embedding=False)
    pipeline = _pipeline(database, registry, settings, space_id=space.id)
    outcome = await pipeline.search("脱壳")
    assert outcome.chunks
    assert all(chunk.vec_score is None for chunk in outcome.chunks)
    assert outcome.chunks[0].bm25_score is not None


async def test_search_respects_document_scope(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """附件范围只在指定文档内检索。"""
    space, documents = await _seed(
        database, settings, mock_registry, {"脱壳笔记": SAMPLE_A, "厂商附录": SAMPLE_B}
    )
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)

    only_b = await pipeline.search("加固", document_ids=[documents[1].id])
    assert only_b.chunks
    assert {chunk.document_id for chunk in only_b.chunks} == {documents[1].id}


async def test_search_top_k_limits_hits(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """top_k 决定最终返回条数。"""
    space, _ = await _seed(
        database, settings, mock_registry, {"脱壳笔记": SAMPLE_A, "厂商附录": SAMPLE_B}
    )
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)
    outcome = await pipeline.search("加固 脱壳", top_k=1)
    assert len(outcome.chunks) == 1


async def test_search_on_empty_space_returns_nothing(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """空 Space 不报错，返回空命中。"""
    space = await _seed_space(database)
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)
    outcome = await pipeline.search("脱壳")
    assert outcome.chunks == []
    assert outcome.reranked is False


async def test_retrieve_with_empty_l3_and_l2(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """L3 / L2 都还是空集（Phase 3 才产出）时，检索照常工作。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)

    result = await pipeline.retrieve("脱壳怎么弄？")
    assert result.chunks
    assert result.insights == []
    assert result.cards == []
    assert result.plan.original == "脱壳怎么弄？"
    assert result.plan.rewritten is None


async def test_retrieve_recalls_active_insights_without_vector_index(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """insights_vec 还是空的时候，按置信度兜底召回 active 经验。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    await _insight(
        database, space.id, trigger="遇到加固壳", guidance="先脱壳再分析", confidence=0.9
    )
    await _insight(
        database, space.id, trigger="低置信度", guidance="仅供参考", confidence=0.3, status="active"
    )
    await _insight(
        database,
        space.id,
        trigger="候选条目",
        guidance="尚未验证",
        confidence=0.95,
        status="candidate",
    )

    result = await _pipeline(database, mock_registry, settings, space_id=space.id).retrieve(
        "壳怎么脱"
    )
    assert [item.guidance for item in result.insights] == ["先脱壳再分析"]


async def test_retrieve_prefers_vector_matched_insights(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """有向量索引时按语义召回，且只取 active。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    active = await _insight(database, space.id, trigger="加固壳", guidance="先脱壳", confidence=0.9)
    archived = await _insight(
        database, space.id, trigger="旧经验", guidance="已归档", confidence=0.9, status="archived"
    )
    vectors = await database.require_vectors()
    await vectors.upsert(
        "insights_vec",
        [
            VectorRecord(
                id=item.id,
                space_id=space.id,
                vector=[0.1] * EMBED_DIM,
                embedding_model="mock-embed",
            )
            for item in (active, archived)
        ],
    )

    result = await _pipeline(database, mock_registry, settings, space_id=space.id).retrieve(
        "壳怎么脱"
    )
    assert [item.id for item in result.insights] == [active.id]


async def test_use_insights_false_skips_recall(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """``use_insights=false`` 必须真的不注入经验（Phase 3 的 A/B 靠它）。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    await _insight(database, space.id, trigger="加固壳", guidance="先脱壳", confidence=0.9)
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)

    with_insights = await pipeline.retrieve("壳怎么脱")
    without = await pipeline.retrieve("壳怎么脱", use_insights=False)
    assert with_insights.insights
    assert without.insights == []


async def test_retrieve_uses_rewritten_query_for_recall(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """给了历史就会尝试改写，改写产物参与检索并记录进 plan。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)
    llm = _FakeLLM(['{"query": "Frida 动态脱壳流程", "changed": true}'])
    pipeline.rewriter = QueryRewriter(_FakeRegistry(llm))

    history: list[Any] = [{"role": "user", "content": "Frida 怎么用？"}]
    result = await pipeline.retrieve("它呢？", history=history)
    assert result.plan.rewritten == "Frida 动态脱壳流程"
    assert result.plan.primary == "Frida 动态脱壳流程"
    assert llm.calls


async def test_hyde_only_feeds_vector_recall(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """HyDE 的假想答案是编出来的，只能用于召回，绝不能进上下文。"""
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(
        database,
        mock_registry,
        settings,
        space_id=space.id,
        params=RetrievalSettings(hyde=True),
    )
    pipeline.rewriter = QueryRewriter(_FakeRegistry(_FakeLLM(["假想答案：加固壳需要先脱壳。"])))

    result = await pipeline.retrieve("壳怎么脱")
    assert result.plan.hyde == "假想答案：加固壳需要先脱壳。"
    assert result.plan.vector_queries == ["假想答案：加固壳需要先脱壳。"]

    messages, _ = pipeline.assemble_context(result, question="壳怎么脱")
    assert "假想答案" not in messages[0].content


async def test_expand_variants_widen_recall(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """多查询扩展的产物真的参与召回：原问题检不到的资料靠扩展式检回来。"""
    embedded = _registry(database, mock_server=mock_server)
    space, documents = await _seed(
        database, settings, embedded, {"脱壳笔记": SAMPLE_A, "厂商附录": SAMPLE_B}
    )
    registry = _registry(database, mock_server=mock_server, embedding=False)
    pipeline = _pipeline(database, registry, settings, space_id=space.id)
    pipeline.rewriter = QueryRewriter(_FakeRegistry(_FakeLLM(['{"queries": ["加固厂商"]}'])))

    plain = await pipeline.retrieve("脱壳")
    expanded = await pipeline.retrieve("脱壳", expand=True)

    assert {chunk.document_id for chunk in plain.chunks} == {documents[0].id}
    assert expanded.plan.variants == ["加固厂商"]
    assert documents[1].id in {chunk.document_id for chunk in expanded.chunks}


async def test_assemble_context_maps_all_layers(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """四层上下文按 L4 → L3 → L2 → L1 装配，并给出 marker 映射。

    L4 人格留在跨轮稳定的 system 里，随问题变化的 L3/L2/L1 装进当轮 user 消息——
    这是前缀缓存的前提，详见 `tests/test_prompt_cache.py`。
    """
    space, _ = await _seed(database, settings, mock_registry, {"脱壳笔记": SAMPLE_A})
    insight = await _insight(
        database, space.id, trigger="加固壳", guidance="先脱壳", confidence=0.9
    )
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)
    result = await pipeline.retrieve("脱壳")
    result.insights = [insight]

    messages, registry = pipeline.assemble_context(result, question="脱壳")
    system = messages[0].content
    turn = messages[-1].content
    assert messages[0].role == "system"
    assert messages[-1].role == "user"
    assert turn.index("learned_insights") < turn.index("retrieved_context")
    assert "先脱壳" in turn
    assert 'marker="c1"' in turn
    assert "learned_insights" not in system and "retrieved_context" not in system
    assert registry.get("c1") is not None
    assert pipeline.retrieved_items(result)[0].rrf is not None


async def test_assemble_context_without_evidence_is_persona_only(
    database: Database, settings: Settings, mock_registry: ProviderRegistry
) -> None:
    """纯聊天模式（无证据）也装配得出来，且不出现证据块。

    注意引用规则**仍留在 system 里**且措辞不变：它跨轮稳定才能被前缀缓存复用，
    所以改成了「提供了资料时…」这种同时适用于两种模式的写法，
    而不是按本轮有没有证据切换。
    """
    space = await _seed_space(database)
    pipeline = _pipeline(database, mock_registry, settings, space_id=space.id)
    result = await pipeline.retrieve("脱壳")
    messages, registry = pipeline.assemble_context(result, question="脱壳")
    assert len(registry) == 0
    assert "retrieved_context" not in messages[0].content
    assert "retrieved_context" not in messages[-1].content
    assert "本轮若未提供任何资料" in messages[0].content


async def test_to_search_response_maps_debug_fields(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """调试面板需要的字段一个都不能少。"""
    registry = _registry(database, mock_server=mock_server, rerank_base=mock_server)
    space, _ = await _seed(database, settings, registry, {"脱壳笔记": SAMPLE_A})
    pipeline = _pipeline(database, registry, settings, space_id=space.id)
    response = pipeline.to_search_response(await pipeline.search("脱壳"))

    assert response.mode == "hybrid"
    assert response.latency_ms is not None
    hit = response.hits[0]
    assert hit.chunk_id
    assert hit.document_id
    assert hit.document_title == "脱壳笔记"
    assert hit.snippet
    assert hit.vec_score is not None
    assert hit.bm25_score is not None
    assert hit.rrf is not None
    assert hit.rerank_score is not None


# ---------------------------------------------------------------------------
# 生成登记表
# ---------------------------------------------------------------------------


async def test_generation_registry_cancels_task() -> None:
    """登记在册的任务可以被取消，取消后状态归位。"""
    registry = GenerationRegistry()
    started = asyncio.Event()

    async def long_running() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(long_running())
    await started.wait()
    registry.register("conv-1", task)
    assert registry.is_running("conv-1") is True
    assert registry.active_count() == 1

    assert registry.cancel("conv-1") is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.cancel("conv-1") is False
    registry.discard("conv-1", task)
    assert registry.active_count() == 0


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _chunk(
    chunk_id: str, *, page: int | None = None, content: str = "加固 APK 的脱壳流程"
) -> ScoredChunk:
    """构造一条命中。"""
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        document_title="脱壳笔记",
        page=page,
        content=content,
    )


def _message(role: MessageRole, content: str) -> MessageModel:
    """构造一条落库形态的消息（用于历史轮次）。"""
    return MessageModel(
        id=f"msg-{role}-{content}",
        conversation_id="conv-1",
        role=role,
        content=content,
        created_at=0,
    )


def _persona() -> PersonaSpec:
    """构造一个最小的 Persona 形状（Prompt 层入参）。"""
    return PersonaSpec(name="专家", domain="Android 逆向")


def _config(
    *, mock_server: str | None, rerank_base: str | None = None, embedding: bool = True
) -> ModelsConfig:
    """构造模型配置：llm / embedding / rerank 三个角色独立开关。"""
    providers = [
        ProviderConfig(
            id="mock-llm",
            kind="llm",
            adapter="openai_compatible",
            base_url=mock_server,
            model="mock-chat",
        )
    ]
    roles = RoleBindings(chat="mock-llm", fast="mock-llm")
    if embedding:
        providers.append(
            ProviderConfig(
                id="mock-embed",
                kind="embedding",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-embed",
                dimension=EMBED_DIM,
            )
        )
        roles.embedding = "mock-embed"
    if rerank_base is not None:
        providers.append(
            ProviderConfig(
                id="mock-rerank",
                kind="rerank",
                adapter="cohere_rerank",
                base_url=rerank_base,
                model="mock-rerank",
            )
        )
        roles.rerank = "mock-rerank"
    return ModelsConfig(providers=providers, roles=roles)


def _registry(
    database: Database,
    *,
    mock_server: str | None,
    rerank_base: str | None = None,
    embedding: bool = True,
) -> ProviderRegistry:
    """按需构造注册表。"""
    return ProviderRegistry(
        _config(mock_server=mock_server, rerank_base=rerank_base, embedding=embedding),
        usage=database.usage,
        space_id=database.space_id,
    )


def _pipeline(
    database: Database,
    registry: ProviderRegistry,
    settings: Settings,
    *,
    space_id: str | None = None,
    params: RetrievalSettings | None = None,
) -> RetrievalPipeline:
    """构造检索管线。"""
    return RetrievalPipeline(
        space_id=space_id or database.space_id or "",
        database=database,
        registry=registry,
        settings=settings,
        retrieval=params or RetrievalSettings(),
    )


async def _seed_space(database: Database) -> Space:
    """建一条 Space 记录（满足 documents 的外键）。"""
    return await database.spaces.create(SpaceCreate(name="逆向", domain="Android 逆向工程"))


async def _seed(
    database: Database,
    settings: Settings,
    registry: ProviderRegistry,
    items: dict[str, str],
) -> tuple[Space, list[Document]]:
    """写入若干文档并跑完摄取（切片 + 全文索引 + 向量）。"""
    space = await _seed_space(database)
    pipeline = IngestPipeline(database, registry, settings)
    documents: list[Document] = []
    for title, content in items.items():
        document = await pipeline.register_text(space_id=space.id, title=title, content=content)
        await pipeline.run(document.id)
        documents.append(document)
    return space, documents


async def _insight(
    database: Database,
    space_id: str,
    *,
    trigger: str,
    guidance: str,
    confidence: float,
    status: InsightStatus = "active",
) -> Insight:
    """写入一条经验条目。"""
    return await database.insights.create(
        InsightCreate(
            space_id=space_id,
            trigger=trigger,
            guidance=guidance,
            kind="heuristic",
            status=status,
            confidence=confidence,
            origin="manual",
        )
    )


# ---------------------------------------------------------------------------
# 意图判别：这次要不要注入 L3 经验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "把第三章的原文贴给我",
        "这段话的原文是什么？",
        "摘录一下第二节的内容",
        "第 3 节的正文写了什么",
        "请一字不差地引用那条结论",
        "quote the paragraph about hERG",
        "verbatim excerpt please",
    ],
)
def test_verbatim_requests_are_detected(question: str) -> None:
    """索取原文的问法要认出来：这类问题没有「场景」，经验只会占预算。"""
    assert looks_like_verbatim_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "评估先导化合物成药性时还要看什么？",
        "原文里提到的方法，实际应该怎么用？",  # 一边要原文一边问做法：经验仍然有用
        "第三章讲了什么",  # 概括而非索取原文
        "这个化合物值得推进吗",
        "帮我总结一下 hERG 相关的风险",
        "",
        "   ",
    ],
)
def test_non_verbatim_requests_keep_insights(question: str) -> None:
    """判错的代价不对称：宁可漏判（多注入几条），也不要误判让该遵守的规则缺席。"""
    assert looks_like_verbatim_request(question) is False


async def test_verbatim_question_skips_insight_injection(
    settings: Settings, tmp_path: Path
) -> None:
    """真接进管线：同一个库里，普通问题会注入经验，索取原文的问题不会。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
        await database.insights.create(
            InsightCreate(
                space_id=space.id,
                trigger="遇到高活性先导化合物时",
                guidance="先评估 ADMET 五项再给结论",
                kind="heuristic",
                status="active",
                confidence=0.9,
                origin="manual",
            )
        )
        registry = ProviderRegistry(
            ModelsConfig(providers=[], roles={}),
        )
        pipeline = RetrievalPipeline(
            space_id=space.id, database=database, registry=registry, settings=settings
        )

        # 没有 embedding 时经验按词面兜底召回，普通问题要和经验的场景有共同的词
        normal = await pipeline.retrieve("高活性先导化合物要先看什么？")
        verbatim = await pipeline.retrieve("把第三章的原文贴给我")

        assert [item.guidance for item in normal.insights], "普通问题照常注入"
        assert verbatim.insights == [], "索取原文时不注入，省下的预算留给证据"
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# 图谱召回：图上相邻的切片要能被捞回来
# ---------------------------------------------------------------------------


async def test_graph_expansion_pulls_in_neighbouring_chunks(
    settings: Settings, tmp_path: Path
) -> None:
    """问题里点到的实体，它在图上相邻的切片要进候选池。

    向量与全文都按「这段文字像不像问题」排序：问「还有哪些和 hERG 协同」时，
    讲的是别的化合物、字面不像的切片进不了 top-N，而图上明明与 hERG 相连。
    默认关闭（会改变检索结果，应当先 A/B 证明有效），开关打开时这条路才参与融合。
    """
    from agentmem.types import RelationCreate

    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="讲义",
                source_type="paste",
                mime="text/markdown",
                sha256="a" * 64,
                size_bytes=3,
            )
        )
        # 两条切片：一条讲 hERG，另一条只讲 CYP3A4（字面与问题无关），
        # 但它们通过关系相连
        herg, cyp = await database.chunks.create_many(
            [
                ChunkCreate(
                    space_id=space.id,
                    document_id=document.id,
                    ordinal=0,
                    content="hERG 阻断会延长 QT 间期，需要按游离浓度评估。",
                    char_start=0,
                    char_end=26,
                    token_count=18,
                ),
                ChunkCreate(
                    space_id=space.id,
                    document_id=document.id,
                    ordinal=1,
                    content="CYP3A4 抑制会抬高联用药物的暴露量，注意窄治疗窗。",
                    char_start=27,
                    char_end=56,
                    token_count=20,
                ),
            ]
        )
        herg_entity = await database.entities.create(
            EntityCreate(space_id=space.id, name="hERG", type="靶点")
        )
        cyp_entity = await database.entities.create(
            EntityCreate(space_id=space.id, name="CYP3A4", type="酶")
        )
        await database.relations.create(
            RelationCreate(
                space_id=space.id,
                src_id=herg_entity.id,
                dst_id=cyp_entity.id,
                predicate="共同影响",
                weight=0.9,
                source_chunks=[cyp.id],
            )
        )

        registry = ProviderRegistry(ModelsConfig(providers=[], roles={}))
        off = RetrievalPipeline(
            space_id=space.id,
            database=database,
            registry=registry,
            settings=settings,
            retrieval=RetrievalSettings(graph_expansion=False),
        )
        on = RetrievalPipeline(
            space_id=space.id,
            database=database,
            registry=registry,
            settings=settings,
            retrieval=RetrievalSettings(graph_expansion=True),
        )
        question = "还有哪些和 hERG 一起要考虑的因素？"

        without = await off.retrieve(question, mode="fts")
        with_graph = await on.retrieve(question, mode="fts")

        assert cyp.id not in {chunk.chunk_id for chunk in without.chunks}, "前提：字面召回捞不到"
        assert cyp.id in {chunk.chunk_id for chunk in with_graph.chunks}, "图谱路要把它带回来"
        assert herg.id in {chunk.chunk_id for chunk in with_graph.chunks}
    finally:
        await database.close()


async def test_retrieved_items_record_which_leg_found_them(
    settings: Settings, tmp_path: Path
) -> None:
    """轨迹要记下「这条证据是哪一路找到的」。

    调试「这条证据为什么会出现」比看它排第几更有用：只看综合分，分不清是向量语义
    召回还是全文关键词命中，也就不知道该改哪一路。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="讲义",
                source_type="paste",
                mime="text/markdown",
                sha256="b" * 64,
                size_bytes=3,
            )
        )
        await database.chunks.create_many(
            [
                ChunkCreate(
                    space_id=space.id,
                    document_id=document.id,
                    ordinal=0,
                    content="hERG 阻断会延长 QT 间期，按游离浓度评估。",
                    char_start=0,
                    char_end=25,
                    token_count=18,
                )
            ]
        )
        registry = ProviderRegistry(ModelsConfig(providers=[], roles={}))
        pipeline = RetrievalPipeline(
            space_id=space.id, database=database, registry=registry, settings=settings
        )

        result = await pipeline.retrieve("hERG 阻断", mode="fts")
        items = pipeline.retrieved_items(result)

        assert items, "全文命中应该至少有一条"
        assert any(item.legs for item in items), "每条命中都要标出召回路径"
        assert all(
            leg.startswith(("fts", "vector", "graph")) for item in items for leg in item.legs
        )
    finally:
        await database.close()


@pytest.mark.parametrize(
    "question",
    [
        "那它的半衰期呢？",
        "这个化合物能不能推进？",
        "那么溶解度呢",
        "它的推荐剂量是多少",
        "CMPD-045 的选择性指数呢？",
    ],
)
def test_questions_that_need_context_are_detected(question: str) -> None:
    """依赖上文的问题必须改写：漏判会让指代没被补全、检索跑偏。"""
    assert needs_contextualize(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "心脏毒性怎么评估？",
        "CMPD-045 的活性是多少？",
        "第 3 节的结论是什么",
        "溶解度阈值是多少？",
        "",
    ],
)
def test_self_contained_questions_skip_the_rewrite(question: str) -> None:
    """自足的问题不改写：改写是一次完整模型调用，实测 4.7 秒。

    不用「句子短就改写」这种长度启发——实测「心脏毒性怎么评估？」（9 字）会被误判。
    """
    assert needs_contextualize(question) is False


async def test_pipeline_skips_the_rewrite_for_self_contained_questions(
    settings: Settings, tmp_path: Path
) -> None:
    """真接进管线：自足的问题不发改写调用，依赖上文的才发。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
        registry = MagicMock()
        pipeline = RetrievalPipeline(
            space_id=space.id, database=database, registry=registry, settings=settings
        )
        history: list[Turn] = [{"role": "user", "content": "CMPD-045 的活性是多少？"}]

        await pipeline.plan("心脏毒性怎么评估？", history=history)
        assert not registry.llm.called, "自足的问题不该发改写调用"

        await pipeline.plan("那它的半衰期呢？", history=history)
        assert registry.llm.called, "依赖上文的问题要改写"
    finally:
        await database.close()
