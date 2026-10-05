"""Space 表的数据访问。"""

from __future__ import annotations

import sqlite3

from agentmem.errors import NotFoundError
from agentmem.store.base import Repository, new_id
from agentmem.store.sqlite import now_ms
from agentmem.types import Space, SpaceCreate, SpaceUpdate


def _to_model(row: sqlite3.Row) -> Space:
    return Space.model_validate(dict(row))


class SpaceRepo(Repository):
    """``spaces`` 表。"""

    table = "spaces"
    has_updated_at = True

    async def create(self, data: SpaceCreate) -> Space:
        """新建 Space。"""
        now = now_ms()
        space = Space(id=new_id(), created_at=now, updated_at=now, **data.model_dump())
        await self.db.execute(
            "INSERT INTO spaces (id, name, domain, icon, color, description, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                space.id,
                space.name,
                space.domain,
                space.icon,
                space.color,
                space.description,
                space.created_at,
                space.updated_at,
            ),
        )
        return space

    async def get(self, space_id: str) -> Space | None:
        """按 id 查询。"""
        row = await self.get_row(space_id)
        return _to_model(row) if row is not None else None

    async def require(self, space_id: str) -> Space:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        space = await self.get(space_id)
        if space is None:
            raise NotFoundError("Space", space_id)
        return space

    async def list_paged(
        self, *, limit: int = 200, cursor: str | None = None
    ) -> tuple[list[Space], str | None]:
        """分页列出 Space。"""
        rows, next_cursor = await self._page(limit=limit, cursor=cursor)
        return [_to_model(row) for row in rows], next_cursor

    async def list_all(self) -> list[Space]:
        """列出全部 Space（内部使用，不分页）。"""
        rows = await self.db.fetchall("SELECT * FROM spaces ORDER BY created_at")
        return [_to_model(row) for row in rows]

    async def update(self, space_id: str, data: SpaceUpdate) -> Space:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        await self._update_row(space_id, fields)
        return await self.require(space_id)

    async def delete(self, space_id: str) -> bool:
        """删除 Space。"""
        return await self.delete_row(space_id)

    async def count(self) -> int:
        """Space 总数。"""
        value = await self.db.fetchvalue("SELECT COUNT(*) FROM spaces")
        return int(value or 0)
