"""应用级运行时：全局设置 + Space 管理 + Provider 注册表。

``apps/api/deps.py`` 只从这里取单例，业务逻辑不落在 Web 层。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import time
from pathlib import Path

import structlog

from agentmem.config import ModelsConfig, Settings, get_settings, load_models_config
from agentmem.errors import NotFoundError
from agentmem.evolve import CritiqueService, EvolutionService, InsightService
from agentmem.expert import ConsistencyService, EvaluationService, ExpertiseService
from agentmem.expert.expertise import Embedder
from agentmem.ingest.pipeline import recover_interrupted
from agentmem.memory import CardService, KnowledgeExtractor, build_graph
from agentmem.memory.prune import drop_unreferenced_entities, prune_chunk_references
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve.chat import ChatService
from agentmem.retrieve.pipeline import RetrievalPipeline
from agentmem.retrieve.session import generation_registry
from agentmem.security import locate_raw_file
from agentmem.space.manager import SpaceManager
from agentmem.store import Database, fts
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    CapabilitiesResponse,
    Conversation,
    DeleteResponse,
    Document,
    GraphResponse,
    ProviderAlertsResponse,
    RoleName,
    StatsResponse,
)

logger = structlog.get_logger(__name__)

#: provider 告警的统计窗口：往回看 30 分钟。
#:
#: 太短会让「刚才连不上、现在还没再调用」的故障消失；太长则会把昨天已经修好的问题
#: 一直挂在界面上。半小时大致等于「这一会儿在用的时候出过问题」。
PROVIDER_ALERT_WINDOW_MS = 30 * 60 * 1000

#: 启动预热只管这两类角色：对话类模型都在远端，没有加载耗时。
WARMUP_ROLES: tuple[RoleName, ...] = ("embedding", "rerank")

#: 进程内加载模型的适配器（sentence-transformers 的双塔与交叉编码器）。
LOCAL_MODEL_ADAPTERS = frozenset({"sentence_transformers", "sentence_transformers_ce"})


def docling_available() -> bool:
    """是否安装了 docling（缺失时解析链自动只用 markitdown）。"""
    return importlib.util.find_spec("docling") is not None


def local_embedding_available() -> bool:
    """本地 Embedding 依赖是否可用。"""
    from agentmem.providers.adapters import local_embedding

    return local_embedding.is_available()


class Runtime:
    """进程内共享的核心单例。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.spaces = SpaceManager(self.settings)
        self.started_at = time.time()
        # 正在重建索引的 Space：重建期间写入会让向量表自相矛盾（先 drop 再重构），
        # 所以这期间拒绝新的摄取与重新解析
        self._maintenance_tokens: dict[str, object] = {}
        self._models: ModelsConfig = ModelsConfig()
        self._registries: dict[tuple[str, tuple[tuple[str, str], ...]], ProviderRegistry] = {}

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """打开全局库、加载模型配置，并收拾上次中断留下的未完成文档。"""
        await self.spaces.open()
        self.reload_models()
        recovered = await self.recover_interrupted_documents()
        # 磁盘上有、注册表里没有的 Space：只告警不自动复活。
        # 自动复活会把 `delete_space(purge=False)`（显式保留数据的删除）又变回来；
        # 但完全不吭声更糟——注册表丢一行，数据就在界面上彻底消失且无从知晓。
        unregistered = await self.spaces.find_unregistered_space_dirs()
        if unregistered:
            logger.warning(
                "unregistered_space_dirs_found",
                count=len(unregistered),
                spaces=[f"{sid}({name})" for sid, name in unregistered],
                hint="数据完好但不会出现在界面上；`uv run agentmem recover` 可重新登记",
            )
        logger.info(
            "runtime_started",
            data_dir=str(self.settings.data_dir),
            providers=len(self._models.providers),
            recovered_documents=recovered,
        )
        if self.settings.warmup_on_start:
            self._warmup_task = asyncio.create_task(self._startup_background(), name="warmup")

    async def _startup_background(self) -> None:
        """启动后的后台活：先预热模型，再回填缺失的经验向量（要用到刚预热好的向量模型）。"""
        await self._warmup_models()
        try:
            await self.backfill_insight_vectors()
        except Exception as exc:  # pragma: no cover - 回填只是补救，失败不影响服务
            logger.warning("insight_backfill_failed", error=str(exc))

    async def backfill_insight_vectors(self) -> list[str]:
        """给「有可召回经验、却一条向量都没有」的 Space 补建经验向量，返回补过的 Space。

        经验只有进了 ``insights_vec`` 才能按语义召回。早期版本的评测判决、蒸馏与整合
        直接写经验表、不同步向量（现在这些流程收尾时都会对账），那时晋升的经验就一直
        没有向量：检索于是退回「按置信度兜底」，问什么都带上同样几条。实测「test」
        Space 的 3 条 active 经验全部如此。这里只补「整张表在该 Space 下为空」的情形，
        已经有向量的 Space 不动——那里的对账由进化流程负责。
        """
        repaired: list[str] = []
        for space in await self.spaces.list_spaces():
            try:
                database = await self.spaces.space_db(space.id)
                vectors = database.vectors
                if vectors is None:
                    continue
                recallable = await database.insights.list_active(
                    space.id, min_confidence=0.0, limit=1
                )
                if not recallable or await vectors.count("insights_vec", space.id) > 0:
                    continue
                registry = await self.registry_for_space(space.id)
                if registry.role_provider_id("embedding") is None:
                    continue
                written = await InsightService(database, registry, space.id).reconcile_all_vectors()
            except Exception as exc:
                logger.warning("insight_backfill_failed", space_id=space.id, error=str(exc))
                continue
            if written:
                repaired.append(space.id)
                logger.info("insight_vectors_backfilled", space_id=space.id, count=written)
        return repaired

    async def _warmup_models(self) -> None:
        """后台预热本地模型与中文分词词典。

        进程内加载的向量与重排模型要几十秒才能就绪（实测 bge-m3 约 30 秒、
        bge-reranker 约 32 秒），而这一步此前发生在**第一次提问时**，用户看到的是
        「思考中…」一直转。启动后立刻在后台加载，等用户真的提问时已经就绪。

        - 只预热本地适配器：远端服务没有「加载」这回事，调一次只是白花一次请求。
        - 全局绑定之外还看各 Space 的模型覆盖，否则换了本地模型的 Space 首问照样慢。
        - 加载与推理都在 ``to_thread`` 里跑、推理持设备锁，不占事件循环；
          失败只记日志——预热只是提速，任何异常都不该影响启动或服务。
        """
        started = time.perf_counter()
        await self._warmup_tokenizer()
        try:
            targets = await self._warmup_targets()
        except Exception as exc:
            logger.warning("warmup_failed", stage="collect", error=str(exc))
            return
        if not targets:
            logger.info("warmup_skipped", reason="没有绑定本地向量 / 重排模型")
            return
        logger.info(
            "warmup_started",
            targets=[f"{role}:{provider_id}" for role, provider_id in targets],
        )
        registry = self.registry()
        for role, provider_id in targets:
            try:
                health = await registry.health(provider_id)
                logger.info(
                    "warmup_done",
                    role=role,
                    provider=provider_id,
                    ok=health.ok,
                    latency_ms=health.latency_ms,
                    error=health.error,
                )
            except Exception as exc:
                logger.warning("warmup_failed", role=role, provider=provider_id, error=str(exc))
        logger.info("warmup_finished", elapsed_ms=int((time.perf_counter() - started) * 1000))

    async def _warmup_tokenizer(self) -> None:
        """加载 jieba 词典：否则第一次检索要先等它读词典（实测最慢 2.4 秒）。

        不管有没有绑定本地模型都要做——全文检索总会用到分词。放在模型之前，
        它只要一两秒，不必排在几十秒的模型加载后面。
        """
        try:
            elapsed = await asyncio.to_thread(fts.warm_up)
            logger.info("warmup_done", role="tokenizer", elapsed_ms=int(elapsed * 1000))
        except Exception as exc:
            logger.warning("warmup_failed", role="tokenizer", error=str(exc))

    async def _warmup_targets(self) -> list[tuple[str, str]]:
        """要预热的 (角色, provider id)：全局绑定与各 Space 覆盖里启用着的本地模型。"""
        registry = self.registry()
        bound: list[tuple[str, str]] = []
        for role in WARMUP_ROLES:
            provider_id = registry.role_provider_id(role)
            if provider_id:
                bound.append((role, provider_id))
        for space in await self.spaces.list_spaces():
            try:
                config = await self.spaces.read_config(space.id)
            except Exception as exc:
                # 某个 Space 的 space.yaml 坏了不该连累别的 Space 预热
                logger.info("warmup_space_config_unreadable", space_id=space.id, error=str(exc))
                continue
            for role in WARMUP_ROLES:
                provider_id = config.models.get(role)
                if provider_id:
                    bound.append((role, provider_id))

        targets: list[tuple[str, str]] = []
        seen: set[str] = set()
        for bound_role, provider_id in bound:
            if provider_id in seen:
                continue
            seen.add(provider_id)
            try:
                provider = registry.provider_config(provider_id)
            except Exception:
                continue  # 绑定指向已删除的 provider：doctor 会报，这里不管
            if provider.enabled and provider.adapter in LOCAL_MODEL_ADAPTERS:
                targets.append((bound_role, provider_id))
        return targets

    async def recover_interrupted_documents(self) -> int:
        """把所有 Space 里被中断的摄取任务判为失败，返回处理条数。

        代价是启动时会逐个打开 Space 库；本应用是本机单用户、Space 数量有限，
        这点开销换来的是「不会永远卡在摄取中」的确定性。
        """
        total = 0
        for space in await self.spaces.list_spaces():
            database = await self.spaces.space_db(space.id)
            total += len(await recover_interrupted(database))
        return total

    async def stop(self) -> None:
        """关闭全部资源。"""
        await generation_registry().cancel_and_wait()
        task = getattr(self, "_warmup_task", None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for registry in self._registries.values():
            await registry.aclose()
        self._registries.clear()
        await self.spaces.close()

    @property
    def uptime_ms(self) -> int:
        """进程运行时长（毫秒）。"""
        return int((time.time() - self.started_at) * 1000)

    # -- 模型配置 ---------------------------------------------------------

    @property
    def models_config(self) -> ModelsConfig:
        """当前模型配置。"""
        return self._models

    def reload_models(self) -> ModelsConfig:
        """重新读取 ``config/models.yaml`` 并清空注册表缓存。"""
        self._models = load_models_config(self.settings.models_config)
        self.invalidate_registries()
        return self._models

    def save_models(self, config: ModelsConfig) -> ModelsConfig:
        """写回模型配置并重新加载。"""
        from agentmem.config import save_models_config

        save_models_config(config, self.settings.models_config)
        return self.reload_models()

    def invalidate_registries(self) -> None:
        """清空注册表缓存（配置变更后调用）。"""
        self._registries.clear()

    def registry(
        self, space_id: str | None = None, overrides: dict[str, str] | None = None
    ) -> ProviderRegistry:
        """取 Provider 注册表；用量记录写入全局库。"""
        key = (space_id or "", tuple(sorted((overrides or {}).items())))
        registry = self._registries.get(key)
        if registry is None:
            registry = ProviderRegistry(
                self._models,
                usage=self.spaces.global_db.usage,
                space_id=space_id,
                overrides=overrides,
            )
            self._registries[key] = registry
        return registry

    async def registry_for_space(self, space_id: str) -> ProviderRegistry:
        """取某 Space 的注册表，应用 ``space.yaml`` 中的模型覆盖。"""
        config = await self.spaces.read_config(space_id)
        overrides: dict[str, str] = {}
        for role, provider_id in config.models.items():
            if role in {"chat", "fast", "distill", "judge", "embedding", "rerank"}:
                overrides[role] = provider_id
        return self.registry(space_id, overrides or None)

    def roles_in_use(self, provider_id: str) -> list[str]:
        """哪些角色引用了该 provider（只看全局绑定）。"""
        return self.registry().roles_in_use(provider_id)

    def role_bindings(self) -> dict[RoleName, str | None]:
        """当前角色绑定。"""
        roles: dict[RoleName, str | None] = {}
        for role in ("chat", "fast", "distill", "judge", "embedding", "rerank"):
            roles[role] = self.registry().role_provider_id(role)
        return roles

    # -- 存储 -------------------------------------------------------------

    async def space_db(self, space_id: str) -> Database:
        """取某 Space 的数据库句柄。"""
        return await self.spaces.space_db(space_id)

    async def global_db(self) -> Database:
        """取全局库。"""
        await self.spaces.open()
        return self.spaces.global_db

    # -- 能力与统计 -------------------------------------------------------

    def capabilities(self) -> CapabilitiesResponse:
        """后端能力声明，前端据此显隐功能。"""
        registry = self.registry()
        rerank_available = False
        try:
            provider_id = registry.role_provider_id("rerank")
            if provider_id:
                rerank_available = registry.provider_config(provider_id).enabled
        except Exception:
            rerank_available = False

        embedding_local = False
        try:
            provider_id = registry.role_provider_id("embedding")
            if provider_id:
                config = registry.provider_config(provider_id)
                embedding_local = config.adapter == "sentence_transformers"
        except Exception:
            embedding_local = False

        def role_ready(role: str) -> bool:
            """该角色是否绑定了启用中的 provider。"""
            try:
                provider_id = registry.role_provider_id(role)  # type: ignore[arg-type]
            except Exception:
                return False
            if not provider_id:
                return False
            try:
                return bool(registry.provider_config(provider_id).enabled)
            except Exception:
                return False

        return CapabilitiesResponse(
            rerank_available=rerank_available,
            local_embedding=embedding_local and local_embedding_available(),
            docling_available=docling_available(),
            graph_enabled=True,
            chat_available=role_ready("chat"),
            distill_available=role_ready("distill"),
        )

    async def provider_alerts(
        self, *, window_ms: int = PROVIDER_ALERT_WINDOW_MS
    ) -> ProviderAlertsResponse:
        """最近一段时间里失败过的 provider。

        用量记录是全局库里的（每次调用都会记一条，成败都记），所以这里不需要另立
        一套运行期状态，也不会随进程重启而丢失。
        """
        await self.spaces.open()
        since = now_ms() - window_ms
        alerts = await self.spaces.global_db.usage.recent_failures(since=since)
        return ProviderAlertsResponse(
            window_ms=window_ms,
            alerts=alerts,
            degraded=any(not alert.recovered for alert in alerts),
        )

    async def stats(self) -> StatsResponse:
        """全局统计：Space 数、文档数、chunk 数、磁盘占用。"""
        await self.spaces.open()
        spaces = await self.spaces.list_spaces()
        documents = 0
        chunks = 0
        insights = 0
        for space in spaces:
            database = await self.spaces.space_db(space.id)
            documents += await database.documents.count()
            chunks += await database.chunks.count()
            insights += await database.insights.count()
        return StatsResponse(
            space_count=len(spaces),
            document_count=documents,
            chunk_count=chunks,
            insight_count=insights,
            disk_usage_bytes=await self.spaces.disk_usage_bytes(),
        )

    def web_dist_dir(self) -> Path:
        """前端产物目录。"""
        return self.settings.web_dist_dir

    # -- 聚合入口（供 apps/api 一次调用完成一件业务事） ---------------------

    async def retrieval_pipeline(self, space_id: str) -> RetrievalPipeline:
        """组装某 Space 的检索管线（数据库 + 注册表 + ``space.yaml`` 参数）。"""
        await self.spaces.require_space(space_id)
        database = await self.spaces.space_db(space_id)
        registry = await self.registry_for_space(space_id)
        config = await self.spaces.read_config(space_id)
        return RetrievalPipeline(
            space_id=space_id,
            database=database,
            registry=registry,
            settings=self.settings,
            retrieval=config.retrieval,
            persona=config.persona,
        )

    async def chat_service(self, conversation_id: str) -> ChatService:
        """按会话 id 组装问答服务。

        会话路由不带 ``space_id``（`03-API-SPEC.md` §5），所以要先把会话
        定位到它所属的 Space，再组装该 Space 的链路。
        """
        space_id, _ = await self.find_conversation(conversation_id)
        database = await self.spaces.space_db(space_id)
        registry = await self.registry_for_space(space_id)
        config = await self.spaces.read_config(space_id)
        return ChatService(
            space_id=space_id,
            database=database,
            registry=registry,
            settings=self.settings,
            retrieval=config.retrieval,
            persona=config.persona,
        )

    async def knowledge_extractor(self, space_id: str) -> KnowledgeExtractor:
        """组装某 Space 的 L2 抽取器。"""
        await self.spaces.require_space(space_id)
        database = await self.spaces.space_db(space_id)
        registry = await self.registry_for_space(space_id)
        return await KnowledgeExtractor.for_space(
            space_id=space_id,
            database=database,
            registry=registry,
            settings=self.settings,
        )

    async def card_service(self, space_id: str) -> CardService:
        """组装某 Space 的 L2 卡片服务。"""
        await self.spaces.require_space(space_id)
        return CardService(
            space_id=space_id,
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
        )

    async def knowledge_graph(
        self,
        space_id: str,
        *,
        center: str | None = None,
        depth: int = 2,
        limit: int = 300,
    ) -> GraphResponse:
        """聚合某 Space 的实体关系图。"""
        await self.spaces.require_space(space_id)
        database = await self.spaces.space_db(space_id)
        return await build_graph(database, space_id, center=center, depth=depth, limit=limit)

    async def critique_service(self, space_id: str) -> CritiqueService:
        """组装某 Space 的评价服务（反馈收集 + LLM-as-Judge + 反馈回流）。"""
        await self.spaces.require_space(space_id)
        config = await self.spaces.read_config(space_id)
        return CritiqueService(
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
            persona=config.persona,
            space_id=space_id,
        )

    async def insight_service(self, space_id: str) -> InsightService:
        """组装某 Space 的 L3 经验生命周期服务（含向量同步）。"""
        await self.spaces.require_space(space_id)
        return InsightService(
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
            space_id=space_id,
        )

    async def consistency_service(self, space_id: str) -> ConsistencyService:
        """组装某 Space 的一致性探测服务（需要检索管线与 embedding）。"""
        pipeline = await self.retrieval_pipeline(space_id)
        return ConsistencyService(
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
            pipeline=pipeline,
        )

    async def expertise_service(self, space_id: str) -> ExpertiseService:
        """组装某 Space 的专家度计算服务。"""
        await self.spaces.require_space(space_id)
        config = await self.spaces.read_config(space_id)
        embed, embed_key = await self.topic_embedder(space_id)
        return ExpertiseService(
            await self.spaces.space_db(space_id), config.persona, embed=embed, embed_key=embed_key
        )

    async def topic_embedder(self, space_id: str) -> tuple[Embedder | None, str]:
        """卡片 → 大纲主题语义归属用的向量化函数，及其缓存键（provider 名）。

        没配 embedding 角色时返回 ``(None, "")``，调用方退回逐字匹配。
        """
        try:
            registry = await self.registry_for_space(space_id)
            route = registry.embedding(purpose="expertise")
        except Exception:
            return None, ""
        if not route.providers:
            return None, ""

        async def embed(texts: list[str]) -> list[list[float]]:
            return await route.embed(texts, kind="doc")

        return embed, route.name

    async def evaluation_service(self, space_id: str) -> EvaluationService:
        """组装某 Space 的评测执行器（依赖检索管线生成答案）。"""
        pipeline = await self.retrieval_pipeline(space_id)
        config = await self.spaces.read_config(space_id)
        return EvaluationService(
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
            pipeline=pipeline,
            persona=config.persona,
        )

    async def evolution_service(self, space_id: str) -> EvolutionService:
        """组装某 Space 的进化闭环编排器。"""
        pipeline = await self.retrieval_pipeline(space_id)
        config = await self.spaces.read_config(space_id)
        return EvolutionService(
            database=await self.spaces.space_db(space_id),
            registry=await self.registry_for_space(space_id),
            pipeline=pipeline,
            persona=config.persona,
        )

    async def find_conversation(self, conversation_id: str) -> tuple[str, Conversation]:
        """定位会话所属的 Space。

        会话只在 Space 自己的 ``meta.db`` 里，全局库不存副本，所以只能逐个 Space 查。
        本地单机场景下 Space 数量是个位数，这点开销可以接受；
        真到了几十个 Space 再考虑在全局库加一张索引表。
        """
        for space in await self.spaces.list_spaces():
            database = await self.spaces.space_db(space.id)
            conversation = await database.conversations.get(conversation_id)
            if conversation is not None:
                return space.id, conversation
        raise NotFoundError("会话", conversation_id)

    async def find_trace_space(self, trace_id: str) -> str:
        """定位轨迹所属的 Space（轨迹路由不带 space_id，见 `03-API-SPEC.md` §7）。

        同 find_conversation：逐 Space 查。Trace 也可从 ``trace.space_id`` 读到，
        但要先知道在哪个库里才能查到它，故仍需遍历。
        """
        for space in await self.spaces.list_spaces():
            database = await self.spaces.space_db(space.id)
            trace = await database.traces.get(trace_id)
            if trace is not None:
                return space.id
        raise NotFoundError("轨迹", trace_id)

    # -- Space 级互斥 -----------------------------------------------------

    def lock_space(self, space_id: str) -> None:
        """标记该 Space 正在做互斥的维护操作（目前只有重建索引）。"""
        token = self.spaces.operation_gate(space_id).lock()
        self._maintenance_tokens[space_id] = token

    def unlock_space(self, space_id: str) -> None:
        """解除标记；重复解除是安全的。"""
        token = self._maintenance_tokens.pop(space_id, None)
        if token is not None:
            self.spaces.operation_gate(space_id).unlock(token)

    def is_space_locked(self, space_id: str) -> bool:
        """该 Space 是否正在做维护操作。"""
        return self.spaces.operation_gate(space_id).locked

    def require_unlocked(self, space_id: str) -> None:
        """维护期间拒绝写操作。

        重建索引会先 ``drop`` 掉整张向量表再逐篇重构：这期间新投喂的文档，
        它的向量要么被 drop 抹掉、要么与重构过程互相覆盖，最后留下一个
        自相矛盾的索引。宁可让用户等一下，也不要把索引写坏。
        """
        self.spaces.operation_gate(space_id).require_writable()

    def maintain_space(self, space_id: str) -> contextlib.AbstractContextManager[object]:
        """独占维护空间，只有持有本次许可的流水线可以写入。"""
        return self.spaces.operation_gate(space_id).maintenance()

    async def delete_document(self, space_id: str, document_id: str) -> DeleteResponse:
        """删除文档及其派生数据：切片、全文索引、向量、解析缓存、原始文件，以及
        只来自这篇文档的知识卡片、关系和孤立实体。

        卡片与关系只靠 ``source_chunks`` 记来源，此前删文档从不碰它们：实测删掉一篇
        粘贴文本后，从它抽出的卡片还在卡片列表里、向量还会被检索召回，
        ``raw/`` 下的原文件也一直留在磁盘上。

        聚合在 core 而不是 API 层逐层调存储：级联顺序（先摘 FTS 再删行）和
        缓存路径都属于实现细节，路由不该知道。
        """
        with self.maintain_space(space_id):
            await self.spaces.require_space(space_id)
            database = await self.spaces.space_db(space_id)
            database.operations.require_document_idle(document_id)
            async with database.operations.document_lock(document_id):
                return await self._delete_idle_document(database, space_id, document_id)

    async def _delete_idle_document(
        self, database: Database, space_id: str, document_id: str
    ) -> DeleteResponse:
        document = await database.documents.require(document_id)
        # 先记下这篇文档提到过哪些实体：删完之后它们的提及明细会随外键消失，
        # 计数得跟着重算，否则图上还挂着已经不存在的提及
        mentioned = await database.entities.entity_ids_for_document(document_id)
        chunk_ids = [chunk.id for chunk in await database.chunks.list_by_document(document_id)]
        await prune_chunk_references(database, chunk_ids, drop_orphans=True)
        await database.chunks.delete_by_document(document_id)
        vectors = database.vectors
        if vectors is not None:
            await vectors.delete_by_field("chunks_vec", "document_id", document_id)
        deleted = await database.documents.delete(document_id)
        await database.entities.refresh_mention_counts(mentioned)
        await drop_unreferenced_entities(database, mentioned)
        await self._remove_parse_cache(space_id, document_id)
        await self._remove_raw_file(space_id, document)
        logger.info("document_deleted", space_id=space_id, document_id=document_id)
        return DeleteResponse(deleted=deleted, id=document.id)

    async def _remove_raw_file(self, space_id: str, document: Document) -> None:
        """删除 ``raw/`` 下的原始文件。只删 raw 目录里的——路径越界（老数据、手工改过库）就不动。"""
        # raw 文件名里带着文档 id 或内容摘要，按文件名回找不会误删别的文档的原文
        path = await asyncio.to_thread(
            locate_raw_file,
            self.spaces.raw_dir(space_id),
            document.meta.raw_path or document.source_uri,
        )
        if path is not None:
            await asyncio.to_thread(path.unlink)

    async def _remove_parse_cache(self, space_id: str, document_id: str) -> None:
        """删除解析结果缓存（``cache/<space_id>/<document_id>.parse.json``）。"""
        cache = self.settings.data_dir / "cache" / space_id / f"{document_id}.parse.json"
        if await asyncio.to_thread(cache.is_file):
            await asyncio.to_thread(cache.unlink)
