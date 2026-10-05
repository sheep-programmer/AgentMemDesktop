"""knowledge_cards / card_versions / entities / relations 表的数据访问。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from agentmem.errors import NotFoundError
from agentmem.store.base import Repository, dump_json, load_str_list, new_id
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    CardVersion,
    Entity,
    EntityCreate,
    EntityUpdate,
    KnowledgeCard,
    KnowledgeCardCreate,
    KnowledgeCardUpdate,
    Relation,
    RelationCreate,
    RelationUpdate,
)

#: 一条 ``IN (...)`` 里最多放多少个占位符。SQLite 的 ``SQLITE_MAX_VARIABLE_NUMBER``
#: 在老版本里只有 999，而图谱邻域动辄上千个实体，超了会直接报 "too many SQL variables"。
SQL_PARAM_CHUNK = 500


def _card(row: sqlite3.Row) -> KnowledgeCard:
    data = dict(row)
    data["aliases"] = load_str_list(row["aliases"])
    data["source_chunks"] = load_str_list(row["source_chunks"])
    return KnowledgeCard.model_validate(data)


def _card_version(row: sqlite3.Row) -> CardVersion:
    data = dict(row)
    data["aliases"] = load_str_list(row["aliases"])
    data["source_chunks"] = load_str_list(row["source_chunks"])
    return CardVersion.model_validate(data)


def _entity(row: sqlite3.Row) -> Entity:
    return Entity.model_validate(dict(row))


def _relation(row: sqlite3.Row) -> Relation:
    data = dict(row)
    data["source_chunks"] = load_str_list(row["source_chunks"])
    return Relation.model_validate(data)


class KnowledgeCardRepo(Repository):
    """``knowledge_cards`` 表。"""

    table = "knowledge_cards"
    has_updated_at = True

    async def create(self, data: KnowledgeCardCreate) -> KnowledgeCard:
        """新建知识卡片。"""
        now = now_ms()
        card = KnowledgeCard(id=new_id(), created_at=now, updated_at=now, **data.model_dump())
        await self.db.execute(
            "INSERT INTO knowledge_cards (id, space_id, kind, title, body, aliases,"
            " source_chunks, confidence, verified_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card.id,
                card.space_id,
                card.kind,
                card.title,
                card.body,
                dump_json(card.aliases),
                dump_json(card.source_chunks),
                card.confidence,
                card.verified_by,
                card.created_at,
                card.updated_at,
            ),
        )
        return card

    async def get(self, card_id: str) -> KnowledgeCard | None:
        """按 id 查询。"""
        row = await self.get_row(card_id)
        return _card(row) if row is not None else None

    async def require(self, card_id: str) -> KnowledgeCard:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        card = await self.get(card_id)
        if card is None:
            raise NotFoundError("知识卡片", card_id)
        return card

    async def list_by_space(
        self,
        space_id: str,
        *,
        kind: str | None = None,
        query: str | None = None,
        min_confidence: float | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[KnowledgeCard], int, str | None]:
        """列出知识卡片。"""
        clauses = ["space_id = ?"]
        params: list[object] = [space_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if query:
            clauses.append("(title LIKE ? OR body LIKE ? OR aliases LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        if min_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(min_confidence)
        where = " AND ".join(clauses)
        rows, next_cursor = await self._page(
            where=where, params=params, limit=limit, cursor=cursor, order_by="confidence DESC"
        )
        total = await self.db.fetchvalue(
            f"SELECT COUNT(*) FROM knowledge_cards WHERE {where}", params
        )
        return [_card(row) for row in rows], int(total or 0), next_cursor

    async def update(self, card_id: str, data: KnowledgeCardUpdate) -> KnowledgeCard:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        for key in ("aliases", "source_chunks"):
            if key in fields and fields[key] is not None:
                fields[key] = dump_json(fields[key])
        await self._update_row(card_id, fields)
        return await self.require(card_id)

    async def delete(self, card_id: str) -> bool:
        """删除。"""
        return await self.delete_row(card_id)

    async def count(self, space_id: str | None = None) -> int:
        """卡片数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM knowledge_cards")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM knowledge_cards WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)


