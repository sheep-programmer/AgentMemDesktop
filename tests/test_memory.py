"""L2 知识层：分批抽取、去重、marker 映射、图谱聚合与失败隔离。

模型用 Mock 服务返回固定 JSON，因此这里验证的是**我们这一侧的行为**：
怎么切批、怎么把 marker 翻译成 chunk_id、重复的卡片与实体怎么合并、
坏掉的条目怎么丢弃、抽取失败会不会影响文档可用性。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agentmem.config import ModelsConfig, Settings
from agentmem.errors import NotFoundError
from agentmem.evolve._persona import to_persona_spec
from agentmem.ingest import IngestPipeline
from agentmem.memory import (
    CardService,
    KnowledgeExtractor,
    batch_chunks,
    build_graph,
    parse_payload,
    resolve_markers,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import RetrievalPipeline
from agentmem.store import Database
from agentmem.types import (
    ChunkCreate,
    Document,
    DocumentCreate,
    EntityCreate,
    KnowledgeCardCreate,
    KnowledgeCardUpdate,
    Persona,
    ProgressEvent,
    ProviderConfig,
    Relation,
    RelationCreate,
    RetrievalSettings,
    RoleBindings,
    Space,
    SpaceCreate,
    VectorRecord,
)

EMBED_DIM = 8

#: 指向死端口的 base_url，用来验证 provider 挂掉时的降级
DEAD_BASE_URL = "http://127.0.0.1:1/v1"

PAYLOAD: dict[str, object] = {
    "cards": [
        {
            "kind": "pitfall",
            "title": "ptrace 反调试会让 Frida attach 失败",
            "body": "目标进程自我 ptrace 占位时，attach 模式建立不了调试连接。",
            "aliases": ["frida attach 失败"],
            "source_chunk_markers": ["c2"],
            "confidence": 0.9,
        }
    ],
    "entities": [
        {"name": "Frida", "type": "工具", "summary": "动态插桩框架"},
        {"name": "ptrace 反调试", "type": "防护技术", "summary": "自我 ptrace 占位"},
    ],
    "relations": [
        {
            "src": "ptrace 反调试",
            "dst": "Frida",
            "predicate": "对抗",
            "source_chunk_markers": ["c2"],
        }
    ],
}

CHUNK_TEXTS = [
    "# 第3章 脱壳\n\n加固 APK 先定位 DexClassLoader 调用点。",
    "Frida 的 attach 模式在目标进程自我 ptrace 时会失败。",
    "内存 dump 之后需要修复 dex header。",
]


def _sample_payload(markers: list[str], title: str = "ptrace 反调试会让 Frida attach 失败") -> str:
    """按给定 marker 生成一份抽取结果 JSON。"""
    payload = json.loads(json.dumps(PAYLOAD))
    payload["cards"][0]["title"] = title
    payload["cards"][0]["source_chunk_markers"] = markers
    payload["relations"][0]["source_chunk_markers"] = markers
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def space(database: Database) -> Space:
    """被测 Space 的元数据行。"""
    return await database.spaces.create(SpaceCreate(name="逆向", domain="Android 逆向工程"))


@pytest.fixture
def registry(database: Database, mock_server: str) -> ProviderRegistry:
    """同时绑定 LLM 与 embedding 角色的注册表，都指向 Mock 服务。"""
    config = ModelsConfig(
        providers=[
            ProviderConfig(
                id="mock-llm",
                kind="llm",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-chat",
            ),
            ProviderConfig(
                id="mock-embed",
                kind="embedding",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-embed",
                dimension=EMBED_DIM,
            ),
        ],
        roles=RoleBindings(
            chat="mock-llm",
            fast="mock-llm",
            distill="mock-llm",
            judge="mock-llm",
            embedding="mock-embed",
        ),
    )
    return ProviderRegistry(config, usage=database.usage, space_id=database.space_id)


@pytest.fixture
def dead_registry(database: Database, mock_server: str) -> ProviderRegistry:
    """LLM 指向死端口的注册表：抽取必然失败，但向量化照常。"""
    config = ModelsConfig(
        providers=[
            ProviderConfig(
                id="dead-llm",
                kind="llm",
                adapter="openai_compatible",
                base_url=DEAD_BASE_URL,
                model="nope",
            ),
            ProviderConfig(
                id="mock-embed",
                kind="embedding",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-embed",
                dimension=EMBED_DIM,
            ),
        ],
        roles=RoleBindings(fast="dead-llm", embedding="mock-embed"),
    )
    return ProviderRegistry(config, usage=database.usage, space_id=database.space_id)


async def _make_document(
    database: Database,
    space: Space,
    texts: list[str],
    *,
    title: str = "脱壳笔记",
    sha256: str = "sha-1",
) -> Document:
    """造一篇已就绪的文档及其切片。"""
    document = await database.documents.create(
        DocumentCreate(
            space_id=space.id,
            title=title,
            source_type="paste",
            sha256=sha256,
            status="ready",
        )
    )
    await database.chunks.create_many(
        [
            ChunkCreate(
                space_id=space.id,
                document_id=document.id,
                ordinal=index,
                content=text,
            )
            for index, text in enumerate(texts)
        ]
    )
    return document


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_batch_chunks_groups_adjacent_chunks() -> None:
    """按相邻顺序切批，条数不超过上限。"""

    class _Chunk:
        def __init__(self, content: str) -> None:
            self.content = content

    chunks = [_Chunk("x" * 10) for _ in range(9)]
    batches = batch_chunks(chunks)  # type: ignore[arg-type]
    assert [len(batch) for batch in batches] == [4, 4, 1]


def test_batch_chunks_splits_on_length() -> None:
    """单批正文超长时提前封批。"""

    class _Chunk:
        def __init__(self, content: str) -> None:
            self.content = content

    chunks = [_Chunk("x" * 7000) for _ in range(3)]
    batches = batch_chunks(chunks)  # type: ignore[arg-type]
    assert [len(batch) for batch in batches] == [1, 1, 1]


def test_resolve_markers_maps_and_drops() -> None:
    """marker 翻译成 chunk_id；认不出的与重复的丢掉。"""
    mapping = {"c1": "chunk-1", "c2": "chunk-2"}
    assert resolve_markers(["c2", "C1", " c1 ", "c9"], mapping) == ["chunk-2", "chunk-1"]


def test_parse_payload_tolerates_bad_items() -> None:
    """单条坏数据只丢那一条，同时归一化类型别名与置信度。"""
    payload, skipped = parse_payload(
        {
            "cards": [
                {"kind": "注意事项", "title": "t", "body": "b", "confidence": 5},
                {"kind": "不存在的类型", "title": "x", "body": "y"},
                "不是对象",
            ],
            "entities": None,
            "relations": [{"src": "a", "dst": "b", "predicate": "用于"}],
        }
    )
    assert skipped == 2
    assert [card.kind for card in payload.cards] == ["pitfall"]
    assert payload.cards[0].confidence == 1.0
    assert payload.entities == []
    assert len(payload.relations) == 1


def test_parse_payload_rejects_non_object() -> None:
    """整体不是 JSON 对象属于整批失败。"""
    with pytest.raises(ValueError):
        parse_payload(["not", "an", "object"])


# ---------------------------------------------------------------------------
# 抽取落库
# ---------------------------------------------------------------------------


async def test_extract_persists_cards_entities_relations(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
) -> None:
    """卡片 / 实体 / 关系都落库，marker 正确映射成 chunk_id。"""
    document = await _make_document(database, space, CHUNK_TEXTS)
    mock_reply(_sample_payload(["c2"]))
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    report = await extractor.extract_document(document)

    chunks = await database.chunks.list_by_document(document.id)
    cards, total, _ = await database.cards.list_by_space(space.id)
    assert total == 1
    card = cards[0]
    assert card.kind == "pitfall"
    assert card.source_chunks == [chunks[1].id]
    assert card.confidence == pytest.approx(0.9)
    assert card.aliases == ["frida attach 失败"]

    entities = await database.entities.list_by_space(space.id)
    assert {entity.name for entity in entities[0]} == {"Frida", "ptrace 反调试"}
    assert {entity.mention_count for entity in entities[0]} == {1}

    relations = await database.relations.list_by_space(space.id)
    assert len(relations) == 1
    relation = relations[0]
    by_name = {entity.name: entity.id for entity in entities[0]}
    assert (relation.src_id, relation.dst_id) == (by_name["ptrace 反调试"], by_name["Frida"])
    assert relation.predicate == "对抗"
    assert relation.source_chunks == [chunks[1].id]

    assert report.cards_created == 1
    assert report.entities_created == 2
    assert report.relations_created == 1
    assert report.failures == []

    vectors = await database.require_vectors()
    assert await vectors.count("cards_vec", space.id) == 1
    hits = await vectors.search("cards_vec", [0.1] * EMBED_DIM, limit=5, space_id=space.id)
    assert [hit.id for hit in hits] == [card.id]


async def test_extract_dedupes_across_batches(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
) -> None:
    """跨批的同一张卡片合并，来源切片取并集；实体累加提及次数。

    marker 每批从 ``c1`` 重新编号，所以两批的 ``c1`` 必须落到各自批次的
    首个切片上——合并后的来源列表正好能证明这一点。
    """
    # 每片都带上卡片依据的那句原文：落地检查会丢掉原文里找不到的卡片
    texts = [f"切片 {index}：{CHUNK_TEXTS[1]}" for index in range(8)]
    document = await _make_document(database, space, texts)
    mock_reply(_sample_payload(["c1"]))
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    progress: list[ProgressEvent] = []

    async def on_progress(event: ProgressEvent) -> None:
        progress.append(event)

    report = await extractor.extract_document(document, on_progress=on_progress)

    chunks = await database.chunks.list_by_document(document.id)
    cards, total, _ = await database.cards.list_by_space(space.id)
    assert total == 1
    assert cards[0].source_chunks == [chunks[0].id, chunks[4].id]
    assert report.cards_created == 1
    assert report.cards_updated == 1

    entities, _ = await database.entities.list_by_space(space.id)
    assert len(entities) == 2
    # 提及计数按「哪篇文档提到过」记：同一篇文档的两批都提到它，仍然只算一次。
    # 这里守的是幂等——计数一累加，重新解析过的文档就会把节点权重越推越高。
    assert {entity.mention_count for entity in entities} == {1}

    relations = await database.relations.list_by_space(space.id)
    assert len(relations) == 1

    assert [(item.done, item.total) for item in progress] == [(1, 2), (2, 2)]
    assert all(item.stage == "extracting" for item in progress)


async def test_extract_skips_unresolvable_sources_and_endpoints(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
) -> None:
    """来源 marker 认不出、关系端点没抽出来的条目都跳过。"""
    document = await _make_document(database, space, CHUNK_TEXTS)
    mock_reply(
        json.dumps(
            {
                "cards": [
                    {
                        "kind": "fact",
                        "title": "没有来源的卡片",
                        "body": "正文",
                        "source_chunk_markers": ["c99"],
                    }
                ],
                "entities": [],
                "relations": [{"src": "查无此实体", "dst": "也不存在", "predicate": "用于"}],
            },
            ensure_ascii=False,
        )
    )
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    report = await extractor.extract_document(document)

    assert await database.cards.count(space.id) == 0
    assert await database.relations.count(space.id) == 0
    stats = report.documents[0]
    assert stats.cards_skipped == 1
    assert stats.relations_skipped == 1


async def test_extract_survives_unparsable_reply(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
) -> None:
    """模型返回脏输出时重问一次，仍然解析不出来就放弃这一批。"""
    document = await _make_document(database, space, CHUNK_TEXTS)
    mock_reply("这不是 JSON，只是一段白话。")
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    report = await extractor.extract_document(document)

    stats = report.documents[0]
    assert stats.batches == 1
    assert stats.batches_failed == 1
    assert await database.cards.count(space.id) == 0


async def test_extraction_records_dimension_mismatch(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
) -> None:
    """向量维度与既有索引不符时，卡片照常落库，但失败原因必须出现在报告里。"""
    vectors = await database.require_vectors()
    await vectors.upsert(
        "cards_vec",
        [
            VectorRecord(
                id="legacy-card",
                space_id=space.id,
                vector=[0.1] * 4,
                embedding_model="legacy",
            )
        ],
    )
    document = await _make_document(database, space, CHUNK_TEXTS)
    mock_reply(_sample_payload(["c2"]))
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    report = await extractor.extract_document(document)

    stats = report.documents[0]
    assert stats.embedding_error is not None
    assert "EMBEDDING_DIM_MISMATCH" in stats.embedding_error
    assert await database.cards.count(space.id) == 1
    assert await vectors.count("cards_vec", space.id) == 1


async def test_extract_reports_failure_without_aborting(
    database: Database,
    space: Space,
    dead_registry: ProviderRegistry,
) -> None:
    """单篇文档抽取失败只记进报告，其余文档继续。"""
    broken = await _make_document(database, space, CHUNK_TEXTS, title="A", sha256="sha-a")
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=dead_registry
    )

    report = await extractor.extract_document(broken)

    assert report.documents[0].batches_failed == 1
    assert report.cards_created == 0


async def test_extract_skips_document_without_chunks(
    database: Database, space: Space, registry: ProviderRegistry
) -> None:
    """没有切片的文档不调用模型。"""
    document = await _make_document(database, space, [])
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    report = await extractor.extract_document(document)

    assert report.documents[0].skipped_reason == "no_chunks"
    assert report.documents[0].batches == 0


async def test_resolve_documents_defaults_to_ready(
    database: Database, space: Space, registry: ProviderRegistry
) -> None:
    """未指定文档时取已就绪的文档；指定不存在的文档报 404。"""
    ready = await _make_document(database, space, CHUNK_TEXTS, sha256="sha-ready")
    pending = await database.documents.create(
        DocumentCreate(space_id=space.id, title="待处理", source_type="paste", sha256="sha-pending")
    )
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )

    resolved = await extractor.resolve_documents([])
    assert [document.id for document in resolved] == [ready.id]
    assert (await extractor.resolve_documents([ready.id]))[0].id == ready.id
    with pytest.raises(NotFoundError):
        await extractor.resolve_documents([pending.id, "no-such-document"])


# ---------------------------------------------------------------------------
# 卡片人工路径
# ---------------------------------------------------------------------------


async def test_manual_edit_marks_verified_and_wins_over_extraction(
    database: Database, space: Space, registry: ProviderRegistry
) -> None:
    """人工校订后置信度拉满，且后续抽取不再覆盖正文。"""
    service = CardService(space_id=space.id, database=database, registry=registry)
    created = await service.merge_extracted(
        KnowledgeCardCreate(space_id=space.id, kind="fact", title="DexClassLoader", body="原始正文")
    )
    assert created[1] is True

    edited = await service.update(created[0].id, KnowledgeCardUpdate(body="人工订正后的正文"))
    assert edited.verified_by == "user"
    assert edited.confidence == 1.0

    merged, is_new = await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space.id,
            kind="fact",
            title="DexClassLoader",
            body="模型又抽了一遍的正文",
            source_chunks=["chunk-9"],
            confidence=0.95,
        )
    )
    assert is_new is False
    assert merged.id == edited.id
    assert merged.body == "人工订正后的正文"
    assert merged.source_chunks == ["chunk-9"]


async def test_delete_card_removes_vector(
    database: Database, space: Space, registry: ProviderRegistry
) -> None:
    """删除卡片要一并清掉向量。"""
    service = CardService(space_id=space.id, database=database, registry=registry)
    card = (
        await service.merge_extracted(
            KnowledgeCardCreate(space_id=space.id, kind="fact", title="标题", body="正文")
        )
    )[0]
    # merge_extracted 只负责落库，向量同步由调用方决定时机
    await service.sync_vectors([card])
    vectors = await database.require_vectors()
    assert await vectors.count("cards_vec", space.id) == 1

    response = await service.delete(card.id)

    assert response.deleted is True
    assert await vectors.count("cards_vec", space.id) == 0
    assert await database.cards.count(space.id) == 0


# ---------------------------------------------------------------------------
# 图谱
# ---------------------------------------------------------------------------


async def _graph_fixture(database: Database, space: Space) -> dict[str, str]:
    """A→B→C→D 的链，提及次数依次递减。"""
    ids: dict[str, str] = {}
    for index, name in enumerate(["A", "B", "C", "D"]):
        entity = await database.entities.create(
            EntityCreate(space_id=space.id, name=name, type="工具", mention_count=4 - index)
        )
        ids[name] = entity.id
    for src, dst in [("A", "B"), ("B", "C"), ("C", "D")]:
        await database.relations.create(
            RelationCreate(
                space_id=space.id,
                src_id=ids[src],
                dst_id=ids[dst],
                predicate="依赖",
                weight=2.0,
            )
        )
    return ids


async def test_graph_returns_nodes_and_edges(database: Database, space: Space) -> None:
    """节点权重是提及次数，边权重来自关系。"""
    ids = await _graph_fixture(database, space)

    graph = await build_graph(database, space.id)

    assert {node.id for node in graph.nodes} == set(ids.values())
    weights = {node.name: node.weight for node in graph.nodes}
    assert weights == {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0}
    assert len(graph.edges) == 3
    assert all(edge.weight == 2.0 for edge in graph.edges)
    assert all(edge.predicate == "依赖" for edge in graph.edges)


async def test_graph_limit_truncates_by_mention_count(database: Database, space: Space) -> None:
    """limit 截断按提及次数，边只保留两端都在结果里的。"""
    await _graph_fixture(database, space)

    graph = await build_graph(database, space.id, limit=2)

    assert [node.name for node in graph.nodes] == ["A", "B"]
    assert len(graph.edges) == 1
    kept_ids = {node.id for node in graph.nodes}
    assert {graph.edges[0].src, graph.edges[0].dst} <= kept_ids


async def test_graph_bfs_neighborhood(database: Database, space: Space) -> None:
    """以实体为中心做 N 度展开。"""
    await _graph_fixture(database, space)

    one_hop = await build_graph(database, space.id, center="B", depth=1)
    assert {node.name for node in one_hop.nodes} == {"A", "B", "C"}
    assert len(one_hop.edges) == 2

    two_hops = await build_graph(database, space.id, center="B", depth=2)
    assert {node.name for node in two_hops.nodes} == {"A", "B", "C", "D"}


async def test_graph_center_survives_limit(database: Database, space: Space) -> None:
    """中心实体即使权重最低也不会被 limit 截掉。"""
    await _graph_fixture(database, space)

    graph = await build_graph(database, space.id, center="D", depth=1, limit=2)

    assert "D" in {node.name for node in graph.nodes}
    assert len(graph.nodes) == 2


async def test_graph_reports_total_beyond_the_limit(database: Database, space: Space) -> None:
    """被 limit 截掉的部分要如实计数：界面靠这个数说明「显示了 2 / 4 个实体」。"""
    await _graph_fixture(database, space)

    graph = await build_graph(database, space.id, limit=2)

    assert graph.total_nodes == 4
    assert graph.total_edges == 3
    assert graph.truncated is True


async def test_graph_only_reads_the_part_it_draws(database: Database, space: Space) -> None:
    """只读画得出来的那部分：不再把整个 Space 的实体与关系装进内存。

    这条钉的是开销的量级。画 2 个节点却把 40 条关系全读回来，在示例数据上看不出问题，
    Space 一大就是另一回事——而图上永远只画 limit 个节点。
    """
    await _graph_fixture(database, space)
    for index in range(20):
        extra = await database.entities.create(
            EntityCreate(space_id=space.id, name=f"边缘{index}", type="工具", mention_count=0)
        )
        await database.relations.create(
            RelationCreate(
                space_id=space.id, src_id=extra.id, dst_id=extra.id, predicate="自环", weight=0.1
            )
        )

    seen: list[int] = []
    original = database.relations.between

    async def spy(space_id: str, entity_ids: Sequence[str]) -> list[Relation]:
        seen.append(len(entity_ids))
        return await original(space_id, entity_ids)

    with patch.object(database.relations, "between", spy):
        graph = await build_graph(database, space.id, limit=2)

    assert [node.name for node in graph.nodes] == ["A", "B"]
    assert seen == [2], "查边时只该带上留在图上的那 2 个节点"
    assert graph.total_nodes == 24, "总数仍要如实报出来"
    assert graph.total_edges == 23


async def test_graph_center_can_be_a_cold_entity(database: Database, space: Space) -> None:
    """中心实体按名字解析，不要求它在「提及最多的那批」里。

    图谱只装载 top-N 节点，而用户可以搜一个提及次数垫底的实体点进去——
    中心的解析因此必须走库，而不是在已装载的那批里找。
    """
    ids = await _graph_fixture(database, space)
    for index in range(5):
        await database.entities.create(
            EntityCreate(space_id=space.id, name=f"热门{index}", type="工具", mention_count=100)
        )

    graph = await build_graph(database, space.id, center=" d ", depth=1, limit=2)

    assert ids["D"] in {node.id for node in graph.nodes}
    # 邻域是 D 与 C，与那五个提及上百的热门实体无关
    assert graph.total_nodes == 2


async def test_graph_center_not_found(database: Database, space: Space) -> None:
    """中心实体不存在时报 404 而不是返回空图。"""
    await _graph_fixture(database, space)

    with pytest.raises(NotFoundError):
        await build_graph(database, space.id, center="查无此实体")


async def test_graph_empty_space(database: Database, space: Space) -> None:
    """没有任何实体时返回空图。"""
    graph = await build_graph(database, space.id)
    assert graph.nodes == []
    assert graph.edges == []


# ---------------------------------------------------------------------------
# 摄取流水线接线
# ---------------------------------------------------------------------------


async def test_pipeline_extracts_cards(
    settings: Settings,
    registry: ProviderRegistry,
    mock_reply: Callable[..., None],
    tmp_path: Path,
) -> None:
    """摄取跑完 extracting 后卡片与图谱都有内容，文档仍是 ready。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        mock_reply(_sample_payload(["c1"]))
        pipeline = IngestPipeline(database, registry, settings)
        document = await pipeline.register_text(
            space_id=space.id, title="脱壳笔记", content="\n\n".join(CHUNK_TEXTS)
        )

        progress: list[ProgressEvent] = []

        async def on_progress(event: ProgressEvent) -> None:
            progress.append(event)

        finished = await pipeline.run(document.id, on_progress=on_progress)

        assert finished.status == "ready"
        assert await database.cards.count(space.id) == 1
        assert await database.entities.count(space.id) == 2
        meta = finished.meta.model_dump()
        assert "extraction_error" not in meta
        assert meta["extraction"]["cards_created"] == 1
        assert "extracting" in {event.stage for event in progress}
    finally:
        await database.close()


