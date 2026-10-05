"""``agentmem doctor``：体检并清理老版本删文档时漏下的派生数据。

老版本删文档不清派生数据（已修），老库里留着孤儿卡片、孤立实体、没人引用的原始文件和
回不了表的向量。这里手工铺一份「老库形状」的数据：一部分是真正的孤儿，一部分是
看起来像孤儿、其实必须留下的正常数据（手工卡片、示例实体、刚上传还没登记的文件）。

``doctor`` 内部用 ``asyncio.run``，所以用例是同步函数，铺数据和查结果也各自 ``asyncio.run``。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentmem.config import Settings
from agentmem.space.doctor import RAW_GRACE_SECONDS
from agentmem.space.manager import SpaceManager
from agentmem.types import (
    ChunkCreate,
    DocumentCreate,
    DocumentMeta,
    EntityCreate,
    KnowledgeCardCreate,
    RelationCreate,
    SpaceCreate,
    VectorRecord,
)
from apps.api.cli import build_parser, doctor

DIM = 4


@dataclass
class Seeded:
    """铺好的数据里各条的 id / 路径，供断言用。"""

    space_id: str
    chunk_id: str
    card_alive: str
    card_manual: str
    card_partial: str
    card_orphan: str
    relation_alive: str
    relation_dead: str
    entity_linked: str
    entity_demo: str
    entity_orphan: str
    entity_only_in_dead_relation: str
    raw_referenced: Path
    raw_paste: Path
    raw_orphan: Path
    raw_recent: Path
    raw_hidden: Path
    cache_alive: Path
    cache_orphan: Path


def _vector(record_id: str, space_id: str, document_id: str | None = None) -> VectorRecord:
    return VectorRecord(
        id=record_id,
        space_id=space_id,
        vector=[0.1] * DIM,
        embedding_model="mock-embed",
        document_id=document_id,
    )


async def _seed(settings: Settings, *, dirty: bool = True) -> Seeded:
    manager = SpaceManager(settings)
    try:
        space = await manager.create_space(SpaceCreate(name="体检", domain="测试"))
        database = await manager.space_db(space.id)
        raw_dir = manager.raw_dir(space.id)
        raw_dir.mkdir(parents=True, exist_ok=True)

        # 正常文档：上传形状（raw 文件名是内容摘要）+ 粘贴形状（raw 文件名以文档 id 开头，
        # 但库里记的路径是坏的——换过启动目录或从备份还原后就是这样）
        raw_referenced = raw_dir / "abcdef0123456789-手册.pdf"
        raw_referenced.write_bytes(b"%PDF")
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="手册",
                source_type="file",
                source_uri=str(raw_referenced),
                sha256="sha-upload",
                status="ready",
            )
        )
        pasted = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="粘贴",
                source_type="paste",
                sha256="sha-paste",
                status="ready",
                meta=DocumentMeta(raw_path="/elsewhere/not-here.md"),
            )
        )
        raw_paste = raw_dir / f"{pasted.id}-paste.md"
        raw_paste.write_text("粘贴原文", encoding="utf-8")

        [chunk] = await database.chunks.create_many(
            [ChunkCreate(space_id=space.id, document_id=document.id, ordinal=0, content="正文")]
        )

        def card(title: str, sources: list[str]) -> KnowledgeCardCreate:
            return KnowledgeCardCreate(
                space_id=space.id, kind="fact", title=title, body=title, source_chunks=sources
            )

        card_alive = await database.cards.create(card("来源完好", [chunk.id]))
        # 手工建的卡片没有来源——不能当孤儿
        card_manual = await database.cards.create(card("手工卡片", []))
        card_partial = await database.cards.create(
            card("来源半失效", [chunk.id, "gone-chunk-2"] if dirty else [chunk.id])
        )
        card_orphan = await database.cards.create(
            card("来源全失效", ["gone-chunk-1"] if dirty else [chunk.id])
        )

        def entity(name: str, mentions: int) -> EntityCreate:
            return EntityCreate(space_id=space.id, name=name, type="概念", mention_count=mentions)

        entity_linked = await database.entities.create(entity("挂在关系上", 0))
        entity_partner = await database.entities.create(entity("关系另一端", 2))
        # 示例数据 / 提及明细上线前的实体：只有计数没有明细，不能按明细判成孤点
        entity_demo = await database.entities.create(entity("只有计数", 3))
        entity_orphan = await database.entities.create(
            entity("孤点" if dirty else "有提及", 0 if dirty else 1)
        )
        entity_dead_end = await database.entities.create(entity("只在失效关系里", 0))

        relation_alive = await database.relations.create(
            RelationCreate(
                space_id=space.id,
                src_id=entity_linked.id,
                dst_id=entity_partner.id,
                predicate="依赖",
                source_chunks=[chunk.id],
            )
        )
        relation_dead = await database.relations.create(
            RelationCreate(
                space_id=space.id,
                src_id=entity_dead_end.id,
                dst_id=entity_partner.id,
                predicate="用于",
                source_chunks=["gone-chunk-1"] if dirty else [chunk.id],
            )
        )

        assert database.vectors is not None
        chunk_vectors = [_vector(chunk.id, space.id, document.id)]
        card_vectors = [_vector(card_alive.id, space.id), _vector(card_orphan.id, space.id)]
        if dirty:
            chunk_vectors.append(_vector("gone-chunk-1", space.id, "deleted-doc"))
            card_vectors.append(_vector("ghost-card", space.id))
        await database.vectors.upsert("chunks_vec", chunk_vectors)
        await database.vectors.upsert("cards_vec", card_vectors)

        old = time.time() - RAW_GRACE_SECONDS - 60
        raw_orphan = raw_dir / "0000000000000000-已删文档.pdf"
        raw_recent = raw_dir / "1111111111111111-刚上传.pdf"
        raw_hidden = raw_dir / ".DS_Store"
        if dirty:
            raw_orphan.write_bytes(b"old")
            os.utime(raw_orphan, (old, old))
            # 上传是先落盘再登记，刚写下的文件可能只是还没来得及登记
            raw_recent.write_bytes(b"new")
            raw_hidden.write_bytes(b"")
            os.utime(raw_hidden, (old, old))
        for path in (raw_referenced, raw_paste):
            os.utime(path, (old, old))

        cache_dir = settings.data_dir / "cache" / space.id
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_alive = cache_dir / f"{document.id}.parse.json"
        cache_alive.write_text("{}", encoding="utf-8")
        cache_orphan = cache_dir / "deleted-doc.parse.json"
        if dirty:
            cache_orphan.write_text("{}", encoding="utf-8")

        return Seeded(
            space_id=space.id,
            chunk_id=chunk.id,
            card_alive=card_alive.id,
            card_manual=card_manual.id,
            card_partial=card_partial.id,
            card_orphan=card_orphan.id,
            relation_alive=relation_alive.id,
            relation_dead=relation_dead.id,
            entity_linked=entity_linked.id,
            entity_demo=entity_demo.id,
            entity_orphan=entity_orphan.id,
            entity_only_in_dead_relation=entity_dead_end.id,
            raw_referenced=raw_referenced,
            raw_paste=raw_paste,
            raw_orphan=raw_orphan,
            raw_recent=raw_recent,
            raw_hidden=raw_hidden,
            cache_alive=cache_alive,
            cache_orphan=cache_orphan,
        )
    finally:
        await manager.close()


@dataclass
class Snapshot:
    """库里此刻剩下什么。"""

    cards: dict[str, list[str]]
    relations: set[str]
    entities: set[str]
    chunk_vectors: set[str]
    card_vectors: set[str]


async def _snapshot(settings: Settings, space_id: str) -> Snapshot:
    manager = SpaceManager(settings)
    try:
        database = await manager.space_db(space_id)
        cards = {
            card.id: card.source_chunks
            for card in (await database.cards.list_by_space(space_id, limit=100))[0]
        }
        relations = {
            str(row["id"]) for row in await database.sqlite.fetchall("SELECT id FROM relations")
        }
        entities = {
            str(row["id"]) for row in await database.sqlite.fetchall("SELECT id FROM entities")
        }
        assert database.vectors is not None
        return Snapshot(
            cards=cards,
            relations=relations,
            entities=entities,
            chunk_vectors=set(await database.vectors.list_ids("chunks_vec")),
            card_vectors=set(await database.vectors.list_ids("cards_vec")),
        )
    finally:
        await manager.close()


def test_dry_run_reports_problems_without_touching_data(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认只读：每类问题都报出数量，而库和磁盘上什么都没变。"""
    seeded = asyncio.run(_seed(settings))
    before = asyncio.run(_snapshot(settings, seeded.space_id))

    assert doctor(settings) == 0

    out = capsys.readouterr().out
    assert seeded.space_id in out
    assert "dry-run" in out
    for fragment in (
        "1  来源切片全部已不存在的知识卡片",
        "1  来源切片部分失效的知识卡片",
        "1  来源切片全部已不存在的关系",
        # 「孤点」加上「只挂在那条失效关系上」的，和 --apply 实际会删的一致
        "2  提及数为 0 且不在任何关系里的实体",
        "1  chunks_vec 里回不了表的向量",
        "1  cards_vec 里回不了表的向量",
        "1  raw/ 下没有文档引用的原始文件",
        "1  已删文档的解析缓存",
    ):
        assert fragment in out, f"缺少：{fragment}\n{out}"

    after = asyncio.run(_snapshot(settings, seeded.space_id))
    assert after == before, "dry-run 不能改库"
    for path in (seeded.raw_orphan, seeded.raw_recent, seeded.cache_orphan):
        assert path.is_file(), f"dry-run 不能删文件：{path.name}"


