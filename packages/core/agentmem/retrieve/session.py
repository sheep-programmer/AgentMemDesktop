"""进行中的生成任务登记与中断。

中断生成有两个入口，最终都落到 :meth:`GenerationRegistry.cancel`：

- 用户点「停止」→ ``POST /conversations/{cid}/stop``；
- SSE 连接断开 → 推送端取消底层任务。

登记表只存在于进程内存：它是瞬时状态，重启即失效，不作任何持久化。
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


class GenerationRegistry:
    """按会话登记正在跑的生成任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._owners: dict[asyncio.Task[None], tuple[str, str | None]] = {}

    def register(
        self, conversation_id: str, task: asyncio.Task[None], *, space_id: str | None = None
    ) -> asyncio.Task[None] | None:
        """登记任务，覆盖同一会话上一个尚未结束的任务。"""
        previous = self._tasks.get(conversation_id)
        if previous is not None and not previous.done():
            logger.info("generation_replaced", conversation_id=conversation_id)
            if not previous.cancelling():
                previous.cancel()
        self._tasks[conversation_id] = task
        self._owners[task] = (conversation_id, space_id)
        task.add_done_callback(lambda finished: self.discard(conversation_id, finished))
        return previous

    def discard(self, conversation_id: str, task: asyncio.Task[None]) -> None:
        """摘除登记；仅当登记的仍是该任务时才移除，避免误删后来者。"""
        if self._tasks.get(conversation_id) is task:
            self._tasks.pop(conversation_id, None)
        self._owners.pop(task, None)

    def cancel(self, conversation_id: str) -> bool:
        """取消该会话正在跑的生成；没有在跑则返回 ``False``。"""
        task = self._tasks.get(conversation_id)
        if task is None or task.done():
            return False
        if not task.cancelling():
            task.cancel()
        return True

    def is_running(self, conversation_id: str) -> bool:
        """该会话是否有生成在跑。"""
        task = self._tasks.get(conversation_id)
        return task is not None and not task.done()

    def active_count(self) -> int:
        """当前在跑的任务数。"""
        return sum(1 for task in self._owners if not task.done())

    async def cancel_and_wait(
        self, *, conversation_id: str | None = None, space_id: str | None = None
    ) -> None:
        """删除或退出前等待当前及被替换的回答完成落库清理。"""
        tasks = [
            task
            for task, (conversation, space) in list(self._owners.items())
            if not task.done()
            and (conversation_id is None or conversation == conversation_id)
            and (space_id is None or space == space_id)
        ]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_registry: GenerationRegistry | None = None


def generation_registry() -> GenerationRegistry:
    """进程内共享的生成登记表。"""
    global _registry
    if _registry is None:
        _registry = GenerationRegistry()
    return _registry


def reset_generation_registry() -> None:
    """重建登记表（测试用，避免用例之间互相影响）。"""
    global _registry
    _registry = GenerationRegistry()
