"""Provider 注册表：按配置构建适配器、按角色取用、降级链、用量记录。

业务代码只写 ``registry.llm("distill")``，不关心背后是哪个厂商。
"""

from __future__ import annotations

import hashlib
from array import array
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Hashable
from typing import Any, Generic, TypeVar, cast

import structlog

from agentmem.config import ModelsConfig
from agentmem.errors import (
    ProviderInUseError,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from agentmem.providers.adapters.anthropic import AnthropicProvider
from agentmem.providers.adapters.cohere_rerank import CohereRerankProvider
from agentmem.providers.adapters.litellm_adapter import LiteLLMProvider
from agentmem.providers.adapters.local_embedding import LocalEmbeddingProvider
from agentmem.providers.adapters.local_rerank import LocalRerankProvider
from agentmem.providers.adapters.ollama_native import OllamaNativeProvider
from agentmem.providers.adapters.openai_compatible import OpenAICompatibleProvider
from agentmem.providers.base import (
    ChatChunk,
    ChatResult,
    EmbeddingProvider,
    EmbedKind,
    HealthCheckable,
    LLMProvider,
    Message,
    RankedDoc,
    ReasoningLLMProvider,
    RerankProvider,
    ToolSpec,
)
from agentmem.store.repos.usage import UsageRepo
from agentmem.types import (
    ProviderConfig,
    ProviderHealth,
    RoleName,
    UsageRecordCreate,
)

logger = structlog.get_logger(__name__)

#: adapter 名 → 构造函数
ADAPTERS: dict[str, Callable[[ProviderConfig], Any]] = {
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "litellm": LiteLLMProvider,
    "sentence_transformers": LocalEmbeddingProvider,
    "sentence_transformers_ce": LocalRerankProvider,
    "cohere_rerank": CohereRerankProvider,
    "ollama_native": OllamaNativeProvider,
}

ROLE_KIND: dict[str, str] = {
    "chat": "llm",
    "fast": "llm",
    "distill": "llm",
    "judge": "llm",
    "embedding": "embedding",
    "rerank": "rerank",
}


class UsageRecorder:
    """把每次模型调用落到 ``usage_records``；未接入存储时只写日志。"""

    def __init__(
        self, usage: UsageRepo | None, *, space_id: str | None = None, purpose: str | None = None
    ) -> None:
        self.usage = usage
        self.space_id = space_id
        self.purpose = purpose

    def with_purpose(self, purpose: str | None) -> UsageRecorder:
        """派生一个使用不同 purpose 的记录器。"""
        return UsageRecorder(self.usage, space_id=self.space_id, purpose=purpose)

    async def record(
        self,
        *,
        provider_id: str,
        model: str,
        kind: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cached_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        latency_ms: int | None = None,
        ok: bool = True,
    ) -> None:
        """写入一条用量记录；失败只告警，不影响主流程。"""
        logger.info(
            "provider_usage",
            provider_id=provider_id,
            model=model,
            kind=kind,
            purpose=self.purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            ok=ok,
        )
        if self.usage is None:
            return
        try:
            await self.usage.create(
                UsageRecordCreate(
                    space_id=self.space_id,
                    provider_id=provider_id,
                    model=model,
                    kind=cast("Any", kind),
                    purpose=self.purpose,
                    prompt_tokens=prompt_tokens or 0,
                    completion_tokens=completion_tokens or 0,
                    cached_tokens=cached_tokens or 0,
                    cache_write_tokens=cache_write_tokens or 0,
                    latency_ms=latency_ms,
                    ok=ok,
                )
            )
        except Exception as exc:
            logger.warning("usage_record_failed", error=str(exc), provider_id=provider_id)


class LLMRoute:
    """角色门面：按顺序尝试主 provider 与降级链，并记录用量。"""

    def __init__(self, role: str, providers: list[LLMProvider], recorder: UsageRecorder) -> None:
        self.role = role
        self.providers = providers
        self.recorder = recorder
        self.name = providers[0].name if providers else role

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """依次尝试各 provider，全部失败则抛出最后一个异常。

        Args:
            reasoning_effort: 推理强度提示，只传给声明了 ``supports_reasoning_effort``
                的适配器；其余适配器的 ``chat`` 签名里没有这个参数，照常调用。
        """
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                if (
                    reasoning_effort is not None
                    and isinstance(provider, ReasoningLLMProvider)
                    and provider.supports_reasoning_effort
                ):
                    result = await provider.chat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                else:
                    result = await provider.chat(
                        messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                    )
            except Exception as exc:
                last_error = exc
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model", "") or provider.name,
                    kind="llm",
                    ok=False,
                )
                if index + 1 < len(self.providers):
                    logger.warning(
                        "provider_fallback",
                        role=self.role,
                        failed=provider.name,
                        next=self.providers[index + 1].name,
                        error=str(exc),
                    )
                continue
            await self.recorder.record(
                provider_id=provider.name,
                model=result.model,
                kind="llm",
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cached_tokens=result.cached_tokens,
                cache_write_tokens=result.cache_write_tokens,
                latency_ms=result.latency_ms,
                ok=True,
            )
            return result
        raise (
            last_error
            if last_error is not None
            else ProviderUnavailableError(f"角色 {self.role} 没有可用的 provider")
        )

    async def stream(self, messages: list[Message], **kw: Any) -> AsyncIterator[ChatChunk]:
        """流式补全；已产出内容后不再降级，避免重复输出。"""
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            produced = False
            collected: list[ChatChunk] = []
            try:
                async for chunk in provider.stream(messages, **kw):
                    produced = True
                    collected.append(chunk)
                    yield chunk
            except Exception as exc:
                last_error = exc
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model", "") or provider.name,
                    kind="llm",
                    ok=False,
                )
                if produced or index + 1 >= len(self.providers):
                    raise
                logger.warning(
                    "provider_fallback",
                    role=self.role,
                    failed=provider.name,
                    next=self.providers[index + 1].name,
                    error=str(exc),
                )
                continue
            prompt_tokens = _last_int(collected, "prompt_tokens")
            completion_tokens = _last_int(collected, "completion_tokens")
            await self.recorder.record(
                provider_id=provider.name,
                model=collected[-1].model if collected and collected[-1].model else provider.name,
                kind="llm",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                # 缓存用量在 Anthropic 的 message_start 事件里给出，在流的**最前面**，
                # 而 _last_int 取的是最后一个非空值——各家出现的位置不同，都能兜住。
                cached_tokens=_last_int(collected, "cached_tokens"),
                cache_write_tokens=_last_int(collected, "cache_write_tokens"),
                ok=True,
            )
            return
        if last_error is not None:
            raise last_error

    async def health(self) -> ProviderHealth:
        """逐个自检，返回首个可用结果。"""
        last: ProviderHealth | None = None
        for provider in self.providers:
            checkable = cast(HealthCheckable, provider)
            result = await checkable.health()
            if result.ok:
                return result
            last = result
        return last or ProviderHealth(ok=False, error="未配置 provider")