async def test_extracted_cards_are_recallable(
    database: Database,
    space: Space,
    registry: ProviderRegistry,
    settings: Settings,
    mock_reply: Callable[..., None],
) -> None:
    """抽取出的卡片写进 cards_vec 后能被检索管线召回（L2 进上下文）。

    故意再放一张置信度更高、但没有向量的卡片：召回如果退化成「按置信度兜底」，
    返回的就会是它而不是抽取出来的那张。
    """
    document = await _make_document(database, space, CHUNK_TEXTS)
    mock_reply(_sample_payload(["c2"]))
    extractor = await KnowledgeExtractor.for_space(
        space_id=space.id, database=database, registry=registry
    )
    await extractor.extract_document(document)
    await database.cards.create(
        KnowledgeCardCreate(
            space_id=space.id,
            kind="fact",
            title="没有向量的人工卡片",
            body="正文",
            confidence=1.0,
        )
    )

    pipeline = RetrievalPipeline(
        space_id=space.id,
        database=database,
        registry=registry,
        settings=settings,
        retrieval=RetrievalSettings(),
    )
    result = await pipeline.retrieve("attach 失败怎么办？")

    assert [card.title for card in result.cards] == ["ptrace 反调试会让 Frida attach 失败"]


async def test_pipeline_survives_extraction_failure(
    settings: Settings, dead_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """抽取失败不影响文档可用：状态仍是 ready，切片与向量都在。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, dead_registry, settings)
        document = await pipeline.register_text(
            space_id=space.id, title="脱壳笔记", content="\n\n".join(CHUNK_TEXTS)
        )

        finished = await pipeline.run(document.id)

        assert finished.status == "ready"
        assert finished.error is None
        assert finished.meta.model_dump().get("extraction_error")
        assert await database.chunks.count(space.id) > 0
        vectors = await database.require_vectors()
        assert await vectors.count("chunks_vec", space.id) > 0
        assert await database.cards.count(space.id) == 0
    finally:
        await database.close()


async def test_mention_count_does_not_drift_on_reextraction(
    settings: Settings, tmp_path: Path
) -> None:
    """实体提及次数按「哪篇文档提到过」记账，重复抽取不再累加。

    旧实现每抽取一次就 ``mention_count + 1``：同一篇文档重新解析两遍，图上节点
    的权重就翻倍。计数现在从提及明细重算，抽十遍也只算一次。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
        first = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="文档甲",
                source_type="paste",
                mime="text/markdown",
                sha256="e" * 64,
                size_bytes=3,
            )
        )
        second = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="文档乙",
                source_type="paste",
                mime="text/markdown",
                sha256="f" * 64,
                size_bytes=3,
            )
        )
        entity = await database.entities.create(
            EntityCreate(space_id=space.id, name="hERG", type="靶点", mention_count=0)
        )

        for _ in range(3):
            await database.entities.record_mentions(
                space_id=space.id, document_id=first.id, entity_ids=[entity.id]
            )
            await database.entities.refresh_mention_counts([entity.id])
        assert (await database.entities.require(entity.id)).mention_count == 1, (
            "同一篇文档抽三遍，计数仍然只算一次"
        )

        await database.entities.record_mentions(
            space_id=space.id, document_id=second.id, entity_ids=[entity.id]
        )
        await database.entities.refresh_mention_counts([entity.id])
        assert (await database.entities.require(entity.id)).mention_count == 2

        # 文档删了，它带来的提及也该跟着消失
        await database.documents.delete(second.id)
        await database.entities.refresh_mention_counts([entity.id])
        assert (await database.entities.require(entity.id)).mention_count == 1
    finally:
        await database.close()


