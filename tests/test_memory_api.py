"""§6 记忆 L2 的 HTTP 行为：卡片 CRUD、图谱查询与抽取 SSE。

SSE 用例直接消费路由返回的 ``body_iterator``：``httpx.ASGITransport`` 会等整个
ASGI 应用跑完才返回响应对象，对长流是死锁，而 starlette 的 ``TestClient`` 在流
未关闭时从同一线程再发请求会与它的 portal 互锁。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.errors import NotFoundError
from agentmem.types import CardExtractRequest, ChunkCreate, DocumentCreate, SpaceCreate
from apps.api.main import create_app
from apps.api.routers.memory import extract_cards
from conftest import app_runtime

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

DEAD_MODELS_YAML = """\
providers:
  - id: dead-llm
    kind: llm
    adapter: openai_compatible
    base_url: http://127.0.0.1:1/v1
    model: nope
  - id: local-embedding
    kind: embedding
    adapter: openai_compatible
    base_url: {base}
    model: mock-embed
    dimension: 8
roles:
  fast: dead-llm
  embedding: local-embedding
fallbacks: {{}}
"""

REPLY: dict[str, object] = {
    "cards": [
        {
            "kind": "procedure",
            "title": "脱壳流程",
            "body": "先定位 DexClassLoader，再 dump 内存中的 dex。",
            "aliases": ["脱壳步骤"],
            "source_chunk_markers": ["c1"],
            "confidence": 0.8,
        }
    ],
    "entities": [
        {"name": "DexClassLoader", "type": "类", "summary": "动态加载 dex 的入口"},
        {"name": "脱壳", "type": "技术", "summary": "从加固包还原原始 dex"},
    ],
    "relations": [
        {
            "src": "脱壳",
            "dst": "DexClassLoader",
            "predicate": "依赖",
            "source_chunk_markers": ["c1"],
        }
    ],
}

PASTE_CONTENT = "# 脱壳\n\n加固 APK 先定位 DexClassLoader 调用点，再 dump 内存中的 dex。"


def _write_settings(tmp_path: Path, mock_server: str, template: str = MODELS_YAML) -> Settings:
    """把模型配置写到临时目录并返回设置。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(template.format(base=mock_server), encoding="utf-8")
    return Settings(data_dir=data_dir, models_config=config_path)


@pytest.fixture
def settings(tmp_path: Path, mock_server: str) -> Settings:
    """数据目录与模型配置都指向临时位置，模型指向 Mock 服务。"""
    return _write_settings(tmp_path, mock_server)


@pytest.fixture
def dead_settings(tmp_path: Path, mock_server: str) -> Settings:
    """LLM 指向死端口，embedding 仍可用。"""
    return _write_settings(tmp_path, mock_server, DEAD_MODELS_YAML)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """启动应用（含 lifespan）。"""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _make_space(client: TestClient) -> str:
    """建一个 Space 并返回 id。"""
    response = client.post(f"{API}/spaces", json={"name": "逆向", "domain": "Android"})
    return str(response.json()["id"])


def _wait_for_status(client: TestClient, space_id: str, document_id: str, targets: set[str]) -> str:
    """轮询文档状态直到进入目标状态之一。

    摄取跑在后台任务里，没有同步等待的接口；轮询也是前端的真实用法。
    """
    deadline = time.monotonic() + 20.0
    status = ""
    while time.monotonic() < deadline:
        status = str(
            client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["status"]
        )
        if status in targets:
            return status
        time.sleep(0.05)
    raise AssertionError(f"等待文档状态超时，当前为 {status}")


async def _consume(response: StreamingResponse) -> list[str]:
    """把 SSE 响应体读成原始帧文本。"""
    frames: list[str] = []
    async for chunk in response.body_iterator:
        frames.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
    return frames


def _done_payload(frames: list[str]) -> dict[str, Any]:
    """从帧文本里取出 ``done`` 事件的载荷。"""
    for frame in frames:
        if frame.startswith("event: done"):
            body = frame.split("data: ", 1)[1].strip()
            payload: dict[str, Any] = json.loads(body)
            return payload
    raise AssertionError(f"没有 done 事件：{frames}")


