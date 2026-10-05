"""对话链路：会话 CRUD、检索路由、SSE 事件序列、引用落位、轨迹落库、中断生成。

⚠️ 测 SSE 不走 HTTP 客户端：``httpx.ASGITransport`` 会 ``await`` 整个 ASGI 应用跑完，
对永不结束的流就是死锁。这里直接调用路由函数并消费 ``StreamingResponse.body_iterator``
（与 ``tests/test_api.py::test_ingest_sse_stream`` 同一姿势）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.ingest import IngestPipeline
from agentmem.retrieve import DeltaChunk, generation_registry, reset_generation_registry
from agentmem.space.runtime import Runtime
from agentmem.types import (
    ChatRequest,
    ConversationCreate,
    DocumentCreate,
    InsightCreate,
    MessageCreate,
    SpaceCreate,
)
from apps.api.main import create_app
from apps.api.routers.chat import chat, stop_generation

API = "/api/v1"

SAMPLE = "# 第3章 脱壳\n\n加固 APK 的脱壳流程：先定位 DexClassLoader 调用点，再 dump 内存中的 dex。"

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
  - id: local-reranker
    kind: rerank
    adapter: cohere_rerank
    base_url: {base}
    model: mock-rerank
roles:
  chat: local-qwen
  fast: local-qwen
  distill: local-qwen
  judge: local-qwen
  embedding: local-embedding
  rerank: local-reranker
fallbacks: {{}}
"""

#: 指向死端口的配置：用来验证 provider 整体不可用时的失败路径
BROKEN_YAML = MODELS_YAML.format(base="http://127.0.0.1:1/v1")


