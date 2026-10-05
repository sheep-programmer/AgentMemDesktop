"""§4 文档 / L0-L1。"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import FileResponse, Response

from agentmem.errors import AgentMemError, NotFoundError, ValidationError
from agentmem.ingest.batch import run_documents
from agentmem.ingest.bus import IngestBus, IngestEvent
from agentmem.ingest.parse import fetch_url_markdown
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.security import locate_raw_file
from agentmem.store.base import new_id
from agentmem.store.worker import run_db_worker
from agentmem.types import (
    Chunk,
    DeleteResponse,
    Document,
    DocumentContent,
    DocumentStatus,
    DocumentUpdate,
    Page,
    PasteDocumentRequest,
    ProgressEvent,
    QueueEvent,
    StatusEvent,
    UploadRejection,
    UploadResponse,
    UrlDocumentRequest,
)

from ..deps import BusDep, DatabaseDep, PipelineDep, RuntimeDep
from ..sse import SseEvent, event, sse_response

logger = structlog.get_logger(__name__)

#: 单个上传文件的体量上限，与前端投喂弹窗写的「单文件 ≤ 100MB」保持一致。
#: 两边必须一起改——只改一边就会变成界面承诺了后端不认的限制。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_READ_CHUNK = 1024 * 1024

router = APIRouter(prefix="/spaces/{space_id}/documents", tags=["documents"])
ingest_router = APIRouter(prefix="/spaces/{space_id}/ingest", tags=["documents"])

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]
StatusCallback = Callable[[StatusEvent], Awaitable[None]]


def _callbacks(bus: IngestBus, space_id: str) -> tuple[ProgressCallback, StatusCallback]:
    """构造把进度/状态事件转发到总线的回调。"""

    async def on_progress(payload: ProgressEvent) -> None:
        bus.publish(space_id, payload)

    async def on_status(payload: StatusEvent) -> None:
        bus.publish(space_id, payload)

    return on_progress, on_status


def _start_ingest(
    bus: IngestBus, pipeline: IngestPipeline, document: Document, space_id: str
) -> None:
    """把摄取放到后台执行，并通过总线推进度。"""
    on_progress, on_status = _callbacks(bus, space_id)
    reservation = ExitStack()
    reservation.enter_context(pipeline.db.operations.reserve_document(document.id))
    try:
        task = bus.spawn(
            space_id,
            f"ingest:{document.id}",
            pipeline.run(document.id, on_progress=on_progress, on_status=on_status),
        )
    except BaseException:
        reservation.close()
        raise
    task.add_done_callback(lambda _task: reservation.close())


@router.get("", response_model=Page[Document], summary="文档列表")
async def list_documents(
    space_id: str,
    database: DatabaseDep,
    status: Annotated[DocumentStatus | None, Query(description="按状态过滤")] = None,
    q: Annotated[str | None, Query(description="标题/来源关键词")] = None,
    tag: Annotated[str | None, Query(description="标签")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[Document]:
    """支持 ``?status=&q=&tag=`` 过滤与游标分页。"""
    items, total, next_cursor = await database.documents.list_by_space(
        space_id, status=status, query=q, tag=tag, limit=limit, cursor=cursor
    )
    return Page[Document](items=items, total=total, next_cursor=next_cursor)


@router.post("/upload", response_model=UploadResponse, summary="上传文档（多文件）")
async def upload_documents(
    space_id: str,
    runtime: RuntimeDep,
    bus: BusDep,
    pipeline: PipelineDep,
    files: Annotated[list[UploadFile], File(description="支持多文件")],
) -> UploadResponse:
    """立即返回 ``status=pending`` 的文档，解析与向量化在后台异步进行。"""
    # 重建索引期间不接受新的摄取：那时向量表正在被重建
    runtime.require_unlocked(space_id)
    with runtime.spaces.operation_gate(space_id).writing():
        documents: list[Document] = []
        rejected: list[UploadRejection] = []
        first_error: AgentMemError | None = None
        # 每个文件单独收：此前一个文件出错（最常见的是拖进来一份库里已有的）整批报失败，
        # 可排在它前面的文件已经登记、正在后台处理——界面说「上传失败」，列表里却冒出新文档
        for upload in files:
            filename = Path(upload.filename or "document").name
            try:
                payload = await _read_limited(upload)
                path, created = await _stash_upload(runtime, space_id, filename, payload)
                try:
                    document = await pipeline.register_file(
                        space_id=space_id,
                        path=path,
                        title=filename,
                        mime=upload.content_type or mimetypes.guess_type(filename)[0],
                    )
                except AgentMemError:
                    # 每个上传请求拥有独立路径，失败时只清理自己的原文副本。
                    if created:
                        await asyncio.to_thread(path.unlink, True)
                    raise
            except AgentMemError as exc:
                first_error = first_error or exc
                rejected.append(
                    UploadRejection(filename=filename, code=exc.code, message=exc.message)
                )
                continue
            documents.append(document)
            _start_ingest(bus, pipeline, document, space_id)
        if not documents and first_error is not None:
            # 一个都没收下：保持原来的错误语义（单文件重复仍是 409 等）
            raise first_error
        return UploadResponse(documents=documents, rejected=rejected)


async def _read_limited(upload: UploadFile) -> bytes:
    """分块读上传内容，超过上限立刻停下。

    此前先 ``upload.read()`` 整个读进内存再比大小：一个 2GB 的文件在报错之前就已经
    占了 2GB 内存，这条上限拦不住它本该拦的事。
    """
    parts: list[bytes] = []
    size = 0
    while chunk := await upload.read(UPLOAD_READ_CHUNK):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise ValidationError(
                f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
                detail={"filename": upload.filename},
            )
        parts.append(chunk)
    if size == 0:
        raise ValidationError("上传文件为空", detail={"filename": upload.filename})
    return b"".join(parts)


@router.post("/url", response_model=Document, status_code=201, summary="抓取网页")
async def add_url_document(
    space_id: str,
    runtime: RuntimeDep,
    bus: BusDep,
    pipeline: PipelineDep,
    payload: UrlDocumentRequest,
) -> Document:
    """抓取网页正文并入库。

    说明：``crawl_depth`` 目前只支持 0，多级爬取留待后续阶段。
    """
    # 重建索引期间不接受新的摄取：那时向量表正在被重建。
    # 这一句原本被误写在 docstring **里面**，函数也没收 runtime 参数——
    # 于是三条摄取路径（上传 / 粘贴 / URL）里只有 URL 这条不受锁保护，
    # 重建索引进行中仍能写进来。
    runtime.require_unlocked(space_id)
    if payload.crawl_depth:
        raise ValidationError(
            "多级爬取尚未支持，请将 crawl_depth 设为 0",
            detail={"crawl_depth": payload.crawl_depth},
        )
    title, markdown = await fetch_url_markdown(payload.url)
    document = await pipeline.register_text(space_id=space_id, title=title, content=markdown)
    document = await pipeline.db.documents.update(
        document.id, DocumentUpdate(source_type="url", source_uri=payload.url)
    )
    _start_ingest(bus, pipeline, document, space_id)
    return document


@router.post("/paste", response_model=Document, status_code=201, summary="粘贴文本")
async def add_paste_document(
    space_id: str,
    runtime: RuntimeDep,
    bus: BusDep,
    pipeline: PipelineDep,
    payload: PasteDocumentRequest,
) -> Document:
    """直接粘贴文本入库。"""
    # 重建索引期间不接受新的摄取：那时向量表正在被重建
    runtime.require_unlocked(space_id)
    document = await pipeline.register_text(
        space_id=space_id, title=payload.title, content=payload.content
    )
    _start_ingest(bus, pipeline, document, space_id)
    return document


@router.get("/{document_id}", response_model=Document, summary="文档详情")
async def get_document(database: DatabaseDep, document_id: str) -> Document:
    """取文档元数据。"""
    return await database.documents.require(document_id)


@router.get(
    "/{document_id}/content",
    response_model=DocumentContent,
    summary="解析后的 Markdown 全文",
)
async def get_document_content(pipeline: PipelineDep, document_id: str) -> DocumentContent:
    """返回解析后的 Markdown，供阅读器渲染。"""
    document = await pipeline.db.documents.require(document_id)
    markdown = await pipeline.document_markdown(document)
    return DocumentContent(document_id=document.id, title=document.title, markdown=markdown)


@router.get("/{document_id}/chunks", response_model=Page[Chunk], summary="切片列表")
async def list_chunks(
    database: DatabaseDep,
    document_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
) -> Page[Chunk]:
    """按文档内顺序分页列出切片。

    阅读器要按 ``char_start`` 定位某一条切片，长文档翻页拿全是必须的——
    取不满就等于「这条切片不存在」，高亮会静默退化。
    """
    await database.documents.require(document_id)
    items, total, next_cursor = await database.chunks.page_by_document(
        document_id, limit=limit, cursor=cursor
    )
    return Page[Chunk](items=items, total=total, next_cursor=next_cursor)


@router.get("/{document_id}/raw", summary="下载原始文件")
async def download_raw(
    runtime: RuntimeDep, database: DatabaseDep, space_id: str, document_id: str
) -> Response:
    """下载原始文件。

    ⚠️ 安全要点：``raw_path`` / ``source_uri`` **不是可信输入**——
    Space 可以从 zip 导入，压缩包里的 ``meta.db`` 由外部提供，
    其中的路径字段可以被构造成 ``/etc/passwd`` 这类绝对路径。
    因此必须把目标限制在本 Space 的 ``raw/`` 目录内才允许读取。
    """
    document = await database.documents.require(document_id)
    # 越界与不存在一律按「不存在」对外表现，不告诉调用方它猜的路径是否真的存在；
    # 路径失效（换目录启动、备份还原到别处）时按文件名回 raw/ 里找
    path = await asyncio.to_thread(
        locate_raw_file,
        runtime.spaces.raw_dir(space_id),
        document.meta.raw_path or document.source_uri,
    )
    if path is None:
        raise NotFoundError("原始文件", document_id)
    return FileResponse(
        path, media_type=document.mime or "application/octet-stream", filename=path.name
    )


@router.delete("/{document_id}", response_model=DeleteResponse, summary="删除文档")
async def delete_document(runtime: RuntimeDep, space_id: str, document_id: str) -> DeleteResponse:
    """级联删除切片、向量、全文索引与解析缓存。

    级联顺序与缓存路径是 core 的实现细节，路由只发起一次调用。
    """
    return await runtime.delete_document(space_id, document_id)


@router.post(
    "/retry-failed",
    summary="重试所有失败的文档（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def retry_failed_documents(
    space_id: str, runtime: RuntimeDep, pipeline: PipelineDep
) -> Response:
    """把该 Space 里失败的文档整条链路重跑一遍，SSE 推送逐篇进度。

    失败的常见原因是 provider 当时不可用、文件格式不支持、或上次进程被中断——
    等用户一个个点「重新解析」既慢又容易漏。每篇失败只记一条 error 事件，
    不阻断其它文档。
    """
    await runtime.spaces.require_space(space_id)
    runtime.require_unlocked(space_id)

    async def generate() -> AsyncIterator[SseEvent]:
        # 无待重试资料时没有写操作，正在摄取的其他文档不应阻止空结果返回。
        _items, failures, _cursor = await pipeline.db.documents.list_by_space(
            space_id, status="failed", limit=1
        )
        if failures == 0:
            yield event("begin", {"total": 0})
            yield event("done", {"total": 0, "retried": 0})
            return
        with runtime.maintain_space(space_id) as token:
            batch_pipeline = IngestPipeline(
                pipeline.db, pipeline.registry, pipeline.settings, operation_token=token
            )
            document_ids: list[str] = []
            cursor: str | None = None
            while True:
                documents, _total, cursor = await pipeline.db.documents.list_by_space(
                    space_id, status="failed", limit=200, cursor=cursor
                )
                document_ids.extend(document.id for document in documents)
                if not cursor or not documents:
                    break
            total = len(document_ids)
            yield event("begin", {"total": total})
            done = 0
            async for outcome in run_documents(
                batch_pipeline,
                document_ids,
                concurrency=runtime.settings.ingest_concurrency,
            ):
                if not outcome.ok:
                    yield event(
                        "error", {"document_id": outcome.document_id, "message": outcome.error}
                    )
                    continue
                done += 1
                yield event(
                    "progress",
                    {
                        "document_id": outcome.document_id,
                        "stage": "retry",
                        "done": done,
                        "total": total,
                        "percent": round(done / total * 100, 2) if total else 100.0,
                    },
                )
            yield event("done", {"total": total, "retried": done})

    return sse_response(generate())


@router.post("/{document_id}/reprocess", response_model=Document, summary="重新解析")
async def reprocess_document(
    space_id: str, runtime: RuntimeDep, bus: BusDep, pipeline: PipelineDep, document_id: str
) -> Document:
    """重新走一遍 解析 → 切分 → 向量化 链路。"""
    # 重建索引期间不接受新的摄取：那时向量表正在被重建
    runtime.require_unlocked(space_id)
    document = await pipeline.db.documents.require(document_id)
    _start_ingest(bus, pipeline, document, space_id)
    return document


@ingest_router.get("/stream", summary="全局摄取进度流（SSE）")
async def ingest_stream(runtime: RuntimeDep, bus: BusDep, space_id: str) -> Response:
    """把该 Space 的摄取进度、状态与错误实时推送给前端。

    先校验 Space 存在：这里此前直接订阅总线，对着一个不存在的 Space 也照样返回 200，
    然后把连接一直挂着。既掩盖了调用方的 bug（别的端点这时都是 404），
    又白留一个永远等不到事件的订阅。
    """
    await runtime.spaces.require_space(space_id)

    async def generate() -> AsyncIterator[SseEvent]:
        async with bus.subscribe(space_id) as events:
            async for payload in events:
                yield _to_sse(payload)

    return sse_response(generate())


def _to_sse(payload: IngestEvent) -> SseEvent:
    """把总线事件映射成 SSE 帧。"""
    if isinstance(payload, ProgressEvent):
        return event("progress", payload)
    if isinstance(payload, StatusEvent):
        return event("status", payload)
    if isinstance(payload, QueueEvent):
        return event("queue", payload)
    return event("error", payload)


async def _stash_upload(
    runtime: RuntimeDep, space_id: str, filename: str, payload: bytes
) -> tuple[Path, bool]:
    """每次上传拥有独立原文路径，重复请求不能清理掉另一请求的文件。"""
    directory = runtime.spaces.raw_dir(space_id)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()[:16]
    target = directory / f"{digest}-{new_id()}-{filename}"
    created = False

    def write() -> None:
        nonlocal created
        with target.open("xb") as handle:
            created = True
            handle.write(payload)

    try:
        await run_db_worker(write)
    except BaseException:
        if created:
            await run_db_worker(target.unlink, True)
        raise
    return target, True
