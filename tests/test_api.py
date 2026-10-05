"""HTTP 层：路由完整性、错误体、Space/文档链路与 SSE。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import cast

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from agentmem.config import ModelsConfig, Settings
from agentmem.ingest.batch import DocumentOutcome
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.space.runtime import Runtime
from agentmem.types import (
    DocumentCreate,
    DocumentUpdate,
    ProviderConfig,
    RoleBindings,
    SpaceCreate,
    StatusEvent,
)
from apps.api.main import create_app
from apps.api.routers.documents import ingest_stream
from conftest import app_runtime

API = "/api/v1"

#: 与前端阅读器同源的表格样例：续段必须自带表头，否则数据行没有列名可读
TABLE_HEADER = "| 化合物 | IC50 (nM) | 状态 |"
TABLE_DELIMITER = "| --- | --- | --- |"

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
    """启动应用（含 lifespan）。"""
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_openapi_schema_is_complete(client: TestClient) -> None:
    """OpenAPI 必须覆盖 §1~§8 的全部路由。"""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    required = [
        "/api/v1/health",
        "/api/v1/capabilities",
        "/api/v1/stats",
        "/api/v1/providers",
        "/api/v1/providers/{provider_id}",
        "/api/v1/providers/{provider_id}/health",
        "/api/v1/providers/discover",
        "/api/v1/providers/roles",
        "/api/v1/providers/usage",
        "/api/v1/spaces",
        "/api/v1/spaces/import",
        "/api/v1/spaces/{space_id}",
        "/api/v1/spaces/{space_id}/persona",
        "/api/v1/spaces/{space_id}/persona/suggest",
        "/api/v1/spaces/{space_id}/settings",
        "/api/v1/spaces/{space_id}/reindex",
        "/api/v1/spaces/{space_id}/export",
        "/api/v1/spaces/{space_id}/documents",
        "/api/v1/spaces/{space_id}/documents/upload",
        "/api/v1/spaces/{space_id}/documents/url",
        "/api/v1/spaces/{space_id}/documents/paste",
        "/api/v1/spaces/{space_id}/documents/{document_id}",
        "/api/v1/spaces/{space_id}/documents/{document_id}/content",
        "/api/v1/spaces/{space_id}/documents/{document_id}/chunks",
        "/api/v1/spaces/{space_id}/documents/{document_id}/raw",
        "/api/v1/spaces/{space_id}/documents/{document_id}/reprocess",
        "/api/v1/spaces/{space_id}/ingest/stream",
        "/api/v1/spaces/{space_id}/search",
        "/api/v1/spaces/{space_id}/conversations",
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/conversations/{conversation_id}/chat",
        "/api/v1/conversations/{conversation_id}/stop",
        "/api/v1/spaces/{space_id}/cards",
        "/api/v1/spaces/{space_id}/cards/extract",
        "/api/v1/spaces/{space_id}/graph",
        "/api/v1/spaces/{space_id}/insights",
        "/api/v1/spaces/{space_id}/insights/conflicts",
        "/api/v1/spaces/{space_id}/insights/conflicts/{group_id}/resolve",
        "/api/v1/spaces/{space_id}/insights/{insight_id}/lineage",
        "/api/v1/traces/{trace_id}",
        "/api/v1/traces/{trace_id}/feedback",
        "/api/v1/traces/{trace_id}/judge",
        "/api/v1/spaces/{space_id}/evolve/pending",
        "/api/v1/spaces/{space_id}/evolve/distill",
        "/api/v1/spaces/{space_id}/evolve/consolidate",
        "/api/v1/spaces/{space_id}/evolve/cycle",
        "/api/v1/spaces/{space_id}/evolve/history",
        "/api/v1/spaces/{space_id}/expertise",
        "/api/v1/spaces/{space_id}/expertise/history",
        "/api/v1/spaces/{space_id}/expertise/gaps",
        "/api/v1/spaces/{space_id}/evals",
        "/api/v1/spaces/{space_id}/evals/generate",
        "/api/v1/spaces/{space_id}/evals/{eval_id}",
        "/api/v1/spaces/{space_id}/evals/run",
        "/api/v1/spaces/{space_id}/evals/runs",
    ]
    missing = [path for path in required if path not in paths]
    assert not missing, f"OpenAPI 缺少路由：{missing}"

    # 每个操作都必须声明响应模型，前端据此生成类型
    no_model = [
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        for method, operation in operations.items()
        if "200" in operation.get("responses", {})
        and "content" in operation["responses"]["200"]
        and "application/json" in operation["responses"]["200"]["content"]
        and "schema" not in operation["responses"]["200"]["content"]["application/json"]
    ]
    assert not no_model, f"缺少 response_model：{no_model}"


def test_system_routes(client: TestClient) -> None:
    """§1 三个路由。"""
    health = client.get(f"{API}/health").json()
    assert health["status"] == "ok"
    assert health["version"]
    assert health["uptime_ms"] >= 0

    capabilities = client.get(f"{API}/capabilities").json()
    assert set(capabilities) == {
        "rerank_available",
        "local_embedding",
        "docling_available",
        "graph_enabled",
        "chat_available",
        "distill_available",
    }
    # 这个测试环境的 models.yaml 绑定了 mock provider，两个角色都算可用；
    # 真实环境里没绑定模型时界面据此解释空态原因
    assert capabilities["chat_available"] is True
    assert capabilities["distill_available"] is True

    stats = client.get(f"{API}/stats").json()
    assert stats["space_count"] == 0
    assert stats["disk_usage_bytes"] >= 0


def test_providers_routes(client: TestClient, mock_server: str) -> None:
    """§2 provider 列表、角色、用量与维度守卫。"""
    providers = client.get(f"{API}/providers").json()
    assert any(item["id"] == "local-qwen" for item in providers)

    roles = client.get(f"{API}/providers/roles").json()
    assert roles["chat"]

    usage = client.get(f"{API}/providers/usage").json()
    assert usage["group_by"] == "provider"

    # 新增 → 更新 → 删除
    created = client.post(
        f"{API}/providers",
        json={
            "id": "mock-local",
            "kind": "llm",
            "adapter": "openai_compatible",
            "base_url": mock_server,
            "model": "mock-chat",
        },
    )
    assert created.status_code == 201
    patched = client.patch(f"{API}/providers/mock-local", json={"model": "mock-chat-2"})
    assert patched.json()["model"] == "mock-chat-2"

    health = client.post(f"{API}/providers/mock-local/health").json()
    assert health["ok"] is True
    assert health["resolved_model"] in {"mock-chat", "mock-chat-2"}

    discovered = client.post(
        f"{API}/providers/discover", json={"adapter": "openai_compatible", "base_url": mock_server}
    ).json()
    assert {item["id"] for item in discovered["models"]} == {"mock-chat", "mock-embed"}

    assert client.delete(f"{API}/providers/mock-local").json()["deleted"] is True
    assert client.delete(f"{API}/providers/mock-local").status_code == 404

    # 被角色引用的 provider 不允许删除
    in_use = client.delete(f"{API}/providers/local-qwen")
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "PROVIDER_IN_USE"

    # embedding 维度变化 → 提示重建索引
    switched = client.put(
        f"{API}/providers/roles",
        json={"roles": {"chat": "local-qwen", "embedding": "local-embedding"}},
    )
    assert switched.status_code == 200
    assert switched.json()["requires_reindex"] is False


def test_space_lifecycle(client: TestClient) -> None:
    """§3 Space 的增查改、persona 与 settings。"""
    created = client.post(
        f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现与临床开发"}
    )
    assert created.status_code == 201
    space_id = created.json()["id"]

    listing = client.get(f"{API}/spaces").json()
    assert listing[0]["id"] == space_id
    assert listing[0]["doc_count"] == 0
    assert listing[0]["expertise_overall"] is None

    detail = client.get(f"{API}/spaces/{space_id}").json()
    assert detail["config"]["persona"]["domain"] == "药物发现与临床开发"

    patched = client.patch(f"{API}/spaces/{space_id}", json={"name": "新药研发工程"})
    assert patched.json()["name"] == "新药研发工程"

    persona = client.get(f"{API}/spaces/{space_id}/persona").json()
    persona["principles"] = ["先静态后动态"]
    updated = client.put(f"{API}/spaces/{space_id}/persona", json=persona).json()
    assert updated["principles"] == ["先静态后动态"]

    suggestion = client.post(f"{API}/spaces/{space_id}/persona/suggest", json={}).json()
    assert suggestion["persona"]["name"]

    settings = client.get(f"{API}/spaces/{space_id}/settings").json()
    assert settings["top_k_vector"] == 50
    assert settings["diversity"] is True
    assert settings["mmr_lambda"] == 0.7
    assert settings["dedup_threshold"] == 0.85
    saved = client.put(
        f"{API}/spaces/{space_id}/settings",
        json={"retrieval": {"top_n_rerank": 5, "diversity": False, "mmr_lambda": 0.5}},
    ).json()
    assert saved["retrieval"]["top_n_rerank"] == 5
    assert saved["retrieval"]["diversity"] is False
    assert saved["retrieval"]["mmr_lambda"] == 0.5

    exported = client.get(f"{API}/spaces/{space_id}/export")
    assert exported.status_code == 200
    assert exported.content[:2] == b"PK"

    deleted = client.delete(f"{API}/spaces/{space_id}", params={"purge": "true"})
    assert deleted.json()["deleted"] is True
    assert client.get(f"{API}/spaces/{space_id}").status_code == 404


def _wait_for_status(
    client: TestClient,
    space_id: str,
    document_id: str,
    targets: set[str],
    *,
    timeout_seconds: float = 20.0,
) -> str:
    """轮询文档状态直到进入目标状态之一，超时则失败。

    摄取是后台 asyncio 任务，没有同步等待的接口；轮询是前端与测试共同的正确姿势。
    """
    deadline = time.monotonic() + timeout_seconds
    status = ""
    while time.monotonic() < deadline:
        response = client.get(f"{API}/spaces/{space_id}/documents/{document_id}")
        assert response.status_code == 200
        status = str(response.json()["status"])
        if status in targets:
            return status
        time.sleep(0.05)
    raise AssertionError(f"等待摄取超时，最后状态为 {status!r}，期望 {targets}")


def test_document_paste_and_ingest(client: TestClient, settings: Settings) -> None:
    """§4 粘贴入库：立即返回 pending，后台推进到 embedding。"""
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}).json()[
        "id"
    ]

    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={
            "title": "成药性笔记",
            "content": "# 第3章 成药性\n\n先导化合物需先评估 ADMET 五项。",
        },
    )
    assert created.status_code == 201
    document_id = created.json()["id"]
    assert created.json()["status"] == "pending"

    content = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/content")
    assert content.status_code == 200
    assert content.json()["markdown"].startswith("# 第3章 成药性")

    listing = client.get(f"{API}/spaces/{space_id}/documents").json()
    assert listing["total"] == 1

    # 摄取是后台任务，切分与向量化在 paste 返回之后才发生。
    # 这里轮询 status —— 和前端的真实用法一致，而不是假设它已经跑完。
    _wait_for_status(client, space_id, document_id, {"ready", "failed"})
    detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
    assert detail["status"] == "ready", f"摄取失败：{detail.get('error')}"

    chunks = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks").json()
    assert chunks["total"] >= 1

    raw = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/raw")
    assert raw.status_code == 200
    assert "成药性" in raw.text

    # 重复粘贴同样内容 → 409 DUPLICATE_DOCUMENT
    duplicate = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={
            "title": "成药性笔记",
            "content": "# 第3章 成药性\n\n先导化合物需先评估 ADMET 五项。",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_DOCUMENT"

    assert client.delete(f"{API}/spaces/{space_id}/documents/{document_id}").json()["deleted"]
    assert client.get(f"{API}/spaces/{space_id}/documents/{document_id}").status_code == 404


def test_chunk_offsets_locate_citations_in_document_content(
    client: TestClient, settings: Settings
) -> None:
    """引用高亮的定位依据：chunks 的偏移必须能切回 content 返回的全文。

    前端拿 ``GET /documents/{id}/content`` 的 markdown 渲染全文，再拿 ``/chunks``
    的 ``char_start`` / ``char_end`` 在上面画高亮。两个端点若对不上，用户点引用就
    会跳到错误的位置——而这条链路没有任何类型能保证，只能靠断言。

    表格续段是唯一的例外：它的 ``content`` 开头多一段重复的表头（列名），
    偏移只覆盖它自己的行区间。
    """
    table = "\n".join(
        [TABLE_HEADER, TABLE_DELIMITER]
        + [f"| CMPD-{index:03d} | {index * 7 % 900 + 5} | active |" for index in range(200)]
    )
    english = " ".join(
        f"Sentence number {index} describes the pharmacokinetic profile in detail."
        for index in range(160)
    )
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "参数表与英文长段", "content": f"# 参数汇总\n\n{table}\n\n{english}"},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]
    _wait_for_status(client, space_id, document_id, {"ready", "failed"})

    markdown = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/content").json()[
        "markdown"
    ]
    listed = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=500").json()
    chunks = listed["items"]

    assert len(chunks) > 5, "长表格与英文长段都应该被切成多条"
    assert listed["total"] == len(chunks), "limit=500 应当覆盖全部切片"
    # 概要切片没有可定位的原文区间（char_start = char_end = 0），偏移不变量只对正文成立
    for chunk in chunks:
        if chunk["kind"] == "summary":
            assert chunk["char_start"] == chunk["char_end"] == 0
            continue
        prefix_len = len(chunk["content"]) - (chunk["char_end"] - chunk["char_start"])
        assert 0 <= prefix_len <= len(chunk["content"])
        body = chunk["content"][prefix_len:]
        assert markdown[chunk["char_start"] : chunk["char_end"]] == body, (
            f"ordinal={chunk['ordinal']} 的偏移切不回正文"
        )
        if prefix_len:
            # 合成前缀只能是重复的表头，且不落在偏移区间里
            assert chunk["content"].startswith(TABLE_HEADER)
            assert not body.startswith(TABLE_HEADER)

    # 前端按 limit 取数；这个参数是后端真正认的那个（page / page_size 会被忽略）
    limited = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=2").json()
    assert len(limited["items"]) == 2
    assert limited["total"] == listed["total"]


def test_blank_paste_is_marked_failed_not_ready(client: TestClient) -> None:
    """贴进一段空白内容：必须判失败并说明原因，不能显示成 ready。

    空白、扫描件、纯图片 PDF 都会落到这条路径。显示 ready 会让用户以为资料
    进库了，其实一个字都检索不到。
    """
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "空白笔记", "content": "   \n\n  "},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]

    _wait_for_status(client, space_id, document_id, {"ready", "failed"})

    detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
    assert detail["status"] == "failed"
    assert "扫描件" in (detail["error"] or "")


def test_chunks_are_paginated_by_cursor(client: TestClient, settings: Settings) -> None:
    """切片列表要能翻页拿全：阅读器按偏移定位某一条，缺页就等于它不存在。"""
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    content = "# 长文\n\n" + "\n\n".join(
        f"第 {index} 段讨论药代动力学参数与清除率的关系，需要与半衰期一起看。"
        for index in range(40)
    )
    document_id = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "长文", "content": content},
    ).json()["id"]
    _wait_for_status(client, space_id, document_id, {"ready", "failed"})

    first = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=3").json()
    assert len(first["items"]) == 3
    total = first["total"]
    assert total > 3 and first["next_cursor"]

    collected = list(first["items"])
    cursor = first["next_cursor"]
    while cursor:
        page = client.get(
            f"{API}/spaces/{space_id}/documents/{document_id}/chunks",
            params={"limit": 3, "cursor": cursor},
        ).json()
        collected.extend(page["items"])
        cursor = page["next_cursor"]

    assert len(collected) == total
    ordinals = [chunk["ordinal"] for chunk in collected]
    assert ordinals == sorted(set(ordinals)), "翻页不能重复或乱序"


def test_upload_multipart(client: TestClient, tmp_path: Path) -> None:
    """§4 文件上传返回待处理文档。"""
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}).json()[
        "id"
    ]
    payload = "# 笔记\n\n这是一份用于测试上传的 Markdown 文档。".encode()
    response = client.post(
        f"{API}/spaces/{space_id}/documents/upload",
        files={"files": ("note.md", payload, "text/markdown")},
    )
    assert response.status_code == 200
    documents = response.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["status"] == "pending"
    assert documents[0]["sha256"]


def test_batch_upload_keeps_good_files_when_one_is_rejected(client: TestClient) -> None:
    """一批里有一个重复文件：其余照收，重复的单独报原因，原文档的原始文件不能被误删。

    此前一个出错整批返回 409，排在前面的文件却已登记并在后台处理——界面说失败，
    列表里又冒出新文档。
    """
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}).json()[
        "id"
    ]
    original = "# 旧笔记\n\n库里已经有这一份。".encode()
    first = client.post(
        f"{API}/spaces/{space_id}/documents/upload",
        files={"files": ("old.md", original, "text/markdown")},
    ).json()["documents"][0]
    raw_before = Path(first["source_uri"])
    assert raw_before.is_file()

    response = client.post(
        f"{API}/spaces/{space_id}/documents/upload",
        files=[
            ("files", ("new.md", "# 新笔记\n\n这是新的。".encode(), "text/markdown")),
            ("files", ("old.md", original, "text/markdown")),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [doc["title"] for doc in body["documents"]] == ["new.md"]
    assert [item["filename"] for item in body["rejected"]] == ["old.md"]
    assert body["rejected"][0]["code"] == "DUPLICATE_DOCUMENT"
    assert raw_before.is_file(), "重复文件复用的是原文档的原始文件，不能删"

    # 全部被拒时仍按原来的错误语义返回
    only_dup = client.post(
        f"{API}/spaces/{space_id}/documents/upload",
        files={"files": ("old.md", original, "text/markdown")},
    )
    assert only_dup.status_code == 409


def test_error_bodies(client: TestClient) -> None:
    """统一错误体与状态码。"""
    missing = client.get(f"{API}/spaces/nope")
    assert missing.status_code == 404
    body = missing.json()
    assert set(body["error"]) == {"code", "message", "detail"}
    assert body["error"]["code"] == "NOT_FOUND"

    invalid = client.post(f"{API}/spaces", json={"name": ""})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    unknown = client.get(f"{API}/providers/does-not-exist")
    assert unknown.status_code == 404


def test_phase3_routes_are_implemented(client: TestClient) -> None:
    """§6 L3 经验、§7 进化、§8 专家度在 Phase 3 全部落地，不再返回 501。

    这些只读路由在空 Space 上应正常返回 200（空列表 / 零分），而不是 501。
    """
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}).json()[
        "id"
    ]
    for path in [
        f"{API}/spaces/{space_id}/insights",
        f"{API}/spaces/{space_id}/expertise",
        f"{API}/spaces/{space_id}/evals",
        f"{API}/spaces/{space_id}/evolve/pending",
        f"{API}/spaces/{space_id}/evolve/history",
        f"{API}/spaces/{space_id}/evals/runs",
        f"{API}/spaces/{space_id}/insights/conflicts",
    ]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"


async def test_ingest_sse_stream(settings: Settings) -> None:
    """摄取 SSE 流按 event/data 帧输出，断开后不残留订阅。

    ⚠️ 这里**故意不走 HTTP 客户端**。``httpx.ASGITransport`` 会 ``await`` 整个 ASGI
    应用执行完毕才返回响应对象——对永不结束的 SSE 流就是死锁；starlette 的
    ``TestClient`` 虽支持流式，但在流未关闭时从同一线程再发请求会与它的 portal 互锁。
    要测的本来就是我们自己的分帧与总线接线，直接消费 ``body_iterator`` 最干净可靠。
    """
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        bus = app.state.bus
        runtime = app.state.runtime
        # 建一个真实存在的 Space：`ingest_stream` 现在会先 require_space，
        # 对不存在的 Space 直接 404（此前它照样返回 200 并把连接一直挂着）
        space = await runtime.spaces.create_space(SpaceCreate(name="摄取流", domain="测试"))
        space_id = space.id

        response = await ingest_stream(runtime, bus, space_id)
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"

        received: list[str] = []

        async def pump() -> None:
            async for chunk in cast(StreamingResponse, response).body_iterator:
                text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                received.append(text)
                if "event: status" in text:
                    break

        pumper = asyncio.create_task(pump())

        # 订阅是惰性建立的（首次 anext 才注册队列），所以持续重发直到被收到，
        # 而不是发一次就指望它一定落在订阅窗口内。
        async def publish_until_done() -> None:
            while not pumper.done():
                bus.publish_progress(space_id, "doc-1", "embedding", 1, 2)
                bus.publish(space_id, StatusEvent(document_id="doc-1", status="ready"))
                await asyncio.sleep(0.02)

        publisher = asyncio.create_task(publish_until_done())
        try:
            await asyncio.wait_for(pumper, timeout=10)
        finally:
            publisher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await publisher
            await cast(StreamingResponse, response).body_iterator.aclose()  # type: ignore[attr-defined]

        # 断开后订阅必须被摘掉，否则每开一次流就泄漏一个队列
        assert not bus._subscribers.get(space_id)

    body = "".join(received)
    assert "event: progress" in body
    assert "event: status" in body
    assert "data: {" in body


def test_settings_injection(settings: Settings) -> None:
    """显式传入 settings 时应用正常构造与启动。"""
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get(f"{API}/health").json()["status"] == "ok"


def test_models_config_roundtrip(tmp_path: Path) -> None:
    """写回模型配置时保留 ${VAR} 占位符。"""
    from agentmem.config import ModelsConfig, load_models_config, save_models_config

    path = tmp_path / "models.yaml"
    path.write_text(
        "providers:\n"
        "  - id: cloud\n"
        "    kind: llm\n"
        "    adapter: openai_compatible\n"
        "    model: m\n"
        "    api_key: ${MY_TEST_KEY}\n"
        "roles:\n"
        "  chat: cloud\n",
        encoding="utf-8",
    )
    config = load_models_config(path, env={"MY_TEST_KEY": "secret-value"})
    assert config.providers[0].api_key == "secret-value"
    assert config.roles.chat == "cloud"

    save_models_config(config, path)
    text = path.read_text(encoding="utf-8")
    assert "secret-value" not in text
    assert "${MY_TEST_KEY}" in text

    reloaded = load_models_config(path, env={"MY_TEST_KEY": "secret-value"})
    assert isinstance(reloaded, ModelsConfig)
    assert reloaded.providers[0].api_key == "secret-value"


def test_role_provider_config_helper() -> None:
    """角色绑定工具函数。"""
    config = ModelsConfig(
        providers=[
            ProviderConfig(id="a", kind="llm", adapter="openai_compatible"),
            ProviderConfig(id="b", kind="embedding", adapter="openai_compatible"),
        ],
        roles=RoleBindings(chat="a", embedding="b"),
    )
    assert config.roles.chat == "a"
    assert config.roles.rerank is None


async def test_raw_download_rejects_path_outside_space(settings: Settings) -> None:
    """不可信的 ``raw_path`` 不得让下载接口读到 Space 目录之外的文件。

    攻击面是真实存在的：Space 可以从 zip 导入，压缩包里的 ``meta.db`` 由外部提供，
    其中的路径字段可被构造成 ``/etc/hosts`` 这类系统文件。

    ⚠️ 必须**等摄取结束**再投毒——后台流水线跑完会重写 ``meta``，
    在它之前投毒会被悄悄覆盖，测试就变成了假通过（本用例最初就踩了这个坑）。
    """
    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}
        ).json()["id"]
        document = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "x", "content": "hello"},
        ).json()
        _wait_for_status(client, space_id, document["id"], {"ready", "failed"})

        async def poison() -> None:
            database = await app.state.runtime.space_db(space_id)
            row = await database.documents.require(document["id"])
            meta = row.meta.model_copy(update={"raw_path": "/etc/hosts"})
            await database.documents.update(
                document["id"], DocumentUpdate(meta=meta, source_uri="/etc/hosts")
            )

        with start_blocking_portal() as portal:
            portal.call(poison)

        response = client.get(f"{API}/spaces/{space_id}/documents/{document['id']}/raw")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
        assert b"Host Database" not in response.content


async def test_url_ingest_blocks_ssrf(settings: Settings) -> None:
    """抓取网页的入口不得被用来探测内网与云元数据。"""
    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}
        ).json()["id"]
        for hostile in (
            "http://127.0.0.1:9/x",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ):
            response = client.post(
                f"{API}/spaces/{space_id}/documents/url",
                json={"url": hostile, "crawl_depth": 0},
            )
            assert response.status_code == 422, hostile
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_validation_error_does_not_echo_api_key(client: TestClient) -> None:
    """422 响应体不得回显用户刚提交的明文密钥。"""
    response = client.post(
        f"{API}/providers",
        json={"id": "", "kind": "llm", "adapter": "openai_compatible", "api_key": "sk-LEAK-1234"},
    )
    assert response.status_code == 422
    assert "sk-LEAK-1234" not in response.text


async def test_delete_document_cascades(settings: Settings) -> None:
    """删除文档必须一次清干净：切片、全文索引、向量、解析缓存。

    这条级联原先散在 API 层，Phase 2 下沉到 ``runtime.delete_document``；
    测试直接查四个存储面，防止某一步在重构里被漏掉。
    """
    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}
        ).json()["id"]
        document = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "成药性笔记", "content": "# 成药性\n\n先导化合物需先评估 ADMET 五项。"},
        ).json()
        _wait_for_status(client, space_id, document["id"], {"ready", "failed"})

        async def state() -> tuple[int, int, int, bool]:
            database = await app.state.runtime.space_db(space_id)
            vectors = database.vectors
            assert vectors is not None
            return (
                await database.chunks.count(document_id=document["id"]),
                await database.fts.count(),
                await vectors.count("chunks_vec", space_id),
                (settings.data_dir / "cache" / space_id / f"{document['id']}.parse.json").is_file(),
            )

        with start_blocking_portal() as portal:
            before = portal.call(state)
        # 一篇文档两条切片：正文 + 文档概要（概要切片让「跨全文汇总」类问题有据可依）
        assert before[:3] == (2, 2, 2), "摄取应当产出切片、全文索引与向量"
        assert before[3] is True, "解析缓存应当存在"

        deleted = client.delete(f"{API}/spaces/{space_id}/documents/{document['id']}")
        assert deleted.json() == {"deleted": True, "id": document["id"]}

        with start_blocking_portal() as portal:
            after = portal.call(state)
        assert after == (0, 0, 0, False)


# ---------------------------------------------------------------------------
# Provider 失联告警
# ---------------------------------------------------------------------------


async def _record_usage(runtime: Runtime, *, provider_id: str, ok: bool, when: int) -> None:
    """往全局用量表里塞一条调用记录。"""
    from agentmem.types import UsageRecordCreate

    database = runtime.spaces.global_db
    record = await database.usage.create(
        UsageRecordCreate(provider_id=provider_id, model="m", kind="llm", ok=ok)
    )
    await database.sqlite.execute(
        "UPDATE usage_records SET created_at = ? WHERE id = ?", (when, record.id)
    )


def test_provider_alerts_reports_failures_and_recovery(client: TestClient) -> None:
    """失败要报出来；之后又成功了就标成已恢复，不再当故障报警。"""
    from anyio.from_thread import start_blocking_portal

    from agentmem.store.sqlite import now_ms

    runtime = app_runtime(client)
    now = now_ms()

    async def seed() -> None:
        # 连不上的那个：失败之后再没成功过
        await _record_usage(runtime, provider_id="dead-llm", ok=False, when=now - 60_000)
        # 抖了一下又通的那个
        await _record_usage(runtime, provider_id="flaky-llm", ok=False, when=now - 120_000)
        await _record_usage(runtime, provider_id="flaky-llm", ok=True, when=now - 30_000)
        # 一直好好的那个不该出现在告警里
        await _record_usage(runtime, provider_id="healthy-llm", ok=True, when=now - 10_000)
        # 窗口之外的旧故障也不该再报
        await _record_usage(runtime, provider_id="ancient-llm", ok=False, when=now - 86_400_000)

    with start_blocking_portal() as portal:
        portal.call(seed)

    body = client.get(f"{API}/system/alerts").json()
    by_id = {item["provider_id"]: item for item in body["alerts"]}

    assert set(by_id) == {"dead-llm", "flaky-llm"}
    assert by_id["dead-llm"]["recovered"] is False
    assert by_id["flaky-llm"]["recovered"] is True
    assert by_id["flaky-llm"]["calls"] == 2
    assert body["degraded"] is True, "还有没恢复的，界面要报警"


def test_provider_alerts_are_empty_when_everything_works(client: TestClient) -> None:
    """没有失败记录时不报警——空态要诚实，不能拿旧数据凑。"""
    body = client.get(f"{API}/system/alerts").json()
    assert body["alerts"] == []
    assert body["degraded"] is False


def test_ingest_stream_rejects_unknown_space(client: TestClient) -> None:
    """对不存在的 Space 开摄取流要 404，不能返回 200 把连接挂着。

    此前这个端点不校验 Space，直接订阅总线：调用方写错 id 时看不到任何报错
    （别的端点这时都是 404），只留下一个永远等不到事件的订阅。
    实际踩到过——mock 模式的演示 Space id 泄漏成真实请求时，后端照单全收。
    """
    response = client.get(f"{API}/spaces/does-not-exist/ingest/stream")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_editing_domain_reaches_the_persona(client: TestClient) -> None:
    """改领域要同步进 space.yaml 的 persona：所有提示词读的都是 persona.domain。

    此前 PATCH 只改库：界面上领域已经改成「高校教务」，重新生成的大纲还是按「长大助手」。
    默认生成的名称与角色描述跟着换；用户自己写过的原样保留。
    """
    space_id = client.post(f"{API}/spaces", json={"name": "长大助手", "domain": "长大助手"}).json()[
        "id"
    ]
    client.patch(f"{API}/spaces/{space_id}", json={"domain": "长江大学教务与校园事务"})
    persona = client.get(f"{API}/spaces/{space_id}/persona").json()
    assert persona["domain"] == "长江大学教务与校园事务"
    assert "长江大学教务与校园事务" in persona["role_description"], "默认角色描述跟着领域换"
    assert persona["name"] == "长大助手专家"

    persona["role_description"] = "我自己写的角色设定"
    client.put(f"{API}/spaces/{space_id}/persona", json=persona)
    client.patch(f"{API}/spaces/{space_id}", json={"name": "长大小助手", "domain": "长江大学"})
    after = client.get(f"{API}/spaces/{space_id}/persona").json()
    assert after["domain"] == "长江大学"
    assert after["role_description"] == "我自己写的角色设定", "用户写过的不能被覆盖"
    assert after["name"] == "长大小助手专家"


def test_retry_failed_documents_includes_all_pages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 200 篇失败资料也必须全部处理，完成事件反映真实成功数量。"""
    space_id = client.post(f"{API}/spaces", json={"name": "批量重试", "domain": "工程"}).json()[
        "id"
    ]
    runtime = app_runtime(client)
    seeded: set[str] = set()

    async def seed() -> None:
        database = await runtime.spaces.space_db(space_id)
        for index in range(201):
            document = await database.documents.create(
                DocumentCreate(
                    space_id=space_id,
                    title=f"失败资料 {index}",
                    source_type="paste",
                    sha256=f"retry-{index}",
                    status="failed",
                )
            )
            seeded.add(document.id)

    assert client.portal is not None
    client.portal.call(seed)
    processed: set[str] = set()

    async def retry(
        pipeline: IngestPipeline, document_ids: Sequence[str], *, concurrency: int
    ) -> AsyncIterator[DocumentOutcome]:
        assert concurrency > 0
        for document_id in document_ids:
            processed.add(document_id)
            await pipeline.db.documents.set_status(document_id, "ready")
            yield DocumentOutcome(document_id=document_id, ok=True)

    monkeypatch.setattr("apps.api.routers.documents.run_documents", retry)
    response = client.post(f"{API}/spaces/{space_id}/documents/retry-failed")
    assert response.status_code == 200
    payloads = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[0] == {"total": 201}
    assert payloads[-1] == {"total": 201, "retried": 201}
    assert processed == seeded
    assert (
        client.get(f"{API}/spaces/{space_id}/documents", params={"status": "failed"}).json()[
            "total"
        ]
        == 0
    )
