"""LanceDB 向量存储封装。

三张表：``chunks_vec`` / ``cards_vec`` / ``insights_vec``。

维度守卫：每张表在 ``vec_meta`` 中登记 ``embedding_model`` 与 ``dim``。当写入向量的
维度与既有表不一致时，抛 :class:`~agentmem.errors.EmbeddingDimMismatchError`，
绝不静默写入。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Self

import lancedb
import pyarrow as pa
import structlog

from agentmem.errors import EmbeddingDimMismatchError, ValidationError
from agentmem.store.sqlite import now_ms
from agentmem.store.worker import run_db_worker
from agentmem.types import (
    VectorHit,
    VectorRecord,
    VectorTableMeta,
    VectorTableName,
)

logger = structlog.get_logger(__name__)

#: 向量表 → 主键列名
PRIMARY_KEYS: dict[VectorTableName, str] = {
    "chunks_vec": "chunk_id",
    "cards_vec": "card_id",
    "insights_vec": "insight_id",
}

#: 允许出现在过滤表达式里的字段。白名单防止调用方把任意字符串当字段名传进来。
ALLOWED_FILTER_FIELDS: frozenset[str] = frozenset(
    {"space_id", "document_id", "chunk_id", "card_id", "insight_id", "embedding_model"}
)


def sql_literal(value: str) -> str:
    """把字符串转义成 LanceDB 过滤表达式里的字面量。

    LanceDB 的 ``where`` 收的是表达式字符串，没有参数绑定，
    所以凡是拼进去的值都必须在这里过一遍——单引号翻倍是 SQL 标准转义。
    """
    return "'" + value.replace("'", "''") + "'"


VECTOR_TABLES: tuple[VectorTableName, ...] = ("chunks_vec", "cards_vec", "insights_vec")

META_TABLE = "vec_meta"


def build_schema(table: VectorTableName, dim: int) -> pa.Schema:
    """构造向量表的 Arrow schema。"""
    fields = [
        pa.field(PRIMARY_KEYS[table], pa.string()),
        pa.field("space_id", pa.string()),
    ]
    if table == "chunks_vec":
        fields.append(pa.field("document_id", pa.string()))
    fields.append(pa.field("vector", pa.list_(pa.float32(), dim)))
    fields.append(pa.field("embedding_model", pa.string()))
    return pa.schema(fields)


def _meta_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("table_name", pa.string()),
            pa.field("embedding_model", pa.string()),
            pa.field("dim", pa.int32()),
            pa.field("updated_at", pa.int64()),
        ]
    )


class VectorStore:
    """LanceDB 封装；一个 Space 一个实例。"""

    def __init__(self, uri: Path) -> None:
        self.uri = Path(uri)
        self._db: lancedb.DBConnection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """打开（或创建）向量库目录。"""
        async with self._lock:
            if self._db is not None:
                return
            self.uri.mkdir(parents=True, exist_ok=True)
            await run_db_worker(self._open_sync)

    def _open_sync(self) -> None:
        self._db = lancedb.connect(str(self.uri))

    async def close(self) -> None:
        """释放连接句柄。"""
        async with self._lock:
            self._db = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def db(self) -> lancedb.DBConnection:
        """底层 LanceDB 连接。"""
        if self._db is None:
            raise RuntimeError("向量库尚未打开，请先 await store.open()")
        return self._db

    # -- 台账 -------------------------------------------------------------

    async def table_meta(self, table: VectorTableName) -> VectorTableMeta | None:
        """读取某张向量表的维度台账。"""
        async with self._lock:
            return await run_db_worker(self._table_meta_sync, table)

    def _table_meta_sync(self, table: VectorTableName) -> VectorTableMeta | None:
        if META_TABLE not in self._table_names_sync():
            return None
        rows = self.db.open_table(META_TABLE).to_arrow().to_pylist()
        for row in rows:
            if str(row["table_name"]) != table:
                continue
            return VectorTableMeta(
                table_name=table,
                embedding_model=str(row["embedding_model"]),
                dim=int(row["dim"]),
                updated_at=int(row["updated_at"]),
            )
        return None

    def _table_names_sync(self) -> list[str]:
        """列出全部表名；兼容不同版本的 LanceDB API。"""
        lister = getattr(self.db, "list_tables", None)
        if lister is not None:
            try:
                response = lister()
                tables = getattr(response, "tables", response)
                return [str(name) for name in tables]
            except TypeError:  # pragma: no cover - 旧版本签名不同
                pass
        try:
            names = self.db.table_names(limit=100)
        except TypeError:  # pragma: no cover - 不接受 limit 参数的旧版本
            names = self.db.table_names()
        return [str(name) for name in names]

    def _write_meta_sync(self, table: VectorTableName, embedding_model: str, dim: int) -> None:
        if META_TABLE not in self._table_names_sync():
            self.db.create_table(META_TABLE, schema=_meta_schema())
        handle = self.db.open_table(META_TABLE)
        handle.delete(f"table_name = '{table}'")
        handle.add(
            [
                {
                    "table_name": table,
                    "embedding_model": embedding_model,
                    "dim": dim,
                    "updated_at": now_ms(),
                }
            ]
        )

    # -- 维度守卫 ---------------------------------------------------------

    async def ensure_table(self, table: VectorTableName, dim: int, embedding_model: str) -> None:
        """确保向量表存在且维度一致。

        Raises:
            EmbeddingDimMismatchError: 既有表的维度与 ``dim`` 不一致。
        """
        if dim < 1:
            raise ValidationError("向量维度必须为正整数", detail={"dim": dim})
        async with self._lock:
            await run_db_worker(self._ensure_table_sync, table, dim, embedding_model)

    def _ensure_table_sync(self, table: VectorTableName, dim: int, embedding_model: str) -> None:
        names = self._table_names_sync()
        if table not in names:
            self.db.create_table(table, schema=build_schema(table, dim))
            self._write_meta_sync(table, embedding_model, dim)
            return
        meta = self._table_meta_sync(table)
        if meta is not None and meta.dim != dim:
            raise EmbeddingDimMismatchError(meta.dim, dim, table=table)
        if meta is not None and meta.embedding_model != embedding_model:
            logger.warning(
                "embedding_model_changed",
                table=table,
                previous=meta.embedding_model,
                current=embedding_model,
            )
        if meta is None:
            self._write_meta_sync(table, embedding_model, dim)

    def existing_dim(self, table: VectorTableName) -> int | None:
        """已存在的向量维度；表不存在时返回 ``None``。"""
        names = self._table_names_sync()
        if table not in names:
            return None
        meta = self._table_meta_sync(table)
        if meta is not None:
            return meta.dim
        field = self.db.open_table(table).schema.field("vector")
        value_type = field.type
        if isinstance(value_type, pa.FixedSizeListType):
            return int(value_type.list_size)
        return None

    # -- 增删改查 ---------------------------------------------------------

    async def upsert(self, table: VectorTableName, records: list[VectorRecord]) -> int:
        """按主键写入或更新向量，返回写入条数。"""
        if not records:
            return 0
        dim = len(records[0].vector)
        for record in records:
            if len(record.vector) != dim:
                raise ValidationError(
                    "同一批向量的维度必须一致",
                    detail={"expected": dim, "actual": len(record.vector), "id": record.id},
                )
        if dim < 1:
            raise ValidationError("向量维度必须为正整数", detail={"dim": dim})
        async with self._lock:
            await run_db_worker(self._upsert_sync, table, records)
        return len(records)

    def _upsert_sync(self, table: VectorTableName, records: list[VectorRecord]) -> None:
        self._ensure_table_sync(table, len(records[0].vector), records[0].embedding_model)
        handle = self.db.open_table(table)
        key = PRIMARY_KEYS[table]
        payload = [self._to_row(table, record) for record in records]
        (
            handle.merge_insert(key)
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute(payload)
        )

    @staticmethod
    def _to_row(table: VectorTableName, record: VectorRecord) -> dict[str, Any]:
        row: dict[str, Any] = {
            PRIMARY_KEYS[table]: record.id,
            "space_id": record.space_id,
            "vector": record.vector,
            "embedding_model": record.embedding_model,
        }
        if table == "chunks_vec":
            row["document_id"] = record.document_id or ""
        return row

    async def delete(self, table: VectorTableName, ids: list[str]) -> int:
        """按主键删除向量。"""
        if not ids:
            return 0
        async with self._lock:
            return await run_db_worker(self._delete_sync, table, ids)

    def _delete_sync(self, table: VectorTableName, ids: list[str]) -> int:
        names = self._table_names_sync()
        if table not in names:
            return 0
        handle = self.db.open_table(table)
        key = PRIMARY_KEYS[table]
        quoted = ", ".join(sql_literal(item) for item in ids)
        handle.delete(f"{key} IN ({quoted})")
        return len(ids)

    async def list_ids(self, table: VectorTableName) -> list[str]:
        """列出表里全部主键（体检用：找出指向已删行的向量）。只读主键列，不把向量读进内存。"""
        async with self._lock:
            return await run_db_worker(self._list_ids_sync, table)

    def _list_ids_sync(self, table: VectorTableName) -> list[str]:
        if table not in self._table_names_sync():
            return []
        key = PRIMARY_KEYS[table]
        # 不带查询向量的 search() 是普通扫描；limit 缺省只有 10 条，必须显式放开
        rows = self.db.open_table(table).search().select([key]).limit(None).to_arrow()
        return [str(value) for value in rows.column(key).to_pylist()]

    async def delete_by_field(self, table: VectorTableName, field: str, value: str) -> None:
        """按单个字段等值删除。

        比 :meth:`delete_where` 更安全：调用方给的是**值**而不是过滤表达式，
        转义由这里统一负责。除非确实需要复杂条件，否则一律用这个。
        """
        if field not in ALLOWED_FILTER_FIELDS:
            raise ValueError(f"不允许按字段 {field!r} 过滤")
        await self.delete_where(table, f"{field} = {sql_literal(value)}")

    async def delete_where(self, table: VectorTableName, where: str) -> None:
        """按条件删除（如 ``document_id = 'xxx'``）。

        ⚠️ ``where`` 是**原样拼进查询**的过滤表达式。
        任何来自用户输入的值都必须先过 :func:`sql_literal`，
        或者直接改用 :meth:`delete_by_field`。
        """
        async with self._lock:
            await run_db_worker(self._delete_where_sync, table, where)

    def _delete_where_sync(self, table: VectorTableName, where: str) -> None:
        if table not in self._table_names_sync():
            return
        self.db.open_table(table).delete(where)

    async def search(
        self,
        table: VectorTableName,
        vector: list[float],
        *,
        limit: int = 50,
        space_id: str | None = None,
        where: str | None = None,
    ) -> list[VectorHit]:
        """向量近邻检索。

        Args:
            table: 向量表名。
            vector: 查询向量。
            limit: 返回条数。
            space_id: 限定 Space。
            where: 额外的 LanceDB 过滤表达式，与 ``space_id`` 取交集。
        """
        async with self._lock:
            return await run_db_worker(self._search_sync, table, vector, limit, space_id, where)

    def _search_sync(
        self,
        table: VectorTableName,
        vector: list[float],
        limit: int,
        space_id: str | None,
        where: str | None,
    ) -> list[VectorHit]:
        if table not in self._table_names_sync():
            return []
        handle = self.db.open_table(table)
        query = handle.search(vector).limit(limit)
        conditions: list[str] = []
        if space_id is not None:
            conditions.append(f"space_id = {sql_literal(space_id)}")
        if where:
            conditions.append(f"({where})")
        if conditions:
            query = query.where(" AND ".join(conditions))
        key = PRIMARY_KEYS[table]
        hits: list[VectorHit] = []
        for row in query.to_list():
            hits.append(
                VectorHit(
                    id=str(row[key]),
                    space_id=str(row["space_id"]),
                    document_id=str(row["document_id"]) if row.get("document_id") else None,
                    score=_to_score(row.get("_distance"), row.get("_score")),
                    embedding_model=str(row["embedding_model"])
                    if row.get("embedding_model")
                    else None,
                )
            )
        return hits

    async def count(self, table: VectorTableName, space_id: str | None = None) -> int:
        """统计向量条数。"""
        async with self._lock:
            return await run_db_worker(self._count_sync, table, space_id)

    def _count_sync(self, table: VectorTableName, space_id: str | None) -> int:
        if table not in self._table_names_sync():
            return 0
        handle = self.db.open_table(table)
        if space_id is None:
            return int(handle.count_rows())
        return int(handle.count_rows(f"space_id = {sql_literal(space_id)}"))

    async def drop(self, table: VectorTableName) -> None:
        """删除整张向量表（重建索引用）。"""
        async with self._lock:
            await run_db_worker(self._drop_sync, table)

    def _drop_sync(self, table: VectorTableName) -> None:
        if table in self._table_names_sync():
            self.db.drop_table(table)
        if META_TABLE in self._table_names_sync():
            self.db.open_table(META_TABLE).delete(f"table_name = '{table}'")

    async def drop_all(self) -> None:
        """清空全部向量表，用于切换 embedding 模型后重建索引。"""
        for table in VECTOR_TABLES:
            await self.drop(table)


def _to_score(distance: Any, score: Any) -> float:
    """把 LanceDB 返回的距离换算成「越大越相关」的分数。"""
    if distance is not None:
        try:
            return 1.0 / (1.0 + float(distance))
        except (TypeError, ValueError):
            return 0.0
    if score is not None:
        try:
            return float(score)
        except (TypeError, ValueError):
            return 0.0
    return 0.0
