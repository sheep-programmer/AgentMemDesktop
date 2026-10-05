"""存储门面：一个 SQLite 文件 + 挂在它上面的全部 Repository。

每个 Space 一个 ``meta.db``；全局库 ``data/agentmem.db`` 保存 Space 注册表与跨
Space 的记录（用量、global 经验）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from agentmem.store.fts import FtsIndex
from agentmem.store.operations import OperationGate
from agentmem.store.repos import (
    CardVersionRepo,
    ChunkRepo,
    ConsistencyRepo,
    ConversationRepo,
    DocumentRepo,
    DomainOutlineRepo,
    EntityRepo,
    EvalItemRepo,
    EvalRunRepo,
    EvolutionRunRepo,
    ExpertiseSnapshotRepo,
    FeedbackRepo,
    InsightEventRepo,
    InsightRepo,
    KnowledgeCardRepo,
    MessageRepo,
    RelationRepo,
    SpaceRepo,
    TraceRepo,
    UsageRepo,
)
from agentmem.store.sqlite import SQLiteDatabase
from agentmem.store.vectors import VectorStore


class Database:
    """一个 SQLite 库及其上的 Repository 集合。

    Args:
        path: SQLite 文件路径。
        vectors_dir: 向量库目录；为空则不启用向量存储。
        space_id: 该库所属的 Space；全局库为 ``None``。
    """

    def __init__(
        self,
        path: Path,
        vectors_dir: Path | None = None,
        space_id: str | None = None,
        *,
        operations: OperationGate | None = None,
    ) -> None:
        self.space_id = space_id
        self.operations = operations or OperationGate()
        self.sqlite = SQLiteDatabase(Path(path))
        self.vectors: VectorStore | None = (
            VectorStore(Path(vectors_dir)) if vectors_dir is not None else None
        )
        self.fts = FtsIndex(self.sqlite)
        self.spaces = SpaceRepo(self.sqlite)
        self.documents = DocumentRepo(self.sqlite)
        self.chunks = ChunkRepo(self.sqlite, self.fts)
        self.cards = KnowledgeCardRepo(self.sqlite)
        self.card_versions = CardVersionRepo(self.sqlite)
        self.entities = EntityRepo(self.sqlite)
        self.relations = RelationRepo(self.sqlite)
        self.insights = InsightRepo(self.sqlite)
        self.insight_events = InsightEventRepo(self.sqlite)
        self.conversations = ConversationRepo(self.sqlite)
        self.messages = MessageRepo(self.sqlite)
        self.traces = TraceRepo(self.sqlite)
        self.feedback = FeedbackRepo(self.sqlite)
        self.eval_items = EvalItemRepo(self.sqlite)
        self.eval_runs = EvalRunRepo(self.sqlite)
        self.consistency = ConsistencyRepo(self.sqlite)
        self.outlines = DomainOutlineRepo(self.sqlite)
        self.evolution = EvolutionRunRepo(self.sqlite)
        self.expertise = ExpertiseSnapshotRepo(self.sqlite)
        self.usage = UsageRepo(self.sqlite)

    @property
    def path(self) -> Path:
        """SQLite 文件路径。"""
        return self.sqlite.path

    async def open(self) -> None:
        """建立连接并执行迁移。"""
        await self.sqlite.connect()
        if self.vectors is not None:
            await self.vectors.open()

    async def close(self) -> None:
        """关闭连接。"""
        if self.vectors is not None:
            await self.vectors.close()
        await self.sqlite.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def require_vectors(self) -> VectorStore:
        """取向量库，未配置时抛错。"""
        if self.vectors is None:
            raise RuntimeError("该数据库未配置向量库目录")
        return self.vectors
