"""conversations / messages 表的数据访问。"""

from __future__ import annotations

import sqlite3
from typing import Any

from agentmem.errors import NotFoundError
from agentmem.store.base import (
    Repository,
    dump_json,
    load_models,
    new_id,
    to_bool,
    to_int_bool,
)
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    Citation,
    Conversation,
    ConversationCreate,
    ConversationUpdate,
    Message,
    MessageCreate,
    MessageUpdate,
)


def _conversation(row: sqlite3.Row) -> Conversation:
    data = dict(row)
    data["pinned"] = to_bool(row["pinned"])
    return Conversation.model_validate(data)


def _message(row: sqlite3.Row) -> Message:
    data = dict(row)
    data["citations"] = load_models(row["citations"], Citation)
    return Message.model_validate(data)


class ConversationRepo(Repository):
    """``conversations`` 表。"""

    table = "conversations"
    has_updated_at = True

    async def create(self, data: ConversationCreate) -> Conversation:
        """新建会话。"""
        now = now_ms()
        conversation = Conversation(
            id=new_id(), created_at=now, updated_at=now, **data.model_dump()
        )
        await self.db.execute(
            "INSERT INTO conversations (id, space_id, title, pinned, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation.id,
                conversation.space_id,
                conversation.title,
                to_int_bool(conversation.pinned),
                conversation.created_at,
                conversation.updated_at,
            ),
        )
        return conversation

    async def get(self, conversation_id: str) -> Conversation | None:
        """按 id 查询。"""
        row = await self.get_row(conversation_id)
        return _conversation(row) if row is not None else None

    async def require(self, conversation_id: str) -> Conversation:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        conversation = await self.get(conversation_id)
        if conversation is None:
            raise NotFoundError("会话", conversation_id)
        return conversation

    async def list_by_space(
        self,
        space_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        query: str | None = None,
    ) -> tuple[list[Conversation], int, str | None]:
        """列出会话，置顶优先、更新时间倒序。"""
        where = "space_id = ?"
        params: list[Any] = [space_id]
        if query and query.strip():
            where += " AND title LIKE ? ESCAPE '\\'"
            literal = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{literal}%")
        rows, next_cursor = await self._page(
            where=where,
            params=params,
            limit=limit,
            cursor=cursor,
            order_by="pinned DESC, updated_at DESC",
        )
        total = await self.db.fetchvalue(
            f"SELECT COUNT(*) FROM conversations WHERE {where}", params
        )
        return [_conversation(row) for row in rows], int(total or 0), next_cursor

    async def update(self, conversation_id: str, data: ConversationUpdate) -> Conversation:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "pinned" in fields and fields["pinned"] is not None:
            fields["pinned"] = to_int_bool(bool(fields["pinned"]))
        await self._update_row(conversation_id, fields)
        return await self.require(conversation_id)

    async def touch(self, conversation_id: str) -> None:
        """刷新会话的更新时间。"""
        await self._update_row(conversation_id, {})

    async def delete(self, conversation_id: str) -> bool:
        """删除会话：消息由外键级联删除，轨迹与反馈在这里显式清掉。

        ``traces`` 对 ``messages`` / ``conversations`` 都没有外键（SQLite 也不能给
        既有表补外键），所以删会话时轨迹会原地留下。实测过后果：一个 Space 里
        31 条轨迹有 23 条属于已删会话，占了 72% 的轨迹正文体量——而轨迹里存着
        提问原文与检索到的内容，界面上的删除确认还写着「引用轨迹与反馈将一并移除」。
        更要紧的是 ``feedback`` 挂在轨迹上：不删轨迹，已删会话的反馈还会继续被
        蒸馏成经验。``feedback`` 对 ``traces`` 有 ON DELETE CASCADE，
        所以删掉轨迹，反馈自然跟着走。
        """
        await self.db.execute("DELETE FROM traces WHERE conversation_id = ?", (conversation_id,))
        return await self.delete_row(conversation_id)

    async def count(self, space_id: str | None = None) -> int:
        """会话数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM conversations")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM conversations WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)


class MessageRepo(Repository):
    """``messages`` 表。"""

    table = "messages"

    async def create(self, data: MessageCreate) -> Message:
        """写入消息。"""
        message = Message(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO messages (id, conversation_id, role, content, citations, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.conversation_id,
                message.role,
                message.content,
                dump_json(message.citations),
                message.created_at,
            ),
        )
        return message

    async def get(self, message_id: str) -> Message | None:
        """按 id 查询。"""
        row = await self.get_row(message_id)
        return _message(row) if row is not None else None

    async def require(self, message_id: str) -> Message:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        message = await self.get(message_id)
        if message is None:
            raise NotFoundError("消息", message_id)
        return message

    async def list_by_conversation(self, conversation_id: str) -> list[Message]:
        """按会话取消息，时间正序。"""
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        )
        return [_message(row) for row in rows]

    async def update(self, message_id: str, data: MessageUpdate) -> Message:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "citations" in fields and fields["citations"] is not None:
            fields["citations"] = dump_json(
                [Citation.model_validate(item) for item in fields["citations"]]
            )
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await self.db.execute(
                f"UPDATE messages SET {assignments} WHERE id = ?",
                [*fields.values(), message_id],
            )
        return await self.require(message_id)

    async def delete(self, message_id: str) -> bool:
        """删除。"""
        return await self.delete_row(message_id)

    async def delete_by_conversation(self, conversation_id: str) -> int:
        """删除会话下全部消息。"""
        return await self.db.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        )

    async def count(self, conversation_id: str | None = None) -> int:
        """消息数。"""
        if conversation_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM messages")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
            )
        return int(value or 0)
