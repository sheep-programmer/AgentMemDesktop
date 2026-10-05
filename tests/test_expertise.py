"""专家度五维的口径测试。

重点在 groundedness：它是总分里权重第二大的一维（0.25），旧口径只看「这次检索
到证据了吗」，于是检索到一条不相关切片后凭模型自身知识作答的回答也能拿满分。
现在按「带引用的句子 / 总句子」算，空口作答必须如实拉低分数。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.expert.expertise import ExpertiseService
from agentmem.store import Database
from agentmem.types import (
    Citation,
    ConversationCreate,
    MessageCreate,
    MessageUpdate,
    Persona,
    SpaceCreate,
    TraceCreate,
)
from apps.api.main import create_app
from conftest import app_runtime


def _sentence_ends(content: str) -> list[int]:
    """每句话结束处的字符下标，与 ``split_sentences`` 的分段一致。"""
    from agentmem.prompts.budget import split_sentences

    ends: list[int] = []
    cursor = 0
    for sentence in split_sentences(content):
        cursor += len(sentence)
        ends.append(cursor)
    return ends


API = "/api/v1"

MODELS_YAML = """\
providers:
  - id: local-qwen
    kind: llm
    adapter: openai_compatible
    base_url: {base}
    model: mock-chat
  - id: local-embedding
    kind: embedding
    adapter: openai_compatible
    base_url: {base}
    model: mock-embed
    dimension: 8
roles:
  chat: local-qwen
  fast: local-qwen
  distill: local-qwen
  judge: local-qwen
  embedding: local-embedding
fallbacks: {{}}
"""


@pytest.fixture
def client(tmp_path: Path, mock_server: str) -> Iterator[TestClient]:
    """接上 mock provider 的应用实例：大纲生成要调模型。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
async def space_db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    yield database
    await database.close()


async def _add_answer(
    database: Database,
    space_id: str,
    *,
    content: str,
    markers: list[str],
    offsets: list[int | None] | None = None,
    retrieved: list[str] | None = None,
) -> None:
    """造一条「提问 → 回答 → 轨迹」的记录。

    ``content`` 是**剥离引用标记之后**的正文，引用位置由 ``offsets`` 给出——
    这正是线上数据的形状：``[^c1]`` 不会留在 ``messages.content`` 里，
    前端渲染的是 citation 芯片，所以判定「哪一句有出处」只能靠位置。
    """
    conversation = await database.conversations.create(
        ConversationCreate(space_id=space_id, title="问答")
    )
    message = await database.messages.create(
        MessageCreate(conversation_id=conversation.id, role="assistant", content=content)
    )
    if markers:
        positions = offsets if offsets is not None else [None] * len(markers)
        await database.messages.update(
            message.id,
            MessageUpdate(
                content=content,
                citations=[
                    Citation(
                        marker=marker,
                        chunk_id=f"chunk-{marker}",
                        document_id="doc-1",
                        snippet="证据片段",
                        char_offset=position,
                    )
                    for marker, position in zip(markers, positions, strict=True)
                ],
            ),
        )
    await database.traces.create(
        TraceCreate(
            space_id=space_id,
            conversation_id=conversation.id,
            message_id=message.id,
            query="问题",
            retrieved=[{"chunk_id": f"chunk-{marker}"} for marker in (retrieved or markers)],
        )
    )


