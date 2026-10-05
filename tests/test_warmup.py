"""启动预热：只碰本地模型与分词词典、不拖启动、失败只记日志；并发加载只加载一份。

全部用假的 provider / 假的模型类，不加载任何真实权重。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentmem.config import ModelsConfig, Settings, save_models_config
from agentmem.providers.adapters import local_embedding, local_rerank
from agentmem.providers.registry import ProviderRegistry
from agentmem.space.runtime import Runtime
from agentmem.store import fts
from agentmem.types import ProviderConfig, ProviderHealth, RoleBindings, SpaceCreate


def _models() -> ModelsConfig:
    return ModelsConfig(
        providers=[
            ProviderConfig(
                id="local-emb", kind="embedding", adapter="sentence_transformers", model="fake"
            ),
            ProviderConfig(
                id="remote-rerank",
                kind="rerank",
                adapter="cohere_rerank",
                base_url="http://127.0.0.1:9",
                model="fake",
            ),
            ProviderConfig(
                id="local-rerank-b",
                kind="rerank",
                adapter="sentence_transformers_ce",
                model="fake",
            ),
            ProviderConfig(
                id="local-emb-off",
                kind="embedding",
                adapter="sentence_transformers",
                model="fake",
                enabled=False,
            ),
        ],
        roles=RoleBindings(embedding="local-emb", rerank="remote-rerank"),
    )


def _settings(tmp_path: Path, *, warmup: bool) -> Settings:
    config_path = tmp_path / "models.yaml"
    save_models_config(_models(), config_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, models_config=config_path, warmup_on_start=warmup)


async def _seed_space_overrides(runtime: Runtime) -> None:
    """两个 Space 各覆盖一个角色：一个指向本地重排，一个指向已停用的本地向量。"""
    await runtime.spaces.open()
    first = await runtime.spaces.create_space(SpaceCreate(name="甲", domain="测试"))
    second = await runtime.spaces.create_space(SpaceCreate(name="乙", domain="测试"))
    for space_id, role, provider_id in (
        (first.id, "rerank", "local-rerank-b"),
        (second.id, "embedding", "local-emb-off"),
    ):
        config = await runtime.spaces.read_config(space_id)
        config.models[role] = provider_id
        await runtime.spaces.write_config(space_id, config)


def test_tests_run_with_warmup_off_by_default(tmp_path: Path) -> None:
    """conftest 用环境变量把预热关掉：测试里新建的 Settings 不会去加载真实模型。"""
    assert Settings(data_dir=tmp_path).warmup_on_start is False


def test_warmup_is_on_outside_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTMEM_WARMUP_ON_START", raising=False)
    assert Settings(data_dir=tmp_path, _env_file=None).warmup_on_start is True


async def test_warmup_only_touches_enabled_local_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全局绑定 + Space 覆盖里的本地模型都预热；远端与已停用的不碰。"""
    called: list[str] = []

    async def fake_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        called.append(provider_id)
        return ProviderHealth(ok=True, latency_ms=1)

    monkeypatch.setattr(ProviderRegistry, "health", fake_health)
    runtime = Runtime(_settings(tmp_path, warmup=True))
    await _seed_space_overrides(runtime)
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._warmup_task, timeout=5)
    finally:
        await runtime.stop()
    assert called == ["local-emb", "local-rerank-b"]


async def test_warmup_failure_never_breaks_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        raise RuntimeError("模拟 Metal 断言之外的普通异常")

    monkeypatch.setattr(ProviderRegistry, "health", broken_health)
    runtime = Runtime(_settings(tmp_path, warmup=True))
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._warmup_task, timeout=5)
        assert runtime._warmup_task.exception() is None
    finally:
        await runtime.stop()


