"""SSE 封装与各条流式路由的端到端测试。

存在的理由：``SseEvent`` 的载荷联合类型曾把传入的 dict 塞进一个空的 ``BaseModel``
实例，构造期毫无异常，直到序列化时才抛错——而那时响应头已经发出，客户端只看到
一条断掉的流。凡是「构造时不报错、序列化时才炸」的东西，只有端到端跑一遍才拦得住。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.ingest.bus import IngestBus
from agentmem.types import DocumentUpdate, DoneEvent
from apps.api.main import create_app
from apps.api.sse import SseEvent, event, format_event
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
    """必须在 yield 里交还：直接 return 会让 with 块在测试体之前退出，
    应用 lifespan 随之关闭，后台摄取任务全部被取消——症状是文档永远停在 pending。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def test_dict_payload_stays_a_dict() -> None:
    """字典载荷必须原样保留——被 pydantic 联合校验改造成模型实例就会在序列化时炸。"""
    payload = {"stage": "distill", "status": "running"}
    frame = SseEvent(event="stage", data=payload)

    assert isinstance(frame.data, dict)
    assert (
        format_event(frame) == 'event: stage\ndata: {"stage": "distill", "status": "running"}\n\n'
    )


def test_model_and_string_payloads_still_format() -> None:
    """模型与字符串两种载荷照常工作。"""
    model_frame = event("done", DoneEvent(message_id="m1", trace_id="t1"))
    assert isinstance(model_frame.data, DoneEvent)
    assert '"message_id":"m1"' in format_event(model_frame)

    assert format_event(event("ping", "raw")) == "event: ping\ndata: raw\n\n"


@pytest.mark.parametrize("payload", [{}, {"a": 1}, {"a": None, "b": [1, 2]}, {"a": {"b": "中"}}])
def test_every_dict_shape_formats(payload: dict[str, Any]) -> None:
    """各种字典形状都要能序列化。"""
    assert "data: " in format_event(event("x", payload))


# ---------------------------------------------------------------------------
# 各条流式路由：真的把事件发出来
# ---------------------------------------------------------------------------


def _stream(
    client: TestClient, path: str, payload: dict[str, Any] | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """跑一条 SSE 路由，返回 (事件名, 载荷) 列表。"""
    body = ""
    with client.stream("POST", path, json=payload or {}) as response:
        assert response.status_code == 200, response.read()
        body = "".join(response.iter_text())
    return _parse_sse(body)


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    data = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name, data = line[7:], ""
        elif line.startswith("data: "):
            data += line[6:]
        elif not line and name:
            events.append((name, json.loads(data)))
            name, data = "", ""
    return events


def _new_space(client: TestClient) -> str:
    created = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()
    return str(created["id"])


def _add_document(client: TestClient, space_id: str, title: str, content: str) -> str:
    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste", json={"title": title, "content": content}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _wait_ready(client: TestClient, space_id: str, document_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
        if detail["status"] in {"ready", "failed"}:
            assert detail["status"] == "ready", detail.get("error")
            return
        time.sleep(0.05)
    raise AssertionError("等待摄取超时")


def test_reindex_streams_progress_and_rechunks(client: TestClient) -> None:
    """重建索引：SSE 全程有事件，且切片被重切（id 换新、向量重建）。"""
    space_id = _new_space(client)
    document_id = _add_document(
        client,
        space_id,
        "成药性笔记",
        "# 成药性\n\n先导化合物需要评估 ADMET 五项，其中 hERG 抑制是常见淘汰原因。",
    )
    _wait_ready(client, space_id, document_id)
    before = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=500").json()[
        "items"
    ]

    events = _stream(client, f"{API}/spaces/{space_id}/reindex")
    names = [name for name, _ in events]
    assert names[0] == "begin"
    assert names[-1] == "done"
    assert "progress" in names
    assert events[-1][1]["reindexed"] == 1

    after = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=500").json()[
        "items"
    ]
    assert after
    assert {chunk["id"] for chunk in after} & {chunk["id"] for chunk in before} == set(), (
        "重建索引应当重新切分，chunk id 不该沿用"
    )
    # 重切之后偏移仍要能切回正文
    markdown = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/content").json()[
        "markdown"
    ]
    for chunk in after:
        if chunk["kind"] == "summary":
            assert chunk["char_start"] == chunk["char_end"] == 0, "概要切片没有原文区间"
            continue
        assert markdown[chunk["char_start"] : chunk["char_end"]] == chunk["content"]


def test_evolve_distill_stream(client: TestClient) -> None:
    """蒸馏路由：stage 事件成对出现（dict 载荷曾经整条流都发不出去）。"""
    space_id = _new_space(client)

    events = _stream(client, f"{API}/spaces/{space_id}/evolve/distill")

    names = [name for name, _ in events]
    assert names == ["stage", "stage"]
    assert events[0][1] == {"stage": "distill", "status": "running"}
    assert events[-1][1]["status"] == "done"


def test_evals_generate_stream(client: TestClient) -> None:
    """自动出题路由：stage 事件与逐题 item 事件都要发得出来。"""
    space_id = _new_space(client)
    document_id = _add_document(
        client,
        space_id,
        "成药性笔记",
        "# 成药性\n\n先导化合物需要评估 ADMET 五项，其中 hERG 抑制是常见淘汰原因。",
    )
    _wait_ready(client, space_id, document_id)

    events = _stream(client, f"{API}/spaces/{space_id}/evals/generate")

    names = [name for name, _ in events]
    assert names[0] == "stage"
    # 前端靠 done 收尾：此前只发 stage，出题成功后按钮也一直转圈
    assert names[-1] == "done"
    assert events[-2][0] == "stage" and events[-2][1]["status"] == "done"
    items = [payload for name, payload in events if name == "item"]
    assert events[-1][1]["count"] == len(items)
    for item in items:
        assert item["question"] and "reference" in item and "created_at" in item, "题目要整条下发"


