"""documents 表的数据访问。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from agentmem.errors import DuplicateDocumentError, NotFoundError
from agentmem.store.base import Repository, dump_json, load_model, new_id
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    Document,
    DocumentCreate,
    DocumentMeta,
    DocumentStatus,
    DocumentUpdate,
)


def _to_model(row: sqlite3.Row) -> Document:
    data = dict(row)
    data["meta"] = load_model(row["meta"], DocumentMeta, DocumentMeta())
    return Document.model_validate(data)


class DocumentRepo(Repository):
    """``documents`` 表。"""

    table = "documents"
    has_updated_at = True

    async def create(self, data: DocumentCreate) -> Document:
        """登记文档。"""
        now = now_ms()
        document = Document(id=new_id(), created_at=now, updated_at=now, **data.model_dump())
        try:
            await self.db.execute(
                "INSERT INTO documents (id, space_id, title, source_type, source_uri, mime,"
                " sha256, size_bytes, status, error, meta, token_count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document.id,
                    document.space_id,
                    document.title,
                    document.source_type,
                    document.source_uri,
                    document.mime,
                    document.sha256,
                    document.size_bytes,
                    document.status,
                    document.error,
                    dump_json(document.meta),
                    document.token_count,
                    document.created_at,
                    document.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            existing = await self.find_by_sha256(data.space_id, data.sha256)
            if existing is not None:
                raise DuplicateDocumentError(data.sha256, document_id=existing.id) from None
            raise
        return document

    async def get(self, document_id: str) -> Document | None:
        """按 id 查询。"""
        row = await self.get_row(document_id)
        return _to_model(row) if row is not None else None

    async def require(self, document_id: str) -> Document:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        document = await self.get(document_id)
        if document is None:
            raise NotFoundError("文档", document_id)
        return document

    async def find_by_sha256(self, space_id: str, sha256: str) -> Document | None:
        """按 sha256 查重。"""
        row = await self.db.fetchone(
            "SELECT * FROM documents WHERE space_id = ? AND sha256 = ?", (space_id, sha256)
        )
        return _to_model(row) if row is not None else None

    async def list_by_space(
        self,
        space_id: str,
        *,
        status: DocumentStatus | None = None,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Document], int, str | None]:
        """列出某 Space 的文档，支持状态/关键词/标签过滤。"""
        clauses = ["space_id = ?"]
        params: list[Any] = [space_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if query and query.strip():
            clauses.append("(title LIKE ? ESCAPE '\\' OR source_uri LIKE ? ESCAPE '\\')")
            literal = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{literal}%", f"%{literal}%"])
        if tag:
            clauses.append("meta LIKE ?")
            params.append(f'%"{tag}"%')
        where = " AND ".join(clauses)
        rows, next_cursor = await self._page(
            where=where, params=params, limit=limit, cursor=cursor, descending=True
        )
        total = await self.db.fetchvalue(f"SELECT COUNT(*) FROM documents WHERE {where}", params)
        return [_to_model(row) for row in rows], int(total or 0), next_cursor

    async def list_overview(self, space_id: str, *, limit: int) -> list[Document]:
        """一个 Space 的文档，就绪的排前面，其余按新到旧。

        知识图谱要一次拿全，而不是分页；超过 ``limit`` 时宁可先丢掉失败 / 处理中的，
        也不丢已经产出知识的那些。
        """
        rows = await self.db.fetchall(
            "SELECT * FROM documents WHERE space_id = ?"
            " ORDER BY (status = 'ready') DESC, created_at DESC, id DESC LIMIT ?",
            (space_id, limit),
        )
        return [_to_model(row) for row in rows]

    async def list_unfinished(self) -> list[Document]:
        """列出所有未完成或失败的文档，供启动时续跑。"""
        rows = await self.db.fetchall(
            "SELECT * FROM documents WHERE status NOT IN ('ready') ORDER BY created_at"
        )
        return [_to_model(row) for row in rows]

    async def update(self, document_id: str, data: DocumentUpdate) -> Document:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "meta" in fields and fields["meta"] is not None:
            fields["meta"] = dump_json(DocumentMeta.model_validate(fields["meta"]))
        await self._update_row(document_id, fields)
        return await self.require(document_id)

    async def titles_by_ids(self, document_ids: Sequence[str]) -> dict[str, str]:
        """按 id 取文档标题，供补齐历史引用记录里的来源标题。"""
        if not document_ids:
            return {}
        placeholders = ", ".join("?" for _ in document_ids)
        rows = await self.db.fetchall(
            f"SELECT id, title FROM documents WHERE id IN ({placeholders})",
            list(document_ids),
        )
        return {str(row["id"]): str(row["title"]) for row in rows}

    async def set_status(
        self, document_id: str, status: DocumentStatus, error: str | None = None
    ) -> None:
        """更新处理状态。"""
        await self._update_row(document_id, {"status": status, "error": error})

    async def delete(self, document_id: str) -> bool:
        """删除文档（chunks 由外键级联删除）。"""
        return await self.delete_row(document_id)

    async def count(self, space_id: str | None = None) -> int:
        """文档数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM documents")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM documents WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)

    async def status_counts(self, space_id: str) -> dict[str, int]:
        """按状态统计。"""
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM documents WHERE space_id = ? GROUP BY status",
            (space_id,),
        )
        return {str(row["status"]): int(row["n"]) for row in rows}
