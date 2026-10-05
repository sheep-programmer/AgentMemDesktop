"""Provider 连通性实测。

适配器自身实现 ``health()`` 时直接复用；没有实现时按能力类型退化为一次最小调用。
"""

from __future__ import annotations

import time

import structlog

from agentmem.providers.base import (
    EmbeddingProvider,
    HealthCheckable,
    LLMProvider,
    Message,
    RerankProvider,
)
from agentmem.types import (
    ProviderConfig,
    ProviderHealth,
)

logger = structlog.get_logger(__name__)


async def check(provider: object) -> ProviderHealth:
    """实测任意 provider 的连通性。

    优先使用适配器自带的 ``health()``；否则按 Protocol 能力做最小调用。
    """
    if isinstance(provider, HealthCheckable):
        return await provider.health()
    started = time.perf_counter()
    try:
        if isinstance(provider, LLMProvider):
            result = await provider.chat([Message(role="user", content="ping")], max_tokens=1)
            latency = int((time.perf_counter() - started) * 1000)
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=result.model)
        if isinstance(provider, EmbeddingProvider):
            vectors = await provider.embed(["ping"])
            latency = int((time.perf_counter() - started) * 1000)
            if not vectors:
                return ProviderHealth(ok=False, latency_ms=latency, error="未返回向量")
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=provider.name)
        if isinstance(provider, RerankProvider):
            ranked = await provider.rerank("ping", ["ping"], top_n=1)
            latency = int((time.perf_counter() - started) * 1000)
            if not ranked:
                return ProviderHealth(ok=False, latency_ms=latency, error="未返回打分")
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=provider.name)
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        logger.warning("provider_health_failed", error=str(exc))
        return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))
    return ProviderHealth(ok=False, error=f"无法识别的 provider 类型：{type(provider).__name__}")


async def check_config(config: ProviderConfig) -> ProviderHealth:
    """按配置构造适配器并实测连通性（不经过注册表缓存）。"""
    from agentmem.providers.registry import ADAPTERS

    factory = ADAPTERS.get(config.adapter)
    if factory is None:
        return ProviderHealth(ok=False, error=f"未知适配器：{config.adapter}")
    try:
        provider = factory(config)
    except Exception as exc:
        logger.warning("provider_build_failed", provider=config.id, error=str(exc))
        return ProviderHealth(ok=False, error=str(exc))
    try:
        return await check(provider)
    finally:
        closer = getattr(provider, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception as exc:
                logger.warning("provider_close_failed", provider=config.id, error=str(exc))
