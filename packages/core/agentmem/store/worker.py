"""等待数据库线程真正结束后再传播取消，避免释放仍在使用的连接。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_db_worker(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """线程不能随协程取消；持锁的调用方必须等线程退出才能释放锁。"""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # shutdown 和上层超时可能连续取消同一个任务，仍须等 SQLite 操作结束。
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