# ---------------------------------------------------------------------------
# 卡片 CRUD
# ---------------------------------------------------------------------------


def test_card_crud(client: TestClient) -> None:
    """手动新建、筛选、编辑、删除卡片。"""
    space_id = _make_space(client)

    created = client.post(
        f"{API}/spaces/{space_id}/cards",
        json={
            "kind": "fact",
            "title": "DexClassLoader 的构造参数",
            "body": "第二个参数是 dex 的输出目录。",
            "aliases": ["classloader"],
            "confidence": 0.6,
        },
    )
    assert created.status_code == 201
    card = created.json()
    # 人工新建即视为用户已核验：模型抽取不该盖掉人写的内容。
    # 置信度显式给了 0.6 就按给的来，不擅自拉满——「谁核验的」与「多确定」是两件事。
    assert card["verified_by"] == "user"
    assert card["confidence"] == 0.6
    assert card["source_chunks"] == []

    assert client.get(f"{API}/spaces/{space_id}/cards").json()["total"] == 1
    assert (
        client.get(f"{API}/spaces/{space_id}/cards", params={"kind": "pitfall"}).json()["total"]
        == 0
    )
    assert (
        client.get(f"{API}/spaces/{space_id}/cards", params={"q": "classloader"}).json()["total"]
        == 1
    )
    assert (
        client.get(f"{API}/spaces/{space_id}/cards", params={"min_confidence": 0.9}).json()["total"]
        == 0
    )

    edited = client.patch(
        f"{API}/spaces/{space_id}/cards/{card['id']}",
        json={"body": "第二个参数是 dex 的输出目录，缺失时抛异常。"},
    )
    assert edited.status_code == 200
    assert edited.json()["verified_by"] == "user"
    assert edited.json()["confidence"] == 1.0
    assert edited.json()["body"].startswith("第二个参数是 dex 的输出目录，缺失时")

    deleted = client.delete(f"{API}/spaces/{space_id}/cards/{card['id']}")
    assert deleted.json()["deleted"] is True
    assert (
        client.patch(f"{API}/spaces/{space_id}/cards/{card['id']}", json={"body": "x"}).status_code
        == 404
    )


def test_card_routes_require_existing_space(client: TestClient) -> None:
    """Space 不存在时按 404 处理。"""
    assert client.get(f"{API}/spaces/nope/cards").status_code == 404


# ---------------------------------------------------------------------------
# 摄取后的卡片与图谱
# ---------------------------------------------------------------------------


def test_paste_extracts_then_graph(client: TestClient, mock_reply: Callable[..., None]) -> None:
    """粘贴入库时抽取阶段就会产出卡片；再手动抽一次不产生重复。"""
    mock_reply(json.dumps(REPLY, ensure_ascii=False))
    space_id = _make_space(client)
    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "脱壳笔记", "content": PASTE_CONTENT},
    )
    document_id = created.json()["id"]
    assert _wait_for_status(client, space_id, document_id, {"ready", "failed"}) == "ready"

    cards = client.get(f"{API}/spaces/{space_id}/cards").json()
    assert cards["total"] == 1
    assert cards["items"][0]["title"] == "脱壳流程"
    assert cards["items"][0]["source_chunks"]

    graph = client.get(f"{API}/spaces/{space_id}/graph").json()
    assert {node["name"] for node in graph["nodes"]} == {"DexClassLoader", "脱壳"}
    assert graph["edges"][0]["predicate"] == "依赖"
    endpoints = {graph["edges"][0]["src"], graph["edges"][0]["dst"]}
    assert endpoints <= {node["id"] for node in graph["nodes"]}

    # 手动再抽一次：同一张卡片按 title + kind 合并，不新增
    response = client.post(
        f"{API}/spaces/{space_id}/cards/extract", json={"document_ids": [document_id]}
    )
    assert response.status_code == 200
    assert "event: done" in response.text
    assert client.get(f"{API}/spaces/{space_id}/cards").json()["total"] == 1

    centered = client.get(
        f"{API}/spaces/{space_id}/graph", params={"center": "脱壳", "depth": 1}
    ).json()
    assert {node["name"] for node in centered["nodes"]} == {"DexClassLoader", "脱壳"}

    assert (
        client.get(f"{API}/spaces/{space_id}/graph", params={"center": "查无此物"}).status_code
        == 404
    )
    assert client.get(f"{API}/spaces/{space_id}/graph", params={"depth": 9}).status_code == 422


