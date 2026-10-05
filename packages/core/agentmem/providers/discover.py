"""模型发现：给定 adapter 与 base_url，探测该端点可用的模型列表。

Ollama 走原生 ``GET /api/tags``；OpenAI 兼容端点走 ``GET /v1/models``。
"""

from __future__ import annotations

import httpx
import structlog

from agentmem.providers.adapters.ollama_native import OllamaNativeProvider, to_root_url
from agentmem.types import DiscoveredModel, DiscoverResponse, ProviderConfig

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT = 15.0

#: 走 Ollama 原生发现的适配器
OLLAMA_ADAPTERS = {"ollama_native"}


def looks_like_ollama(base_url: str | None) -> bool:
    """按端口/协议特征判断是否为本机 Ollama。"""
    if not base_url:
        return False
    value = base_url.lower()
    return "11434" in value or value.endswith("/api") or "/api/" in value


async def discover_ollama(base_url: str | None = None) -> list[DiscoveredModel]:
    """通过 ``GET /api/tags`` 列出 Ollama 已拉取的模型。"""
    config = ProviderConfig(id="discover", kind="llm", adapter="ollama_native", base_url=base_url)
    provider = OllamaNativeProvider(config)
    return await provider.list_models()


async def discover_openai(
    base_url: str | None, api_key: str | None = None
) -> list[DiscoveredModel]:
    """通过 ``GET /v1/models`` 列出 OpenAI 兼容端点的模型。"""
    if not base_url:
        raise ValueError("需要提供 base_url")
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    models: list[DiscoveredModel] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not identifier:
            continue
        owned_by = item.get("owned_by")
        models.append(
            DiscoveredModel(id=str(identifier), owned_by=str(owned_by) if owned_by else None)
        )
    return models


async def discover_models(
    adapter: str,
    base_url: str | None = None,
    api_key: str | None = None,
) -> DiscoverResponse:
    """探测某端点的可用模型。

    Args:
        adapter: 适配器名，见 ``docs/01-ARCHITECTURE.md`` §4.3。
        base_url: 服务地址。
        api_key: 可选鉴权信息。
    """
    try:
        if adapter in OLLAMA_ADAPTERS or looks_like_ollama(base_url):
            models = await discover_ollama(base_url)
        else:
            models = await discover_openai(base_url, api_key)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "discover_failed",
            adapter=adapter,
            base_url=base_url,
            status=exc.response.status_code,
        )
        return DiscoverResponse(
            adapter=adapter,
            base_url=_normalize_base_url(adapter, base_url),
            error=f"HTTP {exc.response.status_code}",
        )
    except Exception as exc:
        logger.warning("discover_failed", adapter=adapter, base_url=base_url, error=str(exc))
        return DiscoverResponse(
            adapter=adapter, base_url=_normalize_base_url(adapter, base_url), error=str(exc)
        )
    return DiscoverResponse(
        adapter=adapter,
        base_url=_normalize_base_url(adapter, base_url),
        models=models,
    )


def _normalize_base_url(adapter: str, base_url: str | None) -> str | None:
    if adapter in OLLAMA_ADAPTERS and base_url:
        return to_root_url(base_url)
    return base_url
