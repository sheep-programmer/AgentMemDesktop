"""eval_items / eval_runs / expertise_snapshots 表的数据访问。"""

from __future__ import annotations

import sqlite3

from agentmem.errors import NotFoundError
from agentmem.store.base import (
    Repository,
    dump_json,
    load_model,
    load_models,
    load_str_list,
    new_id,
)
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    ConsistencyProbe,
    ConsistencyProbeCreate,
    ConsistencyQuestionScore,
    DomainOutline,
    DomainOutlineCreate,
    EvalItem,
    EvalItemCreate,
    EvalItemUpdate,
    EvalRun,
    EvalRunCreate,
    EvalRunDetail,
    EvalRunUpdate,
    ExpertiseSnapshot,
    ExpertiseSnapshotCreate,
    ExpertiseSnapshotUpdate,
    OutlineNode,
)


def _eval_item(row: sqlite3.Row) -> EvalItem:
    data = dict(row)
    data["must_include"] = load_str_list(row["must_include"])
    data["tags"] = load_str_list(row["tags"])
    return EvalItem.model_validate(data)


def _eval_run(row: sqlite3.Row) -> EvalRun:
    data = dict(row)
    data["insight_set"] = load_str_list(row["insight_set"])
    data["detail"] = load_model(row["detail"], EvalRunDetail, EvalRunDetail())
    return EvalRun.model_validate(data)


def _consistency_probe(row: sqlite3.Row) -> ConsistencyProbe:
    data = dict(row)
    data["detail"] = load_models(row["detail"], ConsistencyQuestionScore)
    return ConsistencyProbe.model_validate(data)


def _snapshot(row: sqlite3.Row) -> ExpertiseSnapshot:
    return ExpertiseSnapshot.model_validate(dict(row))