def test_evolve_cycle_stream(client: TestClient) -> None:
    """一键进化：全程 SSE 事件都要能发出去，最后给出 done。"""
    space_id = _new_space(client)

    events = _stream(client, f"{API}/spaces/{space_id}/evolve/cycle")

    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert "stage" in names
    assert events[-1][1]["delta"] == 0
    # 前端据此提示「N 条候选经验待验证」
    assert events[-1][1]["pending_candidates"] == 0


def test_retry_failed_documents_reruns_the_chain(client: TestClient) -> None:
    """失败的文档可以一次全部重试。

    失败常见于 provider 当时不可用、或上次进程被中断——等用户一个个点「重新解析」
    既慢又容易漏。这里守两件事：只挑失败的文档、逐篇跑完并给出可核验的计数。
    """
    space_id = _new_space(client)
    document_id = _add_document(
        client, space_id, "成药性笔记", "# 成药性\n\n先导化合物需要评估 ADMET 五项。"
    )
    _wait_ready(client, space_id, document_id)

    runtime = app_runtime(client)

    async def mark_failed() -> None:
        database = await runtime.space_db(space_id)
        await database.documents.set_status(document_id, "failed", "上一次 provider 不可用")

    with __import__("anyio").from_thread.start_blocking_portal() as portal:
        portal.call(mark_failed)

    events = _stream(client, f"{API}/spaces/{space_id}/documents/retry-failed")

    assert events[0] == ("begin", {"total": 1}), "只重试失败的文档"
    assert events[-1] == ("done", {"total": 1, "retried": 1})

    detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
    assert detail["status"] == "ready"
    assert detail["error"] is None


def test_retry_failed_documents_with_nothing_to_do(client: TestClient) -> None:
    """没有失败文档时也要正常收尾，而不是报错。"""
    space_id = _new_space(client)
    _add_document(client, space_id, "正常文档", "# 标题\n\n正文。")

    events = _stream(client, f"{API}/spaces/{space_id}/documents/retry-failed")

    assert events == [("begin", {"total": 0}), ("done", {"total": 0, "retried": 0})]


def test_reindex_only_stale_skips_fresh_documents(client: TestClient) -> None:
    """`only_stale=true` 只重做切片算法版本过期的文档，并保住其余文档的向量。

    重建中断后可以用它接着跑完剩下的；代价是必须**不**清空整张向量表——
    被跳过的文档得留住已有向量，否则这次「部分重建」会把它们的检索能力抹掉。
    """
    from agentmem.ingest.chunk import CHUNKER_VERSION

    space_id = _new_space(client)
    fresh = _add_document(client, space_id, "新算法文档", "# 标题\n\n正文。")
    stale = _add_document(client, space_id, "旧算法文档", "# 标题\n\n旧正文。")
    _wait_ready(client, space_id, fresh)
    _wait_ready(client, space_id, stale)

    runtime = app_runtime(client)

    async def age_one() -> None:
        database = await runtime.space_db(space_id)
        document = await database.documents.require(stale)
        meta = document.meta.model_copy(update={"chunker_version": CHUNKER_VERSION - 1})
        await database.documents.update(stale, DocumentUpdate(meta=meta))

    with __import__("anyio").from_thread.start_blocking_portal() as portal:
        portal.call(age_one)

    before_fresh = {
        chunk["id"]
        for chunk in client.get(f"{API}/spaces/{space_id}/documents/{fresh}/chunks").json()["items"]
    }

    events = _stream(client, f"{API}/spaces/{space_id}/reindex?only_stale=true")

    assert events[0] == ("begin", {"total": 1, "only_stale": True}), "只挑版本过期的文档"
    assert events[-1][1]["reindexed"] == 1

    after_fresh = {
        chunk["id"]
        for chunk in client.get(f"{API}/spaces/{space_id}/documents/{fresh}/chunks").json()["items"]
    }
    assert after_fresh == before_fresh, "没被选中的文档不该被重切"

    detail = client.get(f"{API}/spaces/{space_id}/documents/{stale}").json()
    assert detail["meta"]["chunker_version"] == CHUNKER_VERSION


def test_stale_reindex_recovers_failed_documents_with_current_chunks(client: TestClient) -> None:
    """切片版本已更新但向量化失败时，增量恢复仍应重做该文档。"""
    from agentmem.ingest.chunk import CHUNKER_VERSION

    space_id = _new_space(client)
    document_id = _add_document(client, space_id, "中断恢复", "# 标题\n\n正文。")
    _wait_ready(client, space_id, document_id)
    runtime = app_runtime(client)

    async def mark_failed() -> None:
        import asyncio

        bus = cast(IngestBus, cast(FastAPI, client.app).state.bus)
        await asyncio.gather(*bus._tasks.get(space_id, ()))
        database = await runtime.space_db(space_id)
        document = await database.documents.require(document_id)
        assert document.meta.chunker_version == CHUNKER_VERSION
        await database.documents.set_status(document_id, "failed", "向量化时中断")

    assert client.portal is not None
    client.portal.call(mark_failed)
    events = _stream(client, f"{API}/spaces/{space_id}/reindex?only_stale=true")
    assert events[0] == ("begin", {"total": 1, "only_stale": True})
    assert events[-1] == ("done", {"total": 1, "reindexed": 1})
    assert (
        client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["status"] == "ready"
    )
