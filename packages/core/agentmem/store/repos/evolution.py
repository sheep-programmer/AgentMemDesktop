"""``evolution_runs`` 表：一键进化的历史。

以前 `/evolve/history` 用评测记录近似，把 with_insights 那一次的**分数**当成
「本次收益」（71.5 这种分数被当成 delta 显示），而一次进化真正的产出——新增几条候选、
合并几条、晋升几条、淘汰几条——算完就丢了。日志表把这些如实留下来。
"""

from __future__ import annotations

import sqlite3

from agentmem.store.base import Repository, new_id
from agentmem.store.sqlite import now_ms
from agentmem.types import EvolutionRun, EvolutionRunCreate


def _to_model(row: sqlite3.Row) -> EvolutionRun:
    return EvolutionRun.model_validate(dict(row))


class EvolutionRunRepo(Repository):
    """``evolution_runs`` 表。"""

    table = "evolution_runs"

    async def create(self, data: EvolutionRunCreate) -> EvolutionRun:
        """写入一次进化日志。"""
        run = EvolutionRun(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO evolution_runs (id, space_id, produced, merged, duplicates, conflicts,"
            " promoted, demoted, eval_delta, expertise_before, expertise_after, duration_ms,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.space_id,
                run.produced,
                run.merged,
                run.duplicates,
                run.conflicts,
                run.promoted,
                run.demoted,
                run.eval_delta,
                run.expertise_before,
                run.expertise_after,
                run.duration_ms,
                run.created_at,
            ),
        )
        return run

    async def list_by_space(self, space_id: str, *, limit: int = 50) -> list[EvolutionRun]:
        """历次进化，新的在前。"""
        rows = await self.db.fetchall(
            "SELECT * FROM evolution_runs WHERE space_id = ? ORDER BY created_at DESC LIMIT ?",
            (space_id, limit),
        )
        return [_to_model(row) for row in rows]
