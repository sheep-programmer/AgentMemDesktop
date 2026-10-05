"""SQLite FTS5 全文索引维护。

中文 BM25 可用性的关键：``chunks_fts`` 使用 ``tokenize='unicode61'``，因此写入前
必须先用 jieba 分词并以空格拼接。索引与查询使用同一套分词参数，保证词元一致。

``chunks_fts`` 是 external content 表（``content='chunks'``），删除时按 FTS5 规范
使用 ``'delete'`` 指令并回传当年写入的（已分词的）内容。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Iterable

import jieba
import structlog

from agentmem.store.sqlite import SQLiteDatabase
from agentmem.store.worker import run_db_worker
from agentmem.types import FtsHit

logger = structlog.get_logger(__name__)

# jieba 导入时给自己的 logger 挂了一个写 stderr 的 handler，日志又会向上传给根 logger
# （API 进程里 ``basicConfig`` 挂了写 stdout 的 handler），于是每行都打两遍——日志里
# 「Building prefix dict ...」「Loading model cost 2.367 seconds」成对出现、耗时一字不差，
# 看着像初始化了两次，其实只加载了一次。摘掉它自带的 handler，只走应用的日志配置。
jieba.default_logger.removeHandler(jieba.log_console)

#: 保留含中日韩文字、字母或数字的词元
_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: 高频虚词不携带检索信息，索引与查询两侧同时丢弃，避免 OR 查询被稀释
STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "及",
        "或",
        "也",
        "就",
        "都",
        "而",
        "被",
        "把",
        "这",
        "那",
        "有",
        "为",
        "以",
        "对",
        "从",
        "到",
        "中",
        "上",
        "下",
        "个",
        "之",
        "其",
        "以及",
        "并且",
        "但是",
        "因为",
        "所以",
        "如果",
        "然后",
        "这样",
        "那样",
        "什么",
        "怎么",
        "为什么",
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "这些",
        "那些",
        "还是",
        "the",
        "a",
        "an",
        "of",
        "to",
        "is",
        "are",
        "was",
        "were",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "that",
        "this",
        "it",
        "as",
        "be",
    }
)


def warm_up() -> float:
    """加载 jieba 词典，返回耗时（秒）。

    jieba 惰性加载：不预热的话，词典（实测 0.5～2.4 秒）是在第一次检索或第一次写入
    全文索引时才读，算在用户的第一个问题头上。``initialize`` 自带锁、已加载时直接返回，
    所以预热与首个检索撞在一起也只会加载一次。同步阻塞，调用方放到线程里跑。
    """
    started = time.perf_counter()
    jieba.initialize()
    return time.perf_counter() - started


def tokenize(text: str) -> list[str]:
    """用 jieba 搜索引擎模式切词，只保留有检索意义的词元。"""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text)
    tokens: list[str] = []
    for raw in jieba.cut_for_search(normalized):
        token = raw.strip()
        if not token:
            continue
        if not _TOKEN_PATTERN.fullmatch(token):
            continue
        lowered = token.lower()
        if lowered in STOPWORDS:
            continue
        tokens.append(lowered)
    return tokens


def to_index_text(text: str) -> str:
    """把原文转成写入 FTS 的形态：分词 + 空格拼接。"""
    return " ".join(tokenize(text))


def build_match_query(query: str) -> str:
    """把自然语言查询转成 FTS5 MATCH 表达式（词元之间取 OR，保证召回）。"""
    tokens = tokenize(query)
    if not tokens:
        return ""
    seen: dict[str, None] = {}
    for token in tokens:
        seen.setdefault(token, None)
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in seen)


class FtsIndex:
    """``chunks_fts`` 的同步维护与检索。"""

    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db

    async def index_chunk(self, chunk_id: str, content: str) -> None:
        """写入或更新单条切片的全文索引。"""
        await self.index_chunks([(chunk_id, content)])

    async def index_chunks(self, items: Iterable[tuple[str, str]]) -> None:
        """批量写入全文索引。

        前置条件：``chunks`` 表中的内容与索引一致（新增切片或重建索引时成立）。
        若要修改切片内容，先用 :meth:`remove_chunks` 摘掉旧索引再重新写入。
        """
        payload = list(items)
        if not payload:
            return
        # 必须独占连接：sqlite3.Connection 不能被多线程并发使用（见 SQLiteDatabase.exclusive）
        async with self.db.exclusive() as conn:
            await run_db_worker(self._index_sync, conn, payload)

    def _index_sync(self, conn: sqlite3.Connection, items: list[tuple[str, str]]) -> None:
        for chunk_id, content in items:
            row = conn.execute(
                "SELECT rowid, content FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if row is None:
                continue
            rowid = int(row["rowid"])
            if self._has_entry(conn, rowid):
                self._delete_entry(conn, rowid, str(row["content"]))
            conn.execute(
                "INSERT INTO chunks_fts(rowid, content) VALUES (?, ?)",
                (rowid, to_index_text(content)),
            )

    async def remove_chunks(self, chunk_ids: list[str]) -> None:
        """按 chunk_id 移除全文索引（须在删除 chunks 行之前调用）。"""
        if not chunk_ids:
            return
        async with self.db.exclusive() as conn:
            await run_db_worker(self._remove_sync, conn, chunk_ids)

    def _remove_sync(self, conn: sqlite3.Connection, chunk_ids: list[str]) -> None:
        for chunk_id in chunk_ids:
            row = conn.execute(
                "SELECT rowid, content FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
            if row is None:
                continue
            rowid = int(row["rowid"])
            if self._has_entry(conn, rowid):
                self._delete_entry(conn, rowid, str(row["content"]))

    @staticmethod
    def _has_entry(conn: sqlite3.Connection, rowid: int) -> bool:
        """判断某 rowid 是否已进入索引。

        注意：对 external content 表做 ``SELECT ... FROM chunks_fts`` 会回读
        ``chunks`` 表，无法用来判断索引状态；这里查 FTS5 的影子表
        ``chunks_fts_docsize``（每个已索引 rowid 一行）。
        """
        try:
            row = conn.execute(
                "SELECT 1 FROM chunks_fts_docsize WHERE id = ? LIMIT 1", (rowid,)
            ).fetchone()
        except sqlite3.OperationalError:
            # 影子表不可用时按「已索引」处理，保证删除路径不会漏删
            return True
        return row is not None

    @staticmethod
    def _delete_entry(conn: sqlite3.Connection, rowid: int, content: str) -> None:
        """按 FTS5 'delete' 指令移除索引条目，需回传当初写入的分词结果。"""
        conn.execute(
            "INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', ?, ?)",
            (rowid, to_index_text(content)),
        )

    async def remove_document(self, document_id: str) -> None:
        """移除某文档全部切片的全文索引。"""
        async with self.db.exclusive() as conn:
            await run_db_worker(self._remove_document_sync, conn, document_id)

    def _remove_document_sync(self, conn: sqlite3.Connection, document_id: str) -> None:
        rows = conn.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (document_id,)
        ).fetchall()
        # 复用同一个 conn：锁不可重入，这里绝不能再走一次 exclusive()
        self._remove_sync(conn, [str(row["id"]) for row in rows])

    async def search(
        self,
        query: str,
        *,
        limit: int = 50,
        space_id: str | None = None,
        document_ids: list[str] | None = None,
    ) -> list[FtsHit]:
        """BM25 检索。

        Args:
            query: 自然语言查询，内部自动分词。
            limit: 返回条数。
            space_id: 限定 Space。
            document_ids: 限定文档（用于"只在指定文档内检索"）。
        """
        match = build_match_query(query)
        if not match:
            return []
        async with self.db.exclusive() as conn:
            return await run_db_worker(
                self._search_sync, conn, match, limit, space_id, document_ids
            )

    def _search_sync(
        self,
        conn: sqlite3.Connection,
        match: str,
        limit: int,
        space_id: str | None,
        document_ids: list[str] | None,
    ) -> list[FtsHit]:
        sql = (
            "SELECT c.id AS chunk_id, c.space_id AS space_id, c.document_id AS document_id, "
            "       c.content AS content, bm25(chunks_fts) AS rank "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH :match "
            "  AND (:space_id IS NULL OR c.space_id = :space_id) "
            "  AND (:no_docs = 1 OR c.document_id IN (SELECT value FROM json_each(:doc_ids))) "
            "ORDER BY rank LIMIT :limit"
        )
        params = {
            "match": match,
            "space_id": space_id,
            "no_docs": 1 if not document_ids else 0,
            "doc_ids": _json_array(document_ids or []),
            "limit": limit,
        }
        rows = conn.execute(sql, params).fetchall()
        hits: list[FtsHit] = []
        for row in rows:
            hits.append(
                FtsHit(
                    chunk_id=str(row["chunk_id"]),
                    space_id=str(row["space_id"]),
                    document_id=str(row["document_id"]),
                    score=-float(row["rank"]),
                    content=str(row["content"]),
                )
            )
        return hits

    async def rebuild(self, space_id: str | None = None) -> int:
        """按 jieba 分词重建全文索引，返回重建条数。"""
        async with self.db.exclusive() as conn:
            return await run_db_worker(self._rebuild_sync, conn, space_id)

    def _rebuild_sync(self, conn: sqlite3.Connection, space_id: str | None) -> int:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('delete-all')")
        if space_id is None:
            rows = conn.execute("SELECT id, content FROM chunks ORDER BY rowid").fetchall()
        else:
            rows = conn.execute(
                "SELECT id, content FROM chunks WHERE space_id = ? ORDER BY rowid", (space_id,)
            ).fetchall()
        count = 0
        for row in rows:
            chunk_row = conn.execute(
                "SELECT rowid FROM chunks WHERE id = ?", (str(row["id"]),)
            ).fetchone()
            if chunk_row is None:
                continue
            conn.execute(
                "INSERT INTO chunks_fts(rowid, content) VALUES (?, ?)",
                (int(chunk_row["rowid"]), to_index_text(str(row["content"]))),
            )
            count += 1
        logger.info("fts_rebuilt", space_id=space_id, chunks=count)
        return count

    async def count(self) -> int:
        """索引中的行数。"""
        value = await self.db.fetchvalue("SELECT COUNT(*) FROM chunks_fts")
        return int(value or 0)


def _json_array(values: list[str]) -> str:
    """把字符串列表编码成 SQLite json_each 可消费的 JSON 数组。"""
    return json.dumps(values, ensure_ascii=False)
