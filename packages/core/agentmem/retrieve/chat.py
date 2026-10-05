"""对话生成：检索 → 上下文装配 → 流式生成 → 引用落位 → 轨迹落库。

事件顺序严格按 ``docs/03-API-SPEC.md`` §5::

    trace_start → rewrite(可选) → retrieval → insights → context → delta → citation → done

两个刻意的约定：

- ``retrieval`` / ``insights`` 即使内容为空也会发（空列表）。前端状态机因此不必为
  「这次没检索」「这次没经验」单独分支，``use_insights=false`` 的 A/B 对比也
  能在同一条序列上直接看出差异。
- 失败时先落库再发 ``error``。半截回答同样是「这次交互」的证据，Phase 3 的
  蒸馏要用到它，丢掉反而是损失。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel

from agentmem.config import Settings, get_settings
from agentmem.errors import AgentMemError, ContextOverflowError, NotFoundError
from agentmem.providers.base import PROVIDER_DEFAULT_REASONING, ChatChunk
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.store.base import new_id
from agentmem.types import (
    ChatRequest,
    Citation,
    CitationEvent,
    DoneEvent,
    Insight,
    InsightEventItem,
    InsightsEvent,
    KnowledgeCard,
    Message,
    MessageCreate,
    MessageUpdate,
    Persona,
    RetrievalEvent,
    RetrievalEventItem,
    RetrievalSettings,
    RewriteEvent,
    RoleName,
    SseErrorEvent,
    TraceCreate,
    TraceStartEvent,
    UsageSummary,
)

from .citations import CitationEmitter, CitationRegistry, CitationStreamParser
from .context import HISTORY_TURNS, to_retrieved_items, to_turns
from .locate import DocumentOutline, Located, claim_before, focused_snippet, load_outline, locate
from .models import ChatEvent, DeltaChunk, QueryPlan, RetrievalResult, ScoredChunk
from .pipeline import RetrievalPipeline

logger = structlog.get_logger(__name__)

#: 默认生成角色
DEFAULT_CHAT_ROLE: RoleName = "chat"

#: 生成温度：问答要的是稳定与忠于证据，不是创造力
CHAT_TEMPERATURE = 0.3

#: 「不要深度思考」时请求的推理强度
NO_REASONING = "none"

#: 检索事件里每条命中的片段长度
EVENT_SNIPPET_CHARS = 160

#: 兜底错误码（不在 ``03-API-SPEC.md`` §9 表内，见 CHANGELOG-INTERFACE）
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"


@dataclass(slots=True)
class _GenerationState:
    """一次回答的累计产物，收尾时统一落库。

    用可变 dataclass 而不是 Pydantic 模型：它在生成过程中被反复追加，
    是流程内的累加器，不参与跨模块传递。
    """

    conversation_id: str
    message_id: str
    trace_id: str
    query: str
    llm_role: RoleName
    started_at: float
    plan: QueryPlan
    chunks: list[ScoredChunk] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)
    cards: list[KnowledgeCard] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    provider_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    deep_thinking: bool | None = None

    @property
    def content(self) -> str:
        """已生成的正文（引用标记已剥离）。"""
        return "".join(self.parts)

    @property
    def latency_ms(self) -> int:
        """从收到请求到落库的总耗时。"""
        return int((time.perf_counter() - self.started_at) * 1000)


class ChatService:
    """单 Space 的问答链路。

    Args:
        space_id: 所属 Space。
        database: 该 Space 的存储门面。
        registry: Provider 注册表。
        settings: 全局设置。
        retrieval: ``space.yaml`` 的检索参数。
        persona: L4 画像。
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
        self.pipeline = RetrievalPipeline(
            space_id=space_id,
            database=database,
            registry=registry,
            settings=self.settings,
            retrieval=retrieval,
            persona=persona,
        )

    async def stream(self, conversation_id: str, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        """持有空间写入许可到半截回答与轨迹清理完毕。"""
        with self.db.operations.writing():
            stream = self._stream(conversation_id, request)
            try:
                async for item in stream:
                    yield item
            finally:
                await aclose_stream(stream)

    async def _stream(self, conversation_id: str, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        """产生一条完整的问答事件流。"""
        state: _GenerationState | None = None
        try:
            conversation = await self.db.conversations.require(conversation_id)
            if conversation.space_id != self.space_id:
                raise NotFoundError("会话", conversation_id)
            # 历史必须在写入本轮消息之前取，否则当前问题会被当成历史里的最后一轮
            history = await self._history(conversation_id)
            await self.db.messages.create(
                MessageCreate(conversation_id=conversation_id, role="user", content=request.content)
            )
            # assistant 消息占位先行落库，是为了让 trace_start 里的 message_id 与最终
            # 落库的消息一致——前端拿到 id 后要立刻把气泡挂在它上面
            placeholder = await self.db.messages.create(
                MessageCreate(conversation_id=conversation_id, role="assistant", content="")
            )
            state = _GenerationState(
                conversation_id=conversation_id,
                message_id=placeholder.id,
                trace_id=new_id(),
                query=request.content,
                llm_role=request.llm_role or DEFAULT_CHAT_ROLE,
                started_at=time.perf_counter(),
                plan=QueryPlan(original=request.content),
                deep_thinking=request.deep_thinking,
            )
            yield _event(
                "trace_start",
                TraceStartEvent(trace_id=state.trace_id, message_id=state.message_id),
            )
            async for item in self._generate(state, request, history):
                yield item
        except asyncio.CancelledError:
            # 取消来自客户端断开或用户点了停止，不是错误，但要留下痕迹
            logger.info("chat_cancelled", conversation_id=conversation_id)
            raise
        except AgentMemError as exc:
            logger.warning(
                "chat_failed", conversation_id=conversation_id, code=exc.code, error=exc.message
            )
            yield _event("error", SseErrorEvent(code=exc.code, message=exc.message))
        except Exception as exc:
            logger.exception("chat_crashed", conversation_id=conversation_id)
            yield _event(
                "error",
                SseErrorEvent(code=INTERNAL_ERROR_CODE, message=f"内部错误：{type(exc).__name__}"),
            )
        finally:
            if state is not None:
                await self._persist(state)

    # -- 生成 -------------------------------------------------------------

    async def _generate(
        self, state: _GenerationState, request: ChatRequest, history: Sequence[Message]
    ) -> AsyncIterator[ChatEvent]:
        """检索 → 装配 → 流式生成。"""
        result = await self._retrieve(request, history)
        state.plan = result.plan
        state.chunks = result.chunks

        if result.plan.rewritten:
            yield _event("rewrite", RewriteEvent(rewritten=result.plan.rewritten))
        focus = await self.pipeline.focus(request.content, result.chunks)
        yield _event(
            "retrieval",
            RetrievalEvent(chunks=_retrieval_items(result.chunks, focus), degraded=result.degraded),
        )
        # 首次按 token 预算保留完整问答；窗口仍溢出时，只缩减实际注入的历史。
        # 最近一轮不做有损摘要，因此极长的一轮也保留现有的溢出兜底。
        attempt_history: list[Message] = [
            message for message in history if message.role in ("user", "assistant")
        ][-HISTORY_TURNS:]
        while True:
            context = self.pipeline.assemble_context_details(
                result,
                question=request.content,
                history=attempt_history,
                context_mode=request.context_mode,
            )
            state.insights = context.insights
            state.cards = context.cards
            yield _event("insights", InsightsEvent(insights=_insight_items(context.insights)))
            yield _event("context", context.usage)
            produced = False
            try:
                async for item in self._generate_answer(state, context.messages, context.registry):
                    produced = True
                    yield item
                return
            except ContextOverflowError:
                used = context.usage.history_messages
                if produced or not used:
                    # 已经吐过内容（重试会重复输出），或历史已空（再砍也没得砍）
                    raise
                kept = used // 2
                trimmed = attempt_history[-kept:] if kept else []
                while trimmed and trimmed[0].role != "user":
                    trimmed = trimmed[1:]
                kept = len(trimmed)
                logger.warning(
                    "chat_context_overflow_retry",
                    conversation_id=state.conversation_id,
                    dropped_turns=used - kept,
                    kept_turns=kept,
                )
                attempt_history = trimmed

    async def _retrieve(self, request: ChatRequest, history: Sequence[Message]) -> RetrievalResult:
        """按入参决定检索范围；``use_retrieval=false`` 时退化为纯聊天。"""
        if not request.use_retrieval:
            return RetrievalResult(space_id=self.space_id, plan=QueryPlan(original=request.content))
        return await self.pipeline.retrieve(
            request.content,
            history=to_turns(history),
            mode=request.search_mode,
            document_ids=request.attachments,
            use_insights=request.use_insights,
        )

    async def _pinpoint(
        self,
        citation: Citation,
        registry: CitationRegistry,
        state: _GenerationState,
        outlines: dict[str, DocumentOutline | None],
    ) -> Citation:
        """把引用从整条切片收窄到回答依据的那一句（见 ``retrieve/locate.py``）。

        纯锦上添花：任何异常都退回切片级定位，不能因为它打断回答流。
        """
        chunk = registry.chunk(citation.marker)
        if chunk is None or citation.kind != "body":
            return citation
        try:
            if citation.document_id not in outlines:
                cache_dir = self.settings.data_dir / "cache" / self.space_id
                outlines[citation.document_id] = await asyncio.to_thread(
                    load_outline, cache_dir, citation.document_id
                )
            update = _quote_fields(
                citation, chunk, "".join(state.parts), outlines[citation.document_id]
            )
        except Exception as exc:  # 定位失败不影响回答
            logger.warning("citation_pinpoint_failed", marker=citation.marker, error=str(exc))
            return citation
        return citation.model_copy(update=update) if update else citation

    async def _generate_answer(
        self,
        state: _GenerationState,
        messages: list[ProviderMessage],
        registry: CitationRegistry,
    ) -> AsyncIterator[ChatEvent]:
        """消费模型流，边解析引用边推送增量。"""
        route = self.registry.llm(state.llm_role, purpose="chat")
        state.provider_id = route.name
        # 把本轮证据编号交给解析器：模型把 [^c1] 写成 [^1] 时，c1 在表里才认作引用
        parser = CitationStreamParser(known=registry.known())
        emitter = CitationEmitter(registry)

        # 显式关闭底层流：客户端断开时不能留下悬挂的 HTTP 连接，
        # 只靠 GC 回收会拖到不确定的时刻
        options: dict[str, object] = {"temperature": CHAT_TEMPERATURE}
        effort = _reasoning_effort(state.deep_thinking)
        if effort is not None:
            options["reasoning_effort"] = effort
            # 轨迹表没有这一列；先留在日志里，排查「这条为什么答得短 / 慢」时可查
            logger.info(
                "chat_reasoning_hint", trace_id=state.trace_id, deep_thinking=state.deep_thinking
            )
        chunks = route.stream(messages, **options)
        emitted_chars = 0  # 已输出正文字数，用来把引用位置换算成绝对下标
        outlines: dict[str, DocumentOutline | None] = {}
        try:
            async for chunk in chunks:
                _track_usage(state, chunk)
                if not chunk.delta:
                    continue
                base = emitted_chars
                feed = parser.feed(chunk.delta)
                if feed.text:
                    state.parts.append(feed.text)
                    emitted_chars += len(feed.text)
                    yield _event("delta", DeltaChunk(text=feed.text))
                # 引用位置要记绝对下标：正文里标记已被剥离，
                # 只有位置能说明「这句话是有出处的」还是「这句是空口说的」
                for accepted in emitter.accept(feed.markers, feed.offsets, base=base):
                    citation = await self._pinpoint(accepted, registry, state, outlines)
                    state.citations.append(citation)
                    yield _event(
                        "citation",
                        CitationEvent(**citation.model_dump()),
                    )
        finally:
            await aclose_stream(chunks)

        tail = parser.flush()
        if tail:
            state.parts.append(tail)
            yield _event("delta", DeltaChunk(text=tail))

        yield _event(
            "done",
            DoneEvent(
                message_id=state.message_id,
                trace_id=state.trace_id,
                usage=UsageSummary(
                    prompt_tokens=state.prompt_tokens or 0,
                    completion_tokens=state.completion_tokens or 0,
                    latency_ms=state.latency_ms,
                ),
            ),
        )

    # -- 落库 -------------------------------------------------------------

    async def _persist(self, state: _GenerationState) -> None:
        """把助手消息与轨迹写入库。

        这是 Phase 3 进化闭环的输入：query / 改写 / 各路分数 / 注入的经验与卡片 /
        模型与 token / 耗时，缺一项都会让「为什么这么答」无法还原。
        """
        try:
            await self.db.messages.update(
                state.message_id,
                MessageUpdate(content=state.content, citations=state.citations),
            )
            await self.db.traces.create(
                TraceCreate(
                    # 沿用 SSE 里已经发给前端的 id：反馈与轨迹面板都靠它回查
                    id=state.trace_id,
                    space_id=self.space_id,
                    conversation_id=state.conversation_id,
                    message_id=state.message_id,
                    query=state.query,
                    rewritten_query=state.plan.rewritten,
                    retrieved=to_retrieved_items(state.chunks),
                    used_insights=[item.id for item in state.insights],
                    used_cards=[item.id for item in state.cards],
                    llm_role=state.llm_role,
                    provider_id=state.provider_id,
                    model=state.model,
                    prompt_tokens=state.prompt_tokens,
                    completion_tokens=state.completion_tokens,
                    latency_ms=state.latency_ms,
                )
            )
            await self.db.conversations.touch(state.conversation_id)
            # 被注入过的经验各记一次「注入」，作为 success_count 的分母
            await self.db.insights.bump_applied([item.id for item in state.insights])
        except Exception:  # pragma: no cover - 落库失败不应打断已经吐出的回答
            logger.exception("chat_persist_failed", conversation_id=state.conversation_id)

    async def _history(self, conversation_id: str) -> list[Message]:
        """取当前问题之前的历史消息（空内容的轮次不进上下文）。"""
        messages = await self.db.messages.list_by_conversation(conversation_id)
        return [message for message in messages if message.content.strip()]


async def aclose_stream(stream: AsyncIterator[object]) -> None:
    """确定性地关闭一个异步迭代器。

    ``LLMProvider.stream`` 的协议类型是 :class:`AsyncIterator`，类型系统上看不到
    ``aclose``，但实现都是异步生成器。断开连接与取消任务时都要立刻把底层
    HTTP 流关掉，不能等垃圾回收。
    """
    closer = getattr(stream, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # pragma: no cover - 清理失败不应掩盖真正的错误
        logger.warning("stream_close_failed", space_id=None)


def _quote_fields(
    citation: Citation, chunk: ScoredChunk, answer: str, outline: DocumentOutline | None
) -> dict[str, object]:
    """引用收窄到具体原句后要改的字段；收窄不了返回空字典。"""
    if citation.char_offset is None:
        return {}
    claim = claim_before(answer, citation.char_offset)
    located = locate(claim, chunk.content, chunk.char_start, chunk.char_end, outline)
    if located is None:
        return {}
    update: dict[str, object] = {
        "quote": located.quote,
        "quote_start": located.start,
        "quote_end": located.end,
    }
    # 章节与页码按原句的位置重新查：切片开头的章节不一定是这句话所在的章节
    if located.heading_path:
        update["heading_path"] = located.heading_path
    if located.page is not None:
        update["page"] = located.page
    return update


def _reasoning_effort(deep_thinking: bool | None) -> str | None:
    """请求里的「深度思考」开关 → 传给模型服务的推理强度提示。

    ``None`` 不传任何提示（沿用服务端默认与 provider 配置）；``False`` 要 ``none``，
    推理模型因此直接作答（实测 agnes-2.5-flash 首字中位数 3.4 → 0.9 秒）；``True``
    要求按服务端默认思考，连 provider 配置里的 ``reasoning_effort`` 也不套用。
    服务端不认推理参数时由适配器去掉重试，开关只是失效，不会让回答失败。
    """
    if deep_thinking is None:
        return None
    return PROVIDER_DEFAULT_REASONING if deep_thinking else NO_REASONING


def _event(name: str, payload: BaseModel) -> ChatEvent:
    """构造一条对话事件。"""
    return ChatEvent(name=name, payload=payload)


def _track_usage(state: _GenerationState, chunk: ChatChunk) -> None:
    """累计本轮生成用到的 provider / 模型 / token 数。"""
    if chunk.provider_id:
        state.provider_id = chunk.provider_id
    if chunk.model:
        state.model = chunk.model
    if chunk.prompt_tokens is not None:
        state.prompt_tokens = chunk.prompt_tokens
    if chunk.completion_tokens is not None:
        state.completion_tokens = chunk.completion_tokens


def _retrieval_items(
    chunks: Sequence[ScoredChunk], focus: Mapping[str, Located] | None = None
) -> list[RetrievalEventItem]:
    """命中 → 检索事件条目；``focus`` 给了就把章节与片段换成命中句的位置。"""
    focus = focus or {}
    return [_retrieval_item(chunk, focus.get(chunk.chunk_id)) for chunk in chunks]


def _retrieval_item(chunk: ScoredChunk, located: Located | None) -> RetrievalEventItem:
    return RetrievalEventItem(
        id=chunk.chunk_id,
        document_id=chunk.document_id,
        title=chunk.document_title or None,
        page=located.page if located and located.page is not None else chunk.page,
        score=chunk.score,
        snippet=focused_snippet(chunk, located, EVENT_SNIPPET_CHARS),
        kind=chunk.kind,
        vec_score=chunk.vec_score,
        bm25_score=chunk.bm25_score,
        rrf=chunk.rrf,
        rerank_score=chunk.rerank_score,
        legs=list(chunk.legs),
        merged_from=list(chunk.merged_from),
        heading_path=(located and located.heading_path) or chunk.heading_path,
        ordinal=chunk.ordinal,
        quote_start=located.start if located else None,
        quote_end=located.end if located else None,
    )


def _insight_items(insights: Sequence[Insight]) -> list[InsightEventItem]:
    """经验 → 经验事件条目。"""
    return [
        InsightEventItem(
            id=insight.id,
            trigger=insight.trigger,
            guidance=insight.guidance,
            confidence=insight.confidence,
            scope=insight.scope,
        )
        for insight in insights
    ]
