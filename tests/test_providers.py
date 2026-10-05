"""Provider 层：OpenAI 兼容适配器对 Mock HTTP 服务、注册表降级链与用量记录。"""

from __future__ import annotations

import pytest

from agentmem.config import ModelsConfig
from agentmem.errors import ProviderNotConfiguredError, ProviderUnavailableError
from agentmem.providers.adapters.ollama_native import OllamaNativeProvider
from agentmem.providers.adapters.openai_compatible import OpenAICompatibleProvider
from agentmem.providers.base import Message
from agentmem.providers.discover import discover_models
from agentmem.providers.health import check_config
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import ProviderConfig, RoleBindings


def _provider(base_url: str, **overrides: object) -> OpenAICompatibleProvider:
    payload: dict[str, object] = {
        "id": "mock",
        "kind": "llm",
        "adapter": "openai_compatible",
        "base_url": base_url,
        "model": "mock-chat",
    }
    payload.update(overrides)
    return OpenAICompatibleProvider(ProviderConfig.model_validate(payload))


async def test_chat_round_trip(mock_server: str) -> None:
    """非流式对话解析出内容、用量与耗时。"""
    provider = _provider(mock_server)
    try:
        result = await provider.chat([Message(role="user", content="你好")])
    finally:
        await provider.close()
    assert result.content == "你好"
    assert result.model == "mock-chat"
    assert result.provider_id == "mock"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2
    assert result.latency_ms >= 0
    assert result.finish_reason == "stop"


async def test_stream_chunks(mock_server: str) -> None:
    """流式对话按分片产出。"""
    provider = _provider(mock_server)
    try:
        collected = [
            chunk async for chunk in provider.stream([Message(role="user", content="你好")])
        ]
    finally:
        await provider.close()
    assert "".join(chunk.delta for chunk in collected) == "你好"
    assert any(chunk.finish_reason == "stop" for chunk in collected)


async def test_embedding_dimension_detected(mock_server: str) -> None:
    """embedding 调用会带上维度信息。"""
    provider = _provider(mock_server, model="mock-embed", kind="embedding")
    try:
        vectors = await provider.embed(["a", "b"])
    finally:
        await provider.close()
    assert len(vectors) == 2
    assert len(vectors[0]) == 8
    assert provider.dimension == 8


async def test_list_models_and_health(mock_server: str) -> None:
    """模型列表与健康检查。"""
    provider = _provider(mock_server)
    try:
        models = await provider.list_models()
        assert {item.id for item in models} == {"mock-chat", "mock-embed"}
        health = await provider.health()
    finally:
        await provider.close()
    assert health.ok is True
    assert health.resolved_model == "mock-chat"
    assert health.error is None


async def test_health_reports_failure() -> None:
    """连不上时必须返回 ok=False 而不是抛异常。"""
    config = ProviderConfig(
        id="dead",
        kind="llm",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:9/v1",
        model="nope",
    )
    health = await check_config(config)
    assert health.ok is False
    assert health.error


async def test_discover_models(mock_server: str) -> None:
    """探测可用模型列表。"""
    response = await discover_models("openai_compatible", mock_server)
    assert response.error is None
    assert {item.id for item in response.models} == {"mock-chat", "mock-embed"}


async def test_discover_ollama_tags(mock_server: str) -> None:
    """Ollama 走 /api/tags。"""
    root = mock_server.removesuffix("/v1")
    response = await discover_models("ollama_native", root)
    assert response.error is None
    assert response.models[0].id == "mock-chat"
    assert response.models[0].family == "mock"


async def test_ollama_native_models(mock_server: str) -> None:
    """原生适配器归一化 base_url 并列出模型。"""
    config = ProviderConfig(
        id="ollama",
        kind="llm",
        adapter="ollama_native",
        base_url=f"{mock_server.removesuffix('/v1')}/v1",
        model="mock-chat",
    )
    provider = OllamaNativeProvider(config)
    assert provider.root_url == mock_server.removesuffix("/v1")
    models = await provider.list_models()
    assert models[0].id == "mock-chat"


async def test_registry_fallback_chain(database: Database, mock_server: str) -> None:
    """主 provider 失败时自动降级，并记录两条用量（一失败一成功）。"""
    config = ModelsConfig(
        providers=[
            ProviderConfig(
                id="broken",
                kind="llm",
                adapter="openai_compatible",
                base_url="http://127.0.0.1:9/v1",
                model="broken",
            ),
            ProviderConfig(
                id="mock",
                kind="llm",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-chat",
            ),
        ],
        roles=RoleBindings(chat="broken"),
        fallbacks={"chat": ["mock"]},
    )
    registry = ProviderRegistry(config, usage=database.usage, space_id=database.space_id)
    route = registry.llm("chat")
    result = await route.chat([Message(role="user", content="你好")])
    assert result.provider_id == "mock"

    # 用量记录按时间倒序返回：先成功的 mock，再失败的 broken
    records, _ = await database.usage.list_by_space(database.space_id or "")
    assert [(record.provider_id, record.ok) for record in records] == [
        ("mock", True),
        ("broken", False),
    ]
    await registry.aclose()


async def test_registry_role_not_configured(database: Database) -> None:
    """角色未绑定 provider 时给出明确错误码。"""
    registry = ProviderRegistry(
        ModelsConfig(providers=[], roles=RoleBindings()),
        usage=database.usage,
    )
    with pytest.raises(ProviderNotConfiguredError) as excinfo:
        registry.llm("distill")
    assert excinfo.value.code == "PROVIDER_NOT_CONFIGURED"
    assert excinfo.value.http_status == 400


async def test_registry_kind_mismatch(database: Database, embed_config: ProviderConfig) -> None:
    """角色与 provider 类型不匹配时拒绝。"""
    config = ModelsConfig(
        providers=[embed_config],
        roles=RoleBindings(chat="mock-embed"),
    )
    registry = ProviderRegistry(config, usage=database.usage)
    with pytest.raises(ProviderNotConfiguredError):
        registry.llm("chat")


async def test_registry_provider_in_use(database: Database, embed_config: ProviderConfig) -> None:
    """被角色引用的 provider 不允许删除。"""
    registry = ProviderRegistry(
        ModelsConfig(providers=[embed_config], roles=RoleBindings(embedding="mock-embed")),
        usage=database.usage,
    )
    assert registry.roles_in_use("mock-embed") == ["embedding"]
    with pytest.raises(Exception) as excinfo:
        registry.assert_deletable("mock-embed")
    assert getattr(excinfo.value, "code", None) == "PROVIDER_IN_USE"


async def test_embedding_route_dimension(
    database: Database, mock_registry: ProviderRegistry
) -> None:
    """embedding 角色门面暴露维度，并写入用量。"""
    route = mock_registry.embedding()
    vectors = await route.embed(["你好"])
    assert len(vectors[0]) == 8
    assert route.dimension == 8
    records, _ = await database.usage.list_by_space(database.space_id or "")
    assert records and records[0].kind == "embedding"


async def test_translate_connection_error() -> None:
    """连接失败翻译成 PROVIDER_UNAVAILABLE。"""
    provider = _provider("http://127.0.0.1:9/v1")
    try:
        with pytest.raises(ProviderUnavailableError):
            await provider.chat([Message(role="user", content="hi")])
    finally:
        await provider.close()
