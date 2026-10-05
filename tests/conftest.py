"""测试公共夹具：临时数据目录、数据库、Mock 模型服务。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentmem.config import ModelsConfig, Settings
from agentmem.providers.registry import ProviderRegistry
from agentmem.space.runtime import Runtime
from agentmem.store import Database
from agentmem.types import ProviderConfig, RoleBindings

EMBED_DIM = 8


def app_runtime(client: TestClient) -> Runtime:
    """从测试客户端取出 :class:`Runtime`。

    ``TestClient.app`` 的静态类型是 ASGI 可调用对象，没有 ``state``——测试里要拿运行时
    做断言（改数据库、看缓存），都得先还原成 FastAPI。集中一处转换，省得每个用例
    各写一遍 cast。
    """
    return cast(Runtime, cast(FastAPI, client.app).state.runtime)


class MockModelHandler(BaseHTTPRequestHandler):
    """最小的 OpenAI 兼容服务，用于测试适配器与摄取链路。

    回复内容可配置，供对话链路的用例驱动出特定的正文（引用标记、多轮改写等）：

    - ``reply_text``：默认回复正文；
    - ``reply_rules``：``[(关键词, 回复)]``，请求体里出现关键词时优先命中；
    - ``stream_delay``：每个流式分片之间的间隔，用于模拟慢速生成。
    """

    protocol_version = "HTTP/1.1"

    reply_text: str = "你好"
    reply_rules: ClassVar[list[tuple[str, str]]] = []
    stream_delay: float = 0.0

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖父类签名
        """静默访问日志。"""

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """模型列表。"""
        if self.path.endswith("/models"):
            self._send(
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-chat", "object": "model", "owned_by": "mock"},
                        {"id": "mock-embed", "object": "model", "owned_by": "mock"},
                    ],
                }
            )
            return
        if self.path.endswith("/api/tags"):
            self._send(
                {"models": [{"name": "mock-chat", "size": 1024, "details": {"family": "mock"}}]}
            )
            return
        self._send({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        """chat / embeddings / rerank。"""
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            reply = self._select_reply(payload)
            if payload.get("stream"):
                self._send_stream(reply)
            else:
                self._send(
                    {
                        "id": "cmpl-1",
                        "object": "chat.completion",
                        "model": payload.get("model", "mock-chat"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": reply},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    }
                )
            return
        if self.path.endswith("/embeddings"):
            inputs = payload.get("input") or []
            if isinstance(inputs, str):
                inputs = [inputs]
            self._send(
                {
                    "object": "list",
                    "model": payload.get("model", "mock-embed"),
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [0.1] * EMBED_DIM}
                        for index, _ in enumerate(inputs)
                    ],
                }
            )
            return
        if self.path.endswith("/rerank"):
            # 打分要可复现且能改变顺序，否则测不出「重排真的生效了」：
            # 这里按文本长度降序给分，长文本排前面。
            documents = payload.get("documents") or []
            order = sorted(range(len(documents)), key=lambda index: -len(str(documents[index])))
            self._send(
                {
                    "results": [
                        {"index": index, "relevance_score": round(1.0 - rank * 0.1, 4)}
                        for rank, index in enumerate(order)
                    ]
                }
            )
            return
        self._send({"error": "not found"}, status=404)

    def _select_reply(self, payload: dict[str, Any]) -> str:
        """按请求体里是否出现关键词选择回复，默认用 ``reply_text``。"""
        request_text = json.dumps(payload.get("messages", []), ensure_ascii=False)
        for keyword, reply in type(self).reply_rules:
            if keyword in request_text:
                return reply
        return type(self).reply_text

    def _send_stream(self, reply: str) -> None:
        """按 OpenAI 规范输出 SSE 分片。

        正文逐字符下发：引用标记 ``[^c1]`` 因此必然被切成多片，
        正好用来验证流式引用解析不会漏检也不会重复发。
        """
        delay = type(self).stream_delay
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for text in reply:
            if delay:
                time.sleep(delay)
            chunk = {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "model": "mock-chat",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            self._write_chunk(f"data: {json.dumps(chunk)}\n\n")
        final = {
            "id": "cmpl-1",
            "object": "chat.completion.chunk",
            "model": "mock-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self._write_chunk(f"data: {json.dumps(final)}\n\n")
        # OpenAI 的 include_usage 形态：空 choices + usage，用于验证 token 落库
        usage = {
            "id": "cmpl-1",
            "object": "chat.completion.chunk",
            "model": "mock-chat",
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        self._write_chunk(f"data: {json.dumps(usage)}\n\n")
        self._write_chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _write_chunk(self, text: str) -> None:
        data = text.encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode())
        self.wfile.write(data)
        self.wfile.write(b"\r\n")


@pytest.fixture(scope="session")
def mock_server() -> Iterator[str]:
    """启动 Mock 模型服务，返回 base_url。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        # host 在 IPv6 下是 bytes，直接进 f-string 会拼成 b'::1'
        yield f"http://{host.decode() if isinstance(host, bytes) else host}:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def mock_reply(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """配置 Mock 模型的回复内容；用例结束后自动还原。

    用法::

        mock_reply("成药性要点[^c1]。")                     # 只改默认回复
        mock_reply("最终答案", [("改写关键词", "{...}")])   # 按请求关键词分流
    """

    def configure(
        text: str, rules: list[tuple[str, str]] | None = None, *, delay: float = 0.0
    ) -> None:
        monkeypatch.setattr(MockModelHandler, "reply_text", text)
        monkeypatch.setattr(MockModelHandler, "reply_rules", list(rules or []))
        monkeypatch.setattr(MockModelHandler, "stream_delay", delay)

    return configure


@pytest.fixture(autouse=True)
def _no_model_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里默认不做启动预热。

    预热会真的去加载 sentence-transformers 权重（每个几十秒、几个 GB）。各测试文件都
    自己建 Settings，逐个传参容易漏，所以统一用环境变量关掉；要测预热的用例显式传
    ``warmup_on_start=True``（显式参数优先于环境变量）。
    """
    monkeypatch.setenv("AGENTMEM_WARMUP_ON_START", "false")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """指向临时目录的全局设置。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, models_config=tmp_path / "models.yaml", env="dev")


@pytest.fixture
async def database(tmp_path: Path) -> Any:
    """一个打开好的 Space 级数据库。"""
    db = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-test")
    await db.open()
    yield db
    await db.close()


@pytest.fixture
def embed_config(mock_server: str) -> ProviderConfig:
    """指向 Mock 服务的 embedding provider。"""
    return ProviderConfig(
        id="mock-embed",
        kind="embedding",
        adapter="openai_compatible",
        base_url=mock_server,
        model="mock-embed",
        dimension=EMBED_DIM,
    )


@pytest.fixture
def mock_registry(database: Database, embed_config: ProviderConfig) -> ProviderRegistry:
    """只绑定 embedding 角色的注册表，用量写入被测数据库。"""
    config = ModelsConfig(
        providers=[embed_config],
        roles=RoleBindings(embedding="mock-embed"),
    )
    return ProviderRegistry(config, usage=database.usage, space_id=database.space_id)
