"""模型可插拔层：三类独立能力的 Protocol 与公共数据类型。

LLM / Embedding / Rerank 三者独立配置、独立切换，业务代码只通过角色名取用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agentmem.types import ProviderHealth

MessageRole = Literal["system", "user", "assistant", "tool"]
EmbedKind = Literal["doc", "query"]


class ProviderModel(BaseModel):
    """providers 层内部数据类型基类。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderContentModel(BaseModel):
    """承载**正文**的 providers 类型：绝不裁剪空白。

    ``str_strip_whitespace`` 对名称、标题这类字段是好事（吞掉用户手滑的空格），
    但对正文是灾难：流式增量常常就是一个 ``" "``，被裁掉之后
    ``"Frida is a toolkit"`` 会变成 ``"Fridaisatoolkit"``；
    代码块的缩进同样会被吃掉。凡是装内容的类型都必须继承本类。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Message(ProviderContentModel):
    """一条对话消息。"""

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class ToolSpec(ProviderModel):
    """工具声明。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(ProviderModel):
    """模型发起的工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatResult(ProviderContentModel):
    """一次非流式补全的结果。"""

    content: str
    model: str
    provider_id: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 前缀缓存的可观测性：没有这两个数，就没法判断上下文装配的改动到底有没有生效。
    # 命中的这部分 token 各家都按远低于原价计费（Anthropic 0.1x，DeepSeek 约 0.1x），
    # 因此 cached_tokens 已包含在 prompt_tokens 里，**不要**再加一次。
    cached_tokens: int | None = Field(
        default=None, description="命中前缀缓存的输入 token 数（已含在 prompt_tokens 内）"
    )
    cache_write_tokens: int | None = Field(
        default=None, description="写入缓存的 token 数；只有 Anthropic 显式区分，且按 1.25x 计费"
    )
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw_reasoning: str | None = None


class ChatChunk(ProviderContentModel):
    """流式增量。"""

    delta: str = ""
    model: str | None = None
    provider_id: str | None = None
    finish_reason: str | None = None
    tool_call: ToolCall | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # 前缀缓存的可观测性：没有这两个数，就没法判断上下文装配的改动到底有没有生效。
    # 命中的这部分 token 各家都按远低于原价计费（Anthropic 0.1x，DeepSeek 约 0.1x），
    # 因此 cached_tokens 已包含在 prompt_tokens 里，**不要**再加一次。
    cached_tokens: int | None = Field(
        default=None, description="命中前缀缓存的输入 token 数（已含在 prompt_tokens 内）"
    )
    cache_write_tokens: int | None = Field(
        default=None, description="写入缓存的 token 数；只有 Anthropic 显式区分，且按 1.25x 计费"
    )


class RankedDoc(ProviderContentModel):
    """重排后的文档。"""

    index: int = Field(ge=0, description="在入参 docs 中的下标")
    score: float
    text: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """文本生成能力。"""

    name: str

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]: ...


#: ``reasoning_effort`` 的特殊取值：这一次**不带**推理参数，也不套用 provider 配置里的
#: ``extra.reasoning_effort``，由服务端按自己的默认来。用于「用户这次明确要深度思考」，
#: 而 provider 被配置成了默认不思考的情形。
PROVIDER_DEFAULT_REASONING = "default"


@runtime_checkable
class ReasoningLLMProvider(Protocol):
    """能接受推理强度提示的 LLM（目前只有 OpenAI 兼容适配器）。

    单独成一个协议而不是给 :class:`LLMProvider` 加参数：其它适配器与第三方实现的
    ``chat`` 签名里没有它，硬塞会让它们在调用时直接 ``TypeError``。
    运行时按 ``supports_reasoning_effort`` 这个标记识别。
    """

    supports_reasoning_effort: bool

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """向量化能力。

    ``concurrent_safe`` 表示这个实现能不能被同时调用多批。进程内加载的本地模型
    （torch / MPS）不能：并发推理会争用同一块设备，实测直接段错误。走 HTTP 的实现
    可以，瓶颈在网络等待上，并发才有收益。
    """

    name: str
    dimension: int
    concurrent_safe: bool

    async def embed(self, texts: list[str], *, kind: EmbedKind = "doc") -> list[list[float]]: ...


@runtime_checkable
class RerankProvider(Protocol):
    """重排能力。"""

    name: str

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]: ...


@runtime_checkable
class HealthCheckable(Protocol):
    """可自检的 provider。"""

    async def health(self) -> ProviderHealth: ...