_V = TypeVar("_V")

#: 每个注册表缓存多少条查询向量。只缓存 ``kind="query"``（问题本身），一条 1024 维
#: 向量按 ``array('d')`` 存约 8 KB，256 条约 2 MB。
QUERY_VECTOR_CACHE_SIZE = 256

#: 每个注册表缓存多少个（问题, 候选）重排分。一个分只是一个浮点数，八千对不到 1 MB。
RERANK_SCORE_CACHE_SIZE = 8192


class LruCache(Generic[_V]):
    """极简的定长 LRU。

    放在注册表上而不是做成模块级全局：注册表随模型配置变更整体重建
    （``Runtime.invalidate_registries``），换了模型缓存跟着作废，不会拿旧模型的
    向量与分数去冒充新模型的；测试里每个注册表也各用各的，互不串味。
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: OrderedDict[Hashable, _V] = OrderedDict()

    def get(self, key: Hashable) -> _V | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: Hashable, value: _V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


def _model_of(provider: object) -> str:
    """缓存键里的模型名：同一个 provider id 换了模型，键也要跟着变。"""
    return str(getattr(provider, "model_name", None) or getattr(provider, "model", "") or "")


def _digest(text: str) -> str:
    """长文本进缓存键前先压成摘要，键本身不必留着整段正文。"""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


class EmbeddingRoute:
    """Embedding 角色门面。

    ``query_cache`` 只服务检索时的问题向量：「重新生成」会把同一个问题原样再问一遍，
    评测的 A/B 两臂也用同一批题目，问题向量完全相同却每次都要过一遍模型。
    文档向量（``kind="doc"``）不缓存——摄取时每段只算一次，缓存只会把内存占满。
    """

    def __init__(
        self,
        role: str,
        providers: list[EmbeddingProvider],
        recorder: UsageRecorder,
        *,
        query_cache: LruCache[array[float]] | None = None,
    ) -> None:
        self.role = role
        self.providers = providers
        self.recorder = recorder
        self.query_cache = query_cache
        self.name = providers[0].name if providers else role
        self.dimension = providers[0].dimension if providers else 0

    async def _embed_one(
        self, provider: EmbeddingProvider, texts: list[str], kind: EmbedKind
    ) -> tuple[list[list[float]], list[str]]:
        """调用一个 provider，返回（向量，真正送去计算的文本）。"""
        cache = self.query_cache if kind == "query" else None
        if cache is None:
            return await provider.embed(texts, kind=kind), texts
        keys = [(provider.name, _model_of(provider), kind, text) for text in texts]
        found = [cache.get(key) for key in keys]
        missing = list(
            dict.fromkeys(text for text, hit in zip(texts, found, strict=True) if hit is None)
        )
        if missing:
            fresh = await provider.embed(missing, kind=kind)
            if len(fresh) != len(missing):
                # 条数对不上说明这次结果不可信：原样交回让调用方按失败处理，也不入缓存
                return fresh, missing
            computed = dict(zip(missing, fresh, strict=True))
            for key, text in zip(keys, texts, strict=True):
                if text in computed:
                    cache.put(key, array("d", computed[text]))
            found = [cache.get(key) for key in keys]
        return [list(item) if item is not None else [] for item in found], missing

    async def embed(self, texts: list[str], *, kind: EmbedKind = "doc") -> list[list[float]]:
        """向量化；失败时按降级链切换。"""
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                vectors, computed = await self._embed_one(provider, texts, kind)
            except Exception as exc:
                last_error = exc
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model_name", "") or provider.name,
                    kind="embedding",
                    ok=False,
                )
                if index + 1 < len(self.providers):
                    logger.warning(
                        "provider_fallback",
                        role=self.role,
                        failed=provider.name,
                        next=self.providers[index + 1].name,
                        error=str(exc),
                    )
                continue
            self.dimension = provider.dimension
            if computed:
                # 全部命中缓存时没有发生调用，不记用量
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model_name", "") or provider.name,
                    kind="embedding",
                    prompt_tokens=sum(len(text) for text in computed),
                    ok=True,
                )
            return vectors
        raise (
            last_error
            if last_error is not None
            else ProviderUnavailableError(f"角色 {self.role} 没有可用的 provider")
        )

    async def health(self) -> ProviderHealth:
        """逐个自检，返回首个可用结果。"""
        last: ProviderHealth | None = None
        for provider in self.providers:
            result = await cast(HealthCheckable, provider).health()
            if result.ok:
                return result
            last = result
        return last or ProviderHealth(ok=False, error="未配置 provider")


class RerankRoute:
    """Rerank 角色门面。

    ``score_cache`` 记住（问题, 候选正文）的重排分。重排是检索里最贵的一步
    （本机 bge-reranker-v2-m3 给 40 条候选打分约 8 秒），而「重新生成」与评测的
    A/B 两臂都会拿同一个问题、同一批候选再打一遍分，结果不会变。
    缓存按候选逐条命中：新入库的文档只让新增的那几条候选重新打分。
    """

    def __init__(
        self,
        role: str,
        providers: list[RerankProvider],
        recorder: UsageRecorder,
        *,
        score_cache: LruCache[float] | None = None,
    ) -> None:
        self.role = role
        self.providers = providers
        self.recorder = recorder
        self.score_cache = score_cache
        self.name = providers[0].name if providers else role

    async def _rerank_one(
        self, provider: RerankProvider, query: str, docs: list[str], top_n: int
    ) -> tuple[list[RankedDoc], bool]:
        """调用一个 provider，返回（排序结果，是否真的发生了调用）。"""
        cache = self.score_cache
        if cache is None:
            return await provider.rerank(query, docs, top_n=top_n), True
        model = _model_of(provider)
        query_key = _digest(query)
        keys = [(provider.name, model, query_key, _digest(doc)) for doc in docs]
        scores = [cache.get(key) for key in keys]
        missing = [index for index, score in enumerate(scores) if score is None]
        if missing:
            # 缺的那几条要拿到**全部**分数才能入缓存，所以 top_n 取缺失条数，截断留给下面
            ranked = await provider.rerank(
                query, [docs[index] for index in missing], top_n=len(missing)
            )
            for item in ranked:
                if 0 <= item.index < len(missing):
                    original = missing[item.index]
                    scores[original] = item.score
                    cache.put(keys[original], item.score)
        merged = [
            RankedDoc(index=index, score=score, text=docs[index])
            for index, score in enumerate(scores)
            if score is not None
        ]
        merged.sort(key=lambda item: item.score, reverse=True)
        return merged[:top_n], bool(missing)

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]:
        """重排；失败时按降级链切换。"""
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                ranked, called = await self._rerank_one(provider, query, docs, top_n)
            except Exception as exc:
                last_error = exc
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model_name", None)
                    or getattr(provider, "model", "")
                    or provider.name,
                    kind="rerank",
                    ok=False,
                )
                if index + 1 < len(self.providers):
                    logger.warning(
                        "provider_fallback",
                        role=self.role,
                        failed=provider.name,
                        next=self.providers[index + 1].name,
                        error=str(exc),
                    )
                continue
            if called:
                await self.recorder.record(
                    provider_id=provider.name,
                    model=getattr(provider, "model_name", None)
                    or getattr(provider, "model", "")
                    or provider.name,
                    kind="rerank",
                    ok=True,
                )
            return ranked
        raise (
            last_error
            if last_error is not None
            else ProviderUnavailableError(f"角色 {self.role} 没有可用的 provider")
        )

    async def health(self) -> ProviderHealth:
        """逐个自检，返回首个可用结果。"""
        last: ProviderHealth | None = None
        for provider in self.providers:
            result = await cast(HealthCheckable, provider).health()
            if result.ok:
                return result
            last = result
        return last or ProviderHealth(ok=False, error="未配置 provider")


class ProviderRegistry:
    """按 ``config/models.yaml`` 构建并缓存适配器实例。"""

    def __init__(
        self,
        config: ModelsConfig,
        *,
        usage: UsageRepo | None = None,
        space_id: str | None = None,
        overrides: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.usage = usage
        self.space_id = space_id
        self.overrides = dict(overrides or {})
        self._instances: dict[str, Any] = {}
        self._query_vectors: LruCache[array[float]] = LruCache(QUERY_VECTOR_CACHE_SIZE)
        self._rerank_scores: LruCache[float] = LruCache(RERANK_SCORE_CACHE_SIZE)

    # -- 配置查询 ---------------------------------------------------------

    def provider_config(self, provider_id: str) -> ProviderConfig:
        """按 id 取 provider 配置。"""
        for item in self.config.providers:
            if item.id == provider_id:
                return item
        raise ProviderNotConfiguredError(
            provider_id, detail={"provider_id": provider_id, "reason": "provider 不存在"}
        )

    def role_provider_id(self, role: RoleName) -> str | None:
        """角色绑定的 provider id（Space 级覆盖优先）。"""
        override = self.overrides.get(role)
        if override:
            return override
        return getattr(self.config.roles, role, None)

    def fallback_ids(self, role: RoleName) -> list[str]:
        """角色的降级链。"""
        ids = list(getattr(self.config.fallbacks, role, []) or [])
        primary = self.role_provider_id(role)
        return [item for item in ids if item != primary]

    def roles_in_use(self, provider_id: str) -> list[str]:
        """哪些角色引用了该 provider。"""
        roles: list[str] = []
        for role in ROLE_KIND:
            bindings = [self.role_provider_id(cast("RoleName", role))]
            bindings.extend(self.fallback_ids(cast("RoleName", role)))
            if provider_id in bindings:
                roles.append(role)
        return roles

    def assert_deletable(self, provider_id: str) -> None:
        """删除前检查引用关系。"""
        roles = self.roles_in_use(provider_id)
        if roles:
            raise ProviderInUseError(provider_id, roles)

    # -- 适配器实例 -------------------------------------------------------

    def build(self, config: ProviderConfig) -> Any:
        """按 adapter 名构造适配器实例（不缓存）。"""
        factory = ADAPTERS.get(config.adapter)
        if factory is None:
            raise ProviderNotConfiguredError(
                config.id,
                detail={
                    "provider_id": config.id,
                    "adapter": config.adapter,
                    "reason": "未知适配器",
                },
            )
        return factory(config)

    def instance(self, provider_id: str) -> Any:
        """取（并缓存）适配器实例。"""
        if provider_id not in self._instances:
            self._instances[provider_id] = self.build(self.provider_config(provider_id))
        return self._instances[provider_id]

    def _chain(self, role: RoleName) -> list[Any]:
        expected_kind = ROLE_KIND[role]
        provider_id = self.role_provider_id(role)
        if not provider_id:
            raise ProviderNotConfiguredError(role, detail={"role": role})
        ids = [provider_id, *self.fallback_ids(role)]
        chain: list[Any] = []
        for item in ids:
            config = self.provider_config(item)
            if not config.enabled:
                logger.warning("provider_disabled", provider_id=item, role=role)
                continue
            if config.kind != expected_kind:
                raise ProviderNotConfiguredError(
                    role,
                    detail={
                        "role": role,
                        "provider_id": item,
                        "expected_kind": expected_kind,
                        "actual_kind": config.kind,
                    },
                )
            chain.append(self.instance(item))
        if not chain:
            raise ProviderNotConfiguredError(role, detail={"role": role, "reason": "全部被禁用"})
        return chain

    # -- 角色取用 ---------------------------------------------------------

    def llm(self, role: RoleName = "chat", *, purpose: str | None = None) -> LLMRoute:
        """取 LLM 角色门面。"""
        recorder = UsageRecorder(self.usage, space_id=self.space_id, purpose=purpose or role)
        providers = [cast(LLMProvider, item) for item in self._chain(role)]
        return LLMRoute(role, providers, recorder)

    def embedding(self, *, purpose: str = "ingest") -> EmbeddingRoute:
        """取 Embedding 角色门面。"""
        recorder = UsageRecorder(self.usage, space_id=self.space_id, purpose=purpose)
        providers = [cast(EmbeddingProvider, item) for item in self._chain("embedding")]
        return EmbeddingRoute("embedding", providers, recorder, query_cache=self._query_vectors)

    def rerank(self, *, purpose: str = "retrieve") -> RerankRoute:
        """取 Rerank 角色门面。"""
        recorder = UsageRecorder(self.usage, space_id=self.space_id, purpose=purpose)
        providers = [cast(RerankProvider, item) for item in self._chain("rerank")]
        return RerankRoute("rerank", providers, recorder, score_cache=self._rerank_scores)

    # -- 运维 -------------------------------------------------------------

    def provider_ids(self) -> list[str]:
        """全部 provider id。"""
        return [item.id for item in self.config.providers]

    async def health(self, provider_id: str) -> ProviderHealth:
        """实测单个 provider 的连通性。"""
        adapter = self.instance(provider_id)
        checkable = cast(HealthCheckable, adapter)
        return await checkable.health()

    def embedding_dimension(self, provider_id: str) -> int | None:
        """角色的向量维度；配置未显式声明时返回 ``None``（需要实际加载模型才能得知）。"""
        config = self.provider_config(provider_id)
        if config.dimension:
            return config.dimension
        instance = self._instances.get(provider_id)
        dimension = getattr(instance, "dimension", 0) if instance is not None else 0
        return int(dimension) if dimension else None

    async def aclose(self) -> None:
        """关闭全部适配器持有的连接。"""
        for instance in self._instances.values():
            close = getattr(instance, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as exc:
                logger.warning("provider_close_failed", error=str(exc))


def _last_int(chunks: list[ChatChunk], field: str) -> int | None:
    for chunk in reversed(chunks):
        value = getattr(chunk, field)
        if value is not None:
            return int(value)
    return None
