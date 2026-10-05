"""L2 知识卡片的持久化。

三件事收在这里，避免抽取流程与路由各写一份：

1. **去重**：同一个 Space 内 ``title + kind`` 相同即视为同一张卡片。模型只保证
   单批内不重复，跨批、跨文档的重复由这里合并。
2. **人工校订规则**：PATCH 过的卡片记为 ``verified_by='user'``、置信度拉满；
   此后自动抽取不再覆盖它的正文。
3. **版本留档**：正文被取代时，旧内容先存进 ``card_versions``。卡片是「事实」，
   而事实会变——新版指南把推荐剂量从 400mg 改成 200mg，旧值不该静默消失，
   「什么时候改的、原来是多少」在医药与合规场景里正是最该看得到的信息。
4. **向量同步**：卡片写入 ``cards_vec`` 才能被检索管线召回。向量表由 store 层
   做维度守卫，维度不符会抛错而不是把向量写进一张对不上的表。
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from agentmem.errors import AgentMemError, ProviderNotConfiguredError, ValidationError
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    CardHistoryResponse,
    CardVersion,
    DeleteResponse,
    KnowledgeCard,
    KnowledgeCardCreate,
    KnowledgeCardRequest,
    KnowledgeCardUpdate,
    VectorRecord,
)

from .models import VectorSyncResult

logger = structlog.get_logger(__name__)

#: 人工校订后的置信度
VERIFIED_CONFIDENCE = 1.0

#: 去重时按标题回捞的条数上限；标题完全相等才算同一条，回捞只是缩小扫描范围
DEDUP_SCAN_LIMIT = 20


def embedding_text(card: KnowledgeCard) -> str:
    """卡片向量化的文本：标题是检索钩子，正文是内容，两者一起编码。"""
    return f"{card.title}\n\n{card.body}"


def _union(left: Sequence[str], right: Sequence[str]) -> list[str]:
    """并集并保持出现顺序。"""
    seen: dict[str, None] = {}
    for item in (*left, *right):
        seen.setdefault(item, None)
    return list(seen)


class CardService:
    """单 Space 的 L2 卡片读写。

    Args:
        space_id: 所属 Space。
        database: 该 Space 的存储门面。
        registry: Provider 注册表（提供 embedding 角色）。
    """

    def __init__(self, *, space_id: str, database: Database, registry: ProviderRegistry) -> None:
        self.space_id = space_id
        self.db = database
        self.registry = registry

    # -- 人工路径 ---------------------------------------------------------

    async def create(self, payload: KnowledgeCardRequest) -> KnowledgeCard:
        """人工新建卡片，并尽力同步向量。

        人写的卡片默认按「已核验」处理：``verified_by='user'``、置信度拉满。
        规则与 PATCH 一致——用户亲手写下的内容不该被下一次自动抽取悄悄覆盖，
        而抽取结果的默认置信度只要高过 0.5 就能盖掉人工录入，这条路径迟早会踩到。
        显式传了 ``confidence`` / ``verified_by`` 就按传的来。
        """
        fields = payload.model_dump(exclude_unset=True)
        fields.setdefault("verified_by", "user")
        fields.setdefault("confidence", VERIFIED_CONFIDENCE)
        data = KnowledgeCardCreate(space_id=self.space_id, **fields)
        card = await self.db.cards.create(data)
        await self._sync_quietly([card])
        return card

    async def update(self, card_id: str, payload: KnowledgeCardUpdate) -> KnowledgeCard:
        """人工校订卡片。

        未显式给出的字段按校订处理：``verified_by='user'``、置信度拉满。
        标题或正文变了才重算向量，只改别名不必付一次 embedding 调用。
        """
        before = await self.db.cards.require(card_id)
        fields = payload.model_dump(exclude_unset=True)
        # 按值比较而不是按「请求里带没带这个字段」：前端每次保存都带上标题和正文，
        # 一个字没改点一下保存也会留一个和当前逐字相同的历史版本
        changes_text = any(
            key in fields and fields[key] != getattr(before, key) for key in ("title", "body")
        )
        fields.setdefault("verified_by", "user")
        fields.setdefault("confidence", VERIFIED_CONFIDENCE)
        if changes_text:
            await self._archive(before)
        card = await self.db.cards.update(card_id, KnowledgeCardUpdate.model_validate(fields))
        if changes_text:
            await self._sync_quietly([card])
        return card

    # -- 版本 -------------------------------------------------------------

    async def history(self, card_id: str) -> CardHistoryResponse:
        """卡片的历史版本（新的在前）与当前版本。"""
        card = await self.db.cards.require(card_id)
        return CardHistoryResponse(
            card_id=card.id,
            card=card,
            versions=await self.db.card_versions.list_by_card(card.id),
        )

    async def _archive(self, card: KnowledgeCard) -> CardVersion:
        """把卡片当前的内容留档，返回写下的历史版本。

        ``valid_from`` 取卡片的 ``updated_at``：上一次改动落库的时间就是这一版
        开始生效的时间；``valid_to`` 是现在——它从此刻起被取代。
        """
        now = now_ms()
        version = await self.db.card_versions.next_version(card.id)
        record = await self.db.card_versions.add(
            card, version=version, valid_from=card.updated_at, valid_to=now
        )
        logger.info(
            "card_version_archived",
            space_id=self.space_id,
            card_id=card.id,
            version=version,
            title=card.title,
        )
        return record

    async def delete(self, card_id: str) -> DeleteResponse:
        """删除卡片并清掉它的向量，避免检索召回一条已不存在的卡片。"""
        card = await self.db.cards.require(card_id)
        if self.db.vectors is not None:
            await self.db.vectors.delete("cards_vec", [card.id])
        deleted = await self.db.cards.delete(card.id)
        return DeleteResponse(deleted=deleted, id=card.id)

    # -- 抽取路径 ---------------------------------------------------------

    async def find_by_title_kind(self, kind: str, title: str) -> KnowledgeCard | None:
        """按 ``title + kind`` 找同一条卡片；标题必须完全相等。"""
        items, _, _ = await self.db.cards.list_by_space(
            self.space_id, kind=kind, query=title, limit=DEDUP_SCAN_LIMIT
        )
        for item in items:
            if item.kind == kind and item.title == title:
                return item
        return None

    async def merge_extracted(self, data: KnowledgeCardCreate) -> tuple[KnowledgeCard, bool]:
        """把抽取结果落库，返回 ``(卡片, 是否新建)``。

        已存在同名同类的卡片时合并而不是新建：来源切片取并集，
        新结果置信度更高才替换正文。

        这里只写关系库，**向量同步由调用方决定时机**——抽取是按批推进的，
        逐卡调用方才能把向量化失败记到对应的统计里。
        """
        existing = await self.find_by_title_kind(data.kind, data.title)
        if existing is None:
            return await self.db.cards.create(data), True
        return await self._merge(existing, data), False

    async def _merge(self, existing: KnowledgeCard, data: KnowledgeCardCreate) -> KnowledgeCard:
        """合并同一张卡片的两次抽取结果。"""
        patch = KnowledgeCardUpdate(
            aliases=_union(existing.aliases, data.aliases),
            source_chunks=_union(existing.source_chunks, data.source_chunks),
        )
        if existing.verified_by != "user" and data.confidence > existing.confidence:
            # 正文要被换掉了：先留档。用户修订过的卡片（verified_by='user'）不走
            # 这条分支，它们本来就不该被自动抽取覆盖。
            if data.body.strip() != existing.body.strip():
                await self._archive(existing)
            patch = KnowledgeCardUpdate(
                aliases=patch.aliases,
                source_chunks=patch.source_chunks,
                body=data.body,
                confidence=data.confidence,
            )
        return await self.db.cards.update(existing.id, patch)

    # -- 向量同步 ---------------------------------------------------------

    async def sync_vectors(self, cards: Sequence[KnowledgeCard]) -> VectorSyncResult:
        """把卡片写入向量表。

        Returns:
            同步结果。缺 embedding 能力（未配向量库或角色未绑定）时记为 ``skipped``；
            其余失败（维度不符、服务不可用）一律抛出，由调用方决定是告警还是中断。
        """
        if not cards:
            return VectorSyncResult()
        if self.db.vectors is None:
            return VectorSyncResult(skipped=True)
        try:
            embedder = self.registry.embedding(purpose="ingest")
        except ProviderNotConfiguredError as exc:
            logger.warning("card_embedding_unavailable", space_id=self.space_id, error=exc.message)
            return VectorSyncResult(skipped=True)
        vectors = await self.db.require_vectors()
        embeddings = await embedder.embed([embedding_text(card) for card in cards], kind="doc")
        if len(embeddings) != len(cards):
            raise ValidationError(
                "向量数量与卡片数量不一致",
                detail={"expected": len(cards), "actual": len(embeddings)},
            )
        await vectors.upsert(
            "cards_vec",
            [
                VectorRecord(
                    id=card.id,
                    space_id=card.space_id,
                    vector=vector,
                    embedding_model=embedder.name,
                )
                for card, vector in zip(cards, embeddings, strict=True)
            ],
        )
        return VectorSyncResult(embedded=len(cards))

    async def _sync_quietly(self, cards: Sequence[KnowledgeCard]) -> None:
        """人工路径的向量同步：失败只告警。

        卡片本身已经落库，检索管线在向量索引为空或召回不到时会按置信度兜底，
        没有理由因为向量服务不可用就让用户的编辑操作报错。
        """
        try:
            result = await self.sync_vectors(cards)
        except AgentMemError as exc:
            logger.warning(
                "card_embedding_failed", space_id=self.space_id, code=exc.code, error=exc.message
            )
            return
        if result.skipped:
            logger.info("card_embedding_skipped", space_id=self.space_id, cards=len(cards))