async def test_groundedness_counts_sentences_with_citations(space_db: Database) -> None:
    """四句里两句带引用 → 50 分，而不是「检索到了证据」的满分。"""
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    content = "结论一是有据的。结论二也有据。这一句只是模型的自信。还有一句同样没有出处。"
    ends = _sentence_ends(content)
    await _add_answer(
        space_db,
        space.id,
        content=content,
        markers=["c1", "c2"],
        # 引用挂在第 1、2 句的句末
        offsets=[ends[0], ends[1]],
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(50.0)


async def test_retrieved_but_uncited_answer_scores_zero(space_db: Database) -> None:
    """检索到了证据但整篇没有引用：旧口径给 100，新口径必须是 0。"""
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    await _add_answer(
        space_db,
        space.id,
        content="这个问题我凭经验也能答。第二个论断同样没有出处。",
        markers=[],
        retrieved=["chunk-1"],
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(0.0)


async def test_uncited_answers_pull_groundedness_down(space_db: Database) -> None:
    """一条句句有出处、一条完全没引用 → 50 分，并报出样本数。

    此前没有任何引用的回答被整条跳过（和「没记位置的老数据」混成了一类），
    凭空作答不进分母，这一维只升不降：实测一个空间只剩一条带引用的回答时显示 100%。
    """
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    grounded = "这一句有出处。"
    await _add_answer(space_db, space.id, content=grounded, markers=["c1"], offsets=[len(grounded)])
    await _add_answer(
        space_db, space.id, content="这一句是模型自己说的。", markers=[], retrieved=["chunk-1"]
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(50.0)
    assert score.groundedness_samples == 2


async def test_hallucinated_marker_is_not_grounded(space_db: Database) -> None:
    """答案里写了标记、但没有解析出对应切片（幻觉编号）不算有据。"""
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    await _add_answer(
        space_db,
        space.id,
        content="这一段引用了并不存在的证据。",
        markers=[],
        retrieved=["chunk-1"],
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(0.0)


async def test_headings_are_not_counted_as_claims(space_db: Database) -> None:
    """标题行是 Markdown 结构，不该拉低分母。"""
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    content = "## 结论\n\n这一句有出处。\n\n## 依据\n"
    await _add_answer(
        space_db,
        space.id,
        content=content,
        markers=["c1"],
        offsets=[content.index("。") + 1],
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(100.0)


async def test_no_answer_history_scores_zero(space_db: Database) -> None:
    """没有回答时是 0 分，不是满分。"""
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(0.0)
    assert score.overall == pytest.approx(0.0)


def test_importing_expert_first_does_not_deadlock() -> None:
    """先 import expert 包也必须能跑通。

    expert 的评价器要用 ``evolve._persona`` 做 Persona 映射，evolve 的编排器要用
    expert 的两个服务，两个包在模块层互相 import 就是环：真实入口恰好先 import
    evolve 才没炸，换一个入口（比如单独跑一个专家度脚本）就 ImportError。
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import agentmem.expert.expertise; print('ok')"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


async def test_answers_without_recorded_positions_are_skipped(space_db: Database) -> None:
    """老数据没有记录引用位置时，整条回答不进分母。

    「没记位置」与「没有引用」是两回事：前者是数据缺口，把它算成 0 分会让这一维
    在升级后凭空塌下去。
    """
    space = await space_db.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = ExpertiseService(space_db, Persona())
    await _add_answer(
        space_db,
        space.id,
        content="这一段有引用，但位置没有记录。",
        markers=["c1"],
        offsets=[None],
    )

    score = await service.compute(space.id)

    assert score.groundedness == pytest.approx(0.0)


def test_outline_is_persisted_and_reused(client: TestClient, mock_reply: Any) -> None:
    """领域大纲生成一次就落库，之后打开盲区页不再调模型。

    此前每次 GET /expertise/gaps 都重新生成大纲：一次几秒的模型调用，provider 不通时
    连盲区都看不到。大纲本该是「建一次、偶尔手动更新」的东西。
    """
    mock_reply(
        json.dumps(
            {
                "outline": [
                    {"topic": "hERG 抑制", "subtopics": ["QT 间期"], "importance": "core"},
                    {"topic": "肝微粒体稳定性", "subtopics": ["CLint"], "importance": "core"},
                ]
            },
            ensure_ascii=False,
        )
    )
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]

    first = client.get(f"{API}/spaces/{space_id}/expertise/gaps")
    assert first.status_code == 200
    body = first.json()
    assert body["outline_size"] > 0, "第一次访问应当生成并落库一份大纲"
    assert body["outline_generated_at"] is not None
    generated_at = body["outline_generated_at"]

    # 第二次：读的是同一份，不再生成
    second = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert second["outline_generated_at"] == generated_at
    assert second["outline_size"] == body["outline_size"]

    refreshed = client.post(f"{API}/spaces/{space_id}/expertise/outline")
    assert refreshed.status_code == 200
    payload = refreshed.json()
    assert payload["total"] == payload["outline"]["nodes"].__len__()
    assert payload["covered"] <= payload["total"]


def test_coverage_uses_the_stored_outline(client: TestClient) -> None:
    """覆盖率用真口径：有卡片对应的节点 / 节点总数。

    没有大纲时才退回「卡片数对数缩放」的估计——那个只反映资料多少的单调趋势，
    不是覆盖率本身。
    """
    from agentmem.types import DomainOutlineCreate, OutlineNode

    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    client.post(
        f"{API}/spaces/{space_id}/cards",
        json={"kind": "fact", "title": "hERG 抑制", "body": "阻断会延长 QT 间期"},
    )

    runtime = app_runtime(client)
    import anyio

    async def seed_outline() -> None:
        database = await runtime.space_db(space_id)
        await database.outlines.create(
            DomainOutlineCreate(
                space_id=space_id,
                domain="药物发现",
                nodes=[
                    OutlineNode(topic="hERG 抑制", subtopics=["QT 间期"]),
                    OutlineNode(topic="肝微粒体稳定性"),
                    OutlineNode(topic="血浆蛋白结合"),
                    OutlineNode(topic="CYP 抑制"),
                ],
            )
        )

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(seed_outline)

    score = client.get(f"{API}/spaces/{space_id}/expertise").json()

    assert score["coverage"] == pytest.approx(25.0), "4 个节点里覆盖了 1 个"


REFUSAL = "我不知道「长大助手」这个领域具体指什么，没有相关资料可以参考。"


def test_outline_retries_once_when_model_refuses(client: TestClient, mock_reply: Any) -> None:
    """模型拒答（不给 JSON）时追问一次，追问后给出的大纲照常落库。

    实测：领域名是「长大助手」这种简称、角色描述里又写着「只依据资料作答」时，
    模型会回一句「没有相关资料」而不是大纲，用户只看到「没有返回可用的节点」。
    """
    good = json.dumps(
        {
            "domain_interpretation": "某高校的校园事务助手",
            "outline": [{"topic": "学籍管理", "subtopics": ["转专业"], "importance": "core"}],
        },
        ensure_ascii=False,
    )
    mock_reply(REFUSAL, [("没有按要求输出 JSON", good)])
    space_id = client.post(f"{API}/spaces", json={"name": "长大助手", "domain": "长大助手"}).json()[
        "id"
    ]

    refreshed = client.post(f"{API}/spaces/{space_id}/expertise/outline")
    assert refreshed.status_code == 200, refreshed.text
    payload = refreshed.json()
    assert [node["topic"] for node in payload["outline"]["nodes"]] == ["学籍管理"]
    assert payload["interpretation"] == "某高校的校园事务助手"
    gaps = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert gaps["outline_interpretation"] == "某高校的校园事务助手", "理解要落库，盲区页常驻展示"


def test_outline_failure_keeps_old_outline_and_says_why(
    client: TestClient, mock_reply: Any
) -> None:
    """追问后仍然拒答：旧大纲不动，错误信息里带上模型原话，而不是一句「没有可用节点」。"""
    good = json.dumps(
        {"outline": [{"topic": "学籍管理", "subtopics": [], "importance": "core"}]},
        ensure_ascii=False,
    )
    mock_reply(good)
    space_id = client.post(f"{API}/spaces", json={"name": "长大助手", "domain": "长大助手"}).json()[
        "id"
    ]
    assert client.post(f"{API}/spaces/{space_id}/expertise/outline").status_code == 200

    mock_reply(REFUSAL)
    failed = client.post(f"{API}/spaces/{space_id}/expertise/outline")
    assert failed.status_code == 422
    message = failed.json()["error"]["message"]
    assert "没有相关资料" in message, "要把模型原话带给用户"
    assert "原有大纲保持不变" in message

    gaps = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert gaps["outline_size"] == 1, "失败的刷新不能把原来的大纲冲掉"


def test_failed_auto_outline_is_not_retried_on_every_visit(
    client: TestClient, mock_reply: Any
) -> None:
    """打开盲区页自动生成大纲失败后，冷却期内再打开不再调模型，且要告诉用户原因。

    此前失败不落任何记号：每次打开都再等一轮（带追问是两轮）模型调用，页面照样空着。
    """
    mock_reply(REFUSAL)
    space_id = client.post(f"{API}/spaces", json={"name": "长大助手", "domain": "长大助手"}).json()[
        "id"
    ]
    first = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert first["outline_size"] == 0
    assert "没有相关资料" in (first["outline_error"] or "")

    # 模型现在能答了，但冷却期内自动路径不重跑——第二次打开不产生大纲
    good = json.dumps(
        {"outline": [{"topic": "学籍管理", "subtopics": [], "importance": "core"}]},
        ensure_ascii=False,
    )
    mock_reply(good)
    second = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert second["outline_size"] == 0
    assert second["outline_error"] == first["outline_error"]

    # 用户手动点「重新生成」不受冷却限制，成功后失败记号清掉
    assert client.post(f"{API}/spaces/{space_id}/expertise/outline").status_code == 200
    third = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert third["outline_size"] == 1
    assert third["outline_error"] is None
