"""摄取流水线：状态机 + 分阶段重试 + 进度回调。

状态流转：``pending → parsing → chunking → embedding → extracting → ready | failed``。
每个阶段可单独重试，失败不影响已完成阶段；进度通过回调上报，供 SSE 使用。

``extracting`` 是增值阶段：它失败只记警告，文档仍然走到 ``ready``。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import aiofiles
import structlog

from agentmem.config import Settings, get_settings
from agentmem.errors import AgentMemError, NotFoundError, ParseFailedError, ValidationError
from agentmem.ingest.chunk import (
    CHUNKER_VERSION,
    ChunkDraft,
    count_tokens,
    split_parse_result,
)
from agentmem.ingest.parse import (
    ParseResult,
    extract_headings,
    parse_document,
    parse_text,
    split_pages,
)
from agentmem.memory import KnowledgeExtractor
from agentmem.memory.grounding import ground_text
from agentmem.memory.prune import prune_chunk_references
from agentmem.prompts import build_context_messages, clean_summary
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.registry import ProviderRegistry
from agentmem.security import locate_raw_file
from agentmem.store import Database
from agentmem.types import (
    Chunk,
    ChunkCreate,
    Document,
    DocumentCreate,
    DocumentMeta,
    DocumentSourceType,
    DocumentStatus,
    DocumentUpdate,
    ProgressEvent,
    StatusEvent,
    VectorRecord,
)

logger = structlog.get_logger(__name__)

#: 进度回调：接收进度事件（供 SSE 转发）
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]
#: 状态回调：接收状态变更事件
StatusCallback = Callable[[StatusEvent], Awaitable[None]]

#: 生成文档级上下文的温度：要的是稳定的关键词，不是发挥
CONTEXT_TEMPERATURE = 0.2

#: 连续失败几次之后，本次运行不再尝试生成文档级上下文
SUMMARY_FAILURE_LIMIT = 2


#: 进程中断后可以自动判失败的状态：这些状态意味着「有任务在跑」，而任务已经随进程消失
_INTERRUPTED_STATUSES = ("pending", "parsing", "chunking", "embedding", "extracting")

#: 中断后写进 ``documents.error`` 的说明。要能让人看懂下一步该做什么。
INTERRUPTED_MESSAGE = "上次处理被中断（应用退出或异常），文档未完成；请点击「重新解析」重试"


async def recover_interrupted(database: Database) -> list[str]:
    """把上次进程中断留下的未完成文档判为失败，返回被处理的文档 id。

    这些文档的状态停在 ``parsing`` / ``embedding`` 之类，表示「有任务正在跑」，
    而任务已经随进程一起消失了：界面会一直显示「摄取中」，永远不会变，用户也
    无从知道该做什么。启动时把它们如实标成失败，并带上可操作的说明，
    比留一个永远转圈的假状态诚实得多。

    这里刻意**不自动续跑**：续跑会在启动瞬间向模型服务打一批请求，用户既没预期、
    也可能正处在没配好 provider 的状态；失败 + 一键重试是更可控的默认。
    """
    recovered: list[str] = []
    for document in await database.documents.list_unfinished():
        if document.status not in _INTERRUPTED_STATUSES:
            continue
        await database.documents.set_status(document.id, "failed", INTERRUPTED_MESSAGE)
        recovered.append(document.id)
    if recovered:
        logger.warning(
            "ingest_recovered_interrupted",
            count=len(recovered),
            documents=recovered[:20],
        )
    return recovered


def index_prefix(document: Document) -> str:
    """拼进索引与嵌入文本的文档级上下文前缀。

    切片正文一个字符都不动——前缀只出现在全文索引与向量里。拿不到上下文时返回空串，
    行为与没有这个特性时完全一致。
    """
    summary = document.meta.context_summary
    if not summary:
        return ""
    return f"《{document.title}》\n{summary}\n---\n"


STAGE_ORDER: tuple[DocumentStatus, ...] = (
    "pending",
    "parsing",
    "chunking",
    "embedding",
    "extracting",
    "ready",
)


#: 可单独重试的阶段
RETRYABLE_STAGES: tuple[str, ...] = ("parsing", "chunking", "embedding", "extracting")


class IngestPipeline:
    """单 Space 的文档摄取。

    Args:
        database: 该 Space 的存储门面。
        registry: Provider 注册表（提供 embedding / llm 角色）。
        settings: 全局设置（切片大小、批次大小等）。
    """

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        settings: Settings | None = None,
        *,
        operation_token: object | None = None,
    ) -> None:
        self.db = database
        self.registry = registry
        self.settings = settings or get_settings()
        self._operation_token = operation_token
        # 文档级上下文生成的连续失败计数，见 _context_summary 的熔断说明
        self._summary_failures = 0

    # -- 去重与登记 -------------------------------------------------------

    @staticmethod
    async def sha256_of_file(path: Path) -> str:
        """计算文件 sha256（分块读取，避免大文件占满内存）。"""

        def digest() -> str:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
            return hasher.hexdigest()

        return await asyncio.to_thread(digest)

    @staticmethod
    def sha256_of_text(content: str) -> str:
        """计算文本 sha256。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def ensure_unique(self, space_id: str, sha256: str) -> None:
        """sha256 去重；已存在则抛 ``DUPLICATE_DOCUMENT``。"""
        from agentmem.errors import DuplicateDocumentError

        existing = await self.db.documents.find_by_sha256(space_id, sha256)
        if existing is not None:
            raise DuplicateDocumentError(sha256, document_id=existing.id)

    async def register_file(
        self,
        *,
        space_id: str,
        path: Path,
        title: str | None = None,
        source_type: DocumentSourceType = "file",
        mime: str | None = None,
    ) -> Document:
        """登记一个待处理的文件（状态 pending）。"""
        with self.db.operations.writing(self._operation_token):
            sha256 = await self.sha256_of_file(path)
            await self.ensure_unique(space_id, sha256)
            size = await asyncio.to_thread(lambda: path.stat().st_size)
            return await self.db.documents.create(
                DocumentCreate(
                    space_id=space_id,
                    title=title or path.name,
                    source_type=source_type,
                    source_uri=str(path),
                    mime=mime,
                    sha256=sha256,
                    size_bytes=size,
                    status="pending",
                    meta=DocumentMeta(),
                )
            )

    async def register_text(self, *, space_id: str, title: str, content: str) -> Document:
        """登记一段粘贴文本（状态 pending），原文落盘到 ``raw/``。"""
        with self.db.operations.writing(self._operation_token):
            sha256 = self.sha256_of_text(content)
            await self.ensure_unique(space_id, sha256)
            document = await self.db.documents.create(
                DocumentCreate(
                    space_id=space_id,
                    title=title,
                    source_type="paste",
                    source_uri=None,
                    mime="text/markdown",
                    sha256=sha256,
                    size_bytes=len(content.encode("utf-8")),
                    status="pending",
                )
            )
            path = await self.write_raw(space_id, document.id, "paste.md", content.encode("utf-8"))
            meta = document.meta.model_copy(update={"raw_path": str(path)})
            return await self.db.documents.update(document.id, DocumentUpdate(meta=meta))

    # -- 主流程 -----------------------------------------------------------

    def _lock_for(self, document_id: str) -> asyncio.Lock:
        return self.db.operations.document_lock(document_id)

    async def run(
        self,
        document_id: str,
        *,
        stages: Iterable[str] | None = None,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Document:
        """执行摄取（可指定只跑某些阶段）。

        Args:
            document_id: 文档 id。
            stages: 限定执行的阶段；为空表示整条链路。
            on_progress: 进度回调。
            on_status: 状态回调。

        Returns:
            处理后的文档。
        """
        with self.db.operations.writing(self._operation_token):
            async with self._lock_for(document_id):
                document = await self.db.documents.require(document_id)
                selected = tuple(stages) if stages else STAGE_ORDER
                try:
                    if "parsing" in selected:
                        await self._stage_parsing(document, on_progress, on_status)
                    document = await self.db.documents.require(document_id)
                    if "chunking" in selected:
                        await self._stage_chunking(document, on_progress, on_status)
                    document = await self.db.documents.require(document_id)
                    if "embedding" in selected:
                        await self._stage_embedding(document, on_progress, on_status)
                    document = await self.db.documents.require(document_id)
                    if "extracting" in selected:
                        await self._stage_extracting(document, on_progress, on_status)
                    await self._set_status(document_id, "ready", on_status)
                except asyncio.CancelledError:
                    await self.db.documents.set_status(
                        document_id, "failed", error="处理已中断，请重新解析该文档后继续。"
                    )
                    if on_status is not None:
                        await on_status(StatusEvent(document_id=document_id, status="failed"))
                    raise
                except AgentMemError as exc:
                    logger.warning(
                        "ingest_failed", document_id=document_id, code=exc.code, error=exc.message
                    )
                    await self.db.documents.set_status(document_id, "failed", error=exc.message)
                    if on_status is not None:
                        await on_status(StatusEvent(document_id=document_id, status="failed"))
                    raise
                except Exception as exc:
                    logger.exception("ingest_crashed", document_id=document_id)
                    await self.db.documents.set_status(document_id, "failed", error=str(exc))
                    if on_status is not None:
                        await on_status(StatusEvent(document_id=document_id, status="failed"))
                    raise
                return await self.db.documents.require(document_id)

    async def retry_stage(
        self,
        document_id: str,
        stage: str,
        *,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> Document:
        """单独重试某个阶段。"""
        if stage not in RETRYABLE_STAGES:
            raise ValidationError(
                f"不支持重试的阶段：{stage}",
                detail={"stage": stage, "allowed": list(RETRYABLE_STAGES)},
            )
        return await self.run(
            document_id, stages=[stage], on_progress=on_progress, on_status=on_status
        )

    async def _set_status(
        self, document_id: str, status: DocumentStatus, on_status: StatusCallback | None
    ) -> None:
        await self.db.documents.set_status(document_id, status)
        if on_status is not None:
            await on_status(StatusEvent(document_id=document_id, status=status))

    # -- 阶段实现 ---------------------------------------------------------

    async def _stage_parsing(
        self,
        document: Document,
        on_progress: ProgressCallback | None,
        on_status: StatusCallback | None,
    ) -> None:
        await self._set_status(document.id, "parsing", on_status)
        await self._report(on_progress, document.id, "parsing", 0, 1)
        parsed = await self._parse(document)
        await self._save_parsed(document, parsed)
        meta = document.meta.model_copy(
            update={"page_count": parsed.page_count or document.meta.page_count}
        )
        # 概要是一次完整的模型调用（实测约 9 秒），解析本身是瞬时的。不单独报进度的话
        # 界面上这 9 秒一直显示「解析中」，看不出真正在等什么
        await self._report(on_progress, document.id, "summarizing", 0, 1)
        summary = await self._context_summary(document, parsed)
        await self._report(on_progress, document.id, "summarizing", 1, 1)
        if summary is not None:
            meta = meta.model_copy(update={"context_summary": summary})
        await self.db.documents.update(document.id, DocumentUpdate(meta=meta))
        await self._report(on_progress, document.id, "parsing", 1, 1)

    async def _context_summary(self, document: Document, parsed: ParseResult) -> str | None:
        """给整篇文档生成一段上下文，供索引与嵌入做前缀。

        失败一律不阻断摄取：拿不到上下文只是回到「没有上下文」的旧行为，
        而让整篇文档因为一次摘要调用失败而变 failed 是得不偿失的。

        ``_summary_failures`` 是个极简的熔断：provider 不通时每次调用都要等连接超时，
        一个 Space 逐篇重建索引会白等「超时 × 文档数」。连续失败两次之后本次运行就
        不再尝试——同一个跑批里，服务不会在几秒内自己好起来。
        """
        if not self.settings.contextual_retrieval:
            return None
        if self._summary_failures >= SUMMARY_FAILURE_LIMIT:
            return None
        try:
            route = self.registry.llm("fast", purpose="contextual")
        except Exception as exc:  # 没配 fast 角色/没配模型时安静跳过
            logger.info("context_summary_skipped", document_id=document.id, reason=str(exc))
            return None
        # Prompt 层产出的是 TypedDict 消息，provider 层要 Pydantic 模型
        messages = [
            ProviderMessage(role=item["role"], content=item["content"])
            for item in build_context_messages(
                title=document.title,
                headings=[item.heading_path for item in extract_headings(parsed.markdown)],
                excerpt=parsed.markdown,
            )
        ]
        try:
            completion = await route.chat(messages, temperature=CONTEXT_TEMPERATURE)
        except Exception as exc:
            self._summary_failures += 1
            logger.warning(
                "context_summary_failed",
                document_id=document.id,
                error=str(exc),
                consecutive_failures=self._summary_failures,
                giving_up=self._summary_failures >= SUMMARY_FAILURE_LIMIT,
            )
            return None
        self._summary_failures = 0
        summary = clean_summary(completion.content)
        if summary is None:
            return None
        # 概要会变成一条可检索、可被引用的切片：原文之外的内容（模型凭常识补的校址、
        # 机构、年份）在这里去掉，整段都站不住就宁可不要概要
        grounded = ground_text(summary, [document.title, parsed.markdown])
        if grounded.changed:
            logger.info(
                "context_summary_trimmed",
                document_id=document.id,
                dropped=grounded.dropped[:5],
                kept=bool(grounded.text),
            )
        return grounded.text or None

    async def _parse(self, document: Document) -> ParseResult:
        """解析文档。

        ``file`` 走解析链；``paste`` / ``url`` 的正文已经是 Markdown，直接按文本处理。
        """
        raw_dir = self._raw_dir(document.space_id)
        if document.source_type in ("paste", "url"):
            stored = document.meta.raw_path or document.source_uri
            path = await asyncio.to_thread(locate_raw_file, raw_dir, stored)
            if path is None:
                raise ParseFailedError("原始文件不存在", detail={"document_id": document.id})
            async with aiofiles.open(path, encoding="utf-8") as handle:
                return await parse_text(await handle.read(), title=document.title)
        if not document.source_uri:
            raise ParseFailedError("文档缺少 source_uri", detail={"document_id": document.id})
        path = await asyncio.to_thread(locate_raw_file, raw_dir, document.source_uri)
        if path is None:
            raise ParseFailedError("原始文件不存在", detail={"document_id": document.id})
        return await parse_document(path)

    def _raw_dir(self, space_id: str) -> Path:
        """本 Space 的 ``raw/``（绝对路径：不依赖启动目录）。"""
        return (self.settings.spaces_dir / space_id / "raw").resolve()

    async def _stage_chunking(
        self,
        document: Document,
        on_progress: ProgressCallback | None,
        on_status: StatusCallback | None,
    ) -> None:
        await self._set_status(document.id, "chunking", on_status)
        parsed = await self._load_parsed(document)
        drafts = await asyncio.to_thread(
            split_parse_result,
            parsed,
            target_tokens=self.settings.chunk_size,
            overlap_tokens=self.settings.chunk_overlap,
        )
        if not drafts:
            # 切不出任何切片就没有可检索的内容。放它走到 ready，用户会以为资料已经
            # 进库，实际检索永远搜不到它——扫描件与纯图片 PDF 走的正是这条路。
            # 在删除旧切片之前就报错：失败不该顺手把已有的东西毁掉。
            raise ParseFailedError(
                "没有从文档中提取到可切分的正文，检索不到任何内容。"
                "扫描件或纯图片 PDF 会出现这种结果，请改用带文本层的文件，"
                "或先做 OCR 再重新上传。",
                detail={"document_id": document.id, "characters": len(parsed.markdown)},
            )
        # 重切会换掉全部 chunk id：卡片与关系里指向旧切片的来源先摘掉（不删卡片——
        # 马上就会重新抽取，同名卡片合并回来，删了会丢掉用户的校订与版本历史）
        old_chunk_ids = [chunk.id for chunk in await self.db.chunks.list_by_document(document.id)]
        await prune_chunk_references(self.db, old_chunk_ids, drop_orphans=False)
        await self.db.chunks.delete_by_document(document.id)
        # 重切会换掉全部 chunk id，旧向量必须一并清掉：只删切片的话，向量表里
        # 会留下指向已删切片的孤儿，检索命中它们却回不了表，top-K 被幽灵占位，
        # 表本身也随每次重切膨胀。
        vectors = self.db.vectors
        if vectors is not None:
            await vectors.delete_by_field("chunks_vec", "document_id", document.id)
        drafts_with_summary = list(drafts)
        summary = document.meta.context_summary
        # 记下产出这批切片的算法版本：重建索引据此判断哪些文档还需要重做
        stamped = document.meta.model_copy(update={"chunker_version": CHUNKER_VERSION})
        created = await self.db.chunks.create_many(
            [
                ChunkCreate(
                    space_id=document.space_id,
                    document_id=document.id,
                    ordinal=draft.ordinal,
                    content=draft.content,
                    heading_path=draft.heading_path,
                    page=draft.page,
                    char_start=draft.char_start,
                    char_end=draft.char_end,
                    token_count=draft.token_count,
                )
                for draft in drafts_with_summary
            ]
            + (
                # 概要切片：一条代表整篇文档的可检索证据，供「跨全文汇总」类问题使用。
                # 它没有可定位的原文区间（char_start = char_end = 0），kind 与正文区分开，
                # 前端因此知道不该拿它去原文里高亮。
                [
                    ChunkCreate(
                        space_id=document.space_id,
                        document_id=document.id,
                        ordinal=len(drafts_with_summary),
                        content=summary,
                        kind="summary",
                        char_start=0,
                        char_end=0,
                        token_count=count_tokens(summary),
                    )
                ]
                if summary
                else []
            ),
            index_prefix=index_prefix(document),
        )
        await self.db.documents.update(
            document.id,
            DocumentUpdate(
                meta=stamped, token_count=sum(chunk.token_count or 0 for chunk in created)
            ),
        )
        await self._report(on_progress, document.id, "chunking", len(created), len(created))

    async def _load_parsed(self, document: Document) -> ParseResult:
        cache = self._cache_dir(document.space_id) / f"{document.id}.parse.json"
        if not cache.is_file():
            raise ParseFailedError(
                "解析结果缓存缺失，请先重试 parsing", detail={"document_id": document.id}
            )
        async with aiofiles.open(cache, encoding="utf-8") as handle:
            return ParseResult.model_validate_json(await handle.read())

    async def _save_parsed(self, document: Document, parsed: ParseResult) -> None:
        """原子写入解析缓存。

        ⚠️ 不能直接以 ``"w"`` 打开目标文件——那会**立即清空**它，而摄取是后台任务，
        此刻前端很可能正在调 ``GET /documents/{id}/content`` 读同一个文件，
        于是读到半截或空内容，报 ``Invalid JSON: EOF``（实测约 1/10 概率）。
        改为「同目录临时文件 + ``os.replace``」：重命名是原子操作，
        读者要么看到旧的完整内容，要么看到新的完整内容，不存在中间态。
        """
        cache = self._cache_dir(document.space_id)
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / f"{document.id}.parse.json"
        # 临时文件必须与目标同目录，否则跨文件系统时 os.replace 不是原子的
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        try:
            async with aiofiles.open(tmp, "w", encoding="utf-8") as handle:
                await handle.write(parsed.model_dump_json())
            await asyncio.to_thread(os.replace, tmp, target)
        except BaseException:
            await asyncio.to_thread(tmp.unlink, True)
            raise

    async def _stage_embedding(
        self,
        document: Document,
        on_progress: ProgressCallback | None,
        on_status: StatusCallback | None,
    ) -> None:
        await self._set_status(document.id, "embedding", on_status)
        chunks = await self.db.chunks.list_by_document(document.id)
        if not chunks:
            await self._report(on_progress, document.id, "embedding", 0, 0)
            return
        vectors = await self.db.require_vectors()
        embedder = self.registry.embedding(purpose="ingest")
        batch = self.settings.embedding_batch_size
        windows = [chunks[start : start + batch] for start in range(0, len(chunks), batch)]
        prefix = index_prefix(document)
        total = len(chunks)

        def payload(window: Sequence[Chunk]) -> list[str]:
            # 概要切片本身已是文档级上下文，不再叠加前缀
            return [
                chunk.content
                if chunk.kind == "summary" or not prefix
                else f"{prefix}{chunk.content}"
                for chunk in window
            ]

        # 并发只用在「取向量」这一步：批与批之间没有依赖，等待时间可以重叠。
        # 落库仍然串行——向量表是共享资源，并发写没有收益，只会引入冲突。
        # 进程内加载的本地模型不能并发（会争用设备），由提供方自己声明。
        concurrency = (
            self.settings.embedding_concurrency
            if getattr(embedder, "concurrent_safe", False)
            else 1
        )
        limit = asyncio.Semaphore(max(1, concurrency))

        async def fetch(window: Sequence[Chunk]) -> list[list[float]]:
            async with limit:
                return await embedder.embed(payload(window), kind="doc")

        results = await asyncio.gather(*(fetch(window) for window in windows))

        done = 0
        for window, embeddings in zip(windows, results, strict=True):
            if len(embeddings) != len(window):
                raise ValidationError(
                    "向量数量与切片数量不一致",
                    detail={"expected": len(window), "actual": len(embeddings)},
                )
            await vectors.upsert(
                "chunks_vec",
                [
                    VectorRecord(
                        id=chunk.id,
                        space_id=chunk.space_id,
                        document_id=chunk.document_id,
                        vector=vector,
                        embedding_model=embedder.name,
                    )
                    for chunk, vector in zip(window, embeddings, strict=True)
                ],
            )
            done += len(window)
            await self._report(on_progress, document.id, "embedding", done, total)

    async def _stage_extracting(
        self,
        document: Document,
        on_progress: ProgressCallback | None,
        on_status: StatusCallback | None,
    ) -> None:
        """L2 知识卡片与实体关系抽取。

        抽取是**增值步骤**：走到这里 L1 的切片与向量都已经可用，用户已经能检索到
        这篇文档了。因此失败绝不能把文档判成 failed——那等于把一份可用的资料
        藏起来。原因写进 ``meta.extraction_error`` 并留在日志里，状态照常走到 ready。
        """
        await self._set_status(document.id, "extracting", on_status)
        try:
            extractor = await KnowledgeExtractor.for_space(
                space_id=document.space_id,
                database=self.db,
                registry=self.registry,
                settings=self.settings,
            )
            report = await extractor.extract_document(document, on_progress=on_progress)
        except Exception as exc:
            logger.warning(
                "l2_extraction_failed",
                space_id=document.space_id,
                document_id=document.id,
                error=str(exc),
            )
            await self._merge_meta(document, {"extraction_error": str(exc)})
            return

        stats = report.documents[0] if report.documents else None
        patch: dict[str, Any] = {}
        if stats is not None:
            patch["extraction"] = stats.model_dump(mode="json")
        if report.failures:
            patch["extraction_error"] = "; ".join(failure.message for failure in report.failures)
        elif stats is not None and stats.batches and stats.batches_failed == stats.batches:
            patch["extraction_error"] = "全部批次抽取失败，详见服务端日志"
        if patch:
            await self._merge_meta(document, patch)

    async def _merge_meta(self, document: Document, patch: dict[str, Any]) -> None:
        """把若干键合进文档 ``meta``。

        重新读一次文档再写：摄取期间 ``meta`` 可能已被别的阶段改过
        （页数、原始路径），拿旧快照覆盖会把它们抹掉。
        """
        current = await self.db.documents.require(document.id)
        meta = current.meta.model_copy(update=patch)
        await self.db.documents.update(document.id, DocumentUpdate(meta=meta))

    async def _report(
        self,
        on_progress: ProgressCallback | None,
        document_id: str,
        stage: str,
        done: int,
        total: int,
    ) -> None:
        if on_progress is None:
            return
        percent = round(done / total * 100, 2) if total else 100.0
        await on_progress(
            ProgressEvent(
                document_id=document_id,
                stage=stage,
                done=done,
                total=total,
                percent=percent,
            )
        )

    # -- 辅助 -------------------------------------------------------------

    def _cache_dir(self, space_id: str) -> Path:
        return self.settings.data_dir / "cache" / space_id

    async def document_markdown(self, document: Document) -> str:
        """读取解析后的 Markdown 全文。

        摄取是后台任务，用户点开刚投喂的文档时解析缓存很可能还没落地。
        对 ``paste`` / ``url`` 来说原文本身就是 Markdown，直接回退读 ``raw/``——
        没有理由让用户等向量化跑完才能看到自己刚粘进去的内容。
        只有必须经解析器处理的 ``file`` 才在缓存缺失时报错。
        """
        try:
            parsed = await self._load_parsed(document)
        except ParseFailedError as exc:
            fallback = await self._raw_markdown(document)
            if fallback is not None:
                return fallback
            raise NotFoundError("解析结果", document.id) from exc
        return parsed.markdown

    async def _raw_markdown(self, document: Document) -> str | None:
        """``paste`` / ``url`` 的原文即 Markdown；其余来源返回 ``None``。

        ⚠️ 必须和解析路径做**同样的归一化**（`split_pages` 会统一换行符并去首尾空白）。
        否则同一个请求会因为后台摄取有没有跑完而返回不同的字节：摄取前拿到原始文本、
        摄取后拿到解析文本。这不只是「差个换行」——切片的 ``char_start`` /
        ``char_end`` 是按**解析后**的正文算的，阅读器若渲染未归一化的原文，
        引用高亮的位置会整体偏移。
        """
        if document.source_type not in ("paste", "url"):
            return None
        path = await asyncio.to_thread(
            locate_raw_file,
            self._raw_dir(document.space_id),
            document.meta.raw_path or document.source_uri,
        )
        if path is None:
            return None
        async with aiofiles.open(path, encoding="utf-8") as handle:
            raw = await handle.read()
        normalized, _pages = split_pages(raw)
        return normalized

    async def write_raw(self, space_id: str, document_id: str, filename: str, data: bytes) -> Path:
        """把原始文件落到 ``raw/`` 目录。"""
        directory = self._raw_dir(space_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{document_id}-{filename}"
        async with aiofiles.open(target, "wb") as handle:
            await handle.write(data)
        return target

    async def drafts_of(self, document: Document) -> list[ChunkDraft]:
        """取该文档的切分草稿（调试用）。"""
        parsed = await self._load_parsed(document)
        return await asyncio.to_thread(
            split_parse_result,
            parsed,
            target_tokens=self.settings.chunk_size,
            overlap_tokens=self.settings.chunk_overlap,
        )
