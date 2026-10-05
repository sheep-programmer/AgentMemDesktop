"""摄取进度总线：后台任务与 SSE 订阅者之间的解耦。

- 后台任务通过 :meth:`IngestBus.publish` 广播进度与状态；
- SSE 路由通过 :meth:`IngestBus.subscribe` 订阅某个 Space 的事件流；
- 订阅队列满时丢弃最旧事件，绝不阻塞摄取；
- :meth:`IngestBus.shutdown` 取消全部后台任务，进程退出时不留孤儿。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

import structlog

from agentmem.errors import SpaceLockedError
from agentmem.types import ProgressEvent, QueueEvent, SseErrorEvent, StatusEvent

logger = structlog.get_logger(__name__)

#: 总线上的事件类型
IngestEvent = ProgressEvent | StatusEvent | QueueEvent | SseErrorEvent

#: 每个订阅者的队列上限
QUEUE_SIZE = 256


class IngestBus:
    """按 Space 分组的进度广播，兼后台任务的并发闸门。

    Args:
        concurrency: 同时运行的后台摄取任务数上限。投喂多篇文档时不再一次性全部启动，
            多出来的排队等待——否则一次投二十篇就是二十条流水线同时压着向量表与模型服务。
    """

    def __init__(self, *, concurrency: int = 2) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[IngestEvent]]] = {}
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self._concurrency = max(1, concurrency)
        self._running: dict[str, int] = {}
        self._closed = False

    # -- 订阅 -------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(self, space_id: str) -> AsyncIterator[AsyncIterator[IngestEvent]]:
        """订阅某 Space 的事件流。

        用法::

            async with bus.subscribe(space_id) as events:
                async for event in events:
                    ...
        """
        queue: asyncio.Queue[IngestEvent] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.setdefault(space_id, set()).add(queue)
        # 先给一份当前队列深度：订阅者通常在任务已经开跑之后才连上来，
        # 只广播变化的话它永远等不到那条事件
        queue.put_nowait(self.queue_depth(space_id))
        try:
            yield _drain(queue)
        finally:
            subscribers = self._subscribers.get(space_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(space_id, None)

    # -- 广播 -------------------------------------------------------------

    def publish(self, space_id: str, event: IngestEvent) -> None:
        """广播事件；队列满时丢弃最旧的一条。"""
        for queue in list(self._subscribers.get(space_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def publish_queue(self, space_id: str) -> None:
        """广播当前队列深度。"""
        self.publish(space_id, self.queue_depth(space_id))

    def queue_depth(self, space_id: str) -> QueueEvent:
        """当前空间实际运行与排队数，不能把全局等待量混入单个空间。"""
        running = self._running.get(space_id, 0)
        return QueueEvent(running=running, waiting=max(0, self.running_tasks(space_id) - running))

    def publish_progress(
        self,
        space_id: str,
        document_id: str,
        stage: str,
        done: int,
        total: int,
    ) -> None:
        """广播一条进度事件。"""
        percent = round(done / total * 100, 2) if total else 100.0
        self.publish(
            space_id,
            ProgressEvent(
                document_id=document_id, stage=stage, done=done, total=total, percent=percent
            ),
        )

    # -- 后台任务 ---------------------------------------------------------

    def spawn(self, space_id: str, name: str, coro: Awaitable[object]) -> asyncio.Task[None]:
        """把一段耗时处理放到后台，并登记以便进程退出时取消。"""

        if self._closed:
            if inspect.iscoroutine(coro):
                coro.close()
            raise RuntimeError("摄取总线已关闭")
        started = False

        async def runner() -> None:
            nonlocal started
            try:
                async with self._slots:
                    started = True
                    self._running[space_id] = self._running.get(space_id, 0) + 1
                    self.publish_queue(space_id)
                    try:
                        await coro
                    finally:
                        running = self._running[space_id] - 1
                        if running:
                            self._running[space_id] = running
                        else:
                            self._running.pop(space_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("ingest_task_failed", space_id=space_id, task=name)
                self.publish(space_id, SseErrorEvent(code="INTERNAL_ERROR", message=str(exc)))

        def finished(task: asyncio.Task[None]) -> None:
            # 在 runner 第一次执行前取消时，runner 的 finally 根本没有机会执行。
            if not started and inspect.iscoroutine(coro):
                coro.close()
            tasks = self._tasks.get(space_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self._tasks.pop(space_id, None)
            self.publish_queue(space_id)

        task = asyncio.create_task(runner(), name=f"ingest:{space_id}:{name}")
        self._tasks.setdefault(space_id, set()).add(task)
        task.add_done_callback(finished)
        self.publish_queue(space_id)
        return task

    def running_tasks(self, space_id: str | None = None) -> int:
        """正在运行或排队中的后台任务数。"""
        if space_id is not None:
            return len(self._tasks.get(space_id, ()))
        return sum(len(items) for items in self._tasks.values())

    @property
    def waiting_tasks(self) -> int:
        """在等名额的任务数。"""
        return sum(self.queue_depth(space_id).waiting for space_id in self._tasks)

    async def cancel_space(self, space_id: str, *, grace_seconds: float = 5.0) -> int:
        """取消某个 Space 的全部后台任务并等它们退出（删除 Space 之前调用）。

        不等的话，任务会对着已关闭的数据库、已删除的目录继续写：解析阶段甚至会把
        ``cache/<space_id>/`` 在删除之后重新建出来。
        """
        tasks = list(self._tasks.get(space_id, set()))
        await self._cancel_and_wait(tasks, grace_seconds=grace_seconds, space_id=space_id)
        return len(tasks)

    async def shutdown(self, *, grace_seconds: float = 5.0) -> None:
        """取消全部后台任务，确认退出后才允许上层关闭数据库。"""
        self._closed = True
        tasks = [task for items in self._tasks.values() for task in items]
        await self._cancel_and_wait(tasks, grace_seconds=grace_seconds)

    async def _cancel_and_wait(
        self,
        tasks: list[asyncio.Task[None]],
        *,
        grace_seconds: float,
        space_id: str | None = None,
    ) -> None:
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            if pending:
                # 超时不能从登记表抹掉任务并关闭它们仍在写的数据库。
                raise SpaceLockedError(
                    "后台任务仍在清理，请稍后再试",
                    detail={"space_id": space_id, "pending_tasks": len(pending)},
                )


async def _drain(queue: asyncio.Queue[IngestEvent]) -> AsyncIterator[IngestEvent]:
    """把队列内容转成异步迭代器。"""
    while True:
        yield await queue.get()
