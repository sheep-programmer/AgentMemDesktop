"""路由体检：把每个端点都真的打一次，看它有没有活着。

`test_api.py::test_openapi_schema_is_complete` 只断言路由**存在**于 schema 里，
而一个路由可以既存在于 schema、又在被调用时 500 或者断流——本轮就抓到过一整类
「用字典发 SSE 事件」的端点集体断流，「为什么这么答」「一键进化」「自动出题」
全都在里面，而每个服务自己的单元测试都是绿的。

所以这里逐个发最朴素的请求，只断言两件事：**不该 5xx**，以及返回体是能解析的
JSON（或预期的流）。不追求覆盖业务分支，那些由各自的测试负责。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from apps.api.main import create_app
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


class World:
    """一次体检用到的全部前置数据。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        self.document_id = client.post(
            f"{API}/spaces/{self.space_id}/documents/paste",
            json={"title": "成药性笔记", "content": "# 成药性\n\n先导化合物需要评估 ADMET 五项。"},
        ).json()["id"]
        _wait_ready(client, self.space_id, self.document_id)
        self.conversation_id = client.post(
            f"{API}/spaces/{self.space_id}/conversations",
            json={"space_id": self.space_id, "title": "讨论"},
        ).json()["id"]
        self.eval_id = client.post(
            f"{API}/spaces/{self.space_id}/evals",
            json={"question": "成药性要看什么？", "must_include": ["ADMET"]},
        ).json()["id"]
        self.card_id = client.post(
            f"{API}/spaces/{self.space_id}/cards",
            json={"kind": "fact", "title": "hERG 早筛", "body": "高活性不代表可推进。"},
        ).json()["id"]
        self.insight_id = client.post(
            f"{API}/spaces/{self.space_id}/insights",
            json={
                "trigger": "遇到高活性先导化合物时",
                "guidance": "先查 ADMET 再给结论",
                "kind": "heuristic",
            },
        ).json()["id"]
        self.trace_id = _chat_once(client, self.conversation_id)


