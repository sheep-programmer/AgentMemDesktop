"""切片没了之后，把卡片、关系里指向它们的来源摘掉。

卡片和关系只通过 ``source_chunks``（JSON 数组）记来源，没有 ``document_id``。
切片一删——删文档、重新解析都会删——这些引用就悬空了：此前删文档之后，从它抽出来的
卡片照样留在卡片列表里、向量照样被检索召回，关系也照样画在图谱上，来源点进去却是空的。

两种场景要的处理不一样：

- **删文档**（``drop_orphans=True``）：来源全部没了的卡片 / 关系一并删掉。
- **重新解析**（``drop_orphans=False``）：只摘掉失效的 id，不删卡片。重切之后马上会重新
  抽取，抽出来的同名卡片会合并回原卡片；这时删掉就等于丢了用户的校订与版本历史。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from agentmem.store import Database
from agentmem.types import KnowledgeCardUpdate, RelationUpdate

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PruneResult:
    """一次清理动了多少东西。"""

    cards_deleted: int = 0
    cards_trimmed: int = 0
    relations_deleted: int = 0
    relations_trimmed: int = 0


async def _rows_referencing(
    database: Database, table: str, chunk_ids: Sequence[str]
) -> list[tuple[str, list[str]]]:
    """取 ``source_chunks`` 里含有这些切片之一的行：``[(id, 原来源列表)]``。"""
    placeholders = ", ".join("?" for _ in chunk_ids)
    rows = await database.sqlite.fetchall(
        f"SELECT DISTINCT t.id, t.source_chunks FROM {table} t, json_each(t.source_chunks) j"
        f" WHERE j.value IN ({placeholders})",
        tuple(chunk_ids),
    )
    return [(row["id"], list(json.loads(row["source_chunks"] or "[]"))) for row in rows]


async def prune_chunk_references(
    database: Database, chunk_ids: Sequence[str], *, drop_orphans: bool
) -> PruneResult:
    """把卡片与关系里指向 ``chunk_ids`` 的来源摘掉；``drop_orphans`` 时删掉来源清空的行。"""
    if not chunk_ids:
        return PruneResult()
    gone = set(chunk_ids)

    cards_deleted = cards_trimmed = 0
    orphan_cards: list[str] = []
    for card_id, sources in await _rows_referencing(database, "knowledge_cards", chunk_ids):
        remaining = [source for source in sources if source not in gone]
        if remaining or not drop_orphans:
            await database.cards.update(card_id, KnowledgeCardUpdate(source_chunks=remaining))
            cards_trimmed += 1
        else:
            orphan_cards.append(card_id)
    if orphan_cards:
        # 向量一起删：只删行的话，检索还会召回一条回不了表的卡片
        if database.vectors is not None:
            await database.vectors.delete("cards_vec", orphan_cards)
        for card_id in orphan_cards:
            await database.cards.delete(card_id)
        cards_deleted = len(orphan_cards)

    relations_deleted = relations_trimmed = 0
    for relation_id, sources in await _rows_referencing(database, "relations", chunk_ids):
        remaining = [source for source in sources if source not in gone]
        if remaining or not drop_orphans:
            await database.relations.update(relation_id, RelationUpdate(source_chunks=remaining))
            relations_trimmed += 1
        else:
            await database.relations.delete(relation_id)
            relations_deleted += 1

    result = PruneResult(
        cards_deleted=cards_deleted,
        cards_trimmed=cards_trimmed,
        relations_deleted=relations_deleted,
        relations_trimmed=relations_trimmed,
    )
    if cards_deleted or cards_trimmed or relations_deleted or relations_trimmed:
        logger.info("chunk_references_pruned", drop_orphans=drop_orphans, **result.__dict__)
    return result


async def drop_unreferenced_entities(database: Database, entity_ids: Sequence[str]) -> int:
    """删掉这些实体里已经没有任何提及、也不在任何关系里的——它们在图上只是一个孤点。"""
    if not entity_ids:
        return 0
    placeholders = ", ".join("?" for _ in entity_ids)
    rows = await database.sqlite.fetchall(
        f"SELECT e.id FROM entities e WHERE e.id IN ({placeholders})"
        " AND COALESCE(e.mention_count, 0) = 0"
        " AND NOT EXISTS (SELECT 1 FROM relations r WHERE r.src_id = e.id OR r.dst_id = e.id)",
        tuple(entity_ids),
    )
    for row in rows:
        await database.entities.delete(row["id"])
    return len(rows)