def test_graph_is_empty_before_any_extraction(client: TestClient) -> None:
    """没有实体时返回空图而不是报错。"""
    space_id = _make_space(client)
    graph = client.get(f"{API}/spaces/{space_id}/graph").json()
    assert graph["nodes"] == [] and graph["edges"] == []
    assert graph["total_nodes"] == 0 and graph["truncated"] is False


def test_extract_route_declares_event_stream(client: TestClient) -> None:
    """抽取路由在 OpenAPI 里声明为 text/event-stream。"""
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/spaces/{space_id}/cards/extract"]["post"]
    assert "text/event-stream" in operation["responses"]["200"]["content"]


# ---------------------------------------------------------------------------
# 抽取 SSE
# ---------------------------------------------------------------------------


async def _seed_document(app: FastAPI) -> tuple[str, str]:
    """建一个 Space 与一篇已就绪的文档，返回 (space_id, document_id)。"""
    runtime = app.state.runtime
    space = await runtime.spaces.create_space(SpaceCreate(name="逆向", domain="Android"))
    database = await runtime.spaces.space_db(space.id)
    document = await database.documents.create(
        DocumentCreate(
            space_id=space.id,
            title="脱壳笔记",
            source_type="paste",
            sha256="sha-1",
            status="ready",
        )
    )
    await database.chunks.create_many(
        [
            ChunkCreate(
                space_id=space.id,
                document_id=document.id,
                ordinal=0,
                content="加固 APK 先定位 DexClassLoader 调用点。",
            )
        ]
    )
    return space.id, document.id


async def test_extract_stream_writes_cards(
    settings: Settings, mock_reply: Callable[..., None]
) -> None:
    """抽取接口按 progress → done 推送，并把卡片与图谱写进库。"""
    mock_reply(json.dumps(REPLY, ensure_ascii=False))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, document_id = await _seed_document(app)
        response = await extract_cards(
            space_id, CardExtractRequest(document_ids=[document_id]), runtime
        )
        frames = await _consume(cast(StreamingResponse, response))
        payload = _done_payload(frames)
        database = await runtime.spaces.space_db(space_id)
        cards, total, _ = await database.cards.list_by_space(space_id)

    assert payload["cards_created"] == 1
    assert payload["entities_created"] == 2
    assert payload["relations_created"] == 1
    assert payload["failures"] == []
    assert total == 1
    assert cards[0].title == "脱壳流程"
    body = "".join(frames)
    assert "event: progress" in body
    assert '"stage":"extracting"' in body.replace(" ", "")


