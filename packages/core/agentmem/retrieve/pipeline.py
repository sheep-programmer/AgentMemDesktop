"""混合检索管线：并行召回 → RRF 融合 → 重排 → 上下文装配。

对应 ``docs/01-ARCHITECTURE.md`` §5::

    Query
     ├─ 查询改写（指代补全 / 扩展 / HyDE，可开关）
     ├─ 并行召回  向量(chunks_vec) ‖ 全文(FTS5 BM25)
     ├─ RRF 融合
     ├─ Rerank（缺省时跳过）
     ├─ 冗余抑制 + MMR 多样性重排
     └─ 上下文装配（L4 + L3 + L2 + L1）

设计上刻意做成「任何一层缺失都能跑」：

- 没配 rerank provider → 直接用 RRF 顺序（仍然过多样性重排）；
- rerank 调用失败 → 记日志后回退 RRF 顺序，不把整次检索拖垮；
- 没绑定 embedding 角色 → 只跑全文检索；
- L3 / L2 还是空的（Phase 3 才产出）→ 上下文里少两层，回答照常。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from typing import Any

import structlog
from pydantic import Field

from agentmem.config import Settings, get_settings
from agentmem.errors import AgentMemError
from agentmem.prompts import Turn
from agentmem.prompts.compaction import ECONOMY_BUDGET, STANDARD_BUDGET, ContextMode
from agentmem.providers.base import EmbedKind
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.store.fts import tokenize
from agentmem.store.vectors import sql_literal
from agentmem.types import (
    AgentMemModel,
    Entity,
    Id,
    Insight,
    KnowledgeCard,
    Message,
    Persona,
    RetrievalSettings,
    SearchHit,
    SearchMode,
    SearchResponse,
    TraceRetrievedItem,
    VectorTableName,
)

from .automerge import merge_siblings
from .citations import CitationRegistry
from .context import AssembledContext, assemble_answer_context, to_persona_spec, to_retrieved_items
from .diversity import mmr_rerank, suppress_redundant
from .fusion import RRF_K, reciprocal_rank_fusion
from .intent import looks_like_verbatim_request, needs_contextualize
from .locate import Located, focus_hits, focused_snippet
from .models import SNIPPET_CHARS, QueryPlan, RetrievalResult, ScoredChunk, SearchOutcome
from .overlap import trim_adjacent_overlap
from .relevance import drop_covered_summaries, drop_low_relevance
from .rewriting import QueryRewriter

logger = structlog.get_logger(__name__)

#: L3 经验的最低注入置信度。低于此值说明它还没被验证过，不该影响回答
INSIGHT_MIN_CONFIDENCE = 0.5

#: 向量召回 L3 时的超采倍数：LanceDB 里没有 status 列，
#: 只能在回表后过滤 active，超采以避免过滤完不够数
INSIGHT_OVERFETCH = 4

#: 单次回答最多注入的 L2 卡片数（``space.yaml`` 只配了 max_insights，卡片用常量兜底）
MAX_CARDS = 4
CARD_OVERFETCH = 4

#: L2 卡片与最相似那张的相似度差距上限。卡片按向量近邻召回，不过重排，也没有任何
#: 相关性门槛——问「长江大学哪一年建立的」，最相似的卡片 0.66，其余三张是 0.39 的
#: EGFR 结构设计卡片，照样被塞进上下文。这里的分是向量召回分 ``1/(1+L2²)``
#: （见 ``store/vectors.py``，归一化向量下 0.66≈余弦 0.75、0.39≈余弦 0.22）。相关卡片
#: 彼此通常在 0.1 以内（本机 39 题里同一题前四张的极差多数不到 0.1），差出 0.2 的
#: 基本是另一个话题。
#:
#: 0.2 只挡得住**跨领域**的噪音。同一篇手册抽出的卡片彼此都沾边，分数挤在一起：
#: 问「电池模块质保多久」，质保卡 0.623，其余六张（E07、接线、安装环境……）
#: 0.457~0.508，全在 0.2 以内，于是每个回答都带着 3 张无关卡片，kimi-k3 还据此在
#: 质保回答里扯上了 E07 锁定规则。实测同领域干扰比首张低 0.105~0.115，
#: 而真正相关的第二张（问 E12 处理时的「E12 判定阈值」）只低 0.065，取 0.1。
CARD_SCORE_GAP = 0.1

#: L3 经验的向量召回分下限（同上的 ``1/(1+L2²)`` 刻度，0.45≈归一化向量余弦 0.39）。
#: 经验的条数通常很少，没有门槛时「召回前 N 条」等于「全部注入」：问长江大学也会带上
#: hERG 阈值与化合物编号两条经验（余弦 0.18~0.22）。本机 42 个问题实测，真正适用的
#: 经验余弦在 0.5 以上（「心脏毒性怎么判定」对 hERG 经验 0.70），取 0.39 留足余量——
#: 漏掉一条该遵守的规则比多注入一条代价大。
INSIGHT_MIN_SCORE = 0.45

#: 没有经验向量可用时（未绑定 embedding、索引尚未回填），用词面重合兜底。
#: 这些词在问题与经验里都常见，却说明不了「是同一个场景」。
#: 单字只认实义字（「壳」「脱」），这些虚字、量词、疑问字在全文检索的停用词表之外，
#: 但同样说明不了什么。
_GENERIC_TERMS = frozenset(
    {"用户", "询问", "回答", "涉及", "问题", "给出", "是否", "具体", "同时", "依据", "对应"}
    | {"怎么", "什么", "多少", "如何", "哪些", "为什么", "时候", "需要", "应该", "可以"}
    | set("时如等哪种度先再多少么怎什吗呢请能会要可该每各将个次")
)

#: 送入 rerank 的候选上限：重排是逐条打分的付费调用，必须封顶
#: 参与重排的候选数上限；Space 可覆盖（``RetrievalSettings.rerank_pool``）。
RERANK_INPUT_LIMIT = 50

#: 多样性重排的候选池倍数。去重要有得选，就得先拿到比最终条数更多的候选——
#: 只拿 limit 条的话，被丢掉的重复条目留下的空位无人可补，结果反而变短。
DIVERSITY_POOL_FACTOR = 2

#: 图谱扩展最多认几个实体、扫描多少条实体记录
GRAPH_ENTITY_LIMIT = 5
GRAPH_SCAN_LIMIT = 500

#: 每个实体往回捞多少条切片（再乘上召回上限）
GRAPH_CHUNK_FACTOR = 2

#: 图谱路给候选的原始分。它的作用只是让这些切片进入融合候选池并按名次参与 RRF，
#: 不是「置信度」——真正决定顺序的是融合与重排。
GRAPH_LEG_SCORE = 0.5


class _EmbeddedQueries(AgentMemModel):
    """各检索式对应的查询向量。"""

    primary: list[float] | None = None
    variants: list[list[float]] = Field(default_factory=list)
    hyde: list[float] | None = None

    def vector_legs(self) -> list[list[float]]:
        """参与向量召回的向量，顺序与 :attr:`QueryPlan.vector_queries` 一致。"""
        if self.primary is None:
            return []
        values = (
            [self.hyde, *self.variants]
            if self.hyde is not None
            else [
                self.primary,
                *self.variants,
            ]
        )
        return [item for item in values if item]


class _RecallLeg(AgentMemModel):
    """一路召回的名次列表（向量或某条检索式的全文）。"""

    name: str
    ids: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)


class RetrievalPipeline:
    """单 Space 的检索编排。

    Args:
        space_id: 所属 Space。
        database: 该 Space 的存储门面。
        registry: Provider 注册表（embedding / rerank / fast 三个角色）。
        settings: 全局设置。
        retrieval: 来自 ``space.yaml`` 的检索参数。
        persona: L4 画像，参与改写与上下文装配。
    """

    def __init__(
        self,
        *,
        space_id: str,
        database: Database,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        retrieval: RetrievalSettings | None = None,
        persona: Persona | None = None,
    ) -> None:
        self.space_id = space_id
        self.db = database
        self.registry = registry
        self.settings = settings or get_settings()
        self.params = retrieval or RetrievalSettings()
        self.persona = persona or Persona()
        self.rewriter = QueryRewriter(registry)
        self._sqlite_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def _sqlite(self) -> AsyncIterator[None]:
        """串行化本管线对 SQLite 的访问。

        检索会把多路召回并发跑起来，而这些召回基本都要读 SQLite。
        存储层的 ``SQLiteDatabase`` 只有自己的方法会加锁，``FtsIndex`` 是直接拿
        底层连接执行 SQL 的，两路全文召回同时跑就会互相踩到同一个连接——
        实测表现为 ``bm25()`` 返回 NULL、召回整路丢失。

        这把锁只覆盖 SQLite 部分：向量召回走 LanceDB、重排与 embedding 走网络，
        它们与 SQL 仍然是并行的。
        """
        async with self._sqlite_lock:
            yield

    # -- 查询改写 ---------------------------------------------------------

    async def plan(
        self,
        question: str,
        *,
        history: Sequence[Turn] = (),
        contextualize: bool = True,
        expand: bool = False,
    ) -> QueryPlan:
        """把问题加工成检索式（指代补全 / 多查询扩展 / HyDE）。"""
        spec = to_persona_spec(self.persona)
        plan = QueryPlan(original=question)
        # 只在问题真的依赖上文时才改写：改写是一次完整模型调用（实测 4.7 秒），
        # 自足的问题改写只是把同样的意思再说一遍
        if (
            contextualize
            and history
            and (not self.params.contextualize_when_needed or needs_contextualize(question))
        ):
            rewritten = await self.rewriter.contextualize(question=question, history=history)
            if rewritten is not None:
                plan.rewritten = rewritten
        if expand:
            plan.variants = await self.rewriter.expand(persona=spec, question=plan.primary)
        if self.params.hyde:
            plan.hyde = await self.rewriter.hyde(persona=spec, question=plan.primary)
        return plan

    # -- 纯检索 -----------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        mode: SearchMode = "hybrid",
        top_k: int | None = None,
        document_ids: Sequence[str] | None = None,
        plan: QueryPlan | None = None,
    ) -> SearchOutcome:
        """跑一遍检索，返回带各路分数的命中列表（不生成）。"""
        started = time.perf_counter()
        resolved = plan or QueryPlan(original=query)
        embedded = await self._embed_queries(resolved)
        chunks, reranked = await self._search_with(
            resolved,
            embedded,
            mode=mode,
            limit=top_k or self.params.top_n_rerank,
            document_ids=document_ids,
        )
        return SearchOutcome(
            query=resolved.primary,
            mode=mode,
            chunks=chunks,
            reranked=reranked,
            latency_ms=_elapsed_ms(started),
        )

    async def focus(self, query: str, chunks: list[ScoredChunk]) -> dict[str, Located]:
        """命中按问题收窄到具体句子（见 ``retrieve/locate.py``）；失败就不收窄。"""
        cache_dir = self.settings.data_dir / "cache" / self.space_id
        try:
            return await asyncio.to_thread(focus_hits, query, chunks, cache_dir)
        except Exception as exc:  # 锦上添花，不能拖垮检索
            logger.warning("retrieval_focus_failed", error=str(exc))
            return {}

    @staticmethod
    def to_search_response(
        outcome: SearchOutcome, focus: Mapping[str, Located] | None = None
    ) -> SearchResponse:
        """检索产物 → ``/spaces/{id}/search`` 响应体。

        ``focus`` 是 :meth:`focus` 的结果：给了就把章节与片段换成命中句所在的位置。
        """
        focus = focus or {}
        hits: list[SearchHit] = []
        for chunk in outcome.chunks:
            located = focus.get(chunk.chunk_id)
            hits.append(
                SearchHit(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    document_title=chunk.document_title or None,
                    heading_path=(located and located.heading_path) or chunk.heading_path,
                    page=located.page if located and located.page is not None else chunk.page,
                    snippet=focused_snippet(chunk, located, SNIPPET_CHARS),
                    score=chunk.score,
                    vec_score=chunk.vec_score,
                    bm25_score=chunk.bm25_score,
                    rrf=chunk.rrf,
                    rerank_score=chunk.rerank_score,
                    legs=list(chunk.legs),
                    quote_start=located.start if located else None,
                    quote_end=located.end if located else None,
                )
            )
        return SearchResponse(
            query=outcome.query, mode=outcome.mode, hits=hits, latency_ms=outcome.latency_ms
        )

    # -- 检索 + L3 / L2 召回 ----------------------------------------------

    async def retrieve(
        self,
        question: str,
        *,
        history: Sequence[Turn] = (),
        mode: SearchMode = "hybrid",
        document_ids: Sequence[str] | None = None,
        use_insights: bool = True,
        use_cards: bool = True,
        contextualize: bool = True,
        expand: bool = False,
        top_k: int | None = None,
    ) -> RetrievalResult:
        """对话链路用的完整检索：L1 命中 + L3 经验 + L2 卡片。

        Args:
            use_insights: 关掉则不召回 L3，供 A/B 对比「经验到底有没有用」。
            document_ids: 限定只在指定文档内检索（对话的附件范围）。
        """
        started = time.perf_counter()
        plan = await self.plan(
            question, history=history, contextualize=contextualize, expand=expand
        )
        embedded = await self._embed_queries(plan)
        max_insights = self.params.max_insights if use_insights else 0
        if (
            max_insights > 0
            and self.params.skip_insights_for_verbatim
            and looks_like_verbatim_request(question)
        ):
            # 纯索取原文：没有场景可言，经验在这里只会占预算、带偏措辞
            max_insights = 0
            logger.info("insights_skipped_for_verbatim", space_id=self.space_id)
        max_cards = MAX_CARDS if use_cards else 0

        # L1 / L3 / L2 三路并行：彼此不依赖，串行跑纯属浪费时间
        (chunks, reranked), insights, cards = await asyncio.gather(
            self._search_with(
                plan,
                embedded,
                mode=mode,
                limit=top_k or self.params.top_n_rerank,
                document_ids=document_ids,
            ),
            self._recall_insights(embedded.primary, max_insights, question=plan.primary),
            self._recall_cards(embedded.primary, max_cards),
        )
        # 只在问答链路上精简，检索调试接口（search）仍返回完整的 top-N：
        # 调参时要看的正是「哪些条目排进来了却不相关」
        kept = drop_covered_summaries(
            drop_low_relevance(chunks, floor=self.params.relevance_floor) if reranked else chunks
        )
        if len(kept) < len(chunks):
            logger.info(
                "evidence_pruned",
                space_id=self.space_id,
                before=len(chunks),
                after=len(kept),
            )
        chunks = kept
        return RetrievalResult(
            space_id=self.space_id,
            plan=plan,
            mode=mode,
            chunks=chunks,
            insights=insights,
            cards=cards,
            reranked=reranked,
            degraded=self._degradations(mode, embedded, chunks, reranked),
            latency_ms=_elapsed_ms(started),
        )

    def _degradations(
        self,
        mode: SearchMode,
        embedded: _EmbeddedQueries,
        chunks: list[ScoredChunk],
        reranked: bool,
    ) -> list[str]:
        """哪些**已配置**的检索环节这一轮没用上。

        从结果推导而不是在失败处记到实例上：管线在同一 Space 的并发请求之间共享，
        实例状态会串。只报「配了却没用上」——压根没绑定 embedding / rerank 角色是
        配置选择，不是故障。
        """
        degraded: list[str] = []
        if (
            mode in ("hybrid", "vector")
            and embedded.primary is None
            and self.registry.role_provider_id("embedding")
        ):
            degraded.append("vector")
        if chunks and not reranked and self.registry.role_provider_id("rerank"):
            degraded.append("rerank")
        return degraded

    def assemble_context(
        self,
        result: RetrievalResult,
        *,
        question: str,
        history: Sequence[Message] = (),
        context_mode: ContextMode = "standard",
    ) -> tuple[list[ProviderMessage], CitationRegistry]:
        """上下文装配：L4 + L3 + L2 + L1 → 可直接生成的 messages。"""
        context = self.assemble_context_details(
            result, question=question, history=history, context_mode=context_mode
        )
        return context.messages, context.registry

    def assemble_context_details(
        self,
        result: RetrievalResult,
        *,
        question: str,
        history: Sequence[Message] = (),
        context_mode: ContextMode = "standard",
    ) -> AssembledContext:
        """预算选择和实际注入清单；索取原文时自动采用标准预算。"""
        budget = ECONOMY_BUDGET if context_mode == "economy" else STANDARD_BUDGET
        if looks_like_verbatim_request(question):
            budget = STANDARD_BUDGET
        return assemble_answer_context(
            persona=self.persona,
            question=question,
            chunks=result.chunks,
            insights=result.insights,
            cards=result.cards,
            history=history,
            budget=budget,
            evidence_query=result.plan.primary,
        )

    def retrieved_items(self, result: RetrievalResult) -> list[TraceRetrievedItem]:
        """轨迹里的检索明细（各路分数）。"""
        return to_retrieved_items(result.chunks)

    # -- 召回实现 ---------------------------------------------------------

    async def _search_with(
        self,
        plan: QueryPlan,
        embedded: _EmbeddedQueries,
        *,
        mode: SearchMode,
        limit: int,
        document_ids: Sequence[str] | None,
    ) -> tuple[list[ScoredChunk], bool]:
        """融合两路召回并（可选）重排，返回最终证据列表。"""
        tasks: list[Coroutine[Any, Any, _RecallLeg]] = []
        if mode in ("hybrid", "vector"):
            for index, vector in enumerate(embedded.vector_legs(), start=1):
                name = f"vector:{index}"
                tasks.append(
                    self._guarded(
                        name,
                        self._vector_leg(name, vector, self.params.top_k_vector, document_ids),
                    )
                )
        if mode in ("hybrid", "fts"):
            for index, text in enumerate(plan.search_queries, start=1):
                name = f"fts:{index}"
                tasks.append(
                    self._guarded(
                        name, self._fts_leg(name, text, self.params.top_k_fts, document_ids)
                    )
                )
        if self.params.graph_expansion:
            tasks.append(
                self._guarded(
                    "graph",
                    self._graph_leg("graph", plan.original, document_ids, self.params.top_k_fts),
                )
            )
        legs = list(await asyncio.gather(*tasks)) if tasks else []
        # 每条候选是被哪几路找到的：调试「这条证据为什么会出现」比看它排第几更有用
        provenance: dict[str, list[str]] = {}
        for leg in legs:
            for chunk_id in leg.ids:
                provenance.setdefault(chunk_id, []).append(leg.name)
        fused = reciprocal_rank_fusion({leg.name: leg.ids for leg in legs if leg.ids}, k=RRF_K)
        if not fused:
            return [], False

        candidates = await self._load_chunks(
            [hit.id for hit in fused][: self.params.rerank_pool],
            # 只有混合模式才记录 RRF 分：单一召回模式下，调试面板想看的
            # 就是该路自己的原始分，而 RRF 的绝对值（1/(60+rank)）既没有
            # 量纲也没法和另一族比较，记上去只会盖掉有用的那个数
            rrf={hit.id: hit.score for hit in fused} if mode == "hybrid" else {},
            vec=_max_scores(legs, "vector"),
            bm25=_max_scores(legs, "fts"),
        )
        # 重排之前先压掉近重复。这些条目在重排之后一样会被丢掉——_diversify 里的
        # suppress_redundant 用的是同一个阈值——先给它们打分纯属白花算力。实测一份
        # 大表格切出来的 50 条候选里有 32 条是重复内容，压掉后配对数少三成。
        #
        # 只在 diversity 打开时做：关掉它的时候 _diversify 不做冗余抑制，提前丢条目
        # 就会真的改变结果。
        #
        # 一处不等价：一组近重复里留下来的那条，现在是 RRF 名次最高的那条，而不是
        # 重排名次最高的那条。它们的正文重合度在 dedup_threshold 以上，进上下文的
        # 文字几乎一样，差的是标注的出处与偏移——拿这点换掉三成重排配对，值。
        if self.params.diversity and len(candidates) > 1:
            candidates = suppress_redundant(candidates, threshold=self.params.dedup_threshold)

        # 多样性重排要有得选：先多取一些候选，处理完再截到 limit。
        # 候选池取 limit 的两倍，但不超出实际候选数（回表本身已按 RERANK_INPUT_LIMIT 封顶）。
        pool = min(len(candidates), limit * DIVERSITY_POOL_FACTOR)
        if mode == "hybrid" and self._rerank_available():
            reranked = await self._rerank(plan.primary, candidates, pool)
            if reranked is not None:
                return self._tag_legs(self._diversify(reranked, limit), provenance), True
        return self._tag_legs(self._diversify(candidates[:pool], limit), provenance), False

    @staticmethod
    def _tag_legs(chunks: list[ScoredChunk], provenance: dict[str, list[str]]) -> list[ScoredChunk]:
        """给最终证据标上召回路径。

        放在最后一步做：中间会经历去重、MMR、重叠裁剪，被裁到过短的条目会整条消失，
        提前标注等于给一批不存在的切片贴标签。
        """
        return [
            chunk.model_copy(update={"legs": provenance.get(chunk.chunk_id, [])})
            for chunk in chunks
        ]

    def _diversify(self, ranked: list[ScoredChunk], limit: int) -> list[ScoredChunk]:
        """冗余抑制 + MMR 多样性重排 + 相邻切片重叠裁剪，最后截到 ``limit``。

        两条分支都必须走这里：重排成功的那条由 reranker 给序，失败回退的那条
        由 RRF 给序，而 RRF 只看名次、完全没有语义去重能力——相邻切片与跨文档
        转载在它眼里就是两条互不相干的高分命中，回退时更需要这一步。

        ``keep_min`` 用默认的 1（只求不清空结果）：重复条目被丢掉后空出的位置由候选池里
        靠后的条目顶上，而不是靠保底把重复塞回来。候选池本身就只有重复内容时，
        结果短于 ``limit`` 才是诚实的——凑满条数等于把同样的文字送两遍。

        重叠裁剪排在 MMR **之后**：它要按「哪条在前」决定保留谁的完整正文，
        而进入上下文的顺序正是 MMR 的输出顺序（证据预算也按这个顺序分配篇幅）。
        被裁到过短的条目在这里整条消失，位置不再由候选池补——那段的文字已经
        完整地留在相邻条目里，补一条进来反而会把预算摊薄。

        ⚠️ **重叠裁剪不受 ``diversity`` 开关控制**：那个开关管的是 MMR 这种
        「牺牲一点相关性换多样性」的**策略选择**，关掉是一种合理的口味。
        而裁掉相邻切片的重复段是**零信息损失**的——被裁的文字完整地留在前一条里，
        没有任何理由因为用户不想要 MMR 就把同一段话送两遍。

        兄弟切片合并（``auto_merge``）排在最后、截断之前：同一节里连号的几条并成一条，
        腾出来的名额由后面的证据顶上。它同样不受 ``diversity`` 影响——把被切开的
        半句话接回去与「要不要牺牲相关性换多样性」是两件事。
        """
        if len(ranked) <= 1:
            return ranked[:limit]
        if not self.params.diversity:
            return self._automerge(trim_adjacent_overlap(ranked), limit)
        kept = suppress_redundant(ranked, threshold=self.params.dedup_threshold)
        # 合并会腾出名额，所以先多留一些候选（limit 的两倍），合完再截到 limit
        pool = limit * DIVERSITY_POOL_FACTOR if self.params.auto_merge else limit
        ordered = mmr_rerank(kept, lambda_=self.params.mmr_lambda, limit=pool)
        return self._automerge(trim_adjacent_overlap(ordered), limit)

    def _automerge(self, chunks: list[ScoredChunk], limit: int) -> list[ScoredChunk]:
        """把同一节里连号的兄弟切片并回父节点，再截到 ``limit``。"""
        if not self.params.auto_merge:
            return chunks[:limit]
        return merge_siblings(chunks)[:limit]

    async def _guarded(self, name: str, coro: Coroutine[Any, Any, _RecallLeg]) -> _RecallLeg:
        """单路召回失败不影响其它路：记日志后当作空结果。"""
        try:
            return await coro
        except AgentMemError as exc:
            logger.warning("recall_leg_failed", leg=name, code=exc.code, error=exc.message)
        except Exception:  # pragma: no cover - 存储层意外错误
            logger.exception("recall_leg_crashed", leg=name)
        return _RecallLeg(name=name)

    async def _vector_leg(
        self,
        name: str,
        vector: list[float],
        limit: int,
        document_ids: Sequence[str] | None,
    ) -> _RecallLeg:
        """向量召回一路。"""
        vectors = self.db.vectors
        if vectors is None:
            return _RecallLeg(name=name)
        hits = await vectors.search(
            "chunks_vec",
            vector,
            limit=limit,
            space_id=self.space_id,
            where=document_filter(document_ids),
        )
        return _RecallLeg(
            name=name, ids=[hit.id for hit in hits], scores={hit.id: hit.score for hit in hits}
        )

    async def _graph_leg(
        self,
        name: str,
        question: str,
        document_ids: Sequence[str] | None,
        limit: int,
    ) -> _RecallLeg:
        """图谱召回一路：问题里点到的实体，它们在图上相邻的切片。

        向量与全文两路都按「这段文字像不像问题」排序，于是「还有哪些和 X 协同作用」
        这类问法会漏掉图上明明与 X 相连、但字面不像的那些切片——我们有图，却等于没有。

        做法刻意保守：只在问题里**逐字出现实体名**时才扩展，命中的实体取它们参与的关系，
        再把关系记下的 ``source_chunks`` 拉进候选池。它作为与向量/全文并列的一路参与
        RRF 融合，不会挤掉原有召回，只是补上图上相邻的那几条。
        """
        entities = await self._entities_in(question, limit=GRAPH_ENTITY_LIMIT)
        if not entities:
            return _RecallLeg(name=name)
        async with self._sqlite():
            chunk_ids = await self.db.relations.chunks_for_entities(
                self.space_id, [entity.id for entity in entities], limit=limit * GRAPH_CHUNK_FACTOR
            )
            if document_ids:
                allowed = set(document_ids)
                rows = await self.db.chunks.get_many(chunk_ids)
                chunk_ids = [chunk.id for chunk in rows if chunk.document_id in allowed]
        return _RecallLeg(
            name=name,
            ids=chunk_ids,
            # 图上相邻属于结构性证据，给它一个略低于字面命中、但足以进入候选池的分数
            scores=dict.fromkeys(chunk_ids, GRAPH_LEG_SCORE),
        )

    async def _entities_in(self, question: str, *, limit: int) -> list[Entity]:
        """问题里逐字出现的实体（长的优先，避免短名把长名吃掉）。"""
        async with self._sqlite():
            entities, _cursor = await self.db.entities.list_by_space(
                self.space_id, limit=GRAPH_SCAN_LIMIT
            )
        matched = [entity for entity in entities if entity.name and entity.name in question]
        matched.sort(key=lambda item: len(item.name), reverse=True)
        return matched[:limit]

    async def _fts_leg(
        self,
        name: str,
        query: str,
        limit: int,
        document_ids: Sequence[str] | None,
    ) -> _RecallLeg:
        """BM25 召回一路。"""
        async with self._sqlite():
            hits = await self.db.fts.search(
                query,
                limit=limit,
                space_id=self.space_id,
                document_ids=list(document_ids) if document_ids else None,
            )
        return _RecallLeg(
            name=name,
            ids=[hit.chunk_id for hit in hits],
            scores={hit.chunk_id: hit.score for hit in hits},
        )

    async def _load_chunks(
        self,
        ids: list[Id],
        *,
        rrf: dict[str, float],
        vec: dict[str, float],
        bm25: dict[str, float],
    ) -> list[ScoredChunk]:
        """按融合顺序回表取切片内容与文档标题。"""
        if not ids:
            return []
        async with self._sqlite():
            rows = await self.db.chunks.get_many(ids)
            titles = await self._document_titles({chunk.document_id for chunk in rows})
        by_id = {chunk.id: chunk for chunk in rows}
        chunks: list[ScoredChunk] = []
        for chunk_id in ids:
            row = by_id.get(chunk_id)
            if row is None:
                continue  # 检索与回表之间文档被删掉了
            chunks.append(
                ScoredChunk(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    document_title=titles.get(row.document_id, ""),
                    heading_path=row.heading_path,
                    page=row.page,
                    ordinal=row.ordinal,
                    char_start=row.char_start,
                    char_end=row.char_end,
                    content=row.content,
                    kind=row.kind,
                    vec_score=vec.get(chunk_id),
                    bm25_score=bm25.get(chunk_id),
                    rrf=rrf.get(chunk_id),
                )
            )
        return chunks

    async def _document_titles(self, document_ids: set[str]) -> dict[str, str]:
        """批量取文档标题（只取最终入选的少数几篇）。"""
        if not document_ids:
            return {}
        found = await asyncio.gather(*(self.db.documents.get(item) for item in document_ids))
        return {document.id: document.title for document in found if document is not None}

    def _rerank_available(self) -> bool:
        """是否配了 rerank provider。没配就跳过重排，不报错。"""
        return self.registry.role_provider_id("rerank") is not None

    async def _rerank(
        self, query: str, candidates: list[ScoredChunk], top_n: int
    ) -> list[ScoredChunk] | None:
        """重排；失败或没打出一个分时返回 ``None``，由调用方回退 RRF 顺序。

        ``top_n`` 取的是候选池大小而不是最终条数：多样性重排需要看到比最终结果
        更多的候选，才有人可以补上被丢掉的重复条目腾出的位置。
        """
        if not candidates:
            return None
        try:
            route = self.registry.rerank(purpose="retrieve")
            ranked = await route.rerank(query, [chunk.content for chunk in candidates], top_n=top_n)
        except AgentMemError as exc:
            logger.warning(
                "rerank_failed", space_id=self.space_id, code=exc.code, error=exc.message
            )
            return None
        result: list[ScoredChunk] = []
        for item in ranked:
            if not 0 <= item.index < len(candidates):
                logger.warning("rerank_index_out_of_range", index=item.index)
                continue
            result.append(candidates[item.index].model_copy(update={"rerank_score": item.score}))
        return result[:top_n] or None

    async def _embed_queries(self, plan: QueryPlan) -> _EmbeddedQueries:
        """把所有需要用到的检索式一次算完向量（同一批请求，省往返）。"""
        texts = [plan.primary, *plan.variants]
        if plan.hyde:
            texts.append(plan.hyde)
        vectors = await self._embed(texts, kind="query")
        if not vectors or len(vectors) != len(texts):
            return _EmbeddedQueries()
        variants = vectors[1 : 1 + len(plan.variants)]
        hyde = vectors[-1] if plan.hyde else None
        return _EmbeddedQueries(primary=vectors[0], variants=variants, hyde=hyde)

    async def _embed(self, texts: list[str], *, kind: EmbedKind) -> list[list[float]]:
        """向量化；未绑定 embedding 角色或调用失败时返回空列表（退化为纯全文检索）。"""
        try:
            route = self.registry.embedding(purpose="retrieve")
            return await route.embed(texts, kind=kind)
        except AgentMemError as exc:
            logger.warning(
                "query_embedding_unavailable",
                space_id=self.space_id,
                code=exc.code,
                error=exc.message,
            )
        except Exception:  # pragma: no cover - 适配器层意外错误
            logger.exception("query_embedding_crashed", space_id=self.space_id)
        return []

    # -- L3 / L2 召回 ------------------------------------------------------

    async def _recall_insights(
        self, vector: list[float] | None, max_insights: int, *, question: str = ""
    ) -> list[Insight]:
        """按问题语义召回高置信度的 active 经验；与问题无关的一条也不注入。

        经验带着「适用时必须遵守」的措辞进上下文，塞进不相关的经验既占 token，
        又可能把回答往别的场景上拽。所以两条路径都设了门槛：

        - 有向量：召回分低于 :data:`INSIGHT_MIN_SCORE` 的不要；
        - 没有向量（未绑定 embedding、或索引还没回填）：按置信度取候选后，
          只留与问题有实词重合的（:func:`_shares_terms`）。此前这条兜底是
          「置信度最高的几条无条件注入」，问什么都带同样两条。
        """
        if max_insights <= 0:
            return []
        if vector is not None:
            hits = await self._vector_scored_hits(
                "insights_vec", vector, max_insights * INSIGHT_OVERFETCH
            )
            if hits:
                ids = [item for item, score in hits if score >= INSIGHT_MIN_SCORE]
                return (await self._active_insights(ids))[:max_insights] if ids else []
            if not await self._vector_index_empty("insights_vec"):
                # 索引里有经验，只是本次问题都不相关——不要硬塞
                return []
        candidates = await self._top_insights(max_insights * INSIGHT_OVERFETCH)
        # jieba 切词是同步 CPU 活，词典没预热时首次要加载一两秒，不放在事件循环上跑
        matched = await asyncio.to_thread(
            lambda: [
                item
                for item in candidates
                if _shares_terms(question, f"{item.trigger}\n{item.guidance}")
            ]
        )
        if len(matched) < len(candidates):
            logger.info(
                "insights_gated_lexically",
                space_id=self.space_id,
                candidates=len(candidates),
                kept=len(matched),
            )
        return matched[:max_insights]

    async def _active_insights(self, ids: list[str]) -> list[Insight]:
        """按向量名次取 active 且置信度达标的经验（保持召回顺序）。"""
        async with self._sqlite():
            found = await self.db.insights.get_many(ids)
        return [
            item
            for item in found
            if item.status == "active" and item.confidence >= INSIGHT_MIN_CONFIDENCE
        ]

    async def _top_insights(self, max_insights: int) -> list[Insight]:
        """没有语义召回能力时按置信度兜底（L3 刚产出、尚未建向量索引的情形）。"""
        async with self._sqlite():
            return await self.db.insights.list_active(
                self.space_id, min_confidence=INSIGHT_MIN_CONFIDENCE, limit=max_insights
            )

    async def _recall_cards(
        self, vector: list[float] | None, max_cards: int
    ) -> list[KnowledgeCard]:
        """按问题语义召回 L2 知识卡片。"""
        if max_cards <= 0:
            return []
        if vector is not None:
            hits = await self._vector_scored_hits("cards_vec", vector, max_cards * CARD_OVERFETCH)
            ids = _near_top(hits, CARD_SCORE_GAP)
            if ids:
                cards = await self._cards_by_ids(ids)
                if cards:
                    return cards[:max_cards]
            if not await self._vector_index_empty("cards_vec"):
                return []
        return await self._top_cards(max_cards)

    async def _cards_by_ids(self, ids: list[str]) -> list[KnowledgeCard]:
        """按 id 取卡片，保持召回顺序；缺失的跳过。"""
        async with self._sqlite():
            found = await asyncio.gather(*(self.db.cards.get(card_id) for card_id in ids))
        return [card for card in found if card is not None]

    async def _top_cards(self, max_cards: int) -> list[KnowledgeCard]:
        """向量索引为空时按置信度取卡片。"""
        async with self._sqlite():
            cards, _, _ = await self.db.cards.list_by_space(self.space_id, limit=max_cards)
        return cards

    async def _vector_hits(
        self, table: VectorTableName, vector: list[float], limit: int
    ) -> list[str]:
        """在某张向量表里按 space 过滤召回主键。"""
        return [item for item, _score in await self._vector_scored_hits(table, vector, limit)]

    async def _vector_scored_hits(
        self, table: VectorTableName, vector: list[float], limit: int
    ) -> list[tuple[str, float]]:
        """同 :meth:`_vector_hits`，但带上相似度（按相似度降序）。"""
        vectors = self.db.vectors
        if vectors is None:
            return []
        try:
            hits = await vectors.search(table, vector, limit=limit, space_id=self.space_id)
        except AgentMemError as exc:
            logger.warning("vector_recall_failed", table=table, code=exc.code, error=exc.message)
            return []
        return [(hit.id, hit.score) for hit in hits]

    async def _vector_index_empty(self, table: VectorTableName) -> bool:
        """该表在本 Space 下是否还没有任何向量。"""
        vectors = self.db.vectors
        if vectors is None:
            return True
        try:
            return await vectors.count(table, self.space_id) == 0
        except AgentMemError as exc:
            logger.warning("vector_count_failed", table=table, code=exc.code, error=exc.message)
            return True


def document_filter(document_ids: Sequence[str] | None) -> str | None:
    """附件范围 → LanceDB 过滤表达式。

    值一律走 :func:`sql_literal` 转义：LanceDB 的 ``where`` 没有参数绑定，
    裸拼字符串等于把过滤表达式注入的口子留给调用方。
    """
    if not document_ids:
        return None
    literals = ", ".join(sql_literal(item) for item in document_ids)
    return f"document_id IN ({literals})"


def _max_scores(legs: Sequence[_RecallLeg], prefix: str) -> dict[str, float]:
    """合并同一族（向量 / 全文）多路的分数的最大值，供调试面板展示。"""
    merged: dict[str, float] = {}
    for leg in legs:
        if not leg.name.startswith(prefix):
            continue
        for chunk_id, score in leg.scores.items():
            current = merged.get(chunk_id)
            if current is None or score > current:
                merged[chunk_id] = score
    return merged


def _shares_terms(question: str, text: str) -> bool:
    """问题与一段文字有没有共同的实词（jieba 切词，与全文检索同一套词元）。

    不在 :data:`_GENERIC_TERMS` 里的词都算（短问题里的单字实词也算：「壳怎么脱」
    靠的就是「壳」）。这是没有语义信号时的兜底：「长江大学哪一年建立的」与
    「hERG 抑制阈值」一个词都不共享，经验就不注入；共享「毒性」「化合物」这类领域词时
    照常注入——宁可多注入一条，也不错过适用的规则。

    代价是同义改写认不出来：「成药性要看什么」与「先评估 ADMET」没有共同的词，
    这条经验在没有向量时不会被注入。向量索引回填之后走语义召回，不受这个限制。
    """
    if not question.strip():
        return False

    def terms(value: str) -> set[str]:
        return set(tokenize(value)) - _GENERIC_TERMS

    return bool(terms(question) & terms(text))


def _near_top(hits: Sequence[tuple[str, float]], gap: float) -> list[str]:
    """只留与最相似那条相差不超过 ``gap`` 的主键（保持原顺序）。"""
    if not hits:
        return []
    top = max(score for _item, score in hits)
    return [item for item, score in hits if score >= top - gap]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
