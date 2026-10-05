"""insights 表的数据访问。"""

from __future__ import annotations

import sqlite3

from agentmem.errors import NotFoundError
from agentmem.store.base import Repository, dump_json, load_str_list, new_id
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    Insight,
    InsightCreate,
    InsightEvent,
    InsightEventCreate,
    InsightStatus,
    InsightUpdate,
)


def _to_model(row: sqlite3.Row) -> Insight:
    data = dict(row)
    data["source_trace_ids"] = load_str_list(row["source_trace_ids"])
    return Insight.model_validate(data)


class InsightRepo(Repository):
    """``insights`` 表。"""

    table = "insights"
    has_updated_at = True

    async def create(self, data: InsightCreate) -> Insight:
        """新建经验条目。"""
        now = now_ms()
        insight = Insight(id=new_id(), created_at=now, updated_at=now, **data.model_dump())
        await self.db.execute(
            "INSERT INTO insights (id, space_id, trigger, guidance, rationale, kind, scope,"
            " confidence, status, origin, applied_count, success_count, eval_delta,"
            " source_trace_ids, supersedes, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                insight.id,
                insight.space_id,
                insight.trigger,
                insight.guidance,
                insight.rationale,
                insight.kind,
                insight.scope,
                insight.confidence,
                insight.status,
                insight.origin,
                insight.applied_count,
                insight.success_count,
                insight.eval_delta,
                dump_json(insight.source_trace_ids),
                insight.supersedes,
                insight.created_at,
                insight.updated_at,
            ),
        )
        return insight

    async def get(self, insight_id: str) -> Insight | None:
        """按 id 查询。"""
        row = await self.get_row(insight_id)
        return _to_model(row) if row is not None else None

    async def require(self, insight_id: str) -> Insight:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        insight = await self.get(insight_id)
        if insight is None:
            raise NotFoundError("经验条目", insight_id)
        return insight

    async def list_by_space(
        self,
        space_id: str,
        *,
        status: InsightStatus | None = None,
        kind: str | None = None,
        query: str | None = None,
        sort: str = "confidence",
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Insight], int, str | None]:
        """列出经验条目。

        Args:
            sort: ``confidence`` | ``recent`` | ``applied``。
        """
        clauses = ["space_id = ?"]
        params: list[object] = [space_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if query:
            clauses.append("(trigger LIKE ? OR guidance LIKE ? OR rationale LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        where = " AND ".join(clauses)
        order_by = {
            "confidence": "confidence DESC, updated_at DESC",
            "recent": "updated_at DESC",
            "applied": "applied_count DESC, confidence DESC",
        }.get(sort, "confidence DESC")
        rows, next_cursor = await self._page(
            where=where, params=params, limit=limit, cursor=cursor, order_by=order_by
        )
        total = await self.db.fetchvalue(f"SELECT COUNT(*) FROM insights WHERE {where}", params)
        return [_to_model(row) for row in rows], int(total or 0), next_cursor

    async def list_active(
        self, space_id: str, *, min_confidence: float, limit: int
    ) -> list[Insight]:
        """取高置信度的活跃经验。"""
        rows = await self.db.fetchall(
            "SELECT * FROM insights WHERE space_id = ? AND status = 'active'"
            " AND confidence >= ? ORDER BY confidence DESC LIMIT ?",
            (space_id, min_confidence, limit),
        )
        return [_to_model(row) for row in rows]

    async def list_live(self, space_id: str, *, limit: int) -> list[Insight]:
        """未归档的经验（candidate / active / conflicted），置信度高的在前。"""
        rows = await self.db.fetchall(
            "SELECT * FROM insights WHERE space_id = ? AND status != 'archived'"
            " ORDER BY confidence DESC, id LIMIT ?",
            (space_id, limit),
        )
        return [_to_model(row) for row in rows]

    async def get_many(self, insight_ids: list[str]) -> list[Insight]:
        """按 id 批量取。"""
        if not insight_ids:
            return []
        placeholders = ", ".join("?" for _ in insight_ids)
        rows = await self.db.fetchall(
            f"SELECT * FROM insights WHERE id IN ({placeholders})", insight_ids
        )
        by_id = {str(row["id"]): _to_model(row) for row in rows}
        return [by_id[item] for item in insight_ids if item in by_id]

    async def list_by_status(self, space_id: str, status: InsightStatus) -> list[Insight]:
        """按状态取全部。"""
        rows = await self.db.fetchall(
            "SELECT * FROM insights WHERE space_id = ? AND status = ? ORDER BY created_at",
            (space_id, status),
        )
        return [_to_model(row) for row in rows]

    async def update(self, insight_id: str, data: InsightUpdate) -> Insight:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "source_trace_ids" in fields and fields["source_trace_ids"] is not None:
            fields["source_trace_ids"] = dump_json(fields["source_trace_ids"])
        await self._update_row(insight_id, fields)
        return await self.require(insight_id)

    async def bump_applied(self, insight_ids: list[str]) -> None:
        """被注入上下文的次数各 +1。

        每次回答都要为当时注入的经验记一笔，属于高频写入，所以只做一次批量自增：
        不读回模型、不触发向量同步，也刻意不动 ``updated_at``——记一次注入不是
        对这条经验的修改，不该把它顶到「最近更新」排序的前面。
        """
        if not insight_ids:
            return
        placeholders = ", ".join("?" for _ in insight_ids)
        await self.db.execute(
            f"UPDATE insights SET applied_count = applied_count + 1 WHERE id IN ({placeholders})",
            insight_ids,
        )

    async def delete(self, insight_id: str) -> bool:
        """删除。"""
        return await self.delete_row(insight_id)

    async def list_for_review(
        self, space_id: str, *, min_applied: int, max_success_rate: float, limit: int = 20
    ) -> list[Insight]:
        """挑出「被应用够多、却很少收到好评」的经验。

        只统计还没有被淘汰的状态（candidate / active / conflicted）：已归档的没什么
        可复查的。成功率用浮点比较，SQLite 里直接算比值即可。
        """
        rows = await self.db.fetchall(
            "SELECT * FROM insights WHERE space_id = ? AND status IN ('candidate', 'active',"
            " 'conflicted') AND applied_count >= ?"
            " AND (CAST(success_count AS REAL) / applied_count) <= ?"
            " ORDER BY (CAST(success_count AS REAL) / applied_count) ASC, applied_count DESC"
            " LIMIT ?",
            (space_id, min_applied, max_success_rate, limit),
        )
        return [_to_model(row) for row in rows]

    async def count(self, space_id: str | None = None, status: InsightStatus | None = None) -> int:
        """经验条数。"""
        clauses: list[str] = []
        params: list[object] = []
        if space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        value = await self.db.fetchvalue(f"SELECT COUNT(*) FROM insights{where}", params)
        return int(value or 0)


def _event(row: sqlite3.Row) -> InsightEvent:
    return InsightEvent.model_validate(dict(row))


class InsightEventRepo(Repository):
    """``insight_events`` 表：经验置信度的变更流水。"""

    table = "insight_events"

    async def add(self, data: InsightEventCreate) -> InsightEvent:
        """记一笔变更。"""
        event = InsightEvent(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO insight_events (id, insight_id, space_id, event, confidence_before,"
            " confidence_after, status_before, status_after, share, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.insight_id,
                event.space_id,
                event.event,
                event.confidence_before,
                event.confidence_after,
                event.status_before,
                event.status_after,
                event.share,
                event.reason,
                event.created_at,
            ),
        )
        return event

    async def list_activations(
        self, space_id: str, *, limit: int
    ) -> list[tuple[InsightEvent, str | None]]:
        """经验「变成 active」的那几笔流水（最近的 ``limit`` 条，新的在前），带上触发条件。

        ``active → active`` 的加减分不算：那是已生效经验的置信度微调，不是一次晋升。
        触发条件用 LEFT JOIN 一并取回，经验被删掉时为 ``None``。
        """
        rows = await self.db.fetchall(
            "SELECT e.*, i.trigger AS insight_trigger FROM insight_events e"
            " LEFT JOIN insights i ON i.id = e.insight_id"
            " WHERE e.space_id = ? AND e.status_after = 'active'"
            " AND (e.status_before IS NULL OR e.status_before != 'active')"
            " ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (space_id, limit),
        )
        result: list[tuple[InsightEvent, str | None]] = []
        for row in rows:
            data = dict(row)
            trigger = data.pop("insight_trigger")
            result.append(
                (InsightEvent.model_validate(data), str(trigger) if trigger is not None else None)
            )
        return result

    async def list_by_insight(self, insight_id: str) -> list[InsightEvent]:
        """某条经验的全部变更，旧的在前。"""
        rows = await self.db.fetchall(
            "SELECT * FROM insight_events WHERE insight_id = ? ORDER BY created_at, id",
            (insight_id,),
        )
        return [_event(row) for row in rows]