async def test_extraction_batches_run_concurrently(settings: Settings, tmp_path: Path) -> None:
    """批次并发跑：一批要等一次模型调用（实测约 13 秒），串行跑长文档要四分多钟。

    断言的是「同时在飞」而不是耗时——耗时断言在 CI 上必然不稳定。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="并发", domain="药物发现"))
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="并发.md",
                source_type="paste",
                sha256="a" * 64,
            )
        )
        await database.chunks.create_many(
            [
                ChunkCreate(
                    space_id=space.id,
                    document_id=document.id,
                    ordinal=index,
                    content=f"第 {index} 段。" * 200,
                    char_start=index * 1000,
                    char_end=(index + 1) * 1000,
                )
                for index in range(16)
            ]
        )

        in_flight = 0
        peak = 0
        registry = MagicMock()
        route = MagicMock()

        async def chat(*args: object, **kwargs: object) -> MagicMock:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            result = MagicMock()
            result.content = '{"cards": [], "entities": [], "relations": []}'
            return result

        route.chat = chat
        registry.llm.return_value = route

        extractor = KnowledgeExtractor(
            space_id=space.id,
            database=database,
            registry=registry,
            persona=to_persona_spec(Persona(domain="药物发现")),
            settings=settings.model_copy(update={"extract_concurrency": 3}),
        )
        report = await extractor.extract_document(document)

        stats = report.documents[0]
        assert stats.batches >= 3, "夹具要能切出多批，否则测不到并发"
        assert peak > 1, "批次应当并发跑"
        assert peak <= 3, "并发不能超过配置上限"
    finally:
        await database.close()
