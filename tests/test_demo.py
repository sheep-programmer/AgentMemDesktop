"""示例 Space：第一次打开应用就能看到有内容的界面。

这里守两件事：数据是**自洽**的（卡片挂在真实切片上、切片偏移能切回原文、
评测分数与指标对得上），以及它是**幂等**的（同名不会再建一个）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.space.demo import DEMO_SPACE_NAME, seed_demo_space
from apps.api.main import create_app

API = "/api/v1"

#: 只给 embedding，不给任何 LLM：示例 Space 的卖点就是「不配模型也能看」
MODELS_YAML = """\
providers:
  - id: local-embedding
    kind: embedding
    adapter: openai_compatible
    base_url: {base}
    model: mock-embed
    dimension: 8
roles:
  embedding: local-embedding
fallbacks: {{}}
"""


@pytest.fixture
def settings(tmp_path: Path, mock_server: str) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    return Settings(data_dir=data_dir, models_config=config_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_demo_space_is_populated_and_consistent(client: TestClient) -> None:
    """六个页面依赖的数据都要有，而且互相自洽。"""
    created = client.post(f"{API}/spaces/demo")
    assert created.status_code == 201, created.text
    space_id = created.json()["id"]
    assert created.json()["name"] == DEMO_SPACE_NAME

    documents = client.get(f"{API}/spaces/{space_id}/documents").json()
    assert documents["total"] == 3

    # 切片偏移必须能切回正文——示例数据同样要满足引用高亮的前提
    document_id = documents["items"][0]["id"]
    markdown = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/content").json()[
        "markdown"
    ]
    chunks = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=500").json()[
        "items"
    ]
    assert chunks
    bodies = [chunk for chunk in chunks if chunk["kind"] == "body"]
    summaries = [chunk for chunk in chunks if chunk["kind"] == "summary"]
    assert bodies, "正文切片"
    for chunk in bodies:
        assert markdown[chunk["char_start"] : chunk["char_end"]] == chunk["content"]
    # 示例 Space 刻意写死了文档级上下文，所以「文档概要」这条能力不依赖模型也能看到
    assert summaries, "示例文档应当带一条概要切片"
    assert all(chunk["char_start"] == chunk["char_end"] == 0 for chunk in summaries)

    cards = client.get(f"{API}/spaces/{space_id}/cards").json()
    assert cards["total"] >= 4
    assert all(card["source_chunks"] for card in cards["items"]), "卡片要挂在真实切片上"

    graph = client.get(f"{API}/spaces/{space_id}/graph").json()
    assert graph["nodes"], "知识图谱要有节点"
    assert graph["edges"], "知识图谱要有边"

    insights = client.get(f"{API}/spaces/{space_id}/insights").json()
    statuses = {item["status"] for item in insights["items"]}
    assert {"active", "candidate", "conflicted"} <= statuses, "三种状态各留一条，界面才有东西看"
    assert any(item["success_count"] > 0 for item in insights["items"])

    evals = client.get(f"{API}/spaces/{space_id}/evals").json()
    assert evals["total"] == 3
    runs = client.get(f"{API}/spaces/{space_id}/evals/runs").json()["runs"]
    assert len(runs) == 2
    scored = {run["variant"]: run["score"] for run in runs}
    assert scored["with_insights"] > scored["baseline"], "示例曲线要能说明经验有用"
    assert all(run["detail"]["metrics"]["audited"] for run in runs if run["detail"].get("metrics"))

    history = client.get(f"{API}/spaces/{space_id}/expertise/history").json()
    assert len(history["snapshots"]) >= 5, "成长曲线要有多个点"
    scores = [item["overall"] for item in history["snapshots"]]
    assert scores == sorted(scores), "示例曲线是上扬的，顺序不能乱"

    # 曲线的终点必须等于页面顶部那个实时分数，否则示例数据自相矛盾：
    # 顶部显示 55、曲线显示 41，用户第一次打开就撞见
    live = client.get(f"{API}/spaces/{space_id}/expertise").json()
    assert scores[-1] == pytest.approx(live["overall"], abs=0.01)
    assert live["groundedness"] > 0, "示例回答里带了引用，依归度不该是 0"

    # 置信度流水：示例数据里也要有一段「它是怎么走到今天的」
    active = [item for item in insights["items"] if item["status"] == "active"]
    assert active, "示例 Space 至少要有一条生效经验"
    history = client.get(f"{API}/spaces/{space_id}/insights/{active[0]['id']}/history").json()
    assert len(history["events"]) >= 4, "示例经验的变更流水要有几笔，界面才有轨迹可画"
    # 流水终点必须等于这张卡片当前的置信度：对不上就是示例数据自相矛盾
    assert history["events"][-1]["confidence_after"] == pytest.approx(
        history["insight"]["confidence"], abs=0.01
    )

    # 领域大纲是写死的：覆盖率的分子分母与知识盲区在演示里就是真口径
    gaps = client.get(f"{API}/spaces/{space_id}/expertise/gaps").json()
    assert gaps["outline_size"] == 8, "示例 Space 内置一份 8 节点的领域大纲"
    assert gaps["outline_generated_at"] is not None
    assert gaps["gaps"], "刻意留几个未覆盖的主题，盲区页才有内容"

    # 一条「被注入够多却很少收到好评」的经验：界面上才有东西可复查
    review = client.get(f"{API}/spaces/{space_id}/insights/review").json()
    assert review["items"], "示例 Space 要留一条值得复查的经验"
    assert review["items"][0]["success_rate"] < review["max_success_rate"]

    pending = client.get(f"{API}/spaces/{space_id}/evolve/pending").json()
    assert pending["pending_count"] >= 1, "Evolve 页要有可蒸馏的反馈"

    persona = client.get(f"{API}/spaces/{space_id}/persona").json()
    assert persona["domain"] == "小分子药物发现（ADMET 早期评价）"


def test_demo_space_is_idempotent(settings: Settings) -> None:
    """同名 Space 已经存在时直接复用，不会建出第二个示例。"""
    import asyncio

    from agentmem.space.runtime import Runtime

    app = create_app(settings)

    async def scenario() -> tuple[str, str, int]:
        async with app.router.lifespan_context(app):
            runtime: Runtime = app.state.runtime
            first = await seed_demo_space(runtime)
            second = await seed_demo_space(runtime)
            spaces = await runtime.spaces.list_spaces()
            return first.id, second.id, len(spaces)

    first_id, second_id, count = asyncio.run(scenario())

    assert first_id == second_id
    assert count == 1