class EvalItemRepo(Repository):
    """``eval_items`` 表。"""

    table = "eval_items"

    async def create(self, data: EvalItemCreate) -> EvalItem:
        """新建测验题。"""
        item = EvalItem(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO eval_items (id, space_id, question, reference, must_include, tags,"
            " source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.space_id,
                item.question,
                item.reference,
                dump_json(item.must_include),
                dump_json(item.tags),
                item.source,
                item.created_at,
            ),
        )
        return item

    async def create_many(self, items: list[EvalItemCreate]) -> list[EvalItem]:
        """批量新建测验题。"""
        return [await self.create(item) for item in items]

    async def get(self, item_id: str) -> EvalItem | None:
        """按 id 查询。"""
        row = await self.get_row(item_id)
        return _eval_item(row) if row is not None else None

    async def require(self, item_id: str) -> EvalItem:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        item = await self.get(item_id)
        if item is None:
            raise NotFoundError("测验题", item_id)
        return item

    async def list_by_space(
        self,
        space_id: str,
        *,
        tag: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[EvalItem], int, str | None]:
        """列出测验题。"""
        clauses = ["space_id = ?"]
        params: list[object] = [space_id]
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        where = " AND ".join(clauses)
        rows, next_cursor = await self._page(
            where=where, params=params, limit=limit, cursor=cursor, order_by="created_at"
        )
        total = await self.db.fetchvalue(f"SELECT COUNT(*) FROM eval_items WHERE {where}", params)
        return [_eval_item(row) for row in rows], int(total or 0), next_cursor

    async def update(self, item_id: str, data: EvalItemUpdate) -> EvalItem:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        for key in ("must_include", "tags"):
            if key in fields and fields[key] is not None:
                fields[key] = dump_json(fields[key])
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE eval_items SET {assignments} WHERE id = ?",
                [*fields.values(), item_id],
            )
        return await self.require(item_id)

    async def delete(self, item_id: str) -> bool:
        """删除。"""
        return await self.delete_row(item_id)

    async def count(self, space_id: str | None = None) -> int:
        """测验题数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM eval_items")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM eval_items WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)


class EvalRunRepo(Repository):
    """``eval_runs`` 表。"""

    table = "eval_runs"

    async def create(self, data: EvalRunCreate) -> EvalRun:
        """写入一次评测结果。"""
        run = EvalRun(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO eval_runs (id, space_id, variant, insight_set, score, detail,"
            " duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.space_id,
                run.variant,
                dump_json(run.insight_set),
                run.score,
                dump_json(run.detail),
                run.duration_ms,
                run.created_at,
            ),
        )
        return run

    async def get(self, run_id: str) -> EvalRun | None:
        """按 id 查询。"""
        row = await self.get_row(run_id)
        return _eval_run(row) if row is not None else None

    async def latest(self, space_id: str, variant: str) -> EvalRun | None:
        """取某变体最近一次评测。"""
        row = await self.db.fetchone(
            "SELECT * FROM eval_runs WHERE space_id = ? AND variant = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (space_id, variant),
        )
        return _eval_run(row) if row is not None else None

    async def list_by_space(
        self, space_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[EvalRun], str | None]:
        """列出评测记录。"""
        rows, next_cursor = await self._page(
            where="space_id = ?",
            params=[space_id],
            limit=limit,
            cursor=cursor,
            order_by="created_at DESC",
        )
        return [_eval_run(row) for row in rows], next_cursor

    async def update(self, run_id: str, data: EvalRunUpdate) -> EvalRun:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "insight_set" in fields and fields["insight_set"] is not None:
            fields["insight_set"] = dump_json(fields["insight_set"])
        if "detail" in fields and fields["detail"] is not None:
            fields["detail"] = dump_json(EvalRunDetail.model_validate(fields["detail"]))
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE eval_runs SET {assignments} WHERE id = ?",
                [*fields.values(), run_id],
            )
        run = await self.get(run_id)
        if run is None:
            raise NotFoundError("评测记录", run_id)
        return run

    async def delete(self, run_id: str) -> bool:
        """删除。"""
        return await self.delete_row(run_id)


class ExpertiseSnapshotRepo(Repository):
    """``expertise_snapshots`` 表。"""

    table = "expertise_snapshots"

    async def create(self, data: ExpertiseSnapshotCreate) -> ExpertiseSnapshot:
        """写入专家度快照。"""
        snapshot = ExpertiseSnapshot(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO expertise_snapshots (id, space_id, coverage, accuracy, consistency,"
            " groundedness, insight_density, overall, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.id,
                snapshot.space_id,
                snapshot.coverage,
                snapshot.accuracy,
                snapshot.consistency,
                snapshot.groundedness,
                snapshot.insight_density,
                snapshot.overall,
                snapshot.created_at,
            ),
        )
        return snapshot

    async def latest(self, space_id: str) -> ExpertiseSnapshot | None:
        """最近一次快照。"""
        row = await self.db.fetchone(
            "SELECT * FROM expertise_snapshots WHERE space_id = ? ORDER BY created_at DESC LIMIT 1",
            (space_id,),
        )
        return _snapshot(row) if row is not None else None

    async def list_by_space(
        self,
        space_id: str,
        *,
        since: int | None = None,
        limit: int = 500,
    ) -> list[ExpertiseSnapshot]:
        """按时间列出快照。"""
        if since is None:
            rows = await self.db.fetchall(
                "SELECT * FROM expertise_snapshots WHERE space_id = ? ORDER BY created_at LIMIT ?",
                (space_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM expertise_snapshots WHERE space_id = ? AND created_at >= ?"
                " ORDER BY created_at LIMIT ?",
                (space_id, since, limit),
            )
        return [_snapshot(row) for row in rows]

    async def recent(self, space_id: str, *, limit: int) -> list[ExpertiseSnapshot]:
        """最近的 ``limit`` 张快照，按时间正序返回。

        ``list_by_space`` 从最早的开始截，快照一多就只剩开头那一段；时间线要的是
        「最近这些」。
        """
        rows = await self.db.fetchall(
            "SELECT * FROM expertise_snapshots WHERE space_id = ? ORDER BY created_at DESC LIMIT ?",
            (space_id, limit),
        )
        return [_snapshot(row) for row in reversed(rows)]

    async def update(self, snapshot_id: str, data: ExpertiseSnapshotUpdate) -> ExpertiseSnapshot:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE expertise_snapshots SET {assignments} WHERE id = ?",
                [*fields.values(), snapshot_id],
            )
        row = await self.get_row(snapshot_id)
        if row is None:
            raise NotFoundError("专家度快照", snapshot_id)
        return _snapshot(row)

    async def delete(self, snapshot_id: str) -> bool:
        """删除。"""
        return await self.delete_row(snapshot_id)


class ConsistencyRepo(Repository):
    """``consistency_probes`` 表：一致性实测的历史。"""

    table = "consistency_probes"

    async def create(self, data: ConsistencyProbeCreate) -> ConsistencyProbe:
        """写入一次探测结果。"""
        probe = ConsistencyProbe(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO consistency_probes (id, space_id, questions, repeats, similarity,"
            " detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                probe.id,
                probe.space_id,
                probe.questions,
                probe.repeats,
                probe.similarity,
                dump_json([item.model_dump() for item in probe.detail]),
                probe.created_at,
            ),
        )
        return probe

    async def latest(self, space_id: str) -> ConsistencyProbe | None:
        """最近一次探测；没有则返回 ``None``。"""
        row = await self.db.fetchone(
            "SELECT * FROM consistency_probes WHERE space_id = ? ORDER BY created_at DESC LIMIT 1",
            (space_id,),
        )
        return _consistency_probe(row) if row is not None else None


def _outline(row: sqlite3.Row) -> DomainOutline:
    data = dict(row)
    data["nodes"] = load_models(row["nodes"], OutlineNode)
    return DomainOutline.model_validate(data)


class DomainOutlineRepo(Repository):
    """``domain_outlines`` 表：落库的领域大纲（每 Space 取最新一份）。"""

    table = "domain_outlines"

    async def create(self, data: DomainOutlineCreate) -> DomainOutline:
        """写入一份新大纲。"""
        outline = DomainOutline(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO domain_outlines (id, space_id, domain, nodes, interpretation, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                outline.id,
                outline.space_id,
                outline.domain,
                dump_json([node.model_dump() for node in outline.nodes]),
                outline.interpretation,
                outline.created_at,
            ),
        )
        return outline

    async def latest(self, space_id: str) -> DomainOutline | None:
        """最近一份大纲；没有则返回 ``None``。"""
        row = await self.db.fetchone(
            "SELECT * FROM domain_outlines WHERE space_id = ? ORDER BY created_at DESC LIMIT 1",
            (space_id,),
        )
        return _outline(row) if row is not None else None
