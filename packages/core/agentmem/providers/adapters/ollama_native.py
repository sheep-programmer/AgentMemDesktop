"""Ollama 原生适配器：模型发现（``GET /api/tags``）与原生 chat。

原生协议在模型发现、``num_ctx`` / ``keep_alive`` 等参数上比 OpenAI 兼容层更完整。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

from agentmem.errors import ProviderTimeoutError, ProviderUnavailableError
from agentmem.providers.base import (
    ChatChunk,
    ChatResult,
    Message,
    ToolSpec,
)
from agentmem.types import (
    DiscoveredModel,
    ProviderConfig,
    ProviderHealth,
)

logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 120.0


def to_root_url(base_url: str | None) -> str:
    """把 ``.../v1`` 形式的地址归一成 Ollama 根地址。"""
    value = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if value.endswith("/v1"):
        value = value[: -len("/v1")]
    return value or DEFAULT_BASE_URL


class OllamaNativeProvider:
    """Ollama 原生 API。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model = config.model or ""
        self.root_url = to_root_url(config.base_url)
        self._timeout = float(config.extra.get("timeout", DEFAULT_TIMEOUT))

    async def list_models(self) -> list[DiscoveredModel]:
        """``GET /api/tags``：列出本机已拉取的模型。"""
        url = f"{self.root_url}/api/tags"
        data = await self._request("GET", url)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        result: list[DiscoveredModel] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            details = item.get("details")
            family = None
            if isinstance(details, dict):
                family = details.get("family")
            name = str(item.get("name") or item.get("model") or "")
            if not name:
                continue
            result.append(
                DiscoveredModel(
                    id=name,
                    owned_by="ollama",
                    size_bytes=int(item["size"]) if isinstance(item.get("size"), int) else None,
                    family=str(family) if family else None,
                )
            )
        return result

    async def ps(self) -> list[str]:
        """``GET /api/ps``：列出当前已加载进显存的模型。"""
        data = await self._request("GET", f"{self.root_url}/api/ps")
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        return [
            str(item.get("name") or item.get("model")) for item in models if isinstance(item, dict)
        ]

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """原生 ``POST /api/chat``（非流式）。"""
        del tools  # Ollama 的工具调用格式与 OpenAI 不同，v1 不启用
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "stream": False,
            "options": self._options(temperature, max_tokens),
        }
        data = await self._request("POST", f"{self.root_url}/api/chat", payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        message = data.get("message") if isinstance(data, dict) else None
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "")
        return ChatResult(
            content=content,
            model=str(data.get("model") or self.model),
            provider_id=self.name,
            latency_ms=latency_ms,
            prompt_tokens=_as_int(data.get("prompt_eval_count")),
            completion_tokens=_as_int(data.get("eval_count")),
            finish_reason=str(data.get("done_reason") or "") or None,
        )

    async def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]:
        """原生 ``POST /api/chat``（流式 NDJSON）。"""
        temperature = float(kw.get("temperature", 0.7))
        max_tokens = kw.get("max_tokens")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "stream": True,
            "options": self._options(
                temperature, max_tokens if isinstance(max_tokens, int) else None
            ),
        }
        url = f"{self.root_url}/api/chat"
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout) as client,
                client.stream("POST", url, json=payload) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    message = event.get("message")
                    text = ""
                    if isinstance(message, dict):
                        text = str(message.get("content") or "")
                    done = bool(event.get("done"))
                    if not text and not done:
                        continue
                    finish = str(event.get("done_reason") or "") or None if done else None
                    yield ChatChunk(
                        delta=text,
                        model=str(event.get("model") or self.model),
                        provider_id=self.name,
                        finish_reason=finish,
                        prompt_tokens=_as_int(event.get("prompt_eval_count")),
                        completion_tokens=_as_int(event.get("eval_count")),
                    )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.name} 响应超时", detail={"provider_id": self.name}
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name} 无法连接：{exc}", detail={"provider_id": self.name}
            ) from exc

    def _options(self, temperature: float, max_tokens: int | None) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        extra_options = self.config.extra.get("options")
        if isinstance(extra_options, dict):
            options.update(extra_options)
        return options

    async def _request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(method, url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.name} 响应超时", detail={"provider_id": self.name, "url": url}
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailableError(
                f"{self.name} 返回 HTTP {exc.response.status_code}",
                detail={"provider_id": self.name, "url": url},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name} 无法连接：{exc}", detail={"provider_id": self.name, "url": url}
            ) from exc
        return data if isinstance(data, dict) else {}

    async def health(self) -> ProviderHealth:
        """用 ``/api/tags`` 实测连通性。"""
        started = time.perf_counter()
        try:
            models = await self.list_models()
            latency = int((time.perf_counter() - started) * 1000)
            resolved = self.model if any(item.id == self.model for item in models) else None
            if resolved is None and models:
                resolved = models[0].id
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=resolved)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None
