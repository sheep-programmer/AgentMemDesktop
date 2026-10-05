"""数据体检：找出（并可选清掉）老版本删文档时漏下的派生数据。

此前删除文档只删文档行和切片，派生数据全留着（已修，见 ``Runtime.delete_document``）。
修好之后新删的文档不再留尾巴，但老库里还躺着：

- 来源切片全部已不存在的知识卡片——还在卡片列表里、向量还会被检索召回；
- 来源部分失效的卡片 / 关系——点进来源是空的；
- 提及数为 0、又不在任何关系里的实体——图谱上的孤点；
- ``raw/`` 下没有任何文档引用的原始文件、``cache/`` 下已删文档的解析缓存——白占磁盘；
- 向量表里主键已经回不了表的向量——召回后在拼上下文时被丢掉，白白挤掉名额。

分成「诊断」和「修复」两步：诊断只读，``agentmem doctor`` 默认只做诊断；修复复用
:func:`~agentmem.memory.prune.prune_chunk_references` 与
:func:`~agentmem.memory.prune.drop_unreferenced_entities`——和删文档走同一条路，
卡片连同向量一起删，不会出现「行删了、向量还在」的新不一致。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentmem.memory.prune import drop_unreferenced_entities, prune_chunk_references
from agentmem.security import locate_raw_file
from agentmem.store import Database
from agentmem.types import VectorTableName

#: 最近这么久内写入的 raw 文件不算孤儿。上传是「先落盘、再登记文档」，
#: 后端开着的时候跑体检，刚落盘还没登记的文件会被误判——留一段宽限期。
RAW_GRACE_SECONDS = 600

#: 向量表 → 回表用的 SQLite 表
_VECTOR_OWNERS: dict[VectorTableName, str] = {
    "chunks_vec": "chunks",
    "cards_vec": "knowledge_cards",
    "insights_vec": "insights",
}

PARSE_CACHE_SUFFIX = ".parse.json"


@dataclass
class DoctorReport:
    """一个 Space 的体检结果（只读得出，不改任何数据）。"""

    space_id: str
    missing_chunk_ids: list[str] = field(default_factory=list)
    orphan_cards: list[str] = field(default_factory=list)
    trimmed_cards: list[str] = field(default_factory=list)
    orphan_relations: list[str] = field(default_factory=list)
    trimmed_relations: list[str] = field(default_factory=list)
    orphan_entities: list[str] = field(default_factory=list)
    orphan_vectors: dict[VectorTableName, list[str]] = field(default_factory=dict)
    orphan_raw_files: list[Path] = field(default_factory=list)
    orphan_parse_caches: list[Path] = field(default_factory=list)

    def findings(self) -> list[tuple[str, int]]:
        """``[(问题描述, 数量)]``，只列数量不为 0 的。"""
        items = [
            ("来源切片全部已不存在的知识卡片（将删除，连同向量）", len(self.orphan_cards)),
            ("来源切片部分失效的知识卡片（将摘掉失效来源）", len(self.trimmed_cards)),
            ("来源切片全部已不存在的关系（将删除）", len(self.orphan_relations)),
            ("来源切片部分失效的关系（将摘掉失效来源）", len(self.trimmed_relations)),
            ("提及数为 0 且不在任何关系里的实体（将删除）", len(self.orphan_entities)),
            *(
                (f"{table} 里回不了表的向量（将删除）", len(ids))
                for table, ids in self.orphan_vectors.items()
            ),
            ("raw/ 下没有文档引用的原始文件（将删除）", len(self.orphan_raw_files)),
            ("已删文档的解析缓存（将删除）", len(self.orphan_parse_caches)),
        ]
        return [(label, count) for label, count in items if count]

    @property
    def is_clean(self) -> bool:
        """没有任何问题。"""
        return not self.findings()


@dataclass(frozen=True)
class RepairResult:
    """一次修复实际动了多少东西。"""

    cards_deleted: int = 0
    cards_trimmed: int = 0
    relations_deleted: int = 0
    relations_trimmed: int = 0
    entities_deleted: int = 0
    vectors_deleted: int = 0
    raw_files_deleted: int = 0
    parse_caches_deleted: int = 0


async def _ids(database: Database, table: str) -> set[str]:
    rows = await database.sqlite.fetchall(f"SELECT id FROM {table}")
    return {str(row["id"]) for row in rows}


async def _sources(database: Database, sql: str) -> list[tuple[str, list[str]]]:
    rows = await database.sqlite.fetchall(sql)
    return [(str(row["id"]), list(json.loads(row["source_chunks"] or "[]"))) for row in rows]


def _classify(
    rows: list[tuple[str, list[str]]], chunk_ids: set[str]
) -> tuple[list[str], list[str], set[str]]:
    """按来源失效情况分成 ``(全部失效, 部分失效, 失效的切片 id)``。

    来源本来就是空的不算问题：用户手工建的卡片没有来源；重新解析时卡片的来源会先被
    摘空、等重新抽取再合并回来——这两种都不能当孤儿删。
    """
    orphans: list[str] = []
    trimmed: list[str] = []
    missing: set[str] = set()
    for row_id, sources in rows:
        gone = [source for source in sources if source not in chunk_ids]
        if not gone:
            continue
        missing.update(gone)
        (orphans if len(gone) == len(sources) else trimmed).append(row_id)
    return orphans, trimmed, missing


def _referenced_raw_files(raw_dir: Path, documents: list[tuple[str, str | None]]) -> set[Path]:
    """被文档引用着的 raw 文件：和删文档、重新解析一样用 :func:`locate_raw_file` 回找。"""
    found: set[Path] = set()
    for _document_id, stored in documents:
        path = locate_raw_file(raw_dir, stored)
        if path is not None:
            found.add(path.resolve())
    return found


def _orphan_raw_files(
    raw_dir: Path, documents: list[tuple[str, str | None]], now: float
) -> list[Path]:
    if not raw_dir.is_dir():
        return []
    referenced = _referenced_raw_files(raw_dir, documents)
    # 粘贴文本的 raw 文件名以文档 id 开头：库里记的路径坏了也认得出来，宁可漏删不可误删
    prefixes = tuple(f"{document_id}-" for document_id, _ in documents)
    orphans: list[Path] = []
    for path in sorted(raw_dir.iterdir()):
        # .DS_Store 之类的隐藏文件不是我们写的，别碰
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.resolve() in referenced or (prefixes and path.name.startswith(prefixes)):
            continue
        if now - path.stat().st_mtime < RAW_GRACE_SECONDS:
            continue
        orphans.append(path)
    return orphans


def _orphan_parse_caches(cache_dir: Path, document_ids: set[str]) -> list[Path]:
    if not cache_dir.is_dir():
        return []
    return [
        path
        for path in sorted(cache_dir.glob(f"*{PARSE_CACHE_SUFFIX}"))
        if path.is_file() and path.name.removesuffix(PARSE_CACHE_SUFFIX) not in document_ids
    ]


async def diagnose(
    database: Database,
    *,
    space_id: str,
    raw_dir: Path,
    cache_dir: Path,
    now: float | None = None,
) -> DoctorReport:
    """体检一个 Space，只读。

    Args:
        database: 该 Space 的数据库。
        space_id: Space id（只用于报告）。
        raw_dir: 该 Space 的 ``raw/`` 目录。
        cache_dir: 该 Space 的解析缓存目录（``cache/<space_id>``）。
        now: 当前时间戳（秒），测试用；缺省取系统时间。
    """
    report = DoctorReport(space_id=space_id)
    chunk_ids = await _ids(database, "chunks")

    cards = await _sources(database, "SELECT id, source_chunks FROM knowledge_cards")
    report.orphan_cards, report.trimmed_cards, missing_from_cards = _classify(cards, chunk_ids)

    relation_rows = await database.sqlite.fetchall(
        "SELECT id, src_id, dst_id, source_chunks FROM relations"
    )
    relations = [
        (str(row["id"]), list(json.loads(row["source_chunks"] or "[]"))) for row in relation_rows
    ]
    report.orphan_relations, report.trimmed_relations, missing_from_relations = _classify(
        relations, chunk_ids
    )
    report.missing_chunk_ids = sorted(missing_from_cards | missing_from_relations)

    # 修复时先删关系、再删实体，所以这里要按「清理之后还剩下的关系」来算孤点，
    # 否则 dry-run 报的数会比 --apply 实际删的少
    dropped_relations = set(report.orphan_relations)
    linked: set[str] = set()
    for row in relation_rows:
        if str(row["id"]) not in dropped_relations:
            linked.update((str(row["src_id"]), str(row["dst_id"])))
    # 只认 mention_count 列，不按 entity_mentions 明细重算：示例数据和提及明细上线之前
    # 抽出的实体都只有计数、没有明细，按明细算会把它们全当孤点删掉
    zero_mentions = await database.sqlite.fetchall(
        "SELECT id FROM entities WHERE COALESCE(mention_count, 0) = 0"
    )
    report.orphan_entities = [
        str(row["id"]) for row in zero_mentions if str(row["id"]) not in linked
    ]

    if database.vectors is not None:
        for table, owner in _VECTOR_OWNERS.items():
            alive = chunk_ids if owner == "chunks" else await _ids(database, owner)
            stale = [item for item in await database.vectors.list_ids(table) if item not in alive]
            if stale:
                report.orphan_vectors[table] = stale

    document_rows = await database.sqlite.fetchall("SELECT id, source_uri, meta FROM documents")
    documents: list[tuple[str, str | None]] = []
    for row in document_rows:
        meta = json.loads(row["meta"] or "{}")
        raw_path = meta.get("raw_path") if isinstance(meta, dict) else None
        documents.append((str(row["id"]), raw_path or row["source_uri"]))
    report.orphan_raw_files = await asyncio.to_thread(
        _orphan_raw_files, raw_dir, documents, time.time() if now is None else now
    )
    report.orphan_parse_caches = await asyncio.to_thread(
        _orphan_parse_caches, cache_dir, {document_id for document_id, _ in documents}
    )
    return report


async def repair(database: Database, report: DoctorReport) -> RepairResult:
    """按体检结果清理。卡片、关系、实体都重新从库里判定，报告只提供要处理的范围。"""
    pruned = await prune_chunk_references(database, report.missing_chunk_ids, drop_orphans=True)

    # 删完关系再判定实体：drop_unreferenced_entities 会重新检查是否还挂在关系上
    candidates = await database.sqlite.fetchall(
        "SELECT id FROM entities WHERE COALESCE(mention_count, 0) = 0"
    )
    entities_deleted = await drop_unreferenced_entities(
        database, [str(row["id"]) for row in candidates]
    )

    vectors_deleted = 0
    if database.vectors is not None:
        for table, ids in report.orphan_vectors.items():
            vectors_deleted += await database.vectors.delete(table, ids)

    def unlink_all(paths: list[Path]) -> int:
        count = 0
        for path in paths:
            if path.is_file():
                path.unlink()
                count += 1
        return count

    raw_deleted = await asyncio.to_thread(unlink_all, report.orphan_raw_files)
    caches_deleted = await asyncio.to_thread(unlink_all, report.orphan_parse_caches)
    return RepairResult(
        cards_deleted=pruned.cards_deleted,
        cards_trimmed=pruned.cards_trimmed,
        relations_deleted=pruned.relations_deleted,
        relations_trimmed=pruned.relations_trimmed,
        entities_deleted=entities_deleted,
        vectors_deleted=vectors_deleted,
        raw_files_deleted=raw_deleted,
        parse_caches_deleted=caches_deleted,
    )
