"""§5 检索与对话。

路由只做三件事：解包请求 → 调 core（``runtime`` 上的聚合方法）→ 包装响应。
检索与生成的编排全部在 ``agentmem.retrieve`` 里。

SSE 的分帧统一走 ``apps/api/sse.py``；本模块只负责把 core 的事件流转成
:class:`~apps.api.sse.SseEvent`，并在客户端断开时取消底层任务。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import Response

from agentmem.errors import ValidationError
from agentmem.retrieve import ChatEvent, ChatService, aclose_stream, generation_registry
from agentmem.retrieve.models import snippet_of
from agentmem.types import (
    ChatRequest,
    Chunk,
    Citation,
    Conversation,
    ConversationCreate,
    ConversationDetail,
    ConversationUpdate,
    DeleteResponse,
    Page,
    SearchRequest,
    SearchResponse,
    StopResponse,
)

from ..deps import RuntimeDep
from ..sse import SseEvent, event, sse_response

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["chat"])

#: 事件桥的队列上限：客户端读得慢时靠它把背压传回模型流，避免无限堆积
QUEUE_SIZE = 256


@router.post("/spaces/{space_id}/search", response_model=SearchResponse, summary="纯检索")
async def search(space_id: str, payload: SearchRequest, runtime: RuntimeDep) -> SearchResponse:
    """混合检索（向量 + FTS + RRF + 重排），返回带各路分数的命中列表。

    用于「检索调试面板」，不生成回答。
    """
    pipeline = await runtime.retrieval_pipeline(space_id)
    outcome = await pipeline.search(payload.query, mode=payload.mode, top_k=payload.top_k)
    focus = await pipeline.focus(payload.query, outcome.chunks)
    return pipeline.to_search_response(outcome, focus)


@router.get(
    "/spaces/{space_id}/conversations",
    response_model=Page[Conversation],
    summary="会话列表",
)
async def list_conversations(
    space_id: str,
    runtime: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    q: Annotated[str | None, Query(max_length=200, description="按会话标题搜索")] = None,
) -> Page[Conversation]:
    """按标题搜索某 Space 的全部会话，结果支持游标分页。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.spaces.space_db(space_id)
    items, total, next_cursor = await database.conversations.list_by_space(
        space_id, limit=limit, cursor=cursor, query=q
    )
    return Page[Conversation](items=items, total=total, next_cursor=next_cursor)


@router.post(
    "/spaces/{space_id}/conversations",
    response_model=Conversation,
    status_code=201,
    summary="新建会话",
)
async def create_conversation(
    space_id: str, payload: ConversationCreate, runtime: RuntimeDep
) -> Conversation:
    """新建会话。

    ``space_id`` 同时出现在路径与请求体里：以路径为准，不一致直接拒绝，
    避免出现「会话建在了另一个 Space」这种静默错位。
    """
    if payload.space_id != space_id:
        raise ValidationError(
            "路径中的 space_id 与请求体不一致",
            detail={"path": space_id, "body": payload.space_id},
        )
    await runtime.spaces.require_space(space_id)
    database = await runtime.spaces.space_db(space_id)
    return await database.conversations.create(payload)


def _location_of(chunk: Chunk | None, citation: Citation) -> dict[str, object]:
    """引用的原文定位以切片为准；切片已被删掉时保留落库时的值。"""
    if chunk is None:
        return {}
    return {
        "page": citation.page if citation.page is not None else chunk.page,
        "heading_path": citation.heading_path or chunk.heading_path,
        "ordinal": chunk.ordinal,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "kind": chunk.kind,
    }


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="会话详情",
)
async def get_conversation(conversation_id: str, runtime: RuntimeDep) -> ConversationDetail:
    """含消息列表。"""
    space_id, conversation = await runtime.find_conversation(conversation_id)
    database = await runtime.spaces.space_db(space_id)
    messages = await database.messages.list_by_conversation(conversation_id)
    # 引用记录是随消息一起落库的，早于「引用带标题」那次改动，读出来时补上；
    # 否则历史会话里的引用只会显示成「未命名文献」
    missing = {
        citation.document_id
        for message in messages
        for citation in message.citations
        if citation.document_title is None
    }
    titles = await database.documents.titles_by_ids(list(missing)) if missing else {}

    # 片段按当前算法重算一遍：它随消息落库，早于「保留换行」那次改动，老片段是被压成
    # 一行的（表格因此在界面上退化成一行竖线）。切片还在就重算，不在了就保留原样。
    chunk_ids = [citation.chunk_id for message in messages for citation in message.citations]
    chunks = await database.chunks.get_many(chunk_ids) if chunk_ids else []
    by_id = {chunk.id: chunk for chunk in chunks}
    snippets = {chunk.id: snippet_of(chunk.content) for chunk in chunks}

    # 前端要用 message.trace_id 拉「为什么这么答」面板与提交反馈；消息表里没有这一列，
    # 按 message_id 关联 traces 读出来
    trace_ids = await database.traces.trace_ids_by_message([message.id for message in messages])
    feedback = await database.feedback.kinds_by_traces(list(trace_ids.values()))

    if titles or snippets or trace_ids:
        messages = [
            message.model_copy(
                update={
                    "trace_id": trace_ids.get(message.id),
                    "feedback": feedback.get(trace_ids.get(message.id, ""), []),
                    "citations": [
                        citation.model_copy(
                            update={
                                "document_title": citation.document_title
                                or titles.get(citation.document_id),
                                "snippet": snippets.get(citation.chunk_id, citation.snippet),
                                # 老记录没存定位；切片还在就从切片补，界面才能标出「第几节第几段」
                                **_location_of(by_id.get(citation.chunk_id), citation),
                            }
                        )
                        for citation in message.citations
                    ],
                }
            )
            for message in messages
        ]
    return ConversationDetail(**conversation.model_dump(), messages=messages)