async def test_extract_stream_reports_provider_failure(dead_settings: Settings) -> None:
    """模型不可用时发 error 事件并记进报告，流本身正常收尾。"""
    app = create_app(dead_settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, document_id = await _seed_document(app)
        response = await extract_cards(
            space_id, CardExtractRequest(document_ids=[document_id]), runtime
        )
        frames = await _consume(cast(StreamingResponse, response))
        database = await runtime.spaces.space_db(space_id)
        cards = await database.cards.count(space_id)

    body = "".join(frames)
    assert "event: error" in body
    payload = _done_payload(frames)
    assert payload["failures"]
    assert payload["documents"][0]["batches_failed"] == 1
    assert cards == 0


async def test_extract_stream_rejects_unknown_document(settings: Settings) -> None:
    """文档不存在时在进入流之前就报 404。"""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, _ = await _seed_document(app)
        with pytest.raises(NotFoundError):
            await extract_cards(space_id, CardExtractRequest(document_ids=["nope"]), runtime)


def test_runtime_exposes_memory_aggregates(settings: Settings) -> None:
    """Runtime 提供抽取器、卡片服务与图谱三个聚合入口。"""
    app = create_app(settings)
    with TestClient(app):
        runtime = app.state.runtime

        async def check() -> tuple[bool, bool, bool]:
            space = await runtime.spaces.create_space(SpaceCreate(name="逆向", domain="Android"))
            extractor = await runtime.knowledge_extractor(space.id)
            service = await runtime.card_service(space.id)
            graph = await runtime.knowledge_graph(space.id)
            return (
                extractor.space_id == space.id,
                service.space_id == space.id,
                graph.nodes == [],
            )

        assert asyncio.run(check()) == (True, True, True)


def test_graph_reports_how_much_was_truncated(client: TestClient) -> None:
    """图被节点上限裁剪时要如实说明，不能让用户以为看到的就是全部。"""
    space_id = _make_space(client)
    runtime = app_runtime(client)
    import anyio

    async def seed() -> None:
        from agentmem.types import EntityCreate, RelationCreate

        database = await runtime.space_db(space_id)
        entities = [
            await database.entities.create(
                EntityCreate(space_id=space_id, name=f"实体{index:03d}", type="靶点")
            )
            for index in range(12)
        ]
        for index in range(11):
            await database.relations.create(
                RelationCreate(
                    space_id=space_id,
                    src_id=entities[index].id,
                    dst_id=entities[index + 1].id,
                    predicate="关联",
                    weight=0.5,
                )
            )

    with anyio.from_thread.start_blocking_portal() as portal:
        portal.call(seed)

    full = client.get(f"{API}/spaces/{space_id}/graph").json()
    assert len(full["nodes"]) == 12
    assert full["total_nodes"] == 12
    assert full["truncated"] is False

    clipped = client.get(f"{API}/spaces/{space_id}/graph", params={"limit": 5}).json()
    assert len(clipped["nodes"]) == 5
    assert clipped["total_nodes"] == 12, "裁剪前有多少实体要报出来"
    assert clipped["truncated"] is True


def test_deleting_document_cleans_up_what_was_extracted_from_it(
    client: TestClient, mock_reply: Callable[..., None], settings: Settings
) -> None:
    """删文档要带走只来自它的卡片、关系、孤立实体和原始文件；别处也引用的卡片只摘来源。

    此前删文档只删切片与向量：卡片留在列表里、向量照样被召回，图上还画着它的关系，
    ``raw/`` 里的原文件也一直在。
    """
    mock_reply(json.dumps(REPLY, ensure_ascii=False))
    space_id = _make_space(client)
    document_id = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "脱壳笔记", "content": PASTE_CONTENT},
    ).json()["id"]
    assert _wait_for_status(client, space_id, document_id, {"ready", "failed"}) == "ready"
    extracted = client.get(f"{API}/spaces/{space_id}/cards").json()["items"]
    assert len(extracted) == 1
    doc_chunks = extracted[0]["source_chunks"]
    shared = client.post(
        f"{API}/spaces/{space_id}/cards",
        json={
            "kind": "fact",
            "title": "两篇资料都提到的结论",
            "body": "别的文档也支撑这一条",
            "source_chunks": [*doc_chunks, "chunk-from-another-doc"],
        },
    ).json()
    raw_path = Path(
        client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["meta"]["raw_path"]
    )
    assert raw_path.is_file()

    assert client.delete(f"{API}/spaces/{space_id}/documents/{document_id}").status_code == 200

    cards = client.get(f"{API}/spaces/{space_id}/cards").json()["items"]
    assert [card["id"] for card in cards] == [shared["id"]], "只来自这篇文档的卡片要删掉"
    assert cards[0]["source_chunks"] == ["chunk-from-another-doc"], "别处也引用的只摘来源"
    graph = client.get(f"{API}/spaces/{space_id}/graph").json()
    assert graph["nodes"] == [] and graph["edges"] == [], "关系与孤立实体要一起清掉"
    assert not raw_path.is_file(), "原始文件要一起删"