class CardVersionRepo(Repository):
    """``card_versions`` 表：知识卡片被取代后的旧版本快照。"""

    table = "card_versions"

    async def add(
        self,
        card: KnowledgeCard,
        *,
        version: int,
        valid_from: int,
        valid_to: int,
    ) -> CardVersion:
        """把一张卡片的当前内容留档为第 ``version`` 版。"""
        record = CardVersion(
            id=new_id(),
            card_id=card.id,
            space_id=card.space_id,
            version=version,
            kind=card.kind,
            title=card.title,
            body=card.body,
            aliases=list(card.aliases),
            source_chunks=list(card.source_chunks),
            confidence=card.confidence,
            verified_by=card.verified_by,
            valid_from=valid_from,
            valid_to=valid_to,
            created_at=now_ms(),
        )
        await self.db.execute(
            "INSERT INTO card_versions (id, card_id, space_id, version, kind, title, body,"
            " aliases, source_chunks, confidence, verified_by, valid_from, valid_to, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.card_id,
                record.space_id,
                record.version,
                record.kind,
                record.title,
                record.body,
                dump_json(record.aliases),
                dump_json(record.source_chunks),
                record.confidence,
                record.verified_by,
                record.valid_from,
                record.valid_to,
                record.created_at,
            ),
        )
        return record

    async def list_by_card(self, card_id: str) -> list[CardVersion]:
        """某张卡片的全部历史版本，新的在前。"""
        rows = await self.db.fetchall(
            "SELECT * FROM card_versions WHERE card_id = ? ORDER BY version DESC", (card_id,)
        )
        return [_card_version(row) for row in rows]

    async def next_version(self, card_id: str) -> int:
        """下一个版本号：当前版本就是历史条数 + 1。"""
        value = await self.db.fetchvalue(
            "SELECT MAX(version) AS v FROM card_versions WHERE card_id = ?", (card_id,)
        )
        return int(value or 0) + 1


