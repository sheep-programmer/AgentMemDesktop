"""OpenAI 兼容适配器。

通过覆盖 ``base_url`` / ``api_key`` 即可接入 OpenAI、DeepSeek、智谱、通义、
Ollama、LM Studio、vLLM 等一切实现了 OpenAI 协议的服务。支持 chat、流式 chat
与 embedding。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from agentmem.errors import (
    ContextOverflowError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from agentmem.providers.base import (
    PROVIDER_DEFAULT_REASONING,
    ChatChunk,
    ChatResult,
    EmbedKind,
    Message,
    ToolCall,
    ToolSpec,
)
from agentmem.types import (
    DiscoveredModel,
    ProviderConfig,
    ProviderHealth,
)

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT = 120.0


def _timeout_of(config: ProviderConfig) -> float:
    value = config.extra.get("timeout", DEFAULT_TIMEOUT)
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _cached_tokens(usage: Any) -> int | None:
    """取命中前缀缓存的 token 数。

    这条协议**没有统一标准**，各家自己发挥，所以只能逐个认：

    - OpenAI / 多数兼容实现：``usage.prompt_tokens_details.cached_tokens``
    - DeepSeek：``usage.prompt_cache_hit_tokens``（它另有 ``..._miss_tokens``）

    都取不到就返回 ``None``（「不知道」），而不是 0（「确认没命中」）——
    这两者在排查「缓存到底有没有生效」时含义完全不同。

    注意这些口径下 ``prompt_tokens`` **已包含**缓存命中部分，与 Anthropic 相反。
    """
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if cached is not None:
        return int(cached)
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    return int(hit) if hit is not None else None


def _rejects_reasoning(exc: Exception) -> bool:
    """这次失败是不是服务端不认 ``reasoning_effort``（参数本身或取值）。

    各家的说法不一（OpenAI：``Unrecognized request argument supplied: reasoning_effort``；
    agnes：取值校验失败并列出可选值），但都会点名这个参数，所以按关键词认。
    """
    if not isinstance(exc, APIStatusError):
        return False
    return getattr(exc, "status_code", None) in (400, 422) and "reasoning" in str(exc).lower()


class OpenAICompatibleProvider:
    """OpenAI 兼容的 HTTP 提供方：并发调用的瓶颈在网络等待上，可以安全并发。"""

    concurrent_safe = True

    #: 认得 ``reasoning_effort`` 提示（见 :meth:`_create`）。``LLMRoute`` 据此决定要不要
    #: 把调用方的提示传下来——其它适配器的签名里没有这个参数。
    supports_reasoning_effort = True

    """OpenAI 协议适配器，可承担 llm / embedding 两种角色。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model = config.model or ""
        self.dimension = config.dimension or 0
        self._client: AsyncOpenAI | None = None
        # 服务端拒绝过 reasoning_effort 之后就不再带：每次都先挨一次 400 再重试，
        # 等于把每次调用的往返翻倍
        self._reasoning_rejected = False

    # -- 客户端 -----------------------------------------------------------

    @property
    def client(self) -> AsyncOpenAI:
        """懒加载的异步客户端。"""
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.config.base_url or None,
                api_key=self.config.api_key or "not-needed",
                timeout=_timeout_of(self.config),
                max_retries=1,
            )
        return self._client

    async def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- 消息转换 ---------------------------------------------------------

    @staticmethod
    def _to_payload(messages: list[Message]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.name:
                item["name"] = message.name
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            payload.append(item)
        return payload

    @staticmethod
    def _to_tools(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]

    # -- 生成 -------------------------------------------------------------

    def _reasoning_effort(self, requested: object) -> str | None:
        """本次请求要带的 ``reasoning_effort``：调用方显式给的优先，其次 provider 配置。

        推理模型（实测 agnes-2.5-flash）默认先想再答：一次「把追问改写成独立问题」
        要先吐 75~90 个推理 token，首字 3.6 秒；``none`` 时 1.2 秒，产出的 JSON 一字不差。
        改写这类机械任务由调用方显式要 ``none``；正式回答默认不碰，交给
        ``extra.reasoning_effort`` 由用户决定（省时与答案详略之间的取舍见
        ``docs/11-TOKEN-EFFICIENCY.md``）。
        """
        if self._reasoning_rejected or requested == PROVIDER_DEFAULT_REASONING:
            return None
        value = requested if requested is not None else self.config.extra.get("reasoning_effort")
        return value if isinstance(value, str) and value else None

    async def _create(self, kwargs: dict[str, Any], **extra: Any) -> Any:
        """发请求；服务端不认 ``reasoning_effort`` 时去掉它重来一次，并记住不再带。

        与 ``stream_options`` 的退让同理：这只是一个提速提示，宁可照常慢一点，
        也不能因为某家不认这个参数就让调用失败。
        """
        try:
            return await self.client.chat.completions.create(**kwargs, **extra)
        except Exception as exc:
            if "reasoning_effort" not in kwargs or not _rejects_reasoning(exc):
                raise
            logger.info("reasoning_effort_unsupported", provider_id=self.name, error=str(exc)[:160])
            self._reasoning_rejected = True
            retry = {key: value for key, value in kwargs.items() if key != "reasoning_effort"}
            return await self.client.chat.completions.create(**retry, **extra)

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """非流式补全。

        Args:
            reasoning_effort: 推理强度提示（如 ``"none"``）；服务端不认时自动去掉。
        """
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_payload(messages),
            "temperature": temperature,
        }
        effort = self._reasoning_effort(reasoning_effort)
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        # 调用方没指定时用 provider 配置里的默认上限：不给的话由服务端决定，各家默认
        # 差别很大（实测某聚合服务默认只给 218 token，长 JSON 直接被截断）
        effective_max_tokens = (
            max_tokens if max_tokens is not None else self.config.extra.get("max_tokens")
        )
        if isinstance(effective_max_tokens, int) and effective_max_tokens > 0:
            kwargs["max_tokens"] = effective_max_tokens
        openai_tools = self._to_tools(tools)
        if openai_tools is not None:
            kwargs["tools"] = openai_tools
        try:
            response = await self._create(kwargs)
        except Exception as exc:
            raise translate_error(exc, self.name) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = response.choices[0] if response.choices else None
        content = (choice.message.content if choice and choice.message else None) or ""
        tool_calls: list[ToolCall] = []
        if choice is not None and choice.message is not None and choice.message.tool_calls:
            for item in choice.message.tool_calls:
                if item.type != "function":
                    continue
                try:
                    arguments = json.loads(item.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": item.function.arguments}
                if not isinstance(arguments, dict):
                    arguments = {"_value": arguments}
                tool_calls.append(
                    ToolCall(id=item.id, name=item.function.name, arguments=arguments)
                )
        usage = getattr(response, "usage", None)
        return ChatResult(
            content=content,
            model=str(response.model or self.model),
            provider_id=self.name,
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            cached_tokens=_cached_tokens(usage),
            finish_reason=choice.finish_reason if choice else None,
            tool_calls=tool_calls,
        )

    async def stream(
        self,
        messages: list[Message],
        **kw: Any,
    ) -> AsyncIterator[ChatChunk]:
        """流式补全。"""
        temperature = float(kw.get("temperature", 0.7))
        max_tokens = kw.get("max_tokens") or self.config.extra.get("max_tokens")
        tools = kw.get("tools")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_payload(messages),
            "temperature": temperature,
            "stream": True,
        }
        if isinstance(max_tokens, int):
            kwargs["max_tokens"] = max_tokens
        effort = self._reasoning_effort(kw.get("reasoning_effort"))
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        openai_tools = self._to_tools(tools if isinstance(tools, list) else None)
        if openai_tools is not None:
            kwargs["tools"] = openai_tools
        # 流式响应默认**不带** usage：OpenAI 兼容协议要显式要一次。不要的话
        # `provider_usage` 里对话这一路永远是 prompt_tokens=None，
        # 设置页「上下文成本与缓存命中」面板对主要工作负载就永远没有数据——
        # 而前缀缓存到底省了多少，恰恰是那个面板要回答的问题。
        #
        # 但「OpenAI 兼容」的实现参差不齐（Ollama、LM Studio、各类聚合站），
        # 有的会因为不认识 stream_options 直接报错。所以带着它试一次，
        # 被拒了就退回不带的写法重来——宁可少一份统计，也不能让对话不可用。
        include_usage = self.config.extra.get("stream_usage", True)
        try:
            if include_usage:
                try:
                    stream = await self._create(kwargs, stream_options={"include_usage": True})
                except Exception as exc:
                    logger.info(
                        "stream_options_unsupported", provider_id=self.name, error=str(exc)[:160]
                    )
                    if self._reasoning_rejected:
                        kwargs.pop("reasoning_effort", None)
                    stream = await self._create(kwargs)
            else:
                stream = await self._create(kwargs)
        except Exception as exc:
            raise translate_error(exc, self.name) from exc
        async for event in stream:
            # usage 帧的形状各家不一：有的单独发一帧（choices 为空），
            # 有的把 usage 挂在最后一帧上而 choices 里还留着一个空 delta
            # （实测 agnes / 部分聚合站就是后者）。此前只在 choices 为空时才读，
            # 后一种写法的 usage 就被整帧跳过——对话这一路的用量于是永远是 None。
            # 所以：任何带 usage 的帧都收下，再照常处理它的 delta。
            usage = getattr(event, "usage", None)
            if usage is not None:
                yield ChatChunk(
                    provider_id=self.name,
                    model=str(event.model or self.model),
                    prompt_tokens=getattr(usage, "prompt_tokens", None),
                    completion_tokens=getattr(usage, "completion_tokens", None),
                    cached_tokens=_cached_tokens(usage),
                )
            if not event.choices:
                continue
            choice = event.choices[0]
            delta = choice.delta
            text = (delta.content if delta else None) or ""
            tool_call: ToolCall | None = None
            if delta is not None and delta.tool_calls:
                item = delta.tool_calls[0]
                if item.function is not None:
                    tool_call = ToolCall(
                        id=item.id or "",
                        name=item.function.name or "",
                        arguments={"partial": item.function.arguments or ""},
                    )
            if not text and tool_call is None and choice.finish_reason is None:
                continue
            yield ChatChunk(
                delta=text,
                model=str(event.model or self.model),
                provider_id=self.name,
                finish_reason=choice.finish_reason,
                tool_call=tool_call,
            )

    # -- 向量化 -----------------------------------------------------------

    async def embed(self, texts: list[str], *, kind: EmbedKind = "doc") -> list[list[float]]:
        """文本向量化。

        Args:
            texts: 待向量化文本。
            kind: 仅本地模型区分 doc/query 前缀，此处忽略。
        """
        del kind
        if not texts:
            return []
        try:
            response = await self.client.embeddings.create(model=self.model, input=texts)
        except Exception as exc:
            raise translate_error(exc, self.name) from exc
        vectors = [list(item.embedding) for item in response.data]
        if vectors and not self.dimension:
            self.dimension = len(vectors[0])
        return vectors

    # -- 运维 -------------------------------------------------------------

    async def list_models(self) -> list[DiscoveredModel]:
        """拉取服务端可用模型列表。"""
        try:
            response = await self.client.models.list()
        except Exception as exc:
            raise translate_error(exc, self.name) from exc
        return [
            DiscoveredModel(id=str(item.id), owned_by=getattr(item, "owned_by", None))
            for item in response.data
        ]

    async def health(self) -> ProviderHealth:
        """实测连通性；llm 角色回退到最小补全，embedding 角色回退到一次向量化。"""
        started = time.perf_counter()
        try:
            if self.config.kind == "embedding":
                vectors = await self.embed(["ping"])
                latency = int((time.perf_counter() - started) * 1000)
                return ProviderHealth(
                    ok=True,
                    latency_ms=latency,
                    resolved_model=self.model,
                    error=None if vectors else "未返回向量",
                )
            models = await self.list_models()
            latency = int((time.perf_counter() - started) * 1000)
            resolved = self.model if any(m.id == self.model for m in models) else None
            if resolved is None and models:
                resolved = models[0].id
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=resolved)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))


