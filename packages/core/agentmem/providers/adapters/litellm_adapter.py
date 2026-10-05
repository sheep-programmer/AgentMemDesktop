"""LiteLLM 兜底适配器：覆盖长尾 provider，不作为默认路径。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import structlog

from agentmem.errors import ProviderUnavailableError
from agentmem.providers.base import (
    ChatChunk,
    ChatResult,
    EmbedKind,
    Message,
    ToolSpec,
)
from agentmem.types import (
    ProviderConfig,
    ProviderHealth,
)

logger = structlog.get_logger(__name__)


class LiteLLMProvider:
    """通过 LiteLLM 统一入口调用长尾模型服务。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model = config.model or ""
        self.dimension = config.dimension or 0

    def _common_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model}
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url:
            kwargs["api_base"] = self.config.base_url
        extra = self.config.extra.get("litellm")
        if isinstance(extra, dict):
            kwargs.update(extra)
        return kwargs

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """非流式补全。"""
        import litellm

        started = time.perf_counter()
        kwargs = self._common_kwargs()
        kwargs["messages"] = [message.model_dump(exclude_none=True) for message in messages]
        kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"{self.name} 调用失败：{exc}", detail={"provider_id": self.name}
            ) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = response.choices[0] if response.choices else None
        content = ""
        if choice is not None and choice.message is not None:
            content = str(choice.message.content or "")
        usage = getattr(response, "usage", None)
        return ChatResult(
            content=content,
            model=str(getattr(response, "model", self.model)),
            provider_id=self.name,
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]:
        """流式补全。"""
        import litellm

        kwargs = self._common_kwargs()
        kwargs["messages"] = [message.model_dump(exclude_none=True) for message in messages]
        kwargs["temperature"] = float(kw.get("temperature", 0.7))
        kwargs["stream"] = True
        max_tokens = kw.get("max_tokens")
        if isinstance(max_tokens, int):
            kwargs["max_tokens"] = max_tokens
        try:
            stream = await litellm.acompletion(**kwargs)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"{self.name} 调用失败：{exc}", detail={"provider_id": self.name}
            ) from exc
        async for event in stream:
            choices = getattr(event, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = str(getattr(delta, "content", "") or "")
            finish = getattr(choices[0], "finish_reason", None)
            if text or finish:
                yield ChatChunk(
                    delta=text,
                    model=str(getattr(event, "model", self.model)),
                    provider_id=self.name,
                    finish_reason=finish,
                )

    async def embed(self, texts: list[str], *, kind: EmbedKind = "doc") -> list[list[float]]:
        """向量化。"""
        del kind
        import litellm

        if not texts:
            return []
        kwargs = self._common_kwargs()
        kwargs["input"] = texts
        try:
            response = await litellm.aembedding(**kwargs)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"{self.name} 向量化失败：{exc}", detail={"provider_id": self.name}
            ) from exc
        vectors = [list(item["embedding"]) for item in response.data]
        if vectors and not self.dimension:
            self.dimension = len(vectors[0])
        return vectors

    async def health(self) -> ProviderHealth:
        """最小补全实测连通性。"""
        started = time.perf_counter()
        try:
            result = await self.chat([Message(role="user", content="ping")], max_tokens=1)
            latency = int((time.perf_counter() - started) * 1000)
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=result.model)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))
