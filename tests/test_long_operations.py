"""长任务的互斥许可必须覆盖整个 SSE，并在失败和断开时释放。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from fastapi.responses import StreamingResponse

from agentmem.errors import SpaceLockedError
from agentmem.store.operations import OperationGate
from apps.api.sse import SseEvent, event, sse_response


def test_activity_rejects_duplicate_and_blocks_maintenance() -> None:
    gate = OperationGate()
    with gate.activity("评测"):
        with pytest.raises(SpaceLockedError, match="评测正在运行"), gate.activity("评测"):
            pass
        with pytest.raises(SpaceLockedError):
            gate.lock()
    with gate.maintenance():
        pass


def test_activity_releases_after_failure_and_refuses_maintenance_space() -> None:
    gate = OperationGate()
    with pytest.raises(ValueError), gate.activity("进化"):
        raise ValueError("failed")
    with gate.maintenance(), pytest.raises(SpaceLockedError), gate.activity("进化"):
        pass
    with gate.activity("进化"):
        pass


async def test_stream_scope_holds_until_nested_generator_cleanup() -> None:
    gate = OperationGate()
    cleaned = asyncio.Event()

    async def source() -> AsyncGenerator[SseEvent, None]:
        try:
            yield event("stage", {"status": "running"})
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    response = sse_response(source(), operation=gate.activity("进化"))
    iterator = response.body_iterator.__aiter__()
    assert "running" in str(await anext(iterator))
    with pytest.raises(SpaceLockedError):
        gate.lock()
    await iterator.aclose()  # type: ignore[attr-defined]
    assert cleaned.is_set()
    with gate.maintenance():
        pass


async def test_duplicate_stream_returns_business_error_without_running_source() -> None:
    gate = OperationGate()
    called = False

    async def source() -> AsyncGenerator[SseEvent, None]:
        nonlocal called
        called = True
        yield event("done", {})

    with gate.activity("评测"):
        response: StreamingResponse = sse_response(source(), operation=gate.activity("评测"))
        frames = [str(frame) async for frame in response.body_iterator]
        assert len(frames) == 1
        assert "SPACE_LOCKED" in frames[0]
        assert not called
    with gate.activity("评测"):
        pass


async def test_failed_stream_releases_its_operation() -> None:
    gate = OperationGate()

    async def source() -> AsyncGenerator[SseEvent, None]:
        yield event("stage", {})
        raise RuntimeError("provider failed")

    response = sse_response(source(), operation=gate.activity("评测"))
    frames = [str(frame) async for frame in response.body_iterator]
    assert "INTERNAL_ERROR" in frames[-1]
    with gate.maintenance():
        pass