@pytest.fixture
def settings(tmp_path: Path, mock_server: str) -> Settings:
    """数据目录与模型配置都指向临时位置，模型指向 Mock 服务。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    return Settings(data_dir=data_dir, models_config=config_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """启动应用（含 lifespan）的同步客户端。"""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
async def broken_app(tmp_path: Path) -> Any:
    """LLM 与 embedding 都连不上的应用。"""
    data_dir = tmp_path / "broken"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "broken.yaml"
    config_path.write_text(BROKEN_YAML, encoding="utf-8")
    app = create_app(Settings(data_dir=data_dir, models_config=config_path))
    async with app.router.lifespan_context(app):
        yield app


# ---------------------------------------------------------------------------
# 会话 CRUD
# ---------------------------------------------------------------------------


def test_conversation_crud(client: TestClient) -> None:
    """§5 会话的增查改删。"""
    space_id = client.post(f"{API}/spaces", json={"name": "逆向", "domain": "Android"}).json()["id"]

    created = client.post(
        f"{API}/spaces/{space_id}/conversations", json={"space_id": space_id, "title": "脱壳"}
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]
    assert created.json()["pinned"] is False

    listing = client.get(f"{API}/spaces/{space_id}/conversations").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == conversation_id

    detail = client.get(f"{API}/conversations/{conversation_id}").json()
    assert detail["title"] == "脱壳"
    assert detail["messages"] == []

    patched = client.patch(
        f"{API}/conversations/{conversation_id}", json={"title": "改过", "pinned": True}
    ).json()
    assert patched["title"] == "改过"
    assert patched["pinned"] is True

    assert client.delete(f"{API}/conversations/{conversation_id}").json()["deleted"] is True
    assert client.get(f"{API}/conversations/{conversation_id}").status_code == 404
    assert client.get(f"{API}/spaces/{space_id}/conversations").json()["total"] == 0


def test_create_conversation_rejects_space_mismatch(client: TestClient) -> None:
    """路径与请求体的 space_id 不一致时拒绝，避免会话静默建到别的 Space。"""
    space_id = client.post(f"{API}/spaces", json={"name": "逆向", "domain": "Android"}).json()["id"]
    response = client.post(
        f"{API}/spaces/{space_id}/conversations",
        json={"space_id": "01OTH00000000000000000000", "title": "脱壳"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_stop_unknown_conversation_is_404(client: TestClient) -> None:
    """对不存在的会话点停止 → 404（而不是静默的成功）。"""
    response = client.post(f"{API}/conversations/nope/stop")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 纯检索路由
# ---------------------------------------------------------------------------


def test_search_route_reports_scores(settings: Settings) -> None:
    """检索路由返回带各路分数的命中，供调试面板展示。"""
    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(f"{API}/spaces", json={"name": "逆向", "domain": "Android"}).json()[
            "id"
        ]
        document = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "脱壳笔记", "content": SAMPLE},
        ).json()
        _wait_until_ingested(client, space_id, document["id"])

        hybrid = client.post(f"{API}/spaces/{space_id}/search", json={"query": "脱壳"}).json()
        assert hybrid["mode"] == "hybrid"
        assert hybrid["hits"]
        top = hybrid["hits"][0]
        assert top["chunk_id"] and top["document_id"] and top["snippet"]
        assert top["vec_score"] is not None
        assert top["bm25_score"] is not None
        assert top["rrf"] is not None
        assert top["rerank_score"] is not None

        fts = client.post(
            f"{API}/spaces/{space_id}/search", json={"query": "脱壳", "mode": "fts"}
        ).json()
        assert fts["hits"][0]["bm25_score"] is not None
        assert fts["hits"][0]["vec_score"] is None

        limited = client.post(
            f"{API}/spaces/{space_id}/search", json={"query": "脱壳", "top_k": 1}
        ).json()
        assert len(limited["hits"]) == 1


def test_search_on_empty_space(client: TestClient) -> None:
    """空 Space 检索返回空列表而不是报错。"""
    space_id = client.post(f"{API}/spaces", json={"name": "逆向", "domain": "Android"}).json()["id"]
    response = client.post(f"{API}/spaces/{space_id}/search", json={"query": "脱壳"})
    assert response.status_code == 200
    assert response.json()["hits"] == []


# ---------------------------------------------------------------------------
# 问答 SSE
# ---------------------------------------------------------------------------


async def test_chat_emits_events_in_order(settings: Settings, mock_reply: Any) -> None:
    """事件顺序：trace_start → retrieval → insights → delta → citation → done。"""
    mock_reply("先定位DexClassLoader[^c1]。")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings)
        frames = await _collect(
            await chat(conversation_id, ChatRequest(content="这个壳怎么脱？"), runtime)
        )

    names = [name for name, _ in frames]
    assert names[0] == "trace_start"
    assert names[-1] == "done"
    assert names.count("retrieval") == 1
    assert names.count("insights") == 1
    assert "rewrite" not in names, "单轮提问不该触发改写"
    assert names.index("retrieval") < names.index("insights")
    assert names.index("insights") < names.index("delta")
    assert names.index("delta") < names.index("citation") < names.index("done")
    assert names.count("delta") > 1, "正文应当是逐片下发的"

    payloads = dict(frames)
    started = payloads["trace_start"]
    assert payloads["retrieval"]["chunks"], "应当报告命中的资料"
    assert payloads["insights"]["insights"] == []
    citation = payloads["citation"]
    assert citation["marker"] == "c1"
    assert citation["chunk_id"] == payloads["retrieval"]["chunks"][0]["id"]
    assert citation["document_id"] == payloads["retrieval"]["chunks"][0]["document_id"]
    done = payloads["done"]
    assert done["message_id"] == started["message_id"]
    assert done["trace_id"] == started["trace_id"]
    assert done["usage"]["prompt_tokens"] == 11
    assert done["usage"]["completion_tokens"] == 7
    assert done["usage"]["latency_ms"] >= 0

    streamed = "".join(payload["text"] for name, payload in frames if name == "delta")
    assert streamed == "先定位DexClassLoader。"

    async def load() -> tuple[Any, Any, Any]:
        database = await runtime.space_db(space_id)
        messages = await database.messages.list_by_conversation(conversation_id)
        return messages, await database.traces.get_by_message(done["message_id"]), database

    messages, trace, database = await load()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "这个壳怎么脱？"
    assistant = messages[1]
    assert assistant.id == done["message_id"]
    assert assistant.content == "先定位DexClassLoader。"
    assert [item.marker for item in assistant.citations] == ["c1"]
    assert assistant.citations[0].chunk_id == citation["chunk_id"]
    assert assistant.citations[0].snippet

    conversation = await database.conversations.require(conversation_id)
    assert conversation.updated_at >= conversation.created_at
    assert trace is not None

    # 实时推给证据栏的召回路径与分数要和轨迹里落库的一致：此前实时事件只带标题与
    # 摘录，证据栏每条都显示「未记录（旧版数据）」，刷新从轨迹加载后才正常
    live = payloads["retrieval"]["chunks"]
    assert [item["id"] for item in live] == [item.chunk_id for item in trace.retrieved]
    for sent, stored in zip(live, trace.retrieved, strict=True):
        assert sent["legs"] == stored.legs and sent["legs"], "召回路径要随实时事件下发"
        assert sent["merged_from"] == stored.merged_from
        for field in ("vec_score", "bm25_score", "rrf", "rerank_score"):
            assert sent[field] == getattr(stored, field), field


def test_delta_payload_preserves_whitespace() -> None:
    """单个空格也是有效增量。

    ``types.DeltaEvent`` 继承的基类会裁剪首尾空白，把「只含空格」的增量变成空串，
    正文会在流式过程中被悄悄改形；这里用不裁剪的载荷模型兜住。
    """
    assert DeltaChunk(text=" ").text == " "
    assert DeltaChunk(text=" 缩进").text == " 缩进"


async def test_trace_records_every_field(settings: Settings, mock_reply: Any) -> None:
    """轨迹字段不能省：Phase 3 的进化闭环全靠它还原「为什么这么答」。

    第二轮提问会带上历史，从而触发查询改写，``rewritten_query`` 才有值。
    """
    mock_reply(
        "结论[^c1]。",
        [
            (
                "可以脱离上下文独立理解",
                '{"query": "脱壳 Frida spawn 反调试", "changed": true}',
            )
        ],
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings)
        await _collect(await chat(conversation_id, ChatRequest(content="这个壳怎么脱？"), runtime))
        frames = await _collect(await chat(conversation_id, ChatRequest(content="它呢？"), runtime))

        names = [name for name, _ in frames]
        assert names[0] == "trace_start"
        assert names[1] == "rewrite", "改写结果必须紧跟 trace_start 发出"
        payloads = dict(frames)
        assert payloads["rewrite"]["rewritten"] == "脱壳 Frida spawn 反调试"

        async def load() -> Any:
            database = await runtime.space_db(space_id)
            return await database.traces.get_by_message(payloads["done"]["message_id"])

        trace = await load()

    assert trace is not None
    assert trace.space_id == space_id
    assert trace.conversation_id == conversation_id
    assert trace.query == "它呢？"
    assert trace.rewritten_query == "脱壳 Frida spawn 反调试"
    assert trace.retrieved, "检索明细不能为空"
    first = trace.retrieved[0]
    assert first.chunk_id
    assert first.vec_score is not None
    assert first.bm25_score is not None
    assert first.rrf is not None
    assert first.rerank_score is not None
    assert trace.used_insights == []
    assert trace.used_cards == []
    assert trace.llm_role == "chat"
    assert trace.provider_id == "local-qwen"
    assert trace.model
    assert trace.prompt_tokens == 11
    assert trace.completion_tokens == 7
    assert trace.latency_ms is not None and trace.latency_ms >= 0


async def test_use_insights_toggle_changes_injection(settings: Settings, mock_reply: Any) -> None:
    """``use_insights=false`` 必须真的不注入 L3，轨迹也要如实记录。"""
    mock_reply("结论[^c1]。")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings)
        database = await runtime.space_db(space_id)
        insight = await database.insights.create(
            InsightCreate(
                space_id=space_id,
                trigger="遇到加固壳",
                guidance="先脱壳再分析",
                kind="heuristic",
                status="active",
                confidence=0.9,
                origin="manual",
            )
        )

        with_insights = await _collect(
            await chat(conversation_id, ChatRequest(content="壳怎么脱"), runtime)
        )
        without = await _collect(
            await chat(
                conversation_id, ChatRequest(content="壳怎么脱", use_insights=False), runtime
            )
        )

        injected = dict(with_insights)["insights"]["insights"]
        assert [item["id"] for item in injected] == [insight.id]
        assert dict(without)["insights"]["insights"] == []

        async def load(message_id: str) -> Any:
            database = await runtime.space_db(space_id)
            return await database.traces.get_by_message(message_id)

        with_trace = await load(dict(with_insights)["done"]["message_id"])
        without_trace = await load(dict(without)["done"]["message_id"])
        applied = await database.insights.require(insight.id)

    assert with_trace is not None and with_trace.used_insights == [insight.id]
    assert without_trace is not None and without_trace.used_insights == []
    # 被注入的一次记一次「注入」；关掉经验那一轮不计
    assert applied.applied_count == 1


async def test_chat_without_retrieval_skips_evidence(settings: Settings, mock_reply: Any) -> None:
    """关闭检索时退化为纯聊天：没有证据，也没有引用。

    模型仍可能凭习惯吐 ``[^c1]``，这时没有任何 marker 可映射，
    属于幻觉编号，必须丢弃而不是写进 citations。
    """
    mock_reply("结论[^c1]。")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings)
        frames = await _collect(
            await chat(conversation_id, ChatRequest(content="你好", use_retrieval=False), runtime)
        )

        async def load() -> Any:
            database = await runtime.space_db(space_id)
            messages = await database.messages.list_by_conversation(conversation_id)
            return messages[-1]

        assistant = await load()

    names = [name for name, _ in frames]
    assert "citation" not in names
    payloads = dict(frames)
    assert payloads["retrieval"]["chunks"] == []
    assert payloads["insights"]["insights"] == []
    assert "".join(p["text"] for n, p in frames if n == "delta") == "结论。"
    assert assistant.content == "结论。"
    assert assistant.citations == []


async def test_chat_reports_provider_failure(broken_app: FastAPI, mock_reply: Any) -> None:
    """provider 连不上时发 error 事件，并且用户提问依然落库。"""
    mock_reply("结论[^c1]。")
    runtime = broken_app.state.runtime
    space_id, conversation_id = await _prepare(broken_app, runtime.settings, content=None)
    frames = await _collect(await chat(conversation_id, ChatRequest(content="脱壳"), runtime))

    names = [name for name, _ in frames]
    assert names[0] == "trace_start"
    assert names[-1] == "error"
    assert "done" not in names
    assert dict(frames)["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert dict(frames)["retrieval"]["chunks"] == [], "向量与全文都取不到时按空结果降级"

    async def load() -> tuple[list[Any], Any]:
        database = await runtime.space_db(space_id)
        messages = await database.messages.list_by_conversation(conversation_id)
        trace = await database.traces.get_by_message(messages[-1].id)
        return messages, trace

    messages, trace = await load()
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "脱壳"
    assert trace is not None, "失败的交互也要留下轨迹"


async def test_sse_disconnect_cancels_generation(settings: Settings, mock_reply: Any) -> None:
    """客户端断开 → 取消底层任务 → 半截回答照常落库，登记表不留残留。"""
    mock_reply("一二三四五六七八九十甲乙丙丁戊己庚辛", delay=0.05)
    reset_generation_registry()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings)
        response = await chat(conversation_id, ChatRequest(content="脱壳"), runtime)
        iterator = cast(StreamingResponse, response).body_iterator.__aiter__()
        first = await anext(iterator)
        assert "trace_start" in str(first)
        # body_iterator 的静态类型是 AsyncIterator，没有 aclose；实际是异步生成器，
        # 这里必须关掉它，否则生成任务会挂在注册表里
        await cast(AsyncGenerator[str | bytes, None], iterator).aclose()

        assert generation_registry().is_running(conversation_id) is False
        assert generation_registry().active_count() == 0

        async def load() -> tuple[list[Any], Any]:
            database = await runtime.space_db(space_id)
            messages = await database.messages.list_by_conversation(conversation_id)
            return messages, await database.traces.get_by_message(messages[-1].id)

        messages, trace = await load()

    assistant = messages[-1]
    assert assistant.role == "assistant"
    assert "一二三四五六七八九十甲乙丙丁戊己庚辛".startswith(assistant.content)
    assert len(assistant.content) < len("一二三四五六七八九十甲乙丙丁戊己庚辛")
    assert trace is not None, "断开的回答也要留下轨迹"


async def test_stop_route_cancels_running_generation(settings: Settings) -> None:
    """停止接口取消登记在册的生成任务。"""
    reset_generation_registry()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        _, conversation_id = await _prepare(app, settings, content=None)

        started = asyncio.Event()

        async def long_running() -> None:
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(long_running())
        await started.wait()
        generation_registry().register(conversation_id, task)

        stopped = await stop_generation(conversation_id, runtime)
        assert stopped.stopped is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await stop_generation(conversation_id, runtime)).stopped is False


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析成 (事件名, 载荷) 列表；心跳帧忽略。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.strip().splitlines() if line]
        if not lines or lines[0].startswith(":"):
            continue
        name = ""
        data = ""
        for line in lines:
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data += line[len("data: ") :]
        frames.append((name, json.loads(data) if data else {}))
    return frames


async def _collect(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """消费完整个 SSE 流并解析。"""
    chunks: list[str] = []
    async for raw in response.body_iterator:
        chunks.append(raw.decode() if isinstance(raw, bytes) else str(raw))
    return _parse_sse("".join(chunks))


async def _prepare(
    app: FastAPI, settings: Settings, *, content: str | None = SAMPLE
) -> tuple[str, str]:
    """建 Space（可选投喂一份文档并跑完摄取）并开一个会话。"""
    runtime = app.state.runtime
    space = await runtime.spaces.create_space(SpaceCreate(name="逆向", domain="Android 逆向工程"))
    database = await runtime.spaces.space_db(space.id)
    if content is not None:
        registry = await runtime.registry_for_space(space.id)
        pipeline = IngestPipeline(database, registry, settings)
        document = await pipeline.register_text(
            space_id=space.id, title="脱壳笔记", content=content
        )
        await pipeline.run(document.id)
    conversation = await database.conversations.create(
        ConversationCreate(space_id=space.id, title="脱壳")
    )
    return space.id, conversation.id


async def test_context_estimate_stream_keeps_stored_history_intact(
    settings: Settings, mock_reply: Any
) -> None:
    mock_reply("结论。")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        space_id, conversation_id = await _prepare(app, settings, content=None)
        runtime = app.state.runtime
        database = await runtime.space_db(space_id)
        for index in range(4):
            for role, content in (("user", f"问题 {index}"), ("assistant", "旧回答。" * 200)):
                await database.messages.create(
                    MessageCreate(conversation_id=conversation_id, role=role, content=content)
                )
        events = await _collect(
            await chat(
                conversation_id,
                ChatRequest(content="新问题", use_retrieval=False, context_mode="economy"),
                runtime,
            )
        )
        estimates = [data for event, data in events if event == "context"]
        assert len(estimates) == 1
        assert estimates[0]["mode"] == "economy"
        assert estimates[0]["history_messages"] == 2
        assert estimates[0]["saved_estimated_tokens"] > 0
        assert estimates[0]["original_estimated_tokens"] > estimates[0]["estimated_tokens"]
        stored = await database.messages.list_by_conversation(conversation_id)
        assert len(stored) == 10
        assert stored[1].content == "旧回答。" * 200


def _wait_until_ingested(
    client: TestClient, space_id: str, document_id: str, *, attempts: int = 200
) -> None:
    """轮询文档状态直到摄取结束（SSE 之外的用例需要同步等它跑完）。"""
    for _ in range(attempts):
        status = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["status"]
        if status in {"ready", "failed"}:
            assert status == "ready"
            return
        time.sleep(0.05)
    raise AssertionError("等待摄取超时")


async def test_citation_carries_the_source_title(settings: Settings, tmp_path: Path) -> None:
    """引用要带上来源文档标题。

    界面用它说明「这条引用出自哪篇资料」；此前引用记录里没有这个字段，历史会话里的
    引用只能显示成「未命名文献」。老数据在读取时补齐。
    """
    from agentmem.types import Citation

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime: Runtime = app.state.runtime
        space = await runtime.spaces.create_space(SpaceCreate(name="引用", domain="药物发现"))
        database = await runtime.space_db(space.id)
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="EGFR 药代动力学评估.md",
                source_type="paste",
                mime="text/markdown",
                sha256="c" * 64,
                size_bytes=10,
            )
        )
        conversation = await database.conversations.create(
            ConversationCreate(space_id=space.id, title="引用标题")
        )
        # 刻意不写 document_title，模拟这次改动之前落库的引用
        await database.messages.create(
            MessageCreate(
                conversation_id=conversation.id,
                role="assistant",
                content="结论[^c1]。",
                citations=[
                    Citation(
                        marker="c1",
                        chunk_id="chunk-x",
                        document_id=document.id,
                        snippet="片段",
                    )
                ],
            )
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"{API}/conversations/{conversation.id}")

    assert detail.status_code == 200, detail.text
    citations = detail.json()["messages"][0]["citations"]
    assert citations[0]["document_title"] == "EGFR 药代动力学评估.md", "读取时补齐来源标题"


async def test_search_mode_reaches_retrieval(settings: Settings, mock_reply: Any) -> None:
    """输入框的「仅向量检索」要真的只走向量：此前模式没进请求，实际仍是混合检索。"""
    mock_reply("结论[^c1]。")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        _space_id, conversation_id = await _prepare(app, settings)
        hybrid = dict(
            await _collect(await chat(conversation_id, ChatRequest(content="怎么脱壳？"), runtime))
        )
        vector = dict(
            await _collect(
                await chat(
                    conversation_id,
                    ChatRequest(content="怎么脱壳？", search_mode="vector"),
                    runtime,
                )
            )
        )

    hybrid_legs = {leg for item in hybrid["retrieval"]["chunks"] for leg in item["legs"]}
    vector_legs = {leg for item in vector["retrieval"]["chunks"] for leg in item["legs"]}
    assert any(leg.startswith("fts") for leg in hybrid_legs), "混合检索应当有全文命中作对照"
    assert vector_legs and not any(leg.startswith("fts") for leg in vector_legs)


async def test_overflow_retry_actually_shrinks_the_prompt(
    settings: Settings, mock_reply: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上下文溢出时每次重试进提示词的历史都要更短。

    此前从完整历史开始折半：20 条砍到 10 条，而装配只取最近 8 条，第一次重试的
    提示词与原来一字不差，必然再溢出一次。
    """
    from itertools import pairwise

    from agentmem.errors import ContextOverflowError
    from agentmem.retrieve.chat import ChatService
    from agentmem.retrieve.context import to_turns
    from agentmem.retrieve.pipeline import RetrievalPipeline
    from agentmem.types import MessageCreate

    mock_reply("结论。")
    seen: list[int] = []
    original = RetrievalPipeline.assemble_context_details

    def recording(
        self: Any, result: Any, *, question: str, history: Any = (), context_mode: Any = "standard"
    ) -> Any:
        seen.append(len(to_turns(history)))
        return original(self, result, question=question, history=history, context_mode=context_mode)

    async def overflow(self: Any, state: Any, messages: Any, registry: Any) -> Any:
        if seen[-1] > 0:
            raise ContextOverflowError("上下文超限")
        for _ in ():  # 不吐任何内容，但仍是异步生成器
            yield _

    monkeypatch.setattr(RetrievalPipeline, "assemble_context_details", recording)
    monkeypatch.setattr(ChatService, "_generate_answer", overflow)

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space_id, conversation_id = await _prepare(app, settings, content=None)
        database = await runtime.space_db(space_id)
        for index in range(10):
            await database.messages.create(
                MessageCreate(conversation_id=conversation_id, role="user", content=f"问{index}")
            )
            await database.messages.create(
                MessageCreate(
                    conversation_id=conversation_id, role="assistant", content=f"答{index}"
                )
            )
        await _collect(await chat(conversation_id, ChatRequest(content="再问"), runtime))

    assert seen[0] > 0
    assert all(later < earlier for earlier, later in pairwise(seen)), seen
    assert seen[-1] == 0


def test_conversation_title_search_api(client: TestClient) -> None:
    """搜索参数真正透传到存储层，计数与空结果不会混淆。"""
    space = client.post(f"{API}/spaces", json={"name": "搜索", "domain": "工程"}).json()
    for title in ["发布流程", "部署故障复盘", "发布清单"]:
        response = client.post(
            f"{API}/spaces/{space['id']}/conversations",
            json={"space_id": space["id"], "title": title},
        )
        assert response.status_code == 201
    response = client.get(f"{API}/spaces/{space['id']}/conversations", params={"q": "发布"})
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 2
    assert {item["title"] for item in result["items"]} == {"发布流程", "发布清单"}
    empty = client.get(
        f"{API}/spaces/{space['id']}/conversations", params={"q": "不存在的关键词"}
    ).json()
    assert empty["total"] == 0
    assert empty["items"] == []
    assert (
        client.get(f"{API}/spaces/{space['id']}/conversations", params={"q": "a" * 201}).status_code
        == 422
    )
