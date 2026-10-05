"""流式背压、摄取排队和大批量任务的生命周期回归。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Sequence
from typing import cast

import pytest

from agentmem.errors import SpaceLockedError
from agentmem.ingest.batch import run_documents
from agentmem.ingest.bus import IngestBus
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.retrieve import ChatService, aclose_stream, reset_generation_registry
from agentmem.retrieve.models import ChatEvent
from agentmem.types import ChatRequest, DeltaEvent, DoneEvent, QueueEvent
from apps.api.routers.chat import _bridge
from apps.api.sse import event, sse_response


class EventService:
    async def stream(self, _conversation: str, _request: ChatRequest) -> AsyncIterator[ChatEvent]:
        for index in range(30):
            yield ChatEvent(name="delta", payload=DeltaEvent(text=str(index)))
        yield ChatEvent(name="done", payload=DoneEvent(message_id="m", trace_id="t"))


async def test_slow_stream_consumer_loses_no_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.api.routers.chat.QUEUE_SIZE", 2)
    reset_generation_registry()
    frames = []
    async for frame in _bridge(
        "conversation", cast(ChatService, EventService()), ChatRequest(content="问")
    ):
        frames.append(frame)
        await asyncio.sleep(0.001)
    assert [cast(DeltaEvent, frame.data).model_dump() for frame in frames[:-1]] == [
        {"text": str(index)} for index in range(30)
    ]
    assert frames[-1].event == "done"


async def test_queue_counts_are_scoped_to_the_subscribed_space() -> None:
    bus = IngestBus(concurrency=1)
    release = asyncio.Event()

    async def job() -> None:
        await release.wait()

    tasks = [bus.spawn("A", "running", job()), bus.spawn("A", "waiting", job())]
    await asyncio.sleep(0)
    tasks.append(bus.spawn("B", "waiting", job()))
    await asyncio.sleep(0)
    try:
        async with bus.subscribe("B") as events:
            event = await anext(events)
            assert isinstance(event, QueueEvent)
            assert event.running == 0
            assert event.waiting == 1
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_cancelled_queued_job_closes_its_coroutine_and_counters() -> None:
    bus = IngestBus(concurrency=1)
    release = asyncio.Event()

    async def job() -> None:
        await release.wait()

    first = bus.spawn("A", "running", job())
    await asyncio.sleep(0)
    queued_coroutine = job()
    queued = bus.spawn("B", "queued", queued_coroutine)
    await asyncio.sleep(0)
    await bus.cancel_space("B")
    try:
        assert queued.cancelled()
        assert inspect.getcoroutinestate(queued_coroutine) == inspect.CORO_CLOSED
        assert bus.waiting_tasks == 0
    finally:
        release.set()
        await first


async def test_cancel_before_task_starts_cleans_up_registration() -> None:
    bus = IngestBus(concurrency=1)

    async def job() -> None:
        await asyncio.sleep(0)

    coroutine = job()
    task = bus.spawn("A", "cancel-now", coroutine)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert bus.running_tasks() == 0
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


class HoldingPipeline:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def run(self, _document: str, *, stages: Sequence[str] | None = None) -> None:
        self.started.set()
        await self.release.wait()


async def test_batch_schedules_only_a_bounded_window(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = HoldingPipeline()
    original = asyncio.create_task
    created: list[asyncio.Task[object]] = []

    def track(*args: object, **kwargs: object) -> asyncio.Task[object]:
        task: asyncio.Task[object] = original(*args, **kwargs)  # type: ignore[arg-type]
        created.append(task)
        return task

    monkeypatch.setattr("agentmem.ingest.batch.asyncio.create_task", track)
    stream = run_documents(
        cast(IngestPipeline, pipeline), [str(index) for index in range(5000)], concurrency=3
    )
    consumer = original(anext(stream))
    await pipeline.started.wait()
    try:
        assert len(created) <= 3, "并发为 3 时不应创建 5000 个等待任务"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()


async def test_cancellation_timeout_keeps_pending_tasks_visible() -> None:
    bus = IngestBus()
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def job() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = bus.spawn("A", "slow-cleanup", job())
    await entered.wait()
    try:
        with pytest.raises(SpaceLockedError):
            await bus.cancel_space("A", grace_seconds=0.001)
        await cleaning.wait()
        assert bus.running_tasks("A") == 1
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert bus.running_tasks("A") == 0


async def test_stream_producer_error_is_not_silently_swallowed() -> None:
    class BrokenService:
        async def stream(
            self, _conversation: str, _request: ChatRequest
        ) -> AsyncIterator[ChatEvent]:
            yield ChatEvent(name="delta", payload=DeltaEvent(text="partial"))
            raise RuntimeError("producer failed")

    frames = _bridge("broken", cast(ChatService, BrokenService()), ChatRequest(content="问"))
    assert (await anext(frames)).event == "delta"
    with pytest.raises(RuntimeError, match="producer failed"):
        await anext(frames)


async def test_replaced_generation_waits_for_old_cleanup() -> None:
    from agentmem.retrieve import generation_registry

    reset_generation_registry()
    release = asyncio.Event()
    cleaning = asyncio.Event()
    new_started = asyncio.Event()

    class OldService:
        async def stream(
            self, _conversation: str, _request: ChatRequest
        ) -> AsyncIterator[ChatEvent]:
            try:
                yield ChatEvent(name="delta", payload=DeltaEvent(text="old"))
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()

    class NewService:
        async def stream(
            self, _conversation: str, _request: ChatRequest
        ) -> AsyncIterator[ChatEvent]:
            new_started.set()
            yield ChatEvent(name="delta", payload=DeltaEvent(text="new"))

    old = _bridge("same", cast(ChatService, OldService()), ChatRequest(content="old"))
    new = _bridge("same", cast(ChatService, NewService()), ChatRequest(content="new"))
    await anext(old)
    pending = asyncio.ensure_future(anext(new))
    try:
        await cleaning.wait()
        assert not new_started.is_set()
        release.set()
        assert cast(DeltaEvent, (await pending).data).text == "new"
    finally:
        release.set()
        await aclose_stream(old)
        await aclose_stream(new)
    assert generation_registry().active_count() == 0


async def test_generation_cleanup_is_space_scoped_and_waited() -> None:
    from agentmem.retrieve.session import GenerationRegistry

    registry = GenerationRegistry()
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def job() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def other_job() -> None:
        await asyncio.Event().wait()

    first = asyncio.create_task(job())
    other = asyncio.create_task(other_job())
    registry.register("a", first, space_id="A")
    registry.register("b", other, space_id="B")
    await entered.wait()
    draining = asyncio.create_task(registry.cancel_and_wait(space_id="A"))
    await cleaning.wait()
    try:
        assert not draining.done()
        assert not other.cancelling()
    finally:
        release.set()
        await draining
        await registry.cancel_and_wait()
    assert registry.active_count() == 0


@pytest.mark.parametrize("heartbeat", [0, -1])
async def test_disabled_heartbeat_does_not_spin_or_emit_pings(heartbeat: int) -> None:
    async def events() -> AsyncIterator[object]:
        await asyncio.sleep(0.001)
        yield event("done", {})

    response = sse_response(events(), heartbeat_seconds=heartbeat)  # type: ignore[arg-type]
    frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1
    assert "event: done" in str(frames[0])
