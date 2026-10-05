"""存储层：迁移、Repository CRUD、FTS5 中文检索、向量维度守卫。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentmem.errors import EmbeddingDimMismatchError, NotFoundError
from agentmem.store import Database
from agentmem.store.fts import build_match_query, to_index_text, tokenize
from agentmem.store.sqlite import MIGRATIONS
from agentmem.types import (
    ChunkCreate,
    ChunkUpdate,
    DocumentCreate,
    DocumentMeta,
    DocumentUpdate,
    EntityCreate,
    InsightCreate,
    InsightUpdate,
    KnowledgeCardCreate,
    SpaceCreate,
    SpaceUpdate,
    TraceCreate,
    TraceRetrievedItem,
    UsageRecordCreate,
    VectorRecord,
)


async def _space_with_document(database: Database) -> tuple[str, str]:
    space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android 逆向"))
    document = await database.documents.create(
        DocumentCreate(
            space_id=space.id,
            title="脱壳笔记",
            source_type="paste",
            sha256="sha-1",
            meta=DocumentMeta(tags=["脱壳"]),
        )
    )
    return space.id, document.id


async def test_migration_creates_schema(database: Database) -> None:
    """迁移全部执行后版本追平清单末尾，且表齐备。

    这里不写死版本号：`schema.sql` 只是 v1 的快照，新库同样要把后续迁移依次跑完
    才算建好。写死版本号会掩盖「迁移没跑」这种最该被发现的故障。
    """
    latest = max(version for version, _ in MIGRATIONS)
    assert await database.sqlite.schema_version() == latest
    rows = await database.sqlite.fetchall(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )
    names = {str(row["name"]) for row in rows}
    for expected in (
        "spaces",
        "documents",
        "chunks",
        "chunks_fts",
        "knowledge_cards",
        "entities",
        "relations",
        "insights",
        "conversations",
        "messages",
        "traces",
        "feedback",
        "eval_items",
        "eval_runs",
        "expertise_snapshots",
        "usage_records",
        "schema_version",
    ):
        assert expected in names, expected


async def test_migration_v2_adds_cache_columns(database: Database) -> None:
    """v2 的列必须真的存在——全新库只跑 schema.sql 是不够的。

    这条守的是一个很容易犯的错：把新列加进 `schema.sql` 而不加迁移（老库缺列），
    或者两边都加（新库 duplicate column 直接建不起来）。
    """
    rows = await database.sqlite.fetchall("PRAGMA table_info(usage_records)")
    columns = {str(row["name"]) for row in rows}
    assert {"cached_tokens", "cache_write_tokens"} <= columns


async def test_entities_top_by_mention_uses_the_index(database: Database) -> None:
    """取提及最多的实体走索引，而不是全表扫完再排序。

    这条守的是 v10 那个索引：没有它，SQLite 会 SCAN entities 再 USE TEMP B-TREE 排序，
    知识图谱的开销随 Space 大小线性上涨——而图上永远只画 300 个节点。
    """
    rows = await database.sqlite.fetchall(
        "EXPLAIN QUERY PLAN SELECT * FROM entities WHERE space_id = ?"
        " ORDER BY mention_count DESC, id LIMIT 10",
        ("s1",),
    )
    plan = " ".join(str(row["detail"]) for row in rows)
    assert "idx_entities_mention" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


async def test_entities_top_by_mention_and_lookups(database: Database) -> None:
    """按提及次数取前 N、按 id 批量取、按名称取（同名取提及最多的那个）。"""
    space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android 逆向"))
    created = {
        name: await database.entities.create(
            EntityCreate(space_id=space.id, name=name, type=type_, mention_count=count)
        )
        for name, type_, count in [("Frida", "工具", 9), ("Frida", "协议", 2), ("IDA", "工具", 5)]
    }

    top = await database.entities.top_by_mention(space.id, limit=2)
    assert [(entity.name, entity.mention_count) for entity in top] == [("Frida", 9), ("IDA", 5)]
    assert await database.entities.top_by_mention(space.id, limit=0) == []

    # 同名不同类型：取提及最多的那个，与图上画得最大的节点一致
    by_name = await database.entities.find_by_name(space.id, "  frida ")
    assert by_name is not None and by_name.type == "工具"
    assert await database.entities.find_by_name(space.id, "查无此名") is None

    fetched = await database.entities.get_many(
        [created["IDA"].id, "不存在的 id", created["IDA"].id]
    )
    assert [entity.id for entity in fetched] == [created["IDA"].id]
    assert await database.entities.get_many([]) == []


async def test_wal_and_foreign_keys_enabled(database: Database) -> None:
    """WAL 与外键约束必须打开。"""
    journal = await database.sqlite.fetchvalue("PRAGMA journal_mode")
    assert str(journal).lower() == "wal"
    fk = await database.sqlite.fetchvalue("PRAGMA foreign_keys")
    assert int(fk) == 1


async def test_foreign_key_cascade(tmp_path: Path) -> None:
    """删除文档时切片级联删除。"""
    async with Database(tmp_path / "meta.db", tmp_path / "vectors", "s1") as db:
        space = await db.spaces.create(SpaceCreate(name="n", domain="d"))
        document = await db.documents.create(
            DocumentCreate(space_id=space.id, title="t", source_type="file", sha256="h")
        )
        await db.chunks.create_many(
            [
                ChunkCreate(
                    space_id=space.id,
                    document_id=document.id,
                    ordinal=0,
                    content="内容",
                )
            ]
        )
        assert await db.chunks.count(document_id=document.id) == 1
        await db.documents.delete(document.id)
        assert await db.chunks.count(document_id=document.id) == 0


async def test_space_crud(database: Database) -> None:
    """Space 的增删改查。"""
    created = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
    assert created.created_at > 0
    assert (await database.spaces.get(created.id)) == created

    updated = await database.spaces.update(created.id, SpaceUpdate(name="逆向工程"))
    assert updated.name == "逆向工程"
    assert updated.updated_at >= created.updated_at

    assert await database.spaces.count() == 1
    assert await database.spaces.delete(created.id) is True
    assert await database.spaces.get(created.id) is None


async def test_document_crud_and_json_meta(database: Database) -> None:
    """文档的 CRUD 与 meta 的 JSON 编解码。"""
    space_id, document_id = await _space_with_document(database)
    document = await database.documents.require(document_id)
    assert document.meta.tags == ["脱壳"]
    assert document.space_id == space_id

    updated = await database.documents.update(
        document_id, DocumentUpdate(status="ready", token_count=128)
    )
    assert updated.status == "ready"
    assert updated.token_count == 128

    # meta 以 JSON 字符串落库，但对外是强类型
    raw = await database.sqlite.fetchvalue(
        "SELECT meta FROM documents WHERE id = ?", (document_id,)
    )
    assert isinstance(raw, str)
    assert '"tags"' in raw

    with pytest.raises(NotFoundError):
        await database.documents.require("missing")


async def test_document_dedup_by_sha256(database: Database) -> None:
    """同 Space 内 sha256 唯一。"""
    space_id, _ = await _space_with_document(database)
    existing = await database.documents.find_by_sha256(space_id, "sha-1")
    assert existing is not None
    assert await database.documents.find_by_sha256(space_id, "sha-other") is None


async def test_chunk_crud_and_fts_sync(database: Database) -> None:
    """写入切片时同步写 FTS，更新与删除也要同步。"""
    space_id, document_id = await _space_with_document(database)
    chunks = await database.chunks.create_many(
        [
            ChunkCreate(
                space_id=space_id,
                document_id=document_id,
                ordinal=0,
                content="Android 加固 APK 的脱壳流程：先定位 DexClassLoader 调用点。",
                heading_path="第3章 > 3.2 脱壳",
                page=12,
                char_start=0,
                char_end=30,
                token_count=20,
            ),
            ChunkCreate(
                space_id=space_id,
                document_id=document_id,
                ordinal=1,
                content="Frida 可以 hook Java 层方法，用于绕过证书校验。",
                token_count=18,
            ),
        ]
    )
    assert len(chunks) == 2
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert chunks[0].heading_path == "第3章 > 3.2 脱壳"
    assert await database.fts.count() == 2

    hits = await database.fts.search("脱壳 流程", space_id=space_id)
    assert hits, "中文分词后应当命中"
    assert hits[0].chunk_id == chunks[0].id

    # 未命中的词不应返回
    assert await database.fts.search("完全无关的词汇", space_id=space_id) == []

    # 更新内容后旧词失效、新词可命中
    await database.chunks.update(chunks[0].id, ChunkUpdate(content="协议分析：抓包定位签名算法。"))
    assert await database.fts.search("脱壳", space_id=space_id) == []
    moved = await database.fts.search("签名算法", space_id=space_id)
    assert moved and moved[0].chunk_id == chunks[0].id

    # 删除文档时清理索引
    await database.chunks.delete_by_document(document_id)
    assert await database.fts.count() == 0


async def test_fts_space_isolation(database: Database) -> None:
    """FTS 检索按 space_id 隔离，并支持限定文档。"""
    space_id, document_id = await _space_with_document(database)
    await database.chunks.create_many(
        [ChunkCreate(space_id=space_id, document_id=document_id, ordinal=0, content="脱壳工具推荐")]
    )
    assert await database.fts.search("脱壳", space_id="other-space") == []
    assert await database.fts.search("脱壳", space_id=space_id, document_ids=["nope"]) == []
    assert await database.fts.search("脱壳", space_id=space_id, document_ids=[document_id])


def test_jieba_tokenization() -> None:
    """分词结果与 MATCH 表达式。"""
    tokens = tokenize("Android 加固 APK 的脱壳流程")
    assert "脱壳" in tokens
    assert to_index_text("脱壳流程") == " ".join(tokenize("脱壳流程"))
    assert build_match_query("") == ""
    assert build_match_query("脱壳") == '"脱壳"'
    assert " OR " in build_match_query("脱壳 流程")


async def test_vector_upsert_search_and_dim_guard(database: Database) -> None:
    """向量写入、检索与维度守卫。"""
    space_id, document_id = await _space_with_document(database)
    vectors = await database.require_vectors()

    await vectors.upsert(
        "chunks_vec",
        [
            VectorRecord(
                id="chunk-1",
                space_id=space_id,
                document_id=document_id,
                vector=[0.1] * 8,
                embedding_model="mock-embed",
            )
        ],
    )
    meta = await vectors.table_meta("chunks_vec")
    assert meta is not None
    assert (meta.dim, meta.embedding_model) == (8, "mock-embed")

    hits = await vectors.search("chunks_vec", [0.1] * 8, limit=5, space_id=space_id)
    assert hits and hits[0].id == "chunk-1"
    assert await vectors.search("chunks_vec", [0.1] * 8, limit=5, space_id="other") == []
    assert await vectors.count("chunks_vec", space_id) == 1

    # 维度不一致必须报错，不能静默写入
    with pytest.raises(EmbeddingDimMismatchError) as excinfo:
        await vectors.upsert(
            "chunks_vec",
            [
                VectorRecord(
                    id="chunk-2",
                    space_id=space_id,
                    document_id=document_id,
                    vector=[0.1] * 16,
                    embedding_model="other",
                )
            ],
        )
    assert excinfo.value.code == "EMBEDDING_DIM_MISMATCH"
    assert excinfo.value.http_status == 409

    # 同一主键重复写入是更新而非新增
    await vectors.upsert(
        "chunks_vec",
        [
            VectorRecord(
                id="chunk-1",
                space_id=space_id,
                document_id=document_id,
                vector=[0.2] * 8,
                embedding_model="mock-embed",
            )
        ],
    )
    assert await vectors.count("chunks_vec", space_id) == 1

    await vectors.delete_where("chunks_vec", f"document_id = '{document_id}'")
    assert await vectors.count("chunks_vec", space_id) == 0


async def test_card_and_trace_json_roundtrip(database: Database) -> None:
    """JSON 列在 Repository 侧完成编解码。"""
    space_id, _ = await _space_with_document(database)
    card = await database.cards.create(
        KnowledgeCardCreate(
            space_id=space_id,
            kind="pitfall",
            title="壳检测",
            body="先判断是否加固",
            aliases=["加固检测"],
            source_chunks=["chunk-1"],
        )
    )
    restored = await database.cards.require(card.id)
    assert restored == card
    assert restored.aliases == ["加固检测"]

    trace = await database.traces.create(
        TraceCreate(
            space_id=space_id,
            conversation_id="cv1",
            message_id="m1",
            query="怎么脱壳",
            retrieved=[TraceRetrievedItem(chunk_id="chunk-1", vec_score=0.9, rrf=0.3)],
            used_insights=["i1"],
            used_cards=[card.id],
        )
    )
    loaded = await database.traces.require(trace.id)
    assert loaded.retrieved[0].rrf == pytest.approx(0.3)
    assert loaded.used_cards == [card.id]


async def test_insight_filters_and_update(database: Database) -> None:
    """经验条目的过滤、排序与更新。"""
    space_id, _ = await _space_with_document(database)
    insight = await database.insights.create(
        InsightCreate(
            space_id=space_id,
            trigger="遇到加固 APK",
            guidance="先脱壳再看 dex",
            kind="heuristic",
            origin="manual",
            status="active",
            confidence=0.8,
            source_trace_ids=["t1", "t2"],
        )
    )
    assert insight.source_trace_ids == ["t1", "t2"]

    items, total, _ = await database.insights.list_by_space(space_id, status="active")
    assert total == 1 and items[0].id == insight.id

    archived = await database.insights.update(insight.id, InsightUpdate(status="archived"))
    assert archived.status == "archived"
    assert await database.insights.count(space_id, status="archived") == 1


async def test_pagination_cursor(database: Database) -> None:
    """游标分页按主键顺序推进。"""
    space = await database.spaces.create(SpaceCreate(name="n", domain="d"))
    for index in range(5):
        await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title=f"doc-{index}",
                source_type="paste",
                sha256=f"h{index}",
            )
        )
    page1, total, cursor = await database.documents.list_by_space(space.id, limit=2)
    assert total == 5 and len(page1) == 2 and cursor is not None
    page2, _, cursor2 = await database.documents.list_by_space(space.id, limit=2, cursor=cursor)
    assert len(page2) == 2
    assert {item.id for item in page1}.isdisjoint({item.id for item in page2})
    assert cursor2 is not None


async def test_usage_aggregate_filters_by_kind(database: Database) -> None:
    """按 kind 过滤是缓存命中率有意义的前提。

    embedding / rerank 根本没有前缀缓存这回事。把它们算进分母，命中率就会被
    稀释成一个没人能解释的数——实测 embedding 曾占掉输入 token 的四成，
    把真实的 25% 压成 14%。看缓存必须只看 llm。
    """
    for kind, prompt, cached in (
        ("llm", 1000, 400),
        ("llm", 1000, 100),
        ("embedding", 8000, 0),  # 量大且恒不命中，正是稀释分母的元凶
    ):
        await database.usage.create(
            UsageRecordCreate(
                provider_id=f"p-{kind}",
                model=f"m-{kind}",
                kind=kind,
                prompt_tokens=prompt,
                cached_tokens=cached,
            )
        )

    all_kinds = await database.usage.aggregate(group_by="kind")
    assert sum(item.prompt_tokens for item in all_kinds) == 10_000

    llm_only = await database.usage.aggregate(group_by="kind", kind="llm")
    assert len(llm_only) == 1
    assert llm_only[0].prompt_tokens == 2000
    assert llm_only[0].cached_tokens == 500
    assert llm_only[0].cache_hit_rate == 0.25

    # 不过滤时命中率被 embedding 稀释到 5%，这正是我们要避免的误导
    unfiltered = await database.usage.aggregate(group_by="provider")
    diluted = sum(i.cached_tokens for i in unfiltered) / sum(i.prompt_tokens for i in unfiltered)
    assert diluted == 0.05


async def test_cards_sorted_by_confidence_can_be_paged_through(database: Database) -> None:
    """按置信度排序的卡片要能翻页：并列的置信度很多，翻完不能重复也不能漏。

    此前非 id 排序一律返回 next_cursor=None：界面显示「卡片 (170)」，永远只翻得到前 50 张。
    """
    space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
    created = [
        await database.cards.create(
            KnowledgeCardCreate(
                space_id=space.id,
                kind="fact",
                title=f"卡片 {index}",
                body="正文",
                confidence=[0.9, 0.9, 0.8, 1.0, 0.9][index % 5],
            )
        )
        for index in range(23)
    ]

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        items, total, cursor = await database.cards.list_by_space(space.id, limit=5, cursor=cursor)
        seen.extend(card.id for card in items)
        pages += 1
        if cursor is None:
            break
    assert total == 23
    assert pages == 5
    assert len(seen) == len(set(seen)) == 23, "不能重复也不能漏"
    by_id = {card.id: card.confidence for card in created}
    assert [by_id[card_id] for card_id in seen] == sorted(by_id.values(), reverse=True)


async def test_tampered_cursor_is_a_validation_error(database: Database) -> None:
    """被改坏的游标报参数错误（422），不是 500。"""
    from agentmem.errors import ValidationError

    space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
    with pytest.raises(ValidationError):
        await database.cards.list_by_space(space.id, cursor="k1.not-base64-json")