@router.patch("/conversations/{conversation_id}", response_model=Conversation, summary="修改会话")
async def update_conversation(
    conversation_id: str, payload: ConversationUpdate, runtime: RuntimeDep
) -> Conversation:
    """改标题或置顶。"""
    space_id, _ = await runtime.find_conversation(conversation_id)
    database = await runtime.spaces.space_db(space_id)
    return await database.conversations.update(conversation_id, payload)


@router.delete(
    "/conversations/{conversation_id}", response_model=DeleteResponse, summary="删除会话"
)
async def delete_conversation(conversation_id: str, runtime: RuntimeDep) -> DeleteResponse:
    """删除会话及其消息。"""
    space_id, _ = await runtime.find_conversation(conversation_id)
    await generation_registry().cancel_and_wait(conversation_id=conversation_id)
    database = await runtime.spaces.space_db(space_id)
    deleted = await database.conversations.delete(conversation_id)
    return DeleteResponse(deleted=deleted, id=conversation_id)


@router.post(
    "/conversations/{conversation_id}/chat",
    summary="流式问答（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def chat(conversation_id: str, payload: ChatRequest, runtime: RuntimeDep) -> Response:
    """SSE 事件序列：trace_start → rewrite → retrieval → insights → delta → citation → done。

    失败时以 ``error`` 事件结束。
    """
    service = await runtime.chat_service(conversation_id)
    return sse_response(_bridge(conversation_id, service, payload))


@router.post(
    "/conversations/{conversation_id}/stop", response_model=StopResponse, summary="中断生成"
)
async def stop_generation(conversation_id: str, runtime: RuntimeDep) -> StopResponse:
    """中断正在进行的生成。"""
    await runtime.find_conversation(conversation_id)
    return StopResponse(stopped=generation_registry().cancel(conversation_id))


async def _bridge(
    conversation_id: str, service: ChatService, payload: ChatRequest
) -> AsyncIterator[SseEvent]:
    """把 core 的事件流桥接成 SSE 帧。

    生成跑在独立的 ``asyncio.Task`` 上而不是当前请求任务里，原因有两个：

    1. 「停止」要从**另一个请求**里取消它，登记在册的必须是一个真正的任务；
    2. 客户端断开时只需取消这个任务，模型流随之关闭，不会留下悬挂连接。

    队列同时充当背压：客户端读得慢时 ``put`` 会挂起，模型流跟着变慢。
    """
    registry = generation_registry()
    queue: asyncio.Queue[ChatEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)

    previous: asyncio.Task[None] | None = None

    async def pump() -> None:
        # 替换同会话的请求时，先等旧回答清理与落库，防止两轮历史交叉。
        if previous is not None and not previous.done():
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:
                logger.warning("previous_generation_failed", conversation_id=conversation_id)
        stream = service.stream(conversation_id, payload)
        try:
            async for item in stream:
                await queue.put(item)
        finally:
            await aclose_stream(stream)

    task = asyncio.create_task(pump(), name=f"chat:{conversation_id}")
    previous = registry.register(conversation_id, task, space_id=getattr(service, "space_id", None))
    reader: asyncio.Task[ChatEvent] | None = None
    try:
        while True:
            if not queue.empty():
                item = queue.get_nowait()
            elif task.done():
                if not task.cancelled():
                    task.result()  # 异常交给统一 SSE 包装器输出 error 帧。
                break
            else:
                reader = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {reader, task}, return_when=asyncio.FIRST_COMPLETED
                )
                if reader not in done:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader
                    reader = None
                    continue
                item = reader.result()
                reader = None
            yield event(item.name, item.payload)
    finally:
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if not task.done() and not task.cancelling():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        registry.discard(conversation_id, task)
