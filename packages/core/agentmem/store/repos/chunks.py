"""chunks 表的数据访问；写切片时同步维护 FTS5 全文索引。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from agentmem.errors import NotFoundError
from agentmem.store.base import Repository, new_id
from agentmem.store.fts import FtsIndex
from agentmem.store.repos.memory import SQL_PARAM_CHUNK
from agentmem.store.sqlite import SQLiteDatabase, now_ms
from agentmem.types import Chunk, ChunkCreate, ChunkUpdate


def _to_model(row: sqlite3.Row) -> Chunk:
    return Chunk.model_validate(dict(row))


class ChunkRepo(Repository):
    """``chunks`` 表；构造时可注入共享的 :class:`FtsIndex`。"""

    table = "chunks"

    def __init__(self, db: SQLiteDatabase, fts: FtsIndex | None = None) -> None:
        super().__init__(db)
        self.fts = fts if fts is not None else FtsIndex(db)

    async def create_many(
        self, items: list[ChunkCreate], *, index_prefix: str | None = None
    ) -> list[Chunk]:
        """批量写入切片并同步全文索引。

        ``index_prefix`` 只影响全文索引里的文本（文档级上下文），
        落库的 ``content`` 与字符偏移一个字符都不动——引用高亮依赖它们。
        """
        if not items:
            return []
        now = now_ms()
        chunks = [Chunk(id=new_id(), created_at=now, **item.model_dump()) for item in items]
        async with self.db.transaction() as tx:
            await tx.executemany(
                "INSERT INTO chunks (id, space_id, document_id, ordinal, content, heading_path,"
                " page, char_start, char_end, token_count, kind, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        chunk.id,
                        chunk.space_id,
                        chunk.document_id,
                        chunk.ordinal,
                        chunk.content,
                        chunk.heading_path,
                        chunk.page,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.token_count,
                        chunk.kind,
                        chunk.created_at,
                    )
                    for chunk in chunks
                ],
            )
        prefix = f"{index_prefix}\n" if index_prefix else ""
        await self.fts.index_chunks(
            [
                # 概要切片的正文本身就是文档级上下文，再前缀一次等于同一段文字写两遍
                (
                    chunk.id,
                    chunk.content if chunk.kind == "summary" else f"{prefix}{chunk.content}",
                )
                for chunk in chunks
            ]
        )
        return chunks

    async def get(self, chunk_id: str) -> Chunk | None:
        """按 id 查询。"""
        row = await self.get_row(chunk_id)
        return _to_model(row) if row is not None else None

    async def require(self, chunk_id: str) -> Chunk:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        chunk = await self.get(chunk_id)
        if chunk is None:
            raise NotFoundError("切片", chunk_id)
        return chunk

    async def list_by_document(self, document_id: str) -> list[Chunk]:
        """按文档取全部切片，保持文档内顺序。"""
        rows = await self.db.fetchall(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal", (document_id,)
        )
        return [_to_model(row) for row in rows]

    async def list_by_space(
        self, space_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Chunk], int, str | None]:
        """按 Space 分页取切片。"""
        rows, next_cursor = await self._page(
            where="space_id = ?", params=[space_id], limit=limit, cursor=cursor, order_by="id"
        )
        total = await self.db.fetchvalue(
            "SELECT COUNT(*) FROM chunks WHERE space_id = ?", (space_id,)
        )
        return [_to_model(row) for row in rows], int(total or 0), next_cursor

    async def get_many(self, chunk_ids: list[str]) -> list[Chunk]:
        """按 id 批量取切片，返回顺序与入参一致（缺失项跳过）。"""
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        rows = await self.db.fetchall(
            f"SELECT * FROM chunks WHERE id IN ({placeholders})", chunk_ids
        )
        by_id = {str(row["id"]): _to_model(row) for row in rows}
        return [by_id[item] for item in chunk_ids if item in by_id]

    async def document_ids(self, chunk_ids: Sequence[str]) -> dict[str, str]:
        """``chunk_id -> document_id``，只读两列，缺失项不出现在结果里。

        知识图谱要把每张卡片连回它的来源文档：一张卡挂好几条切片，几百张卡就是
        上千个 id，逐条 ``get`` 是 N+1，``get_many`` 又会把整段正文全读出来。
        按 ``SQL_PARAM_CHUNK`` 分批，避免超出 SQLite 的占位符上限。
        """
        unique = list(dict.fromkeys(chunk_ids))
        mapping: dict[str, str] = {}
        for start in range(0, len(unique), SQL_PARAM_CHUNK):
            batch = unique[start : start + SQL_PARAM_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db.fetchall(
                f"SELECT id, document_id FROM chunks WHERE id IN ({placeholders})", batch
            )
            mapping.update({str(row["id"]): str(row["document_id"]) for row in rows})
        return mapping

    async def update(self, chunk_id: str, data: ChunkUpdate) -> Chunk:
        """部分更新；内容变化时同步重建该切片的全文索引。"""
        fields = data.model_dump(exclude_unset=True)
        if "content" in fields and fields["content"] is not None:
            await self.fts.remove_chunks([chunk_id])
        await self._update_row(chunk_id, fields)
        chunk = await self.require(chunk_id)
        if "content" in fields and fields["content"] is not None:
            await self.fts.index_chunk(chunk.id, chunk.content)
        return chunk

    async def delete(self, chunk_id: str) -> bool:
        """删除单条切片与对应全文索引。"""
        await self.fts.remove_chunks([chunk_id])
        return await self.delete_row(chunk_id)

    async def delete_by_document(self, document_id: str) -> int:
        """删除某文档的全部切片与全文索引，返回删除条数。"""
        rows = await self.db.fetchall("SELECT id FROM chunks WHERE document_id = ?", (document_id,))
        chunk_ids = [str(row["id"]) for row in rows]
        if not chunk_ids:
            return 0
        await self.fts.remove_chunks(chunk_ids)
        affected = await self.db.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        return affected

    async def page_by_document(
        self,
        document_id: str,
        *,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[Chunk], int, str | None]:
        """按文档内顺序分页取切片。

        切片按 ``ordinal`` 排序，而游标分页基于主键（ULID 即时间序）——两者顺序
        一致，因为同一批切片是一次写入的。上限之外的长文档要靠翻页拿全，
        阅读器要按偏移高亮某一条切片时，缺页就等于「这条切片不存在」。
        """
        # 游标直接用 ordinal，不走基类的 id 游标：ULID 在同一毫秒内不保证单调，
        # 而「文档内第几条」才是这里唯一稳定的顺序。
        after = int(cursor) if cursor and cursor.isdigit() else -1
        rows = await self.db.fetchall(
            "SELECT * FROM chunks WHERE document_id = ? AND ordinal > ? ORDER BY ordinal LIMIT ?",
            (document_id, after, limit + 1),
        )
        next_cursor = str(rows[limit - 1]["ordinal"]) if len(rows) > limit else None
        total = await self.db.fetchvalue(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (document_id,)
        )
        return [_to_model(row) for row in rows[:limit]], int(total or 0), next_cursor

    async def count(self, space_id: str | None = None, document_id: str | None = None) -> int:
        """切片数。"""
        clauses: list[str] = []
        params: list[str] = []
        if space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        value = await self.db.fetchvalue(f"SELECT COUNT(*) FROM chunks{where}", params)
        return int(value or 0)