class EntityRepo(Repository):
    """``entities`` 表。"""

    table = "entities"

    async def create(self, data: EntityCreate) -> Entity:
        """新建实体；同 (space_id, name, type) 已存在时返回既有实体。"""
        existing = await self.find(data.space_id, data.name, data.type)
        if existing is not None:
            return existing
        entity = Entity(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO entities (id, space_id, name, type, summary, card_id, mention_count,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity.id,
                entity.space_id,
                entity.name,
                entity.type,
                entity.summary,
                entity.card_id,
                entity.mention_count,
                entity.created_at,
            ),
        )
        return entity

    async def find(self, space_id: str, name: str, type_: str) -> Entity | None:
        """按唯一键查询。"""
        row = await self.db.fetchone(
            "SELECT * FROM entities WHERE space_id = ? AND name = ? AND type = ?",
            (space_id, name, type_),
        )
        return _entity(row) if row is not None else None

    async def get(self, entity_id: str) -> Entity | None:
        """按 id 查询。"""
        row = await self.get_row(entity_id)
        return _entity(row) if row is not None else None

    async def require(self, entity_id: str) -> Entity:
        """按 id 查询，不存在则抛 :class:`NotFoundError`。"""
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFoundError("实体", entity_id)
        return entity

    async def list_by_space(
        self, space_id: str, *, limit: int = 300, cursor: str | None = None
    ) -> tuple[list[Entity], str | None]:
        """列出实体。"""
        rows, next_cursor = await self._page(
            where="space_id = ?", params=[space_id], limit=limit, cursor=cursor
        )
        return [_entity(row) for row in rows], next_cursor

    async def find_by_name(self, space_id: str, name: str) -> Entity | None:
        """按名称查询（忽略大小写与首尾空白），同名多类型时取提及最多的那个。

        图谱的 ``center`` 参数允许用户直接写实体名，而名称在 ``entities`` 里
        只在 ``(space_id, name, type)`` 上唯一：同一个名字可能既是「工具」又是
        「协议」。取提及次数最高的那个，与图上画得最大的那个节点一致。
        """
        row = await self.db.fetchone(
            "SELECT * FROM entities WHERE space_id = ? AND lower(trim(name)) = ?"
            " ORDER BY mention_count DESC, id LIMIT 1",
            (space_id, name.strip().lower()),
        )
        return _entity(row) if row is not None else None

    async def top_by_mention(self, space_id: str, *, limit: int) -> list[Entity]:
        """提及次数最高的 ``limit`` 个实体（并列时按 id 定序，结果可复现）。

        知识图谱画的就是这一批。交给 SQL 而不是「全捞进内存再排序」：后者的代价
        随 Space 大小线性上涨，而且一旦为了内存给装载量封顶，排在封顶之外的
        高提及实体就再也进不了图。
        """
        if limit <= 0:
            return []
        rows = await self.db.fetchall(
            "SELECT * FROM entities WHERE space_id = ? ORDER BY mention_count DESC, id LIMIT ?",
            (space_id, limit),
        )
        return [_entity(row) for row in rows]

    async def get_many(self, entity_ids: Sequence[str]) -> list[Entity]:
        """按 id 批量取实体，返回顺序与提及次数降序一致；不存在的 id 直接跳过。"""
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return []
        entities: list[Entity] = []
        for start in range(0, len(ids), SQL_PARAM_CHUNK):
            batch = ids[start : start + SQL_PARAM_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db.fetchall(
                f"SELECT * FROM entities WHERE id IN ({placeholders})", batch
            )
            entities.extend(_entity(row) for row in rows)
        entities.sort(key=lambda entity: (-entity.mention_count, entity.id))
        return entities

    async def update(self, entity_id: str, data: EntityUpdate) -> Entity:
        """部分更新。"""
        affected = await self._update_row(entity_id, data.model_dump(exclude_unset=True))
        if affected == 0:
            raise NotFoundError("实体", entity_id)
        return await self.require(entity_id)

    async def delete(self, entity_id: str) -> bool:
        """删除。"""
        return await self.delete_row(entity_id)

    async def count(self, space_id: str | None = None) -> int:
        """实体数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM entities")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM entities WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)

    # -- 提及明细 ---------------------------------------------------------

    async def record_mentions(
        self, *, space_id: str, document_id: str, entity_ids: Sequence[str]
    ) -> int:
        """记下「这篇文档提到了这些实体」，返回新增的提及条数。

        按 ``(entity, document, chunk)`` 去重：同一个文档重复抽取（重新解析、
        重建索引）不会再累加——这正是此前 ``mention_count`` 虚高的原因。
        """
        if not entity_ids:
            return 0
        now = now_ms()
        rows = [
            (new_id(), entity_id, space_id, document_id, "", now)
            for entity_id in dict.fromkeys(entity_ids)
        ]
        async with self.db.transaction() as tx:
            for row in rows:
                await tx.execute(
                    "INSERT OR IGNORE INTO entity_mentions"
                    " (id, entity_id, space_id, document_id, chunk_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
        return len(rows)

    async def refresh_mention_counts(self, entity_ids: Sequence[str]) -> None:
        """按提及明细重算 ``mention_count``。

        计数只从明细推出来，不做增量累加——累加的账一旦错一次就再也回不来。
        """
        if not entity_ids:
            return
        await self.db.executemany(
            "UPDATE entities SET mention_count ="
            " (SELECT COUNT(*) FROM entity_mentions WHERE entity_id = entities.id)"
            " WHERE id = ?",
            [(entity_id,) for entity_id in dict.fromkeys(entity_ids)],
        )

    async def entity_ids_for_document(self, document_id: str) -> list[str]:
        """这篇文档提到过哪些实体（删除文档前取，用来重算它们的计数）。"""
        rows = await self.db.fetchall(
            "SELECT DISTINCT entity_id FROM entity_mentions WHERE document_id = ?",
            (document_id,),
        )
        return [str(row["entity_id"]) for row in rows]


class RelationRepo(Repository):
    """``relations`` 表。"""

    table = "relations"

    async def create(self, data: RelationCreate) -> Relation:
        """新建关系。"""
        relation = Relation(id=new_id(), created_at=now_ms(), **data.model_dump())
        await self.db.execute(
            "INSERT INTO relations (id, space_id, src_id, dst_id, predicate, weight,"
            " source_chunks, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                relation.id,
                relation.space_id,
                relation.src_id,
                relation.dst_id,
                relation.predicate,
                relation.weight,
                dump_json(relation.source_chunks),
                relation.created_at,
            ),
        )
        return relation

    async def get(self, relation_id: str) -> Relation | None:
        """按 id 查询。"""
        row = await self.get_row(relation_id)
        return _relation(row) if row is not None else None

    async def list_by_space(self, space_id: str) -> list[Relation]:
        """列出某 Space 的全部关系。"""
        rows = await self.db.fetchall("SELECT * FROM relations WHERE space_id = ?", (space_id,))
        return [_relation(row) for row in rows]

    async def neighbors(self, space_id: str, entity_ids: Sequence[str]) -> set[str]:
        """这些实体在图上直接相连的实体 id（不含它们自己）。

        邻居展开一层查一次，而不是把整个 Space 的边装进内存建邻接表：BFS 的层数是
        个位数，而边数随 Space 一直涨。
        """
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return set()
        found: set[str] = set()
        for start in range(0, len(ids), SQL_PARAM_CHUNK):
            batch = ids[start : start + SQL_PARAM_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db.fetchall(
                f"SELECT src_id, dst_id FROM relations WHERE space_id = ?"
                f" AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders}))",
                [space_id, *batch, *batch],
            )
            for row in rows:
                found.add(str(row["src_id"]))
                found.add(str(row["dst_id"]))
        return found - set(ids)

    async def between(self, space_id: str, entity_ids: Sequence[str]) -> list[Relation]:
        """两端都在给定集合里的关系。

        图上只画留下来的那些节点，边自然也只要这些——按节点去查，比「全量装载再过滤」
        少读的正是被裁掉的那一大半。
        """
        ids = list(dict.fromkeys(entity_ids))
        if not ids:
            return []
        wanted = set(ids)
        relations: dict[str, Relation] = {}
        for start in range(0, len(ids), SQL_PARAM_CHUNK):
            batch = ids[start : start + SQL_PARAM_CHUNK]
            placeholders = ", ".join("?" for _ in batch)
            rows = await self.db.fetchall(
                f"SELECT * FROM relations WHERE space_id = ? AND src_id IN ({placeholders})",
                [space_id, *batch],
            )
            for row in rows:
                relation = _relation(row)
                # 分批查的是「起点在这一批」，终点可能落在别的批次里，这里再筛一次
                if relation.dst_id in wanted:
                    relations[relation.id] = relation
        return list(relations.values())

    async def chunks_for_entities(
        self, space_id: str, entity_ids: Sequence[str], *, limit: int = 40
    ) -> list[str]:
        """这些实体在图上相连的关系所引用的切片 id（去重，按关系权重排序）。

        图谱扩展的原料：关系在抽取时记下了它的 ``source_chunks``，
        那些切片就是「图上跟这个实体相邻」的证据所在。
        """
        if not entity_ids:
            return []
        placeholders = ", ".join("?" for _ in entity_ids)
        rows = await self.db.fetchall(
            f"SELECT source_chunks, weight FROM relations WHERE space_id = ?"
            f" AND (src_id IN ({placeholders}) OR dst_id IN ({placeholders}))"
            f" ORDER BY weight DESC",
            [space_id, *entity_ids, *entity_ids],
        )
        seen: dict[str, None] = {}
        for row in rows:
            for chunk_id in load_str_list(row["source_chunks"]):
                seen.setdefault(chunk_id, None)
                if len(seen) >= limit:
                    return list(seen)
        return list(seen)

    async def list_by_entity(self, space_id: str, entity_id: str) -> list[Relation]:
        """列出与某实体相连的关系。"""
        rows = await self.db.fetchall(
            "SELECT * FROM relations WHERE space_id = ? AND (src_id = ? OR dst_id = ?)",
            (space_id, entity_id, entity_id),
        )
        return [_relation(row) for row in rows]

    async def update(self, relation_id: str, data: RelationUpdate) -> Relation:
        """部分更新。"""
        fields = data.model_dump(exclude_unset=True)
        if "source_chunks" in fields and fields["source_chunks"] is not None:
            fields["source_chunks"] = dump_json(fields["source_chunks"])
        affected = await self._update_row(relation_id, fields, touch=False)
        if affected == 0:
            raise NotFoundError("关系", relation_id)
        relation = await self.get(relation_id)
        if relation is None:
            raise NotFoundError("关系", relation_id)
        return relation

    async def delete(self, relation_id: str) -> bool:
        """删除。"""
        return await self.delete_row(relation_id)

    async def count(self, space_id: str | None = None) -> int:
        """关系数。"""
        if space_id is None:
            value = await self.db.fetchvalue("SELECT COUNT(*) FROM relations")
        else:
            value = await self.db.fetchvalue(
                "SELECT COUNT(*) FROM relations WHERE space_id = ?", (space_id,)
            )
        return int(value or 0)
