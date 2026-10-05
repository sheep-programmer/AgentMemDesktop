"""Repository 公共设施：ULID 生成、JSON 编解码、分页与更新拼装。

数据库中以 JSON 字符串存储的列一律在此层完成编解码，上层只接触 Pydantic 模型。
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from typing import Any, ClassVar, TypeVar

import structlog
from pydantic import BaseModel
from ulid import ULID

from agentmem.errors import NotFoundError, ValidationError
from agentmem.store.sqlite import SQLiteDatabase, now_ms

logger = structlog.get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


def new_id() -> str:
    """生成新的 ULID 主键。"""
    return str(ULID())


def dump_json(value: Any) -> str | None:
    """把 Pydantic 模型 / 列表编码成 JSON 字符串；``None`` 原样返回。"""
    if value is None:
        return None
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, list):
        value = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value
        ]
    return json.dumps(value, ensure_ascii=False)


def load_json(raw: Any, default: Any) -> Any:
    """解析 JSON 字符串；空值或损坏时返回默认值。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        logger.warning("json_decode_failed", raw=str(raw)[:200])
        return default


def load_str_list(raw: Any) -> list[str]:
    """解析 ``JSON string[]`` 列。"""
    value = load_json(raw, [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def load_models(raw: Any, model_cls: type[ModelT]) -> list[ModelT]:
    """解析 ``JSON object[]`` 列。"""
    value = load_json(raw, [])
    if not isinstance(value, list):
        return []
    result: list[ModelT] = []
    for item in value:
        if isinstance(item, dict):
            result.append(model_cls.model_validate(item))
    return result


def load_model(raw: Any, model_cls: type[ModelT], default: ModelT) -> ModelT:
    """解析 ``JSON object`` 列。"""
    value = load_json(raw, None)
    if isinstance(value, dict):
        return model_cls.model_validate(value)
    return default


def to_bool(value: Any) -> bool:
    """把 SQLite 的 0/1 转成布尔值。"""
    return bool(value)


def to_int_bool(value: bool) -> int:
    """把布尔值转成 SQLite 的 0/1。"""
    return 1 if value else 0


class Repository:
    """所有 Repository 的基类。"""

    table: ClassVar[str] = ""
    #: 该表是否有 ``updated_at`` 列
    has_updated_at: ClassVar[bool] = False

    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    async def _update_row(
        self, row_id: str, fields: dict[str, Any], *, touch: bool | None = None
    ) -> int:
        """按主键更新给定字段；``touch`` 缺省时按表是否有 ``updated_at`` 决定。"""
        payload = dict(fields)
        if touch is None:
            touch = self.has_updated_at
        if touch:
            payload["updated_at"] = now_ms()
        if not payload:
            return 0
        assignments = ", ".join(f"{name} = ?" for name in payload)
        params: list[Any] = [*payload.values(), row_id]
        return await self.db.execute(f"UPDATE {self.table} SET {assignments} WHERE id = ?", params)

    async def _page(
        self,
        *,
        where: str = "",
        params: list[Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
        order_by: str = "id",
        descending: bool = False,
        select: str = "*",
    ) -> tuple[list[sqlite3.Row], str | None]:
        """游标分页：返回本页行与下一页游标。

        按 ``id`` 排序时游标就是上一页最后一行的主键（ULID 字典序即时间序）。

        按其他列排序（如置信度）时用 keyset 游标：记下上一页最后一行的各排序列取值
        加主键，下一页从「严格排在它后面」的行开始，主键兜底打破并列。此前非 id 排序
        一律返回 ``next_cursor=None``——卡片按置信度排，于是界面写着「卡片 (170)」，
        却永远只翻得到前 50 张。
        """
        primary = order_by.split(",")[0].strip().lower()
        id_ordered = primary in {"id", "id asc", "id desc"}
        if not id_ordered:
            return await self._keyset_page(
                where=where,
                params=params,
                limit=limit,
                cursor=cursor,
                order_by=order_by,
                select=select,
            )
        order_clause = f"id {'DESC' if descending else 'ASC'}" if order_by == "id" else order_by
        args: list[Any] = list(params or [])
        clauses: list[str] = []
        if where:
            clauses.append(f"({where})")
        if cursor:
            clauses.append("id < ?" if descending else "id > ?")
            args.append(cursor)
        sql = f"SELECT {select} FROM {self.table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order_clause} LIMIT ?"
        args.append(limit + 1)
        rows = await self.db.fetchall(sql, args)
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = str(rows[-1]["id"])
        return rows, next_cursor

    async def _keyset_page(
        self,
        *,
        where: str,
        params: list[Any] | None,
        limit: int,
        cursor: str | None,
        order_by: str,
        select: str,
    ) -> tuple[list[sqlite3.Row], str | None]:
        """按任意列排序的 keyset 分页（``id`` 自动追加为最后一个排序键）。"""
        keys = _parse_order(order_by)
        if not any(column == "id" for column, _ in keys):
            keys.append(("id", keys[-1][1]))
        args: list[Any] = list(params or [])
        clauses: list[str] = [f"({where})"] if where else []
        if cursor:
            values = _decode_keyset(cursor, len(keys))
            # (c1, c2, ..., id) 严格排在游标之后：逐级「前面都相等、这一列越过」
            alternatives: list[str] = []
            for index, (column, direction) in enumerate(keys):
                equal = [f"{keys[j][0]} = ?" for j in range(index)]
                beyond = f"{column} {'<' if direction == 'DESC' else '>'} ?"
                alternatives.append("(" + " AND ".join([*equal, beyond]) + ")")
                args.extend([*values[:index], values[index]])
            clauses.append("(" + " OR ".join(alternatives) + ")")
        sql = f"SELECT {select} FROM {self.table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY " + ", ".join(f"{column} {direction}" for column, direction in keys)
        sql += " LIMIT ?"
        args.append(limit + 1)
        rows = await self.db.fetchall(sql, args)
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_keyset([last[column] for column, _ in keys])
        return rows, next_cursor

    async def exists(self, row_id: str) -> bool:
        """主键是否存在。"""
        row = await self.db.fetchone(f"SELECT 1 FROM {self.table} WHERE id = ?", (row_id,))
        return row is not None

    async def require_row(self, row_id: str, resource: str | None = None) -> sqlite3.Row:
        """取单行，不存在则抛 :class:`NotFoundError`。"""
        row = await self.get_row(row_id)
        if row is None:
            raise NotFoundError(resource or self.table, row_id)
        return row

    async def get_row(self, row_id: str) -> sqlite3.Row | None:
        """取原始行。"""
        return await self.db.fetchone(f"SELECT * FROM {self.table} WHERE id = ?", (row_id,))

    async def delete_row(self, row_id: str) -> bool:
        """按主键删除。"""
        affected = await self.db.execute(f"DELETE FROM {self.table} WHERE id = ?", (row_id,))
        return affected > 0


_ORDER_TERM = re.compile(r"^([a-z_][a-z0-9_]*)(?:\s+(asc|desc))?$", re.IGNORECASE)
_KEYSET_PREFIX = "k1."


def _parse_order(order_by: str) -> list[tuple[str, str]]:
    """``"confidence DESC, updated_at DESC"`` → ``[("confidence", "DESC"), ...]``。

    排序串来自仓库内部常量，这里仍然只放行「列名 + 方向」，免得哪天有人把
    用户输入拼进来变成注入点。
    """
    keys: list[tuple[str, str]] = []
    for term in order_by.split(","):
        match = _ORDER_TERM.match(term.strip())
        if match is None:
            raise ValueError(f"不支持的排序：{order_by!r}")
        keys.append((match.group(1), (match.group(2) or "ASC").upper()))
    return keys


def _encode_keyset(values: list[Any]) -> str:
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
    return _KEYSET_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_keyset(cursor: str, size: int) -> list[Any]:
    """解出游标里的排序键；格式不对（旧游标、被篡改）就报参数错误而不是 500。"""
    try:
        if not cursor.startswith(_KEYSET_PREFIX):
            raise ValueError("prefix")
        body = cursor[len(_KEYSET_PREFIX) :]
        values = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationError("分页游标无效，请从第一页重新加载") from exc
    if not isinstance(values, list) or len(values) != size:
        raise ValidationError("分页游标无效，请从第一页重新加载")
    return values
