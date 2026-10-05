"""traces / feedback 表的数据访问。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from agentmem.errors import NotFoundError
from agentmem.store.base import (
    Repository,
    dump_json,
    load_models,
    load_str_list,
    new_id,
    to_bool,
    to_int_bool,
)
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    Feedback,
    FeedbackCreate,
    FeedbackUpdate,
    Trace,
    TraceCreate,
    TraceRetrievedItem,
    TraceUpdate,
)


def _trace(row: sqlite3.Row) -> Trace:
    data = dict(row)
    data["retrieved"] = load_models(row["retrieved"], TraceRetrievedItem)
    data["used_insights"] = load_str_list(row["used_insights"])
    data["used_cards"] = load_str_list(row["used_cards"])
    return Trace.model_validate(data)


def _feedback(row: sqlite3.Row) -> Feedback:
    data = dict(row)
    data["distilled"] = to_bool(row["distilled"])
    return Feedback.model_validate(data)


class TraceRepo(Repository):
    """``traces`` 表。"""

    table = "traces"

    async def create(self, data: TraceCreate) -> Trace:
        """写入轨迹。调用方指定了 id 就沿用——前端手里的 trace_id 必须能查回来。"""
        payload = data.model_dump(exclude={"id"})
        trace = Trace(id=data.id or new_id(), created_at=now_ms(), **payload)
        await self.db.execute(
            "INSERT INTO traces (id, space_id, conversation_id, message_id, query,"
            " rewritten_query, retrieved, used_insights, used_cards, llm_role, provider_id,"
            " model, prompt_tokens, completion_tokens, latency_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace.id,
                trace.space_id,
                trace.conversation_id,
                trace.message_id,
                trace.query,
                trace.rewritten_query,
                dump_json(trace.retrieved) or "[]",
                dump_json(trace.used_insights),
                dump_json(trace.used_cards),
                trace.llm_role,
                trace.provider_id,
                trace.model,
                trace.prompt_tokens,
                trace.completion_tokens,
                trace.latency_ms,
                trace.created_at,
            ),
        )
        return trace

    async def get(self, trace_id: str) -> Trace | None:
        """按 id 查询。"""
        row = await self.get_row(trace_id)
        return _trace(row) if row is not None else None

    async def require(self, trace_id: str) -> Trace:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        trace = await self.get(trace_id)
        if trace is None:
            raise NotFoundError("轨迹", trace_id)
        return trace

    async def get_by_message(self, message_id: str) -> Trace | None:
        """按 assistant 消息取轨迹。"""
        row = await self.db.fetchone(
            "SELECT * FROM traces WHERE message_id = ? ORDER BY created_at DESC LIMIT 1",
            (message_id,),
        )
        return _trace(row) if row is not None else None

    async def trace_ids_by_message(self, message_ids: Sequence[str]) -> dict[str, str]:
        """按 assistant 消息批量取轨迹 id，供会话详情把 trace_id 带给前端。"""
        if not message_ids:
            return {}
        placeholders = ", ".join("?" for _ in message_ids)
        rows = await self.db.fetchall(
            f"SELECT message_id, id FROM traces WHERE message_id IN ({placeholders})"
            f" ORDER BY created_at",
            list(message_ids),
        )
        return {str(row["message_id"]): str(row["id"]) for row in rows}

    async def used_cards_by_ids(self, trace_ids: Sequence[str]) -> dict[str, list[str]]:
        """``trace_id -> 那次回答注入过的卡片 id``；只读两列，已删除的轨迹不出现。"""
        unique = list(dict.fromkeys(trace_ids))
        if not unique:
            return {}
        placeholders = ", ".join("?" for _ in unique)
        rows = await self.db.fetchall(
            f"SELECT id, used_cards FROM traces WHERE id IN ({placeholders})", unique
        )
        return {str(row["id"]): load_str_list(row["used_cards"]) for row in rows}

    async def list_by_space(
        self, space_id: str, *, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Trace], int, str | None]:
        """列出某 Space 的轨迹。"""
        rows, next_cursor = await self._page(
            where="space_id = ?",
            params=[space_id],
            limit=limit,
            cursor=cursor,
            order_by="created_at DESC",
        )
        total = await self.db.fetchvalue(
            "SELECT COUNT(*) FROM traces WHERE space_id = ?", (space_id,)
        )
        return [_trace(row) for row in rows], int(total or 0), next_cursor

    async def update(self, trace_id: str, data: TraceUpdate) -> Trace:
        """部分更新（补写改写结果 / token 用量等）。"""
        fields = data.model_dump(exclude_unset=True)
        if "retrieved" in fields and fields["retrieved"] is not None:
            fields["retrieved"] = dump_json(
                [TraceRetrievedItem.model_validate(item) for item in fields["retrieved"]]
            )
        for key in ("used_insights", "used_cards"):
            if key in fields and fields[key] is not None:
                fields[key] = dump_json(fields[key])
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE traces SET {assignments} WHERE id = ?",
                [*fields.values(), trace_id],
            )
        return await self.require(trace_id)

    async def delete(self, trace_id: str) -> bool:
        """删除。"""
        return await self.delete_row(trace_id)

    async def count(self, space_id: str | None = None) -> int:
        """轨迹数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM traces")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM traces WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)


