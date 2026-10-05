"""usage_records 表的数据访问与聚合。"""

from __future__ import annotations

import sqlite3

from agentmem.store.base import Repository, new_id, to_bool, to_int_bool
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    ProviderAlert,
    UsageGroupItem,
    UsageRecord,
    UsageRecordCreate,
    UsageRecordUpdate,
)

_GROUP_COLUMNS = {"provider": "provider_id", "model": "model", "purpose": "purpose", "kind": "kind"}


def _to_model(row: sqlite3.Row) -> UsageRecord:
    data = dict(row)
    data["ok"] = to_bool(row["ok"])
    return UsageRecord.model_validate(data)


class UsageRepo(Repository):
    """``usage_records`` 表。"""

    table = "usage_records"

    async def create(self, data: UsageRecordCreate) -> UsageRecord:
        """写入一条用量记录。"""
        record = UsageRecord(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO usage_records (id, space_id, provider_id, model, kind, purpose,"
            " prompt_tokens, completion_tokens, cached_tokens, cache_write_tokens,"
            " latency_ms, ok, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.space_id,
                record.provider_id,
                record.model,
                record.kind,
                record.purpose,
                record.prompt_tokens,
                record.completion_tokens,
                record.cached_tokens,
                record.cache_write_tokens,
                record.latency_ms,
                to_int_bool(record.ok),
                record.created_at,
            ),
        )
        return record

    async def get(self, record_id: str) -> UsageRecord | None:
        """按 id 查询。"""
        row = await self.get_row(record_id)
        return _to_model(row) if row is not None else None

    async def list_by_space(
        self, space_id: str, *, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[UsageRecord], str | None]:
        """列出用量记录。"""
        rows, next_cursor = await self._page(
            where="space_id = ?",
            params=[space_id],
            limit=limit,
            cursor=cursor,
            order_by="created_at DESC",
        )
        return [_to_model(row) for row in rows], next_cursor

    async def recent_failures(
        self, *, since: int, space_id: str | None = None
    ) -> list[ProviderAlert]:
        """窗口内失败过的 provider，按最近一次失败时间倒序。

        ``recovered`` 用「最近一次调用是否成功」判定：provider 抖了一下又通了，
        与一直连不上，对用户是两件完全不同的事，不该报同一种警。
        """
        clauses = ["created_at >= ?"]
        params: list[object] = [since]
        if space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        where = " AND ".join(clauses)
        rows = await self.db.fetchall(
            "SELECT provider_id, kind,"
            " SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,"
            " COUNT(*) AS calls,"
            " MAX(CASE WHEN ok = 0 THEN created_at ELSE 0 END) AS last_failed_at,"
            " MAX(created_at) AS last_at,"
            " MAX(CASE WHEN ok = 1 THEN created_at ELSE 0 END) AS last_ok_at"
            f" FROM usage_records WHERE {where}"
            " GROUP BY provider_id, kind HAVING failures > 0"
            " ORDER BY last_failed_at DESC",
            params,
        )
        return [
            ProviderAlert(
                provider_id=str(row["provider_id"]),
                kind=str(row["kind"]),
                failures=int(row["failures"] or 0),
                calls=int(row["calls"] or 0),
                last_failed_at=int(row["last_failed_at"] or 0),
                recovered=int(row["last_ok_at"] or 0) > int(row["last_failed_at"] or 0),
            )
            for row in rows
        ]

    async def aggregate(
        self,
        *,
        group_by: str = "provider",
        since: int | None = None,
        space_id: str | None = None,
        kind: str | None = None,
    ) -> list[UsageGroupItem]:
        """按维度聚合用量。

        Args:
            group_by: ``provider`` | ``model`` | ``purpose`` | ``kind``。
            since: 起始时间（Unix 毫秒）。
            space_id: 限定 Space。
            kind: 限定能力类型（``llm`` / ``embedding`` / ``rerank``）。
                看缓存命中率时**必须**传 ``llm``：embedding 与 rerank 根本没有
                前缀缓存这回事，把它们算进分母会把命中率稀释成一个没有意义的数
                （实测 embedding 占了输入 token 的四成，命中率被从 25% 压到 14%）。
        """
        column = _GROUP_COLUMNS.get(group_by, "provider_id")
        clauses = [f"{column} IS NOT NULL"]
        params: list[object] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if space_id is not None:
            clauses.append("space_id = ?")
            params.append(space_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = " AND ".join(clauses)
        rows = await self.db.fetchall(
            f"SELECT {column} AS key, COUNT(*) AS calls,"
            " SUM(prompt_tokens) AS prompt_tokens,"
            " SUM(completion_tokens) AS completion_tokens,"
            " SUM(cached_tokens) AS cached_tokens,"
            " SUM(cache_write_tokens) AS cache_write_tokens,"
            " AVG(latency_ms) AS avg_latency_ms"
            f" FROM usage_records WHERE {where} GROUP BY {column}"
            " ORDER BY (SUM(prompt_tokens) + SUM(completion_tokens)) DESC",
            params,
        )
        items: list[UsageGroupItem] = []
        for row in rows:
            prompt = int(row["prompt_tokens"] or 0)
            completion = int(row["completion_tokens"] or 0)
            cached = int(row["cached_tokens"] or 0)
            items.append(
                UsageGroupItem(
                    key=str(row["key"]),
                    calls=int(row["calls"] or 0),
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    # cached_tokens 已含在 prompt_tokens 内，这里**不能**再加一次
                    total_tokens=prompt + completion,
                    cached_tokens=cached,
                    cache_write_tokens=int(row["cache_write_tokens"] or 0),
                    cache_hit_rate=min(cached / prompt, 1.0) if prompt > 0 else 0.0,
                    avg_latency_ms=float(row["avg_latency_ms"])
                    if row["avg_latency_ms"] is not None
                    else None,
                )
            )
        return items

    async def update(self, record_id: str, data: UsageRecordUpdate) -> UsageRecord:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "ok" in fields and fields["ok"] is not None:
            fields["ok"] = to_int_bool(bool(fields["ok"]))
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE usage_records SET {assignments} WHERE id = ?",
                [*fields.values(), record_id],
            )
        record = await self.get(record_id)
        if record is None:
            from agentmem.errors import NotFoundError

            raise NotFoundError("用量记录", record_id)
        return record

    async def delete(self, record_id: str) -> bool:
        """删除。"""
        return await self.delete_row(record_id)

    async def count(self) -> int:
        """记录数。"""
        value = await self.db.fetchvalue("SELECT COUNT(*) FROM usage_records")
        return int(value or 0)

    async def total_tokens(self, *, since: int | None = None) -> int:
        """总 token 数。"""
        if since is None:
            value = await self.db.fetchvalue(
                "SELECT SUM(prompt_tokens + completion_tokens) FROM usage_records"
            )
        else:
            value = await self.db.fetchvalue(
                "SELECT SUM(prompt_tokens + completion_tokens) FROM usage_records"
                " WHERE created_at >= ?",
                (since,),
            )
        return int(value or 0)
