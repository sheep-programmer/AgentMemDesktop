"""L2 知识抽取：把 L1 切片分批喂给模型，产出知识卡片、实体与关系。

分批的理由是上下文预算：一次喂 3~6 个**相邻**切片，既有足够的上下文让模型
看清指代关系，又不会因为拼成一整篇而超出窗口。相邻关系来自切片的
``ordinal`` 顺序。

几个刻意的约定：

- **marker 每批重新编号**（``c1`` … ``cN``），批内翻译成 ``chunk_id`` 后立即丢弃，
  不跨批维护一张全局表。模型只会在本批上下文里引用 marker，跨批编号没有意义。
- **单条坏数据只丢那一条**：某个条目类型不认识、正文为空、置信度越界，
  都只跳过该条并计数，不牵连整批。
- **抽取失败不影响文档可用**：L1 检索此时已经能用了，所以异常在这里被隔离成
  日志与报告，不向上抛给摄取状态机。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

import structlog
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from agentmem.config import Settings, get_settings, load_space_yaml
from agentmem.errors import AgentMemError
from agentmem.prompts import (
    ChunkRef,
    ExtractSource,
    PersonaSpec,
    build_extract_messages,
    extract_json,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve.context import to_persona_spec, to_provider_messages
from agentmem.store import Database
from agentmem.types import (
    Chunk,
    Document,
    Entity,
    EntityCreate,
    EntityUpdate,
    KnowledgeCard,
    KnowledgeCardCreate,
    ProgressEvent,
    RelationCreate,
    RoleName,
    SseErrorEvent,
)

from .cards import CardService
from .grounding import ground_text
from .models import (
    ExtractedCard,
    ExtractedEntity,
    ExtractedRelation,
    ExtractionFailure,
    ExtractionPayload,
    ExtractionProgressCallback,
    ExtractionReport,
    ExtractionStats,
    MemoryEvent,
)

logger = structlog.get_logger(__name__)

#: 抽取用的模型角色：要的是快与忠实，不需要最强推理
DEFAULT_EXTRACT_ROLE: RoleName = "fast"

#: 忠实转录，任何创造性都是污染
EXTRACT_TEMPERATURE = 0.1

#: 抽取输出容易被截断：一张卡片就是一段带论据的正文，8 条就能顶到 4k。
#: 截断后靠 extract_json 的残片抢救兜底，但能一次说完更好。
EXTRACT_MAX_TOKENS = 8192

#: 进度事件里的阶段名，与摄取状态机一致
EXTRACT_STAGE = "extracting"

#: 每批的相邻切片数上限
BATCH_CHUNK_LIMIT = 4

#: 每批的正文长度上限，超过就提前切批
BATCH_CHAR_LIMIT = 12000

#: 单批最多问几次模型：脏输出再问一次，两次都解析不出来就放弃这一批
BATCH_ATTEMPTS = 2

#: 喂给模型的已有实体名上限
KNOWN_ENTITY_LIMIT = 50

#: 装载实体索引的分页大小与上限
ENTITY_PAGE_SIZE = 500
ENTITY_INDEX_LIMIT = 2000

#: 未指定文档时，一次最多抽取多少个已就绪文档
DOCUMENT_PAGE_SIZE = 100
READY_DOCUMENT_LIMIT = 200

#: 兜底错误码
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"

#: 读取不到领域信息时的兜底
DEFAULT_DOMAIN = "通用领域"

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def batch_chunks(chunks: Sequence[Chunk]) -> list[list[Chunk]]:
    """把切片按相邻顺序切成批次。

    逐条累加，达到条数上限或长度上限就封批。
    """
    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    size = 0
    for chunk in chunks:
        length = len(chunk.content)
        if current and (len(current) >= BATCH_CHUNK_LIMIT or size + length > BATCH_CHAR_LIMIT):
            batches.append(current)
            current, size = [], 0
        current.append(chunk)
        size += length
    if current:
        batches.append(current)
    return batches


def batch_refs(document: Document, batch: Sequence[Chunk]) -> tuple[list[ChunkRef], dict[str, str]]:
    """给一批切片编号并构造证据块，返回 ``(证据块, marker → chunk_id)``。"""
    refs: list[ChunkRef] = []
    mapping: dict[str, str] = {}
    for index, chunk in enumerate(batch, start=1):
        marker = f"c{index}"
        mapping[marker] = chunk.id
        refs.append(
            ChunkRef(
                marker=marker,
                chunk_id=chunk.id,
                document_title=document.title,
                heading_path=chunk.heading_path,
                page=chunk.page,
                content=chunk.content,
            )
        )
    return refs, mapping


def resolve_markers(markers: Sequence[str], mapping: dict[str, str]) -> list[str]:
    """把 marker（如 ``c1``）翻译成 ``chunk_id``，认不出的丢掉并保持顺序。"""
    resolved: list[str] = []
    for marker in markers:
        chunk_id = mapping.get(marker.strip().lower())
        if chunk_id is None or chunk_id in resolved:
            continue
        resolved.append(chunk_id)
    return resolved


def parse_payload(raw: object) -> tuple[ExtractionPayload, int]:
    """把模型返回的 JSON 归一成载荷。

    Returns:
        ``(载荷, 被丢弃的条目数)``。

    Raises:
        ValueError: 整体不是 JSON 对象——这属于整批失败，不是单条失败。
    """
    if not isinstance(raw, dict):
        raise ValueError("抽取结果不是 JSON 对象")
    cards, skipped = _collect(ExtractedCard, raw.get("cards"))
    entities, skipped_entities = _collect(ExtractedEntity, raw.get("entities"))
    relations, skipped_relations = _collect(ExtractedRelation, raw.get("relations"))
    payload = ExtractionPayload(cards=cards, entities=entities, relations=relations)
    return payload, skipped + skipped_entities + skipped_relations


def _collect(model: type[_ModelT], items: object) -> tuple[list[_ModelT], int]:
    """逐条校验列表字段；不合格的条目丢弃并计数。"""
    if not isinstance(items, list):
        return [], 0
    accepted: list[_ModelT] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            accepted.append(model.model_validate(item))
        except PydanticValidationError as exc:
            skipped += 1
            logger.debug("extraction_item_skipped", model=model.__name__, error=str(exc)[:200])
    return accepted, skipped


async def load_persona_spec(settings: Settings, database: Database, space_id: str) -> PersonaSpec:
    """读出某 Space 的 L4 画像。

    ``space.yaml`` 缺失或没写领域时退回 ``spaces`` 行——领域名会进抽取提示词的
    系统消息，空着会让模型失去领域先验。
    """
    config = await asyncio.to_thread(load_space_yaml, settings.spaces_dir / space_id / "space.yaml")
    persona = config.persona
    if not persona.domain:
        space = await database.spaces.get(space_id)
        if space is not None and space.domain:
            persona = persona.model_copy(update={"domain": space.domain})
    if not persona.domain:
        persona = persona.model_copy(update={"domain": DEFAULT_DOMAIN})
    return to_persona_spec(persona)


@dataclass(slots=True)
class _BatchOutcome:
    """一批切片的抽取结果。"""

    payload: ExtractionPayload | None = None
    skipped: int = 0
    error: str | None = None
    code: str = INTERNAL_ERROR_CODE


@dataclass(slots=True)
class _ExtractionState:
    """一次抽取任务的跨文档累加器。

    实体索引与关系键在整轮内共享：同一批文档里的同一个实体只落一行，
    重复的关系也不会因为重新抽取而堆积。
    """

    report: ExtractionReport
    entities: dict[str, Entity] = field(default_factory=dict)
    known_names: list[str] = field(default_factory=list)
    relations: set[tuple[str, str, str]] = field(default_factory=set)
    last_error: tuple[str, str] | None = None


class KnowledgeExtractor:
    """单 Space 的 L2 抽取。

    Args:
        space_id: 所属 Space。
        database: 该 Space 的存储门面。
        registry: Provider 注册表（llm 与 embedding 两个角色）。
        persona: L4 画像，供提示词描述领域与术语表。
        settings: 全局设置（定位 ``space.yaml``）。
        llm_role: 抽取使用的模型角色。
    """

    def __init__(
        self,
        *,
        space_id: str,
        database: Database,
        registry: ProviderRegistry,
        persona: PersonaSpec,
        settings: Settings | None = None,
        llm_role: RoleName = DEFAULT_EXTRACT_ROLE,
    ) -> None:
        self.space_id = space_id
        self.db = database
        self.registry = registry
        self.persona = persona
        self.settings = settings or get_settings()
        self.llm_role = llm_role
        #: 同时进行的批次上限。模型调用是耗时大头，可以并发；落库仍走一把锁串行。
        self.batch_concurrency = self.settings.extract_concurrency
        self.cards = CardService(space_id=space_id, database=database, registry=registry)

    @classmethod
    async def for_space(
        cls,
        *,
        space_id: str,
        database: Database,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        llm_role: RoleName = DEFAULT_EXTRACT_ROLE,
    ) -> KnowledgeExtractor:
        """按 Space 的落盘配置组装抽取器。"""
        resolved = settings or get_settings()
        persona = await load_persona_spec(resolved, database, space_id)
        return cls(
            space_id=space_id,
            database=database,
            registry=registry,
            persona=persona,
            settings=resolved,
            llm_role=llm_role,
        )

    # -- 入口 -------------------------------------------------------------

    async def resolve_documents(self, document_ids: Sequence[str]) -> list[Document]:
        """把请求中的文档 id 解析成文档。

        没有指定 id 时取该 Space 全部**已就绪**的文档：抽取依赖切片，
        还在解析或向量化中的文档此时没有可用的 L1 内容。

        Raises:
            NotFoundError: 指定的文档不存在。
        """
        if document_ids:
            return [await self.db.documents.require(document_id) for document_id in document_ids]
        documents: list[Document] = []
        cursor: str | None = None
        while len(documents) < READY_DOCUMENT_LIMIT:
            page, _, cursor = await self.db.documents.list_by_space(
                self.space_id, status="ready", limit=DOCUMENT_PAGE_SIZE, cursor=cursor
            )
            documents.extend(page)
            if cursor is None:
                break
        return documents[:READY_DOCUMENT_LIMIT]

    async def extract_document(
        self, document: Document, *, on_progress: ExtractionProgressCallback | None = None
    ) -> ExtractionReport:
        """抽取单个文档。"""
        return await self.extract_documents([document], on_progress=on_progress)

    async def extract_documents(
        self,
        documents: Sequence[Document],
        *,
        on_progress: ExtractionProgressCallback | None = None,
    ) -> ExtractionReport:
        """抽取若干文档并返回汇总报告。

        进度事件逐条转给 ``on_progress``；单个文档失败记进报告，不影响其余文档。
        """
        report = ExtractionReport(space_id=self.space_id)
        async for item in self.stream(documents):
            if isinstance(item.payload, ExtractionReport):
                report = item.payload
            elif on_progress is not None and isinstance(item.payload, ProgressEvent):
                await on_progress(item.payload)
        return report

    async def stream(self, documents: Sequence[Document]) -> AsyncIterator[MemoryEvent]:
        """事件流形式：``progress`` … ``error``（可选）… ``done``。"""
        state = _ExtractionState(report=ExtractionReport(space_id=self.space_id))
        started = time.perf_counter()
        primed_error: str | None = None
        try:
            await self._prime(state)
        except Exception as exc:
            primed_error = str(exc)
            logger.exception("l2_extraction_index_failed", space_id=self.space_id)

        if primed_error is None:
            for document in documents:
                async for item in self._document_events(document, state):
                    yield item
        else:
            for document in documents:
                state.report.failures.append(
                    ExtractionFailure(
                        document_id=document.id, code=INTERNAL_ERROR_CODE, message=primed_error
                    )
                )
                yield _error_event(document.id, INTERNAL_ERROR_CODE, primed_error)

        for stats in state.report.documents:
            state.report.absorb(stats)
        state.report.duration_ms = int((time.perf_counter() - started) * 1000)
        yield MemoryEvent(name="done", payload=state.report)

    # -- 逐文档 -----------------------------------------------------------

    async def _document_events(
        self, document: Document, state: _ExtractionState
    ) -> AsyncIterator[MemoryEvent]:
        """抽一个文档，把异常隔离成失败记录与 ``error`` 事件。"""
        try:
            async for item in self._run_document(document, state):
                yield item
        except AgentMemError as exc:
            state.report.failures.append(
                ExtractionFailure(document_id=document.id, code=exc.code, message=exc.message)
            )
            logger.warning(
                "l2_extraction_failed",
                space_id=self.space_id,
                document_id=document.id,
                code=exc.code,
                error=exc.message,
            )
            yield _error_event(document.id, exc.code, exc.message)
        except Exception as exc:
            logger.exception(
                "l2_extraction_crashed", space_id=self.space_id, document_id=document.id
            )
            message = str(exc) or type(exc).__name__
            state.report.failures.append(
                ExtractionFailure(
                    document_id=document.id, code=INTERNAL_ERROR_CODE, message=message
                )
            )
            yield _error_event(document.id, INTERNAL_ERROR_CODE, message)

    async def _run_document(
        self, document: Document, state: _ExtractionState
    ) -> AsyncIterator[MemoryEvent]:
        """读切片、分批抽取、逐批上报进度。"""
        stats = ExtractionStats(document_id=document.id, document_title=document.title)
        state.report.documents.append(stats)
        state.last_error = None
        # 概要切片是摄取时由模型写的派生文本，不是原文：拿它再抽卡片，模型在概要里补进去的
        # 东西（实测「长江大学」的校址与前身院校）就被固化成一张「有来源」的知识卡片
        chunks = [
            chunk
            for chunk in await self.db.chunks.list_by_document(document.id)
            if chunk.kind == "body"
        ]
        if not chunks:
            stats.skipped_reason = "no_chunks"
            logger.info("l2_extraction_skipped", document_id=document.id, reason="no_chunks")
            return
        batches = batch_chunks(chunks)
        stats.batches = len(batches)
        async for item in self._run_batches(document, batches, stats, state):
            yield item
        if stats.batches_failed and stats.batches_failed == stats.batches:
            # 一批都没成，说明模型这一侧出了问题，按整篇失败上报而不是只改计数
            code, message = state.last_error or (
                INTERNAL_ERROR_CODE,
                "全部批次抽取失败，详见服务端日志",
            )
            state.report.failures.append(
                ExtractionFailure(document_id=document.id, code=code, message=message)
            )
            yield _error_event(document.id, code, message)

    async def _run_batches(
        self,
        document: Document,
        batches: Sequence[Sequence[Chunk]],
        stats: ExtractionStats,
        state: _ExtractionState,
    ) -> AsyncIterator[MemoryEvent]:
        """并发问模型，按批次顺序落库与上报进度。

        一批要等一次模型调用（实测约 13 秒），串行跑 19 批就是四分多钟，所以问模型
        这一步并发。落库必须按批次顺序：卡片的来源切片取的是并集，落库顺序一变，
        同一份文档两次抽取得到的来源列表就不一样了。
        """
        if not batches:
            return
        total = len(batches)
        semaphore = asyncio.Semaphore(self._batch_concurrency())
        outcomes: list[_BatchOutcome | None] = [None] * total
        ready = [asyncio.Event() for _ in range(total)]

        async def ask(index: int, batch: Sequence[Chunk]) -> None:
            try:
                refs, _ = batch_refs(document, batch)
                async with semaphore:
                    outcomes[index] = await self._ask(document, refs, state)
            finally:
                ready[index].set()

        askers = [asyncio.create_task(ask(index, batch)) for index, batch in enumerate(batches)]
        try:
            for index, batch in enumerate(batches):
                await ready[index].wait()
                # 批次任务真的抛了异常（例如角色没绑模型）就照常往上抛：上层据此把这篇
                # 文档记成抽取失败。吞掉它会让失败静默消失，文档照样走到 ready。
                failure = askers[index].exception() if askers[index].done() else None
                if failure is not None:
                    raise failure
                _, mapping = batch_refs(document, batch)
                contents = {chunk.id: chunk.content for chunk in batch}
                await self._store_batch(
                    document, mapping, outcomes[index], stats, state, contents=contents
                )
                yield MemoryEvent(
                    name="progress",
                    payload=ProgressEvent(
                        document_id=document.id,
                        stage=EXTRACT_STAGE,
                        done=index + 1,
                        total=total,
                        percent=round((index + 1) / total * 100, 2),
                    ),
                )
        finally:
            for task in askers:
                if not task.done():
                    task.cancel()
            for task in askers:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def _batch_concurrency(self) -> int:
        """这一轮能同时跑几批。

        以 provider 声明的 ``concurrent_safe`` 为准：远程 API 可以并发，进程内加载的
        本地模型不行（同一个模型实例被多批同时调用会互相踩）。拿不准就退回串行——
        慢总好过结果错。
        """
        if self.batch_concurrency <= 1:
            return 1
        try:
            provider_id = self.registry.role_provider_id(self.llm_role)
            if not provider_id:
                return 1
            instance = self.registry.instance(provider_id)
            if not getattr(instance, "concurrent_safe", False):
                return 1
        except Exception:
            return 1
        return self.batch_concurrency

    async def _store_batch(
        self,
        document: Document,
        mapping: dict[str, str],
        outcome: _BatchOutcome | None,
        stats: ExtractionStats,
        state: _ExtractionState,
        *,
        contents: dict[str, str] | None = None,
    ) -> None:
        """把一批的抽取结果落库。``contents`` 是本批切片原文，供卡片做落地检查。"""
        if outcome is None:
            return
        stats.cards_skipped += outcome.skipped
        if outcome.payload is None:
            stats.batches_failed += 1
            if outcome.error is not None:
                state.last_error = (outcome.code, outcome.error)
            return
        stored = await self._store_cards(
            document, outcome.payload.cards, mapping, stats, contents=contents
        )
        await self._store_entities(document, outcome.payload.entities, state, stats)
        await self._store_relations(document, outcome.payload.relations, mapping, state, stats)
        await self._embed_cards(stored, stats)

    async def _ask(
        self,
        document: Document,
        refs: Sequence[ChunkRef],
        state: _ExtractionState,
    ) -> _BatchOutcome:
        """问一次模型（必要时再问一次）。"""
        source: ExtractSource = {
            "document_title": document.title,
            "chunks": list(refs),
            "known_entities": state.known_names,
        }
        messages = to_provider_messages(build_extract_messages(persona=self.persona, source=source))
        llm = self.registry.llm(self.llm_role, purpose="extract")
        for attempt in range(1, BATCH_ATTEMPTS + 1):
            try:
                result = await llm.chat(
                    messages, temperature=EXTRACT_TEMPERATURE, max_tokens=EXTRACT_MAX_TOKENS
                )
            except Exception as exc:
                # provider 不通时重问一次也是白问，直接放弃这一批
                logger.warning(
                    "l2_extraction_call_failed",
                    space_id=self.space_id,
                    document_id=document.id,
                    attempt=attempt,
                    error=str(exc),
                )
                code = exc.code if isinstance(exc, AgentMemError) else INTERNAL_ERROR_CODE
                return _BatchOutcome(error=str(exc) or type(exc).__name__, code=code)
            try:
                payload, skipped = parse_payload(extract_json(result.content))
            except (ValueError, PydanticValidationError) as exc:
                logger.warning(
                    "l2_extraction_parse_failed",
                    space_id=self.space_id,
                    document_id=document.id,
                    attempt=attempt,
                    error=str(exc)[:200],
                )
                continue
            return _BatchOutcome(payload=payload, skipped=skipped)
        logger.warning("l2_extraction_batch_given_up", document_id=document.id)
        return _BatchOutcome(error="模型输出不是可解析的 JSON")

    # -- 落库 -------------------------------------------------------------

    async def _store_cards(
        self,
        document: Document,
        cards: Sequence[ExtractedCard],
        mapping: dict[str, str],
        stats: ExtractionStats,
        *,
        contents: dict[str, str] | None = None,
    ) -> list[KnowledgeCard]:
        """卡片落库，返回本批写入的卡片（供调用方统一向量化）。

        给了 ``contents`` 时先做落地检查（:func:`ground_text`）：卡片正文里原文站不住的
        小句去掉，整张都站不住的卡片不入库。比对范围是本批**全部**切片而不只是模型
        自报的来源——来源编号常常报漏一两个，只比自报来源会把真有出处的句子误丢。
        """
        stored: list[KnowledgeCard] = []
        batch_text = [document.title, *(contents or {}).values()]
        for item in cards:
            sources = resolve_markers(item.source_chunk_markers, mapping)
            if not sources:
                # 没有来源的卡片不可追溯，抽出来也不该进知识库
                stats.cards_skipped += 1
                logger.info("l2_card_without_source", document_id=document.id, title=item.title)
                continue
            body = item.body
            if contents:
                grounded = ground_text(item.body, batch_text)
                if not grounded.text or not ground_text(item.title, batch_text).text:
                    stats.cards_skipped += 1
                    logger.info(
                        "l2_card_ungrounded",
                        document_id=document.id,
                        title=item.title,
                        dropped=grounded.dropped[:5],
                    )
                    continue
                if grounded.changed:
                    logger.info(
                        "l2_card_trimmed",
                        document_id=document.id,
                        title=item.title,
                        dropped=grounded.dropped[:5],
                    )
                    body = grounded.text
            data = KnowledgeCardCreate(
                space_id=self.space_id,
                kind=item.kind,
                title=item.title,
                body=body,
                aliases=item.aliases,
                source_chunks=sources,
                confidence=item.confidence,
            )
            card, created = await self.cards.merge_extracted(data)
            stored.append(card)
            if created:
                stats.cards_created += 1
            else:
                stats.cards_updated += 1
        return stored

    async def _store_entities(
        self,
        document: Document,
        entities: Sequence[ExtractedEntity],
        state: _ExtractionState,
        stats: ExtractionStats,
    ) -> None:
        """实体落库：同名同类型只保留一行；提及按「哪篇文档提到了它」记账。

        计数不再做增量累加：``mention_count + 1`` 每抽取一次就涨一次，重新解析过的
        文档会把节点权重越推越高。现在先把明细写进 ``entity_mentions``（按
        entity + document 去重），再从明细重算计数，同一篇文档抽十次也只算一次。
        """
        touched: list[str] = []
        for item in entities:
            name = item.name.strip()
            if not name:
                continue
            key = name.lower()
            existing = await self.db.entities.find(self.space_id, name, item.type)
            if existing is None:
                entity = await self.db.entities.create(
                    EntityCreate(
                        space_id=self.space_id,
                        name=name,
                        type=item.type,
                        summary=item.summary,
                        mention_count=0,  # 由提及明细重算
                    )
                )
                state.entities.setdefault(key, entity)
                if name not in state.known_names:
                    state.known_names.append(name)
                stats.entities_created += 1
            else:
                entity = await self.db.entities.update(
                    existing.id,
                    EntityUpdate(summary=existing.summary or item.summary),
                )
                state.entities.setdefault(key, entity)
                stats.entities_updated += 1
            touched.append(entity.id)

        if touched:
            await self.db.entities.record_mentions(
                space_id=self.space_id, document_id=document.id, entity_ids=touched
            )
            await self.db.entities.refresh_mention_counts(touched)

    async def _store_relations(
        self,
        document: Document,
        relations: Sequence[ExtractedRelation],
        mapping: dict[str, str],
        state: _ExtractionState,
        stats: ExtractionStats,
    ) -> None:
        """关系落库：两端先解析成实体 id，解析不到就跳过。"""
        for item in relations:
            src = state.entities.get(item.src.strip().lower())
            dst = state.entities.get(item.dst.strip().lower())
            if src is None or dst is None or src.id == dst.id:
                stats.relations_skipped += 1
                logger.info(
                    "l2_relation_endpoint_missing",
                    document_id=document.id,
                    src=item.src,
                    dst=item.dst,
                )
                continue
            key = (src.id, dst.id, item.predicate)
            if key in state.relations:
                stats.relations_skipped += 1
                continue
            await self.db.relations.create(
                RelationCreate(
                    space_id=self.space_id,
                    src_id=src.id,
                    dst_id=dst.id,
                    predicate=item.predicate,
                    weight=item.weight,
                    source_chunks=resolve_markers(item.source_chunk_markers, mapping),
                )
            )
            state.relations.add(key)
            stats.relations_created += 1

    async def _embed_cards(self, cards: Sequence[KnowledgeCard], stats: ExtractionStats) -> None:
        """卡片向量化。

        失败不中断抽取——卡片与图谱已经落库，检索管线在向量索引为空时会按
        置信度兜底；但失败原因会写进报告与日志，维度不符这类需要重建索引的
        问题不会被吞掉。
        """
        try:
            result = await self.cards.sync_vectors(cards)
        except AgentMemError as exc:
            stats.embedding_error = f"[{exc.code}] {exc.message}"
            logger.warning(
                "l2_card_embedding_failed",
                space_id=self.space_id,
                code=exc.code,
                error=exc.message,
            )
            return
        if result.skipped:
            stats.embedding_error = "embedding 角色未绑定，卡片未写入向量索引"

    # -- 索引 -------------------------------------------------------------

    async def _prime(self, state: _ExtractionState) -> None:
        """装载实体索引与已有关系键，供跨批去重与实体名对齐。"""
        state.entities = await self._load_entities()
        state.known_names = [
            entity.name
            for entity in sorted(
                state.entities.values(), key=lambda item: (-item.mention_count, item.id)
            )[:KNOWN_ENTITY_LIMIT]
        ]
        relations = await self.db.relations.list_by_space(self.space_id)
        state.relations = {
            (relation.src_id, relation.dst_id, relation.predicate) for relation in relations
        }

    async def _load_entities(self) -> dict[str, Entity]:
        """按小写名称索引该 Space 的实体；同名不同类型时先出现的胜出。"""
        index: dict[str, Entity] = {}
        cursor: str | None = None
        while len(index) < ENTITY_INDEX_LIMIT:
            page, cursor = await self.db.entities.list_by_space(
                self.space_id, limit=ENTITY_PAGE_SIZE, cursor=cursor
            )
            for entity in page:
                index.setdefault(entity.name.lower(), entity)
            if cursor is None:
                break
        return index


def _error_event(document_id: str, code: str, message: str) -> MemoryEvent:
    """构造一条 ``error`` 事件。"""
    return MemoryEvent(
        name="error", payload=SseErrorEvent(code=code, message=message, document_id=document_id)
    )