class FeedbackRepo(Repository):
    """``feedback`` 表。"""

    table = "feedback"

    async def create(self, data: FeedbackCreate) -> Feedback:
        """写入反馈。"""
        feedback = Feedback(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO feedback (id, trace_id, kind, comment, judge_score, judge_reason,"
            " distilled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feedback.id,
                feedback.trace_id,
                feedback.kind,
                feedback.comment,
                feedback.judge_score,
                feedback.judge_reason,
                to_int_bool(feedback.distilled),
                feedback.created_at,
            ),
        )
        return feedback

    async def get(self, feedback_id: str) -> Feedback | None:
        """按 id 查询。"""
        row = await self.get_row(feedback_id)
        return _feedback(row) if row is not None else None

    async def kinds_by_traces(self, trace_ids: Sequence[str]) -> dict[str, list[str]]:
        """每条轨迹收到过的反馈种类（按时间顺序），会话详情用它还原赞 / 踩 / 纠错状态。"""
        if not trace_ids:
            return {}
        placeholders = ", ".join("?" for _ in trace_ids)
        rows = await self.db.fetchall(
            f"SELECT trace_id, kind FROM feedback WHERE trace_id IN ({placeholders})"
            " ORDER BY created_at",
            tuple(trace_ids),
        )
        kinds: dict[str, list[str]] = {}
        for row in rows:
            kinds.setdefault(row["trace_id"], []).append(row["kind"])
        return kinds

    async def list_by_trace(self, trace_id: str) -> list[Feedback]:
        """取某轨迹的全部反馈。"""
        rows = await self.db.fetchall(
            "SELECT * FROM feedback WHERE trace_id = ? ORDER BY created_at", (trace_id,)
        )
        return [_feedback(row) for row in rows]

    async def count_pending_by_kind(self, space_id: str) -> dict[str, int]:
        """待蒸馏反馈按种类计数（up / down / correction / edit）。"""
        rows = await self.db.fetchall(
            "SELECT f.kind AS kind, COUNT(*) AS n FROM feedback f"
            " JOIN traces t ON t.id = f.trace_id"
            " WHERE t.space_id = ? AND f.distilled = 0 GROUP BY f.kind",
            (space_id,),
        )
        return {row["kind"]: int(row["n"]) for row in rows}

    async def list_pending(self, space_id: str, *, limit: int = 50) -> list[Feedback]:
        """取待蒸馏的反馈（join traces 限定 Space）。"""
        rows = await self.db.fetchall(
            "SELECT f.* FROM feedback f JOIN traces t ON t.id = f.trace_id"
            " WHERE t.space_id = ? AND f.distilled = 0 ORDER BY f.created_at LIMIT ?",
            (space_id, limit),
        )
        return [_feedback(row) for row in rows]

    async def count_pending(self, space_id: str | None = None) -> int:
        """待蒸馏反馈数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM feedback WHERE distilled = 0")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM feedback f JOIN traces t ON t.id = f.trace_id"
                " WHERE t.space_id = ? AND f.distilled = 0",
                (space_id,),
            )
        return int(value or 0)

    async def update(self, feedback_id: str, data: FeedbackUpdate) -> Feedback:
        """部分更新（如标记已蒸馏、写入 Judge 结果）。"""
        fields = data.model_dump(exclude_unset=True)
        if "distilled" in fields and fields["distilled"] is not None:
            fields["distilled"] = to_int_bool(bool(fields["distilled"]))
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE feedback SET {assignments} WHERE id = ?",
                [*fields.values(), feedback_id],
            )
        feedback = await self.get(feedback_id)
        if feedback is None:
            raise NotFoundError("反馈", feedback_id)
        return feedback

    async def delete(self, feedback_id: str) -> bool:
        """删除。"""
        return await self.delete_row(feedback_id)
