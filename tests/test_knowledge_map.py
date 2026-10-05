"""知识图谱接口：网络结构、主题掌握度、进化脉络。

最要紧的是「主题是否覆盖」必须与专家度 coverage 同一口径——图上某主题挂着卡片、
专家度却说它没覆盖，用户会直接怀疑两边哪个是假的。所以这里同时跑专家度，逐项对账。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.expert.expertise import ExpertiseService
from agentmem.expert.knowledge_map import (
    CARD_SATURATION,
    KnowledgeMapResponse,
    build_knowledge_map,
    topic_mastery,
)
from agentmem.store import Database
from agentmem.types import (
    ChunkCreate,
    DocumentCreate,
    DocumentStatus,
    DomainOutlineCreate,
    EvolutionRunCreate,
    ExpertiseSnapshotCreate,
    InsightCreate,
    InsightEventCreate,
    KnowledgeCard,
    KnowledgeCardCreate,
    OutlineNode,
    Persona,
    Space,
    TraceCreate,
)
from apps.api.main import create_app

API = "/api/v1"
SPACE_ID = "space-km"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", SPACE_ID)
    await database.open()
    # 每个 Space 的库里都镜像着一行自己的 spaces 记录，文档等表的外键指向它
    await database.sqlite.execute(
        "INSERT INTO spaces (id, name, domain, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (SPACE_ID, "新药研发", "药物发现", 1_000, 1_000),
    )
    yield database
    await database.close()


@pytest.fixture
def space() -> Space:
    return Space(
        id=SPACE_ID,
        name="新药研发",
        domain="药物发现",
        description="**早期**药物发现的 ADMET 评估",
        created_at=1_000,
        updated_at=1_000,
    )


async def _document(
    db: Database, title: str, *, status: DocumentStatus = "ready", error: str | None = None
) -> tuple[str, list[str]]:
    """建一份文档和两条切片，返回 ``(文档 id, 切片 id)``。"""
    document = await db.documents.create(
        DocumentCreate(
            space_id=SPACE_ID,
            title=title,
            source_type="paste",
            sha256=f"sha-{title}",
            status=status,
            error=error,
        )
    )
    chunks = await db.chunks.create_many(
        [
            ChunkCreate(
                space_id=SPACE_ID,
                document_id=document.id,
                ordinal=index,
                content=f"{title} {index}",
            )
            for index in range(2)
        ]
    )
    return document.id, [chunk.id for chunk in chunks]


async def _card(
    db: Database,
    title: str,
    *,
    confidence: float = 0.5,
    aliases: list[str] | None = None,
    chunks: list[str] | None = None,
) -> KnowledgeCard:
    return await db.cards.create(
        KnowledgeCardCreate(
            space_id=SPACE_ID,
            kind="fact",
            title=title,
            body=f"# {title}\n\n关于 `{title}` 的正文，[链接](http://x) 一段。",
            aliases=aliases or [],
            source_chunks=chunks or [],
            confidence=confidence,
        )
    )


async def _outline(db: Database, nodes: list[OutlineNode]) -> None:
    await db.outlines.create(DomainOutlineCreate(space_id=SPACE_ID, domain="药物发现", nodes=nodes))


async def _set_time(db: Database, table: str, row_id: str, at: int) -> None:
    """仓库落库时用的是当前时间；时间线用例要自己摆时间。"""
    await db.sqlite.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (at, row_id))


def _assert_well_formed(result: KnowledgeMapResponse) -> None:
    """图的基本不变量：id 不重复、连线两端都在、没有飘着的节点。"""
    ids = [node.id for node in result.nodes]
    assert len(ids) == len(set(ids))
    assert sum(1 for node in result.nodes if node.kind == "domain") == 1
    known = set(ids)
    for edge in result.edges:
        assert edge.src in known and edge.dst in known, edge
    touched = {edge.src for edge in result.edges} | {edge.dst for edge in result.edges}
    domain_id = f"domain:{SPACE_ID}"
    floating = [node_id for node_id in ids if node_id not in touched and node_id != domain_id]
    assert floating == []
    for node in result.nodes:
        assert node.detail is None or len(node.detail) <= 120
        assert node.mastery is None or 0.0 <= node.mastery <= 1.0
    times = [milestone.at for milestone in result.milestones]
    assert times == sorted(times)


async def test_empty_space_returns_a_minimal_map(db: Database, space: Space) -> None:
    """没大纲、没资料：只有一个领域节点，总评为空而不是 0。"""
    result = await build_knowledge_map(db, space)

    assert [node.kind for node in result.nodes] == ["domain"]
    domain = result.nodes[0]
    assert domain.id == f"domain:{SPACE_ID}"
    assert domain.ref_id == SPACE_ID
    assert domain.label == "新药研发"
    assert domain.mastery is None
    assert domain.detail == "早期药物发现的 ADMET 评估", "Markdown 标记要剥成纯文本"
    assert result.edges == []
    assert result.topics == []
    assert result.milestones == []
    assert result.truncated is False
    assert result.stats.model_dump() == {
        "documents": 0,
        "cards": 0,
        "insights_active": 0,
        "insights_candidate": 0,
        "topics_total": 0,
        "topics_covered": 0,
        "overall": None,
    }
    assert result.space.model_dump() == {"id": SPACE_ID, "name": "新药研发", "domain": "药物发现"}


async def test_structure_and_source_edges(db: Database, space: Space) -> None:
    """卡片经来源切片连回文档；失败文档也在图上，并带上失败原因。"""
    good, good_chunks = await _document(db, "ADMET 指南")
    other, other_chunks = await _document(db, "药代综述")
    failed, _ = await _document(db, "坏文件", status="failed", error="解析失败：PDF 加密")
    both = await _card(db, "hERG 抑制", confidence=0.9, chunks=[good_chunks[0], other_chunks[1]])
    single = await _card(db, "肝微粒体稳定性", confidence=0.6, chunks=[good_chunks[1]])
    manual = await _card(db, "手写笔记", confidence=0.4)
    await _outline(db, [OutlineNode(topic="hERG 抑制")])

    result = await build_knowledge_map(db, space)
    _assert_well_formed(result)

    kinds = Counter(node.kind for node in result.nodes)
    assert kinds == {"domain": 1, "topic": 1, "document": 3, "card": 3}
    sources = {(edge.src, edge.dst) for edge in result.edges if edge.kind == "source"}
    assert (f"doc:{good}", f"card:{both.id}") in sources
    assert (f"doc:{other}", f"card:{both.id}") in sources, "多来源卡片每份文档都要连"
    assert (f"doc:{good}", f"card:{single.id}") in sources
    for document_id in (good, other, failed):
        assert (f"domain:{SPACE_ID}", f"doc:{document_id}") in sources
    covers = {(edge.src, edge.dst) for edge in result.edges if edge.kind == "covers"}
    assert ("topic:0", f"card:{both.id}") in covers
    # 没主题、没来源文档的卡片兜底挂到领域上
    assert (f"domain:{SPACE_ID}", f"card:{manual.id}") in covers
    assert (f"domain:{SPACE_ID}", f"card:{single.id}") not in covers, "有来源文档就不必兜底"

    by_id = {node.id: node for node in result.nodes}
    failed_node = by_id[f"doc:{failed}"]
    assert failed_node.status == "failed"
    assert failed_node.detail == "解析失败：PDF 加密"
    assert failed_node.ref_id == failed
    card_node = by_id[f"card:{both.id}"]
    assert card_node.mastery == pytest.approx(0.9)
    assert card_node.ref_id == both.id
    assert (
        card_node.detail is not None and "#" not in card_node.detail and "`" not in card_node.detail
    )
    assert "链接" in card_node.detail and "http" not in card_node.detail
    # 产出卡片最多的文档最显著，没产出的也保留一个下限
    assert by_id[f"doc:{good}"].weight == pytest.approx(1.0)
    assert by_id[f"doc:{failed}"].weight == pytest.approx(0.2)

    assert result.stats.documents == 3
    assert result.stats.cards == 3
    # 只有就绪的文档算「导入」里程碑
    assert [m.kind for m in result.milestones] == ["document", "document"]


async def test_topic_coverage_matches_expertise(db: Database, space: Space) -> None:
    """主题覆盖与专家度 coverage 逐项一致；掌握度 = 数量饱和 × 平均置信度。"""
    await _card(db, "hERG 抑制", confidence=0.9)
    await _card(db, "QT 间期延长", confidence=0.6)
    await _card(db, "CYP3A4", confidence=0.8, aliases=["CYP 抑制"])
    await _card(db, "CYP2D6 抑制", confidence=0.5, aliases=["CYP 抑制"])
    await _card(db, "CYP 诱导", confidence=0.2)
    await _card(db, "毫不相干", confidence=1.0)
    nodes = [
        OutlineNode(topic="心脏毒性", subtopics=["hERG", "QT 间期"], importance="core"),
        OutlineNode(topic="CYP", importance="common"),
        OutlineNode(topic="血浆蛋白结合", subtopics=["白蛋白"], importance="advanced"),
    ]
    await _outline(db, nodes)

    result = await build_knowledge_map(db, space)
    _assert_well_formed(result)

    service = ExpertiseService(db, Persona())
    covered, total = await service.covered_node_count(SPACE_ID)
    assert (result.stats.topics_covered, result.stats.topics_total) == (covered, total) == (2, 3)
    score = await service.compute(SPACE_ID)
    assert score.coverage == pytest.approx(covered / total * 100, abs=0.01)

    heart, cyp, plasma = result.topics
    assert (heart.covered, heart.card_count) == (True, 2)
    assert heart.mastery == pytest.approx(round(2 / CARD_SATURATION * (0.9 + 0.6) / 2, 3))
    assert (cyp.covered, cyp.card_count) == (True, 3), "主题词出现在标题或别名里都算"
    assert cyp.mastery == pytest.approx(round((0.8 + 0.5 + 0.2) / 3, 3)), "满 3 张后只看质量"
    assert (plasma.covered, plasma.card_count, plasma.mastery) == (False, 0, 0.0)
    assert result.stats.overall == pytest.approx(round((heart.mastery + cyp.mastery + 0.0) / 3, 3))

    by_id = {node.id: node for node in result.nodes}
    assert by_id["topic:0"].status == "covered"
    assert by_id["topic:2"].status == "uncovered"
    assert by_id["topic:0"].weight > by_id["topic:1"].weight > by_id["topic:2"].weight
    assert by_id[f"domain:{SPACE_ID}"].mastery == result.stats.overall
    covers = Counter(edge.src for edge in result.edges if edge.kind == "covers")
    assert covers["topic:0"] == 2 and covers["topic:1"] == 3 and covers["topic:2"] == 0
    assert [edge.dst for edge in result.edges if edge.kind == "topic"] == [
        "topic:0",
        "topic:1",
        "topic:2",
    ]


def test_topic_mastery_saturates() -> None:
    """数量项封顶：一百张低置信卡片刷不满掌握度。"""

    def cards(count: int, confidence: float) -> list[KnowledgeCard]:
        return [
            KnowledgeCard(
                id=f"c{index}",
                space_id=SPACE_ID,
                kind="fact",
                title="t",
                body="b",
                confidence=confidence,
                created_at=1,
                updated_at=1,
            )
            for index in range(count)
        ]

    assert topic_mastery([]) == 0.0
    assert topic_mastery(cards(1, 0.9)) == pytest.approx(0.3)
    assert topic_mastery(cards(100, 0.3)) == pytest.approx(0.3)
    assert topic_mastery(cards(3, 1.0)) == pytest.approx(1.0)


async def test_cards_are_truncated_but_still_count_toward_mastery(
    db: Database, space: Space
) -> None:
    """画布只放得下前 N 张，但掌握度按全部卡片算。"""
    for index in range(5):
        await _card(db, f"CYP 卡片 {index}", confidence=0.5 + index * 0.1)
    await _outline(db, [OutlineNode(topic="CYP")])

    result = await build_knowledge_map(db, space, max_cards=2)
    _assert_well_formed(result)

    shown = [node for node in result.nodes if node.kind == "card"]
    assert [node.label for node in shown] == ["CYP 卡片 4", "CYP 卡片 3"], "按置信度取前 N"
    assert result.truncated is True
    assert result.stats.cards == 5
    assert result.topics[0].card_count == 5
    assert sum(1 for edge in result.edges if edge.kind == "covers") == 2

    full = await build_knowledge_map(db, space, max_cards=5000)
    assert full.truncated is False
    assert sum(1 for node in full.nodes if node.kind == "card") == 5

    clamped = await build_knowledge_map(db, space, max_cards=0)
    assert sum(1 for node in clamped.nodes if node.kind == "card") == 1


async def test_insights_link_through_their_source_conversation(db: Database, space: Space) -> None:
    """经验经「来源对话 → 那次用到的卡片」连到卡片；找不到来源就挂到领域。归档的不画。"""
    card = await _card(db, "hERG 抑制", confidence=0.9)
    trace = await db.traces.create(
        TraceCreate(
            space_id=SPACE_ID,
            conversation_id="conv-1",
            message_id="msg-1",
            query="hERG 阈值是多少",
            used_cards=[card.id, "card-deleted"],
        )
    )
    linked = await db.insights.create(
        InsightCreate(
            space_id=SPACE_ID,
            trigger="用户问 hERG 阈值时",
            guidance="同时给出 30 μM 与 50 μM 两级口径",
            kind="correction",
            origin="user_correction",
            status="active",
            confidence=0.8,
            source_trace_ids=[trace.id],
        )
    )
    orphan = await db.insights.create(
        InsightCreate(
            space_id=SPACE_ID,
            trigger="来源对话已删除" * 10,
            guidance="g",
            kind="heuristic",
            origin="negative_feedback",
            status="candidate",
            source_trace_ids=["trace-gone"],
        )
    )
    archived = await db.insights.create(
        InsightCreate(
            space_id=SPACE_ID,
            trigger="已归档",
            guidance="g",
            kind="heuristic",
            origin="manual",
            status="archived",
        )
    )

    result = await build_knowledge_map(db, space)
    _assert_well_formed(result)

    insight_nodes = {node.ref_id: node for node in result.nodes if node.kind == "insight"}
    assert set(insight_nodes) == {linked.id, orphan.id}
    assert archived.id not in insight_nodes
    assert insight_nodes[linked.id].status == "active"
    assert insight_nodes[linked.id].mastery == pytest.approx(0.8)
    assert insight_nodes[orphan.id].status == "candidate"
    assert len(insight_nodes[orphan.id].label) <= 48
    edges = {(edge.src, edge.dst) for edge in result.edges if edge.kind == "insight"}
    assert edges == {
        (f"card:{card.id}", f"insight:{linked.id}"),
        (f"domain:{SPACE_ID}", f"insight:{orphan.id}"),
    }
    assert (result.stats.insights_active, result.stats.insights_candidate) == (1, 1)


async def test_milestones_tell_the_growth_story(db: Database, space: Space) -> None:
    """导入 → 经验生效 → 进化 → 快照，按时间排；进化自带的那张快照不重复画。"""
    document_id, _ = await _document(db, "ADMET 指南")
    await _set_time(db, "documents", document_id, 1_000)
    insight = await db.insights.create(
        InsightCreate(
            space_id=SPACE_ID,
            trigger="用户问 hERG 阈值时",
            guidance="g",
            kind="correction",
            origin="user_correction",
            status="active",
            confidence=0.6,
        )
    )
    promoted = await db.insight_events.add(
        InsightEventCreate(
            insight_id=insight.id,
            space_id=SPACE_ID,
            event="eval_improved",
            confidence_before=0.3,
            confidence_after=0.6,
            status_before="candidate",
            status_after="active",
        )
    )
    await _set_time(db, "insight_events", promoted.id, 2_000)
    bump = await db.insight_events.add(
        InsightEventCreate(
            insight_id=insight.id,
            space_id=SPACE_ID,
            event="positive_feedback",
            confidence_before=0.6,
            confidence_after=0.65,
            status_before="active",
            status_after="active",
        )
    )
    await _set_time(db, "insight_events", bump.id, 2_500)
    scores = {
        "coverage": 10.0,
        "accuracy": 50.0,
        "consistency": 0.0,
        "groundedness": 40.0,
        "insight_density": 5.0,
    }
    run_snapshot = await db.expertise.create(
        ExpertiseSnapshotCreate(space_id=SPACE_ID, overall=42.5, **scores)
    )
    await _set_time(db, "expertise_snapshots", run_snapshot.id, 2_999)
    run = await db.evolution.create(
        EvolutionRunCreate(
            space_id=SPACE_ID, produced=2, promoted=1, expertise_before=40.0, expertise_after=42.5
        )
    )
    await _set_time(db, "evolution_runs", run.id, 3_000)
    standalone = await db.expertise.create(
        ExpertiseSnapshotCreate(space_id=SPACE_ID, overall=45.0, **scores)
    )
    await _set_time(db, "expertise_snapshots", standalone.id, 900_000)

    result = await build_knowledge_map(db, space)
    _assert_well_formed(result)

    timeline = [(m.at, m.kind, m.value) for m in result.milestones]
    assert timeline == [
        (1_000, "document", None),
        (2_000, "insight", 0.6),
        (3_000, "evolution", 42.5),
        (900_000, "snapshot", 45.0),
    ]
    labels = [m.label for m in result.milestones]
    assert labels[0] == "导入《ADMET 指南》"
    assert labels[1] == "经验生效：用户问 hERG 阈值时"
    assert labels[2] == "进化：新增候选 2 · 晋升 1"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir, models_config=tmp_path / "models.yaml")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_endpoint_returns_the_map(client: TestClient) -> None:
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    client.post(
        f"{API}/spaces/{space_id}/cards",
        json={"kind": "fact", "title": "hERG 抑制", "body": "阻断会延长 QT 间期"},
    )

    response = client.get(f"{API}/spaces/{space_id}/knowledge-map")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"space", "nodes", "edges", "topics", "milestones", "stats", "truncated"}
    assert body["space"]["id"] == space_id
    kinds = Counter(node["kind"] for node in body["nodes"])
    assert kinds == {"domain": 1, "card": 1}
    assert body["stats"]["cards"] == 1
    assert body["stats"]["overall"] is None

    # 越界的 max_cards 夹紧，不报 422
    assert client.get(f"{API}/spaces/{space_id}/knowledge-map?max_cards=0").status_code == 200
    assert client.get(f"{API}/spaces/{space_id}/knowledge-map?max_cards=99999").status_code == 200


def test_unknown_space_is_404(client: TestClient) -> None:
    response = client.get(f"{API}/spaces/01NOPE0000000000000000000/knowledge-map")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_cards_attach_to_topics_by_meaning_not_only_verbatim() -> None:
    """大纲写概括说法、卡片写具体条目时，逐字匹配一条都对不上，要按语义归属。

    每张卡片只归到最像的那一个主题；最像的也不够像（低于门槛）就不归属，
    向量化失败时退回逐字匹配而不是报错。
    """
    from agentmem.expert.expertise import match_outline_semantic
    from agentmem.types import OutlineNode

    nodes = [
        OutlineNode(topic="故障诊断与应急处理", subtopics=["电池类故障处理（过温、过压）"]),
        OutlineNode(topic="质保与售后", subtopics=["质保条款"]),
    ]
    cards = [
        KnowledgeCard(
            id=card_id,
            space_id=SPACE_ID,
            kind="fact",
            title=title,
            body=title,
            created_at=0,
            updated_at=0,
        )
        for card_id, title in (("c1", "E07 过温保护的触发条件"), ("c2", "安装环境条件要求"))
    ]
    axes = {
        "故障诊断与应急处理": [1.0, 0.0, 0.0],
        "电池类故障处理（过温、过压）": [0.9, 0.1, 0.0],
        "质保与售后": [0.0, 1.0, 0.0],
        "质保条款": [0.0, 0.9, 0.1],
        "E07 过温保护的触发条件": [0.95, 0.05, 0.0],
        "安装环境条件要求": [0.3, 0.3, 0.9],
    }

    async def embed(texts: list[str]) -> list[list[float]]:
        return [axes[text] for text in texts]

    matches = await match_outline_semantic(nodes, cards, embed, cache_key="test-axes")
    assert [[card.id for card in hits] for hits in matches] == [["c1"], []]

    async def broken(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding 服务不通")

    fallback = await match_outline_semantic(nodes, cards, broken, cache_key="test-broken")
    assert fallback == [[], []]
