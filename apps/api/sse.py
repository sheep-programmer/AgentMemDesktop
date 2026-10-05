"""统一 SSE 封装。

所有流式路由都通过 :func:`sse_response` 输出，路由侧只负责产出 :class:`SseEvent`，
不自己拼字符串。封装负责：

- 事件格式化（``event:`` / ``data:`` / 空行分隔）；
- 心跳（默认 15 秒一条注释行，防中间层断流）；
- 客户端断开时取消底层 ``asyncio.Task`` 并关闭异步生成器。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from typing import Any

import structlog
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agentmem.errors import AgentMemError
from agentmem.types import ProgressEvent, SseErrorEvent, StatusEvent

logger = structlog.get_logger(__name__)

#: 心跳间隔（秒）
HEARTBEAT_SECONDS = 15.0

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class SseEvent(BaseModel):
    """一条 SSE 事件。"""

    event: str
    #: 载荷：Pydantic 模型（自动序列化）、已是 JSON 友好的字典，或原始字符串。
    #:
    #: ⚠️ 联合类型里 ``dict`` 必须排在 ``BaseModel`` 前面。反过来写时 pydantic 的
    #: 联合校验会把传入的 dict 塞进一个空的 ``BaseModel`` 实例里——构造期不报错，
    #: 直到 ``format_event`` 调 ``model_dump_json()`` 才抛 ``PydanticUserError``，
    #: 而那时 SSE 响应头已经发出去了，客户端只会看到一条断掉的流。
    data: dict[str, Any] | BaseModel | str


def event(name: str, payload: BaseModel | dict[str, Any] | str) -> SseEvent:
    """构造一条 SSE 事件。"""
    return SseEvent(event=name, data=payload)


def progress_event(payload: ProgressEvent) -> SseEvent:
    """摄取进度事件。"""
    return event("progress", payload)


def status_event(payload: StatusEvent) -> SseEvent:
    """摄取状态事件。"""
    return event("status", payload)


def error_event(payload: SseErrorEvent) -> SseEvent:
    """流内错误事件。"""
    return event("error", payload)


def error_frame(exc: Exception) -> SseEvent:
    """把流内异常转成一条 ``error`` 事件。

    响应头在第一个事件发出时就已经落地，业务异常这时再往外抛，客户端只会看到
    一条断掉的流，连接是被重置还是被截断取决于中间的代理。所以业务错误必须在流内
    如实说清楚；非预期异常记日志，对外仍给一个通用错误码。
    """
    if isinstance(exc, AgentMemError):
        return error_event(SseErrorEvent(code=exc.code, message=exc.message))
    logger.exception("sse_stream_failed", error=str(exc))
    return error_event(SseErrorEvent(code="INTERNAL_ERROR", message=str(exc)))


def format_event(item: SseEvent) -> str:
    """把事件格式化成 SSE 文本帧。"""
    payload = item.data
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json()
    elif isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(payload, ensure_ascii=False)
    lines = [f"event: {item.event}"]
    lines.extend(f"data: {line}" for line in body.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


async def _next_event(iterator: AsyncIterator[SseEvent]) -> SseEvent:
    """取下一个事件。"""
    return await anext(iterator)


def sse_response(
    events: AsyncIterator[SseEvent],
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    operation: AbstractContextManager[object] | None = None,
) -> StreamingResponse:
    """把事件流包装成 ``text/event-stream`` 响应。

    Args:
        events: 事件异步迭代器。
        heartbeat_seconds: 心跳间隔；``<=0`` 表示关闭心跳。

    Returns:
        Starlette 流式响应。客户端断开时，生成器被取消，进而取消正在等待的底层任务。
    """

    async def scoped_events() -> AsyncIterator[SseEvent]:
        with operation if operation is not None else contextlib.nullcontext():
            iterator = events.__aiter__()
            try:
                async for item in iterator:
                    yield item
            finally:
                closer = getattr(iterator, "aclose", None)
                if closer is not None:
                    with contextlib.suppress(Exception):
                        await closer()

    async def stream() -> AsyncIterator[str]:
        iterator = scoped_events() if operation is not None else events.__aiter__()
        pending: asyncio.Task[SseEvent] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(_next_event(iterator))
                done, _ = await asyncio.wait(
                    {pending}, timeout=heartbeat_seconds if heartbeat_seconds > 0 else None
                )
                if not done:
                    yield ": ping\n\n"
                    continue
                task, pending = pending, None
                try:
                    item = task.result()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    yield format_event(error_frame(exc))
                    break
                yield format_event(item)
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await pending
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=dict(SSE_HEADERS))


def single_event_response(payload: SseEvent) -> StreamingResponse:
    """只发一条事件的流（用于短任务）。"""

    async def generate() -> AsyncIterator[SseEvent]:
        yield payload

    return sse_response(generate())
