"""Anthropic Messages 适配器。

Anthropic 的消息格式与 OpenAI 不同：system 需要单独提出，消息必须是 user /
assistant 交替，流式事件也是另一套（``content_block_delta`` / ``message_delta``）。

## 前缀缓存

OpenAI 与 DeepSeek 的前缀缓存是**自动**的，Anthropic 不是——必须显式在内容块上
打 ``cache_control`` 断点，否则一个 token 都不会被缓存。我们的上下文装配
（见 `agentmem.prompts.answer`）特意把跨轮不变的内容收进 system，
就是为了在这里能打出一个有意义的断点；不打断点的话那份设计等于白做。

断点策略（最多 4 个，这里用 2 个）：

1. **system** —— 同一个 Space 内逐字节稳定，是最值钱的一段；
2. **历史里的最后一条消息** —— 于是 ``system + 全部历史``整体入缓存，轮数越多收益越大。

⚠️ **绝不在最后一条 user 消息上打断点**：那里装的是本轮独有的检索证据，
下一轮必然不同，缓存写入（1.25x 计费）纯属浪费。

⚠️ 低于 ``MIN_CACHEABLE_TOKENS`` 的前缀官方不予缓存，打了也是白打，
因此这里有个粗估的下限判断，避免在短对话上空耗断点。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)

from agentmem.errors import ProviderTimeoutError, ProviderUnavailableError
from agentmem.providers.base import (
    ChatChunk,
    ChatResult,
    Message,
    ToolSpec,
)
from agentmem.types import (
    ProviderConfig,
    ProviderHealth,
)

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT = 120.0

# 官方对可缓存前缀有最小长度要求（Sonnet/Opus 1024，Haiku 2048）。取大的那个，
# 宁可少打一个断点，也不要为不可能命中的前缀付 1.25x 的写入费。
MIN_CACHEABLE_TOKENS = 2048

# 粗估：CJK 1 字≈1 token，其余 4 字符≈1 token。这里只用于「够不够得上缓存下限」的判断，
# 不需要精确，也不值得为此引入一个分词器依赖。
_CJK_START, _CJK_END = "一", "鿿"


def _timeout_of(config: ProviderConfig) -> float:
    value = config.extra.get("timeout", DEFAULT_TIMEOUT)
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for char in text if _CJK_START <= char <= _CJK_END)
    return cjk + (len(text) - cjk) // 4


def _cache_enabled(config: ProviderConfig) -> bool:
    """是否启用前缀缓存断点。

    默认开启。留这个开关是因为不少用户走的是第三方中转（见
    `agentmem.providers.local_agents`），部分中转不认 ``cache_control``
    会直接 400；遇到这种情况把 ``extra.prompt_cache`` 设为 false 即可，
    不必换供应商。
    """
    return bool(config.extra.get("prompt_cache", True))


class AnthropicProvider:
    """Anthropic Messages API 适配器。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model = config.model or "claude-sonnet-4-5"
        self._client: AsyncAnthropic | None = None

    @property
    def client(self) -> AsyncAnthropic:
        """懒加载客户端。"""
        if self._client is None:
            self._client = AsyncAnthropic(
                base_url=self.config.base_url or None,
                api_key=self.config.api_key or "",
                timeout=_timeout_of(self.config),
                max_retries=1,
            )
        return self._client

    async def close(self) -> None:
        """关闭连接。"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- 格式转换 ---------------------------------------------------------

    @staticmethod
    def split_system(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        """把 system 消息抽出来，其余消息整理成 user/assistant 交替的序列。"""
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_parts.append(message.content)
                continue
            role = "assistant" if message.role == "assistant" else "user"
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"] += "\n\n" + message.content
                continue
            converted.append({"role": role, "content": message.content})
        if not converted:
            converted.append({"role": "user", "content": ""})
        return "\n\n".join(system_parts), converted

    def _build_kwargs(
        self,
        messages: list[Message],
        *,
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        """装配请求参数，并在稳定前缀上打好缓存断点。"""
        system, converted = self.split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        cache = _cache_enabled(self.config)
        if system:
            # 断点 1：system。它在同一个 Space 内逐字节稳定，是最值得缓存的一段。
            # 但只有「system + 历史」整体够得上官方下限时才打——单独的 system
            # 往往只有几百 token，达不到下限，打了也不会被缓存。
            prefix_tokens = _estimate_tokens(system) + sum(
                _estimate_tokens(str(m["content"])) for m in converted[:-1]
            )
            if cache and prefix_tokens >= MIN_CACHEABLE_TOKENS:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                # 断点 2：历史的最后一条。这样整段「system + 历史」一并入缓存，
                # 而**最后一条 user（本轮独有的证据）被刻意排除在外**。
                if len(converted) > 1:
                    converted[-2] = _with_cache_control(converted[-2])
            else:
                kwargs["system"] = system

        anthropic_tools = self._to_tools(tools)
        if anthropic_tools is not None:
            kwargs["tools"] = anthropic_tools
        return kwargs

    @staticmethod
    def _to_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    # -- 生成 -------------------------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """非流式补全。"""
        started = time.perf_counter()
        kwargs = self._build_kwargs(
            messages,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            tools=tools,
        )
        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as exc:
            raise _translate(exc, self.name) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        text_parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(str(getattr(block, "text", "")))
        usage = getattr(response, "usage", None)
        cached, written = _cache_usage(usage)
        return ChatResult(
            content="".join(text_parts),
            model=str(response.model or self.model),
            provider_id=self.name,
            latency_ms=latency_ms,
            prompt_tokens=_total_prompt_tokens(usage),
            completion_tokens=getattr(usage, "output_tokens", None),
            cached_tokens=cached,
            cache_write_tokens=written,
            finish_reason=getattr(response, "stop_reason", None),
        )

    async def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]:
        """流式补全。"""
        temperature = float(kw.get("temperature", 0.7))
        max_tokens = kw.get("max_tokens")
        tools = kw.get("tools")
        kwargs = self._build_kwargs(
            messages,
            temperature=temperature,
            max_tokens=max_tokens if isinstance(max_tokens, int) else 4096,
            tools=tools if isinstance(tools, list) else None,
        )
        try:
            stream = await self.client.messages.create(stream=True, **kwargs)
        except Exception as exc:
            raise _translate(exc, self.name) from exc
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                text = str(getattr(delta, "text", "") or "")
                if text:
                    yield ChatChunk(delta=text, model=self.model, provider_id=self.name)
            elif event_type == "message_delta":
                delta = getattr(event, "delta", None)
                usage = getattr(event, "usage", None)
                yield ChatChunk(
                    model=self.model,
                    provider_id=self.name,
                    finish_reason=getattr(delta, "stop_reason", None),
                    completion_tokens=getattr(usage, "output_tokens", None),
                )
            elif event_type == "message_start":
                message = getattr(event, "message", None)
                usage = getattr(message, "usage", None)
                cached, written = _cache_usage(usage)
                yield ChatChunk(
                    model=self.model,
                    provider_id=self.name,
                    prompt_tokens=_total_prompt_tokens(usage),
                    cached_tokens=cached,
                    cache_write_tokens=written,
                )

    async def health(self) -> ProviderHealth:
        """最小补全实测连通性。"""
        started = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            latency = int((time.perf_counter() - started) * 1000)
            return ProviderHealth(
                ok=True, latency_ms=latency, resolved_model=str(response.model or self.model)
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))


def _with_cache_control(message: dict[str, Any]) -> dict[str, Any]:
    """把一条消息的 content 转成带缓存断点的内容块形式。

    ``cache_control`` 只能挂在内容块上，不能挂在整条消息上，所以纯字符串的
    content 必须先展开成 ``[{"type": "text", ...}]``。
    """
    content = message["content"]
    if isinstance(content, str):
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
    else:
        blocks = [dict(block) for block in content]
    if not blocks:
        return message
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return {**message, "content": blocks}


def _cache_usage(usage: Any) -> tuple[int | None, int | None]:
    """从 usage 里取 (命中缓存的 token, 写入缓存的 token)。

    ⚠️ Anthropic 的 ``input_tokens`` **不包含**缓存读写的部分，与 OpenAI 的口径相反。
    我们对外统一成「``prompt_tokens`` 含缓存部分」，所以调用处要把它们加回去，
    否则用量统计会凭空少一大截。
    """
    if usage is None:
        return None, None
    return (
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "cache_creation_input_tokens", None),
    )


def _total_prompt_tokens(usage: Any) -> int | None:
    """按「含缓存」的统一口径算输入 token。"""
    if usage is None:
        return None
    base = getattr(usage, "input_tokens", None)
    if base is None:
        return None
    cached, written = _cache_usage(usage)
    return int(base) + int(cached or 0) + int(written or 0)


def _translate(exc: Exception, provider_id: str) -> Exception:
    """把 SDK 异常翻译成 AgentMem 异常。"""
    if isinstance(exc, APITimeoutError):
        return ProviderTimeoutError(f"{provider_id} 响应超时", detail={"provider_id": provider_id})
    if isinstance(exc, APIConnectionError):
        return ProviderUnavailableError(
            f"{provider_id} 无法连接", detail={"provider_id": provider_id}
        )
    if isinstance(exc, APIStatusError):
        return ProviderUnavailableError(
            f"{provider_id} 调用失败：{exc}", detail={"provider_id": provider_id}
        )
    return ProviderUnavailableError(
        f"{provider_id} 调用失败：{exc}", detail={"provider_id": provider_id}
    )