async def test_start_does_not_wait_for_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预热卡住（模型下载慢、加载慢）时启动照样立刻返回，关闭时任务被取消。"""
    gate = asyncio.Event()

    async def slow_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        await gate.wait()
        return ProviderHealth(ok=True)

    monkeypatch.setattr(ProviderRegistry, "health", slow_health)
    runtime = Runtime(_settings(tmp_path, warmup=True))
    await asyncio.wait_for(runtime.start(), timeout=5)
    task = runtime._warmup_task
    assert not task.done()
    await runtime.stop()
    assert task.cancelled()


async def test_warmup_disabled_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    async def fake_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        called.append(provider_id)
        return ProviderHealth(ok=True)

    monkeypatch.setattr(ProviderRegistry, "health", fake_health)
    runtime = Runtime(_settings(tmp_path, warmup=False))
    try:
        await runtime.start()
        await asyncio.sleep(0)
        assert getattr(runtime, "_warmup_task", None) is None
    finally:
        await runtime.stop()
    assert called == []


class _SlowFakeModel:
    """构造要花一点时间的假模型，用来制造「预热还没加载完，提问就来了」。"""

    loads = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).loads += 1
        import time

        time.sleep(0.05)

    def get_sentence_embedding_dimension(self) -> int:
        return 4


async def test_concurrent_instances_share_one_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """预热用全局实例、提问用 Space 实例：两者同时加载时只该加载一份。

    此前每个实例各有一把加载锁，缓存没命中的第二个实例会自己再加载一遍。
    """
    _SlowFakeModel.loads = 0
    monkeypatch.setattr(local_embedding, "_AVAILABLE", True)
    monkeypatch.setattr(local_embedding, "SentenceTransformer", _SlowFakeModel)
    monkeypatch.setattr(local_embedding, "_MODEL_CACHE", {})
    config = ProviderConfig(
        id="local-emb",
        kind="embedding",
        adapter="sentence_transformers",
        model="fake",
        device="cpu",
    )
    warm = local_embedding.LocalEmbeddingProvider(config)
    asking = local_embedding.LocalEmbeddingProvider(config)
    first, second = await asyncio.gather(warm._get_model(), asking._get_model())
    assert first is second
    assert _SlowFakeModel.loads == 1


async def test_loaded_model_is_not_blocked_by_another_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重排模型已就绪时，不能因为向量模型正在加载而跟着等。"""
    monkeypatch.setattr(local_rerank, "_AVAILABLE", True)
    config = ProviderConfig(
        id="local-rerank", kind="rerank", adapter="sentence_transformers_ce", model="fake"
    )
    provider = local_rerank.LocalRerankProvider(config)
    ready = object()
    provider._model = ready
    async with local_embedding.MODEL_LOAD_LOCK:
        got = await asyncio.wait_for(provider._get_model(), timeout=1)
    assert got is ready


async def test_warmup_loads_tokenizer_even_without_local_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全文检索总要分词：没绑本地模型也要预热 jieba 词典，且排在模型之前。"""
    order: list[str] = []

    def fake_warm_up() -> float:
        order.append("tokenizer")
        return 0.0

    async def fake_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        order.append(provider_id)
        return ProviderHealth(ok=True)

    async def no_targets(self: Runtime) -> list[tuple[str, str]]:
        return []

    monkeypatch.setattr(fts, "warm_up", fake_warm_up)
    monkeypatch.setattr(ProviderRegistry, "health", fake_health)
    runtime = Runtime(_settings(tmp_path, warmup=True))
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._warmup_task, timeout=5)
    finally:
        await runtime.stop()
    assert order[0] == "tokenizer"

    order.clear()
    monkeypatch.setattr(Runtime, "_warmup_targets", no_targets)
    runtime = Runtime(_settings(tmp_path / "second", warmup=True))
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._warmup_task, timeout=5)
    finally:
        await runtime.stop()
    assert order == ["tokenizer"]


async def test_tokenizer_warmup_failure_does_not_stop_model_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def broken_warm_up() -> float:
        raise OSError("词典缓存目录不可写")

    async def fake_health(self: ProviderRegistry, provider_id: str) -> ProviderHealth:
        called.append(provider_id)
        return ProviderHealth(ok=True)

    monkeypatch.setattr(fts, "warm_up", broken_warm_up)
    monkeypatch.setattr(ProviderRegistry, "health", fake_health)
    runtime = Runtime(_settings(tmp_path, warmup=True))
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._warmup_task, timeout=5)
        assert runtime._warmup_task.exception() is None
    finally:
        await runtime.stop()
    assert called == ["local-emb"]


async def test_warmup_disabled_skips_tokenizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[None] = []

    def fake_warm_up() -> float:
        called.append(None)
        return 0.0

    monkeypatch.setattr(fts, "warm_up", fake_warm_up)
    runtime = Runtime(_settings(tmp_path, warmup=False))
    try:
        await runtime.start()
        await asyncio.sleep(0)
    finally:
        await runtime.stop()
    assert called == []