def _chat_once(client: TestClient, conversation_id: str) -> str:
    with client.stream(
        "POST", f"{API}/conversations/{conversation_id}/chat", json={"content": "成药性要看什么？"}
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    for line in body.splitlines():
        if line.startswith("data: ") and '"trace_id"' in line:
            return str(json.loads(line[6:])["trace_id"])
    raise AssertionError(body[-400:])


def _wait_ready(client: TestClient, space_id: str, document_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
        if detail["status"] in {"ready", "failed"}:
            assert detail["status"] == "ready", detail.get("error")
            return
        time.sleep(0.05)
    raise AssertionError("等待摄取超时")


def test_read_routes_all_respond(client: TestClient) -> None:
    """所有 GET 端点都要返回 200 且能解析成 JSON。"""
    world = World(client)
    paths = [
        "/health",
        "/capabilities",
        "/stats",
        "/providers",
        "/providers/roles",
        "/providers/usage",
        "/providers/local-agents",
        "/spaces",
        f"/spaces/{world.space_id}",
        f"/spaces/{world.space_id}/settings",
        f"/spaces/{world.space_id}/persona",
        f"/spaces/{world.space_id}/documents",
        f"/spaces/{world.space_id}/documents/{world.document_id}",
        f"/spaces/{world.space_id}/documents/{world.document_id}/content",
        f"/spaces/{world.space_id}/documents/{world.document_id}/chunks",
        f"/spaces/{world.space_id}/cards",
        f"/spaces/{world.space_id}/graph",
        f"/spaces/{world.space_id}/insights",
        f"/spaces/{world.space_id}/insights/conflicts",
        f"/spaces/{world.space_id}/insights/{world.insight_id}/lineage",
        f"/spaces/{world.space_id}/conversations",
        f"/spaces/{world.space_id}/evals",
        f"/spaces/{world.space_id}/evals/runs",
        f"/spaces/{world.space_id}/evolve/pending",
        f"/spaces/{world.space_id}/evolve/history",
        f"/spaces/{world.space_id}/expertise",
        f"/spaces/{world.space_id}/expertise/history",
        f"/conversations/{world.conversation_id}",
        f"/traces/{world.trace_id}",
    ]

    broken: list[str] = []
    for path in paths:
        response = client.get(f"{API}{path}")
        if response.status_code != 200:
            broken.append(f"GET {path} -> {response.status_code} {response.text[:120]}")
            continue
        try:
            response.json()
        except ValueError:
            broken.append(f"GET {path} -> 非法 JSON")

    assert not broken, "\n".join(broken)


def test_mutating_routes_all_respond(client: TestClient) -> None:
    """所有写端点都要有像样的响应，不能 5xx。"""
    world = World(client)
    space = world.space_id
    calls: list[tuple[str, str, dict[str, Any] | None, set[int]]] = [
        ("POST", f"/spaces/{space}/documents/{world.document_id}/reprocess", None, {200}),
        ("POST", f"/traces/{world.trace_id}/feedback", {"kind": "up"}, {201}),
        ("POST", f"/traces/{world.trace_id}/judge", None, {200}),
        (
            "POST",
            f"/spaces/{space}/cards/extract",
            {"document_ids": [world.document_id]},
            {200, 202},
        ),
        ("POST", f"/spaces/{space}/insights/{world.insight_id}/promote", None, {200}),
        ("POST", f"/spaces/{space}/insights/{world.insight_id}/archive", None, {200}),
        ("POST", f"/spaces/{space}/search", {"query": "成药性"}, {200}),
        ("POST", "/providers/discover", {"adapter": "openai_compatible"}, {200}),
        # 本机没有可导入的 Agent 配置时返回 422 是正常的业务拒绝
        ("POST", "/providers/import-local", {}, {200, 400, 422}),
        ("POST", "/providers/{provider_id}/health", None, {200, 404}),
        ("POST", f"/conversations/{world.conversation_id}/stop", None, {200}),
        ("GET", f"/spaces/{space}/expertise/gaps", None, {200}),
    ]

    broken: list[str] = []
    for method, path, payload, allowed in calls:
        target = path
        if "{provider_id}" in path:
            providers = client.get(f"{API}/providers").json()
            items = providers["items"] if isinstance(providers, dict) else providers
            if not items:
                continue
            target = path.replace("{provider_id}", items[0]["id"])
        response = client.request(method, f"{API}{target}", json=payload)
        if response.status_code not in allowed:
            broken.append(f"{method} {target} -> {response.status_code} {response.text[:120]}")

    assert not broken, "\n".join(broken)


def test_feedback_attribution_does_not_swallow_errors(client: TestClient) -> None:
    """反馈回流出错时不能被静默吞掉。

    回流本身包在 try/except 里——反馈落库比一次置信度加减更重要，这是刻意的。
    但正因为如此，回流里的异常只会留下一条日志，接口照样 201。这里直接调服务层，
    让异常暴露出来：曾经就是一条 ``logger.info(..., event=...)``（structlog 的
    event 是保留字段）让每次回流都抛 TypeError、被吞掉，而所有断言数据库状态的
    测试都是绿的。
    """
    import asyncio

    from anyio.from_thread import start_blocking_portal

    world = World(client)
    runtime = app_runtime(client)

    async def attribute() -> None:
        database = await runtime.space_db(world.space_id)
        service = await runtime.critique_service(world.space_id)
        trace = await database.traces.require(world.trace_id)
        await service._attribute(trace, "down", "probe")

    with start_blocking_portal() as portal:
        portal.call(attribute)

    assert asyncio.get_event_loop_policy() is not None  # 占位：异常未被吞即为通过


def test_export_then_import_round_trip(client: TestClient) -> None:
    """导出再导入要能还原：这是「换台机器接着用」唯一的路径。"""
    world = World(client)

    exported = client.get(f"{API}/spaces/{world.space_id}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] in {"application/zip", "application/octet-stream"}
    assert exported.content[:2] == b"PK", "导出的不是 zip"

    # 同 id 已存在时导入会被拒绝（不覆盖既有数据），所以先删掉再导回来——
    # 这正是「换台机器接着用」的真实流程
    duplicate = client.post(
        f"{API}/spaces/import",
        files={"file": ("space.zip", exported.content, "application/zip")},
    )
    assert duplicate.status_code == 422
    assert "已存在" in duplicate.json()["error"]["message"]

    assert client.delete(f"{API}/spaces/{world.space_id}").json()["deleted"] is True
    imported = client.post(
        f"{API}/spaces/import",
        files={"file": ("space.zip", exported.content, "application/zip")},
    )
    assert imported.status_code in {200, 201}, imported.text
    restored = imported.json()["space_id"]
    assert restored == world.space_id
    documents = client.get(f"{API}/spaces/{restored}/documents").json()
    assert documents["total"] == 1
    assert documents["items"][0]["title"] == "成药性笔记"


def test_ingest_is_refused_while_the_space_is_reindexing(client: TestClient) -> None:
    """重建索引期间拒绝新的摄取。

    重建会先 drop 掉整张向量表再逐篇重构：这期间放进来的文档，它的向量要么被
    drop 抹掉、要么与重构互相覆盖，最后留下一个自相矛盾的索引。
    """
    world = World(client)
    runtime = app_runtime(client)

    runtime.lock_space(world.space_id)
    try:
        blocked = client.post(
            f"{API}/spaces/{world.space_id}/documents/paste",
            json={"title": "重建期间", "content": "# 标题\n\n正文。"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "SPACE_LOCKED"

        reprocess = client.post(
            f"{API}/spaces/{world.space_id}/documents/{world.document_id}/reprocess"
        )
        assert reprocess.status_code == 409
    finally:
        runtime.unlock_space(world.space_id)

    accepted = client.post(
        f"{API}/spaces/{world.space_id}/documents/paste",
        json={"title": "解锁之后", "content": "# 标题\n\n正文。"},
    )
    assert accepted.status_code == 201


def test_reindex_releases_the_lock_even_when_it_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重建失败或中断也要把锁放掉，否则这个 Space 会永久拒绝写入。"""
    world = World(client)
    runtime = app_runtime(client)

    from agentmem.ingest import IngestPipeline

    async def boom(self: object, document_id: str, **kwargs: object) -> None:
        raise RuntimeError("重建中途炸了")

    monkeypatch.setattr(IngestPipeline, "run", boom)

    with client.stream("POST", f"{API}/spaces/{world.space_id}/reindex") as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert runtime.is_space_locked(world.space_id) is False
    accepted = client.post(
        f"{API}/spaces/{world.space_id}/documents/paste",
        json={"title": "解锁之后", "content": "# 标题\n\n正文。"},
    )
    assert accepted.status_code == 201


def test_chat_request_accepts_the_documented_field(client: TestClient) -> None:
    """`POST /conversations/{id}/chat` 的入参字段是 `content`。

    前端曾经发的是 `query`，真实后端下每次提问都 422——mock 模式不走后端所以一直
    没暴露。这里把契约钉住：`content` 必须被接受，`query` 必须被拒绝（而不是被静默
    当成空问题）。
    """
    world = World(client)
    conversation = client.post(
        f"{API}/spaces/{world.space_id}/conversations",
        json={"space_id": world.space_id, "title": "字段名契约"},
    ).json()

    wrong = client.post(
        f"{API}/conversations/{conversation['id']}/chat", json={"query": "溶解度阈值是多少？"}
    )
    assert wrong.status_code == 422, "发错字段名要立刻报错，不能静默当成空问题"

    with client.stream(
        "POST",
        f"{API}/conversations/{conversation['id']}/chat",
        json={"content": "溶解度阈值是多少？"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: trace_start" in body
