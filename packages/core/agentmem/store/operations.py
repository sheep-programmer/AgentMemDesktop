"""共享空间维护与文档操作保护；检查和登记之间不经过 await。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from weakref import WeakValueDictionary

from agentmem.errors import SpaceLockedError


class OperationGate:
    def __init__(self) -> None:
        self._exclusive: object | None = None
        self._writers = 0
        self._scheduled: set[str] = set()
        self._activities: set[str] = set()
        self._documents: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @property
    def locked(self) -> bool:
        return self._exclusive is not None

    def require_writable(self, token: object | None = None) -> None:
        if self._exclusive is not None and token is not self._exclusive:
            raise SpaceLockedError("知识空间正在维护，请完成后再操作")

    @contextmanager
    def writing(self, token: object | None = None) -> Iterator[None]:
        self.require_writable(token)
        self._writers += 1
        try:
            yield
        finally:
            self._writers -= 1

    def lock(self) -> object:
        if self.locked or self._writers:
            raise SpaceLockedError(
                "知识空间还有操作未完成，请完成后再维护",
                detail={"active_writers": self._writers, "maintenance": self.locked},
            )
        self._exclusive = object()
        return self._exclusive

    @contextmanager
    def activity(self, name: str) -> Iterator[None]:
        """保护有副作用的长任务，同空间同类任务不能重复执行。"""
        with self.writing():
            if name in self._activities:
                raise SpaceLockedError(f"{name}正在运行，请完成后再试", detail={"operation": name})
            self._activities.add(name)
            try:
                yield
            finally:
                self._activities.discard(name)

    def unlock(self, token: object) -> None:
        if token is self._exclusive:
            self._exclusive = None

    @contextmanager
    def maintenance(self) -> Iterator[object]:
        # 显式许可可以跨异步生成器恢复和子任务使用，不能依赖跨任务 ContextVar。
        token = self.lock()
        try:
            yield token
        finally:
            self.unlock(token)

    @contextmanager
    def reserve_document(self, document_id: str) -> Iterator[None]:
        with self.writing():
            if document_id in self._scheduled or self.document_lock(document_id).locked():
                raise SpaceLockedError("该文档已在处理中，请勿重复提交")
            self._scheduled.add(document_id)
            try:
                yield
            finally:
                self._scheduled.discard(document_id)

    def document_lock(self, document_id: str) -> asyncio.Lock:
        lock = self._documents.get(document_id)
        if lock is None:
            lock = asyncio.Lock()
            self._documents[document_id] = lock
        return lock

    def require_document_idle(self, document_id: str) -> None:
        if document_id in self._scheduled or self.document_lock(document_id).locked():
            raise SpaceLockedError("该文档正在处理中，请完成后再删除")
