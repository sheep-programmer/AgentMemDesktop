"""SQLite 连接管理与版本化迁移。

- 打开即启用 WAL 与外键约束；
- 所有调用通过 ``asyncio.to_thread`` 下放到线程池，并用一把互斥锁保证同一时刻
  只有一个线程使用该连接；
- 迁移脚本按序号记录在 ``schema_version`` 表中，只前进不后退。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import structlog

from agentmem.store.worker import run_db_worker

logger = structlog.get_logger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

SqlParams = Sequence[Any] | dict[str, Any]


def now_ms() -> int:
    """当前 Unix 毫秒时间戳（UTC）。"""
    return int(time.time() * 1000)


def _split_statements(script: str) -> list[str]:
    """按分号拆分 SQL 脚本；脚本中的注释由 SQLite 自行处理。"""
    return [chunk.strip() for chunk in script.split(";") if chunk.strip()]


def load_schema_sql() -> str:
    """读取内置建表脚本。"""
    return SCHEMA_PATH.read_text(encoding="utf-8")


class Transaction:
    """事务句柄：供一个事务内的多条语句复用同一连接（此时已持有互斥锁）。"""

    def __init__(self, db: SQLiteDatabase) -> None:
        self._db = db

    async def execute(self, sql: str, params: SqlParams = ()) -> int:
        """执行写语句，返回受影响行数。"""
        return await run_db_worker(self._db._execute_sync, sql, params)

    async def executemany(self, sql: str, seq: Iterable[SqlParams]) -> None:
        """批量执行写语句。"""
        await run_db_worker(self._db._executemany_sync, sql, list(seq))

    async def fetchone(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        """查询单行。"""
        return await run_db_worker(self._db._fetchone_sync, sql, params)

    async def fetchall(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        """查询多行。"""
        return await run_db_worker(self._db._fetchall_sync, sql, params)


class SQLiteDatabase:
    """单个 SQLite 文件的异步门面。

    典型用法::

        db = SQLiteDatabase(path)
        await db.connect()
        ...
        await db.close()
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- 生命周期 ---------------------------------------------------------

    async def connect(self) -> None:
        """建立连接、设置 PRAGMA 并执行迁移。"""
        async with self._lock:
            if self._conn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            await run_db_worker(self._connect_sync)

    def _connect_sync(self) -> None:
        self._conn = self._open_sync()
        try:
            self._migrate_sync()
        except BaseException:
            self._conn.close()
            self._conn = None
            raise

    async def close(self) -> None:
        """关闭连接。"""
        async with self._lock:
            conn = self._conn
            if conn is not None:
                try:
                    await run_db_worker(conn.close)
                finally:
                    self._conn = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _open_sync(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接；未连接时抛错。

        ⚠️ **不要直接用它执行 SQL。** 一个 ``sqlite3.Connection`` 不能被多个线程
        并发使用：两路全文检索同跑时，轻则 ``bm25()`` 返回 NULL 让整路召回静默丢失，
        重则直接抛 ``InterfaceError``。需要自己写 SQL 时请用 :meth:`exclusive`。
        """
        if self._conn is None:
            raise RuntimeError("数据库尚未连接，请先 await db.connect()")
        return self._conn

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[sqlite3.Connection]:
        """独占底层连接，供必须自己写 SQL 的场景使用（如 FTS5 维护）。

        持有的是与 :meth:`execute` / :meth:`fetchall` 同一把锁，因此和常规读写互斥。

        ⚠️ 锁**不可重入**：在本上下文内不要再 ``await`` 任何会取同一把锁的方法
        （``execute`` / ``fetchall`` / ``transaction`` / 嵌套的 ``exclusive``），否则死锁。
        块内的数据库调用请用 ``run_db_worker``，确保取消后仍等线程退出再释放锁。
        """
        async with self._lock:
            yield self.connection

    @property
    def _raw(self) -> sqlite3.Connection:
        return self.connection

    # -- 迁移 -------------------------------------------------------------

    async def migrate(self) -> int:
        """执行尚未应用的迁移，返回当前 schema 版本。"""
        async with self._lock:
            return await run_db_worker(self._migrate_sync)

    def _migrate_sync(self) -> int:
        conn = self._raw
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, "
            "applied_at INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"]) if row is not None and row["v"] is not None else 0
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split_statements(script):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, now_ms()),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            current = version
            logger.info("schema_migrated", version=version, path=str(self.path))
        return current

    async def schema_version(self) -> int:
        """当前 schema 版本。"""
        row = await self.fetchone("SELECT MAX(version) AS v FROM schema_version")
        if row is None or row["v"] is None:
            return 0
        return int(row["v"])

    # -- 语句执行 ---------------------------------------------------------

    def _execute_sync(self, sql: str, params: SqlParams = ()) -> int:
        cursor = self._raw.execute(sql, params)
        return cursor.rowcount

    def _executemany_sync(self, sql: str, seq: Sequence[SqlParams]) -> None:
        self._raw.executemany(sql, seq)

    def _fetchone_sync(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        cursor = self._raw.execute(sql, params)
        row = cursor.fetchone()
        return row if isinstance(row, sqlite3.Row) else None

    def _fetchall_sync(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        cursor = self._raw.execute(sql, params)
        return list(cursor.fetchall())

    async def execute(self, sql: str, params: SqlParams = ()) -> int:
        """执行写语句，返回受影响行数。"""
        async with self._lock:
            return await run_db_worker(self._execute_sync, sql, params)

    async def executemany(self, sql: str, seq: Iterable[SqlParams]) -> None:
        """批量执行写语句。"""
        rows = list(seq)
        if not rows:
            return
        async with self._lock:
            await run_db_worker(self._executemany_sync, sql, rows)

    async def fetchone(self, sql: str, params: SqlParams = ()) -> sqlite3.Row | None:
        """查询单行。"""
        async with self._lock:
            return await run_db_worker(self._fetchone_sync, sql, params)

    async def fetchall(self, sql: str, params: SqlParams = ()) -> list[sqlite3.Row]:
        """查询多行。"""
        async with self._lock:
            return await run_db_worker(self._fetchall_sync, sql, params)

    async def fetchvalue(self, sql: str, params: SqlParams = ()) -> Any:
        """查询单个标量值。"""
        row = await self.fetchone(sql, params)
        if row is None or len(row) == 0:
            return None
        return row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """开启一个写事务；异常时自动回滚。"""
        async with self._lock:
            try:
                await run_db_worker(self._begin_sync)
                yield Transaction(self)
                await run_db_worker(self._commit_sync)
            except BaseException:
                if self.connection.in_transaction:
                    await run_db_worker(self._rollback_sync)
                raise

    def _begin_sync(self) -> None:
        self._raw.execute("BEGIN IMMEDIATE")

    def _rollback_sync(self) -> None:
        self._raw.execute("ROLLBACK")

    def _commit_sync(self) -> None:
        self._raw.execute("COMMIT")

    # -- 维护 -------------------------------------------------------------

    async def vacuum(self) -> None:
        """整理数据库文件。"""
        await self.execute("VACUUM")


#: 迁移脚本清单：(版本号, SQL)。新增变更时追加，不得修改已发布条目。
#:
#: ⚠️ **`schema.sql` 就是版本 1 的快照，不是「当前 schema」**。新库也要把
#: 后续迁移依次跑一遍才追平，所以改结构时**只改这个清单，不要动 `schema.sql`**——
#: 在 `schema.sql` 里加了列又在迁移里 ALTER 同一列，新库会直接
#: `duplicate column name` 建不起来。
MIGRATIONS: list[tuple[int, str]] = [
    (1, load_schema_sql()),
    # v2：记录前缀缓存命中量。没有这两列就无法回答「上下文装配的优化到底省了多少」，
    # 而那正是这套设计的全部意义所在。
    (
        2,
        """
        ALTER TABLE usage_records ADD COLUMN cached_tokens INTEGER DEFAULT 0;
        ALTER TABLE usage_records ADD COLUMN cache_write_tokens INTEGER DEFAULT 0;
        """,
    ),
    # v3：知识卡片的历史版本。L2 卡片是「事实」，事实会变——新指南覆盖旧值时，
    # 旧值必须留下来，否则用户永远看不到「什么时候改的、原来写的是什么」。
    (
        3,
        """
        CREATE TABLE card_versions (
            id            TEXT PRIMARY KEY,
            card_id       TEXT NOT NULL REFERENCES knowledge_cards(id) ON DELETE CASCADE,
            space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            version       INTEGER NOT NULL,
            kind          TEXT NOT NULL,
            title         TEXT NOT NULL,
            body          TEXT NOT NULL,
            aliases       TEXT NOT NULL DEFAULT '[]',
            source_chunks TEXT NOT NULL DEFAULT '[]',
            confidence    REAL NOT NULL DEFAULT 0.5,
            verified_by   TEXT,
            valid_from    INTEGER NOT NULL,
            valid_to      INTEGER NOT NULL,
            created_at    INTEGER NOT NULL
        );
        CREATE INDEX idx_card_versions_card ON card_versions(card_id, version DESC);
        """,
    ),
    # v4：实体提及明细。此前 mention_count 每抽取一次就 +1，重新解析过的文档会把
    # 计数越推越高，知识图谱的节点权重因此虚高。改成按「哪篇文档提到了它」记账，
    # 同一个文档重复抽取不再重复计数，计数也从明细重算而不是累加。
    (
        4,
        """
        CREATE TABLE entity_mentions (
            id          TEXT PRIMARY KEY,
            entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_id    TEXT NOT NULL DEFAULT '',
            created_at  INTEGER NOT NULL,
            UNIQUE (entity_id, document_id, chunk_id)
        );
        CREATE INDEX idx_entity_mentions_document ON entity_mentions(document_id);
        """,
    ),
    # v5：一致性实测。此前雷达上的「逻辑一致性」算的是「active 经验占比」——
    # 量的是规则沉淀多不多，与「同样的问题问两遍会不会得到两个说法」毫无关系。
    # 真口径要重复提问并比较答案语义，代价是 N× 生成调用，所以按需触发、结果落库。
    (
        5,
        """
        CREATE TABLE consistency_probes (
            id          TEXT PRIMARY KEY,
            space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            questions   INTEGER NOT NULL,
            repeats     INTEGER NOT NULL,
            similarity  REAL NOT NULL,
            detail      TEXT NOT NULL DEFAULT '[]',
            created_at  INTEGER NOT NULL
        );
        CREATE INDEX idx_consistency_probes_space
            ON consistency_probes(space_id, created_at DESC);
        ALTER TABLE expertise_snapshots ADD COLUMN consistency_source TEXT NOT NULL DEFAULT 'proxy';
        ALTER TABLE expertise_snapshots ADD COLUMN consistency_measured_at INTEGER;
        """,
    ),
    # v6：进化日志。此前 `/evolve/history` 用评测记录近似：把 with_insights 那一次的
    # **分数**当成「本次收益」返回（71.5 这种分数被显示成 delta），而一次进化真正的
    # 产出（新增几条、晋升几条、淘汰几条）算完就丢了。日志表把这些如实留下来。
    (
        6,
        """
        CREATE TABLE evolution_runs (
            id               TEXT PRIMARY KEY,
            space_id         TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            produced         INTEGER NOT NULL DEFAULT 0,
            merged           INTEGER NOT NULL DEFAULT 0,
            duplicates       INTEGER NOT NULL DEFAULT 0,
            conflicts        INTEGER NOT NULL DEFAULT 0,
            promoted         INTEGER NOT NULL DEFAULT 0,
            demoted          INTEGER NOT NULL DEFAULT 0,
            eval_delta       REAL,
            expertise_before REAL NOT NULL DEFAULT 0,
            expertise_after  REAL NOT NULL DEFAULT 0,
            duration_ms      INTEGER,
            created_at       INTEGER NOT NULL
        );
        CREATE INDEX idx_evolution_runs_space ON evolution_runs(space_id, created_at DESC);
        """,
    ),
    # v7：经验置信度变更流水。产品的立身之本是「经验可证伪」，而此前每一次加减分
    # 都是就地覆盖：用户只能看到当前置信度，看不到「它是怎么走到今天的」——
    # 一次好评 +0.05、一次 A/B +0.2、一次差评 -0.15，这些轨迹算完就丢了。
    (
        7,
        """
        CREATE TABLE insight_events (
            id            TEXT PRIMARY KEY,
            insight_id    TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
            space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            event         TEXT NOT NULL,
            confidence_before REAL,
            confidence_after  REAL NOT NULL,
            status_before TEXT,
            status_after  TEXT NOT NULL,
            share         REAL,
            reason        TEXT,
            created_at    INTEGER NOT NULL
        );
        CREATE INDEX idx_insight_events_insight ON insight_events(insight_id, created_at);
        """,
    ),
    # v8：切片分「正文」与「文档概要」。跨全文汇总类问题（「这篇报告讲了哪些限制」）
    # 需要一条能代表整篇文档的可检索证据，而正文切片各自只有几百 token、谁也不代表全文。
    # 概要切片没有可定位的原文区间（char_start = char_end = 0），因此必须能与正文区分开，
    # 否则前端会拿它去原文里高亮出一个空区间。
    (
        8,
        """
        ALTER TABLE chunks ADD COLUMN kind TEXT NOT NULL DEFAULT 'body';
        """,
    ),
    # v9：领域大纲落库。大纲由模型的先验知识生成（要一把外部尺子量「缺了什么」），
    # 而此前每次打开专家度页都要重新生成一遍：一次几秒的模型调用、provider 不通时
    # 直接拿不到盲区。大纲本该是「建一次、偶尔手动更新」的东西。
    (
        9,
        """
        CREATE TABLE domain_outlines (
            id         TEXT PRIMARY KEY,
            space_id   TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
            domain     TEXT NOT NULL DEFAULT '',
            nodes      TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX idx_domain_outlines_space ON domain_outlines(space_id, created_at DESC);
        """,
    ),
    # v10：按提及次数取实体的索引。知识图谱默认只画提及最多的 300 个节点，而此前的
    # 取法是「翻页把实体全捞进内存再排序」，且封顶 5000 条——一个实体超过 5000 的
    # Space，排在 5000 名之后的高提及实体根本进不了候选，图上画出来的不是最重要的
    # 那些节点。有了这个索引，取 top-N 就是一条 SQL 的事，也不必再封顶。
    (
        10,
        """
        CREATE INDEX idx_entities_mention
            ON entities(space_id, mention_count DESC, id);
        """,
    ),
    # v11：清掉已删会话遗留的轨迹。traces 对 messages / conversations 都没有外键
    # （SQLite 不能给既有表补外键），所以删会话时轨迹原地留下：实测一个 Space 里
    # 31 条轨迹有 23 条已无对应消息，占 72% 的轨迹正文体量，里面存着提问原文与
    # 检索到的内容。更要紧的是 feedback 挂在轨迹上，已删会话的反馈还会继续被蒸馏。
    # 新增的删除路径已在 ConversationRepo.delete 里补上，这里只处理存量。
    # feedback 对 traces 有 ON DELETE CASCADE，会跟着一起清掉。
    (
        11,
        """
        DELETE FROM traces
        WHERE conversation_id NOT IN (SELECT id FROM conversations);
        """,
    ),
    # v12：记下模型把领域理解成了什么。空间名常是「长大助手」这类简称，模型会按字面
    # 猜（实测三次都理解成「儿童成长」），不把理解存下来，用户只能从一堆主题里倒推。
    (
        12,
        """
        ALTER TABLE domain_outlines ADD COLUMN interpretation TEXT;
        """,
    ),
]