#: 上下文超限的常见措辞。各家 OpenAI 兼容实现不统一，只能按关键词认。
_CONTEXT_OVERFLOW_HINTS = (
    "context_length_exceeded",
    "maximum context length",
    "context window",
    "reduce the length of the messages",
    "too many tokens",
    "prompt is too long",
    "input length",
)


def _looks_like_context_overflow(message: str) -> bool:
    """按关键词判断一条 400 是不是上下文超限。"""
    lowered = message.lower()
    return any(hint in lowered for hint in _CONTEXT_OVERFLOW_HINTS)


def translate_error(exc: Exception, provider_id: str) -> Exception:
    """把 SDK 异常翻译成 AgentMem 的 provider 异常。"""
    if isinstance(exc, APITimeoutError):
        return ProviderTimeoutError(f"{provider_id} 响应超时", detail={"provider_id": provider_id})
    if isinstance(exc, APIConnectionError):
        return ProviderUnavailableError(
            f"{provider_id} 无法连接", detail={"provider_id": provider_id}
        )
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        # 上下文超限单独认出来：它可以靠裁短历史自动恢复，不该和「服务不可用」混为一谈。
        # 各家措辞不统一（OpenAI 给 code=context_length_exceeded，别家多是自然语言），
        # 所以按关键词认，宁可漏认（退回原来的行为）也不要误认。
        if status == 400 and _looks_like_context_overflow(str(exc)):
            return ContextOverflowError(
                f"{provider_id} 上下文超出窗口上限",
                detail={"provider_id": provider_id, "status": status},
            )
        message = f"{provider_id} 返回 HTTP {status}"
        if status is not None and status >= 500:
            return ProviderUnavailableError(message, detail={"provider_id": provider_id})
        return ProviderUnavailableError(
            f"{message}：{exc}", detail={"provider_id": provider_id, "status": status}
        )
    return ProviderUnavailableError(
        f"{provider_id} 调用失败：{exc}", detail={"provider_id": provider_id}
    )
