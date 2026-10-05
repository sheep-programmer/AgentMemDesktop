"""取消与关闭必须等 SQLite 工作线程结束，防止本地进程崩溃。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agentmem.store.sqlite import SQLiteDatabase, SqlParams
from agentmem.store.vectors import VectorStore
from agentmem.types import VectorTableName


async def test_cancelled_write_keeps_connection_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "cancel.db")
    await db.connect()
    await db.execute("CREATE TABLE work (value INTEGER)")
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = db._execute_sync

    def blocked(sql: str, params: SqlParams = ()) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "测试线程未释放"
        return original(sql, params)

    monkeypatch.setattr(db, "_execute_sync", blocked)
    write = asyncio.create_task(db.execute("INSERT INTO work VALUES (42)"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        write.cancel()
        await asyncio.sleep(0)
        write.cancel()
        read = asyncio.create_task(db.fetchvalue("SELECT COUNT(*) FROM work"))
        await asyncio.sleep(0)
        assert not write.done()
        assert not read.done(), "取消不能让另一线程提前使用连接"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert await read == 1
    finally:
        release.set()
        await db.close()


async def test_close_waits_for_in_flight_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "close.db")
    await db.connect()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = db._execute_sync

    def blocked(sql: str, params: SqlParams = ()) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(sql, params)

    monkeypatch.setattr(db, "_execute_sync", blocked)
    write = asyncio.create_task(db.execute("CREATE TABLE work (value INTEGER)"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        close = asyncio.create_task(db.close())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await write
        await close
        with pytest.raises(RuntimeError, match="尚未连接"):
            await db.fetchone("SELECT 1")
    finally:
        release.set()
        await db.close()


async def test_cancel_during_begin_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "transaction.db")
    await db.connect()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = db._begin_sync

    def blocked_begin() -> None:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        original()

    monkeypatch.setattr(db, "_begin_sync", blocked_begin)

    async def transaction() -> None:
        async with db.transaction() as tx:
            await tx.execute("CREATE TABLE work (value INTEGER)")

    task = asyncio.create_task(transaction())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.connection.in_transaction
        async with db.transaction() as tx:
            assert await tx.fetchone("SELECT 1") is not None
    finally:
        release.set()
        await db.close()


async def test_concurrent_connect_uses_single_connection(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "connect.db")
    try:
        await asyncio.gather(*(db.connect() for _ in range(8)))
        assert await db.schema_version() > 0
        assert (
            await db.fetchvalue("SELECT COUNT(*) FROM schema_version") == await db.schema_version()
        )
    finally:
        await db.close()


async def test_vector_close_waits_for_cancelled_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = VectorStore(tmp_path / "vectors")
    await store.open()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store._count_sync

    def blocked(table: VectorTableName, space_id: str | None) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(table, space_id)

    monkeypatch.setattr(store, "_count_sync", blocked)
    read = asyncio.create_task(store.count("chunks_vec"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        read.cancel()
        close = asyncio.create_task(store.close())
        await asyncio.sleep(0)
        assert not close.done(), "向量库不能在后台线程退出之前关闭"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await read
        await close
        with pytest.raises(RuntimeError, match="尚未打开"):
            _ = store.db
    finally:
        release.set()
        await store.close()