def test_apply_removes_orphans_and_keeps_healthy_data(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply 清掉孤儿卡片（连同向量）、孤立实体、没人引用的 raw 文件；正常数据原样保留。"""
    seeded = asyncio.run(_seed(settings))

    assert doctor(settings, apply=True) == 0
    assert "已清理" in capsys.readouterr().out

    after = asyncio.run(_snapshot(settings, seeded.space_id))

    # 卡片：全失效的删掉（向量一起删），半失效的摘掉失效来源，手工卡片不动
    assert seeded.card_orphan not in after.cards
    assert seeded.card_orphan not in after.card_vectors, "删卡片必须连向量一起删"
    assert after.cards[seeded.card_partial] == [seeded.chunk_id]
    assert after.cards[seeded.card_alive] == [seeded.chunk_id]
    assert after.cards[seeded.card_manual] == []
    assert after.card_vectors == {seeded.card_alive}
    assert after.chunk_vectors == {seeded.chunk_id}

    # 关系与实体
    assert after.relations == {seeded.relation_alive}
    assert seeded.entity_orphan not in after.entities
    assert seeded.entity_only_in_dead_relation not in after.entities
    assert seeded.entity_linked in after.entities, "还挂在有效关系上的实体要留下"
    assert seeded.entity_demo in after.entities, "只有计数、没有提及明细的实体不能删"

    # 文件
    assert not seeded.raw_orphan.exists()
    assert not seeded.cache_orphan.exists()
    assert seeded.raw_referenced.is_file()
    assert seeded.raw_paste.is_file(), "库里路径坏了，但文件名带文档 id，仍然算被引用"
    assert seeded.raw_recent.is_file(), "宽限期内的文件可能只是还没登记"
    assert seeded.raw_hidden.is_file()
    assert seeded.cache_alive.is_file()

    # 再跑一遍应当是干净的
    assert doctor(settings) == 0
    assert "未发现问题" in capsys.readouterr().out


def test_healthy_space_is_left_alone(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有问题的 Space：报「未发现问题」，--apply 也一条不删。"""
    seeded = asyncio.run(_seed(settings, dirty=False))
    before = asyncio.run(_snapshot(settings, seeded.space_id))

    assert doctor(settings, space_id=seeded.space_id, apply=True) == 0

    assert "未发现问题" in capsys.readouterr().out
    assert asyncio.run(_snapshot(settings, seeded.space_id)) == before
    assert seeded.raw_referenced.is_file()
    assert seeded.raw_paste.is_file()
    assert seeded.cache_alive.is_file()


def test_unknown_space_is_reported(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    """--space 写错时给出明确提示并返回非零，而不是抛一屏堆栈。"""
    assert doctor(settings, space_id="no-such-space") == 1
    assert "no-such-space" in capsys.readouterr().out


def test_doctor_command_line_flags() -> None:
    """``agentmem doctor [--space SPACE_ID] [--apply]``：缺省只读、查全部 Space。"""
    parser = build_parser()
    defaults = parser.parse_args(["doctor"])
    assert (defaults.command, defaults.space, defaults.apply) == ("doctor", None, False)
    given = parser.parse_args(["doctor", "--space", "s1", "--apply"])
    assert (given.space, given.apply) == ("s1", True)
