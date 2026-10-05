"""L3 经验的生命周期管理：增删改、提升/归档、冲突裁决、溯源、向量同步、反馈回流。

**为什么向量同步在这里**：检索管线用问题向量在 ``insights_vec`` 里召回经验
（`retrieve/pipeline.py`）。一条经验只有被写进 ``insights_vec`` 才可能被召回。
所以凡是「让经验生效」的动作（手动新建、人工确认、冲突合并结果转正）都必须
同步向量；凡是「让经验失效」的动作（归档、删除）都必须移除向量——否则会召回到
已归档的经验。这一层把「置信度状态」与「是否可被召回」这两件事保持一致。

被向量化的是经验的 ``trigger``（适用场景），因为召回时是拿当前问题去匹配「什么
场景下适用」，而不是匹配「怎么做」。
"""

from __future__ import annotations

import structlog
from pydantic import Field

from agentmem.errors import ProviderNotConfiguredError, ValidationError
from agentmem.evolve.confidence import (
    ConfidenceEvent,
    apply_confidence_event,
    feedback_event,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import (
    AgentMemModel,
    ConflictResolveRequest,
    FeedbackKind,
    Insight,
    InsightConflictGroup,
    InsightCreate,
    InsightEventCreate,
    InsightHistoryResponse,
    InsightKind,
    InsightLineage,
    InsightReviewItem,
    InsightReviewResponse,
    InsightScope,
    InsightUpdate,
    Trace,
    VectorRecord,
)

logger = structlog.get_logger(__name__)

#: 「值得复查」的入选门槛：至少被注入这么多次，才有统计意义
REVIEW_MIN_APPLIED = 5

#: 成功率低于此值才入选
REVIEW_MAX_SUCCESS_RATE = 0.4

#: 一次最多列这么多条
REVIEW_LIMIT = 20

#: 经验被视为「可召回」的状态：只有这些状态的经验才该出现在 insights_vec 里
RECALLABLE_STATUSES = frozenset({"active", "candidate"})


class FeedbackAttribution(AgentMemModel):
    """一次用户反馈回流到经验上的结果。"""

    event: ConfidenceEvent | None = Field(default=None, description="映射到的置信度事件")
    insight_ids: list[str] = Field(default_factory=list, description="被更新置信度的经验")
    archived: list[str] = Field(default_factory=list, description="因置信度跌破阈值被归档的经验")
    share: float = Field(default=1.0, description="每条经验分到的证据份额")


async def reconcile_insight_vectors(
    database: Database, registry: ProviderRegistry, space_id: str
) -> None:
    """蒸馏 / 整合 / 判决收尾时调用：向量池跟经验状态对账。失败只记日志，不打断进化。"""
    try:
        await InsightService(database, registry, space_id).reconcile_all_vectors()
    except Exception:  # pragma: no cover - 向量化失败不该让已经落库的进化结果回滚
        logger.exception("insight_vector_reconcile_failed", space_id=space_id)


class InsightService:
    """L3 经验的读写与生命周期。一个 Space 一个实例。"""

    def __init__(self, database: Database, registry: ProviderRegistry, space_id: str) -> None:
        self.db = database
        self.registry = registry
        self.space_id = space_id

    # -- CRUD -------------------------------------------------------------

    async def create_manual(
        self,
        *,
        trigger: str,
        guidance: str,
        kind: InsightKind,
        rationale: str | None = None,
        scope: InsightScope = "space",
        confidence: float = 0.8,
    ) -> Insight:
        """人工新增经验：直接 active（用户手写的即视为已确认），并向量化。"""
        insight = await self.db.insights.create(
            InsightCreate(
                space_id=self.space_id,
                trigger=trigger,
                guidance=guidance,
                rationale=rationale,
                kind=kind,
                scope=scope,
                confidence=confidence,
                status="active",
                origin="manual",
            )
        )
        await self._sync_vector(insight)
        return insight

    async def update(self, insight_id: str, patch: InsightUpdate) -> Insight:
        """编辑经验。改动 trigger 或 status 时同步向量。"""
        before = await self.db.insights.require(insight_id)
        updated = await self.db.insights.update(insight_id, patch)
        # trigger 变了要重算向量；status 变了要决定加入还是移出召回池
        if patch.trigger is not None or patch.status is not None:
            await self._reconcile_vector(before, updated)
        return updated

    async def delete(self, insight_id: str) -> bool:
        """硬删除经验并移除其向量。"""
        await self._remove_vector(insight_id)
        return await self.db.insights.delete(insight_id)

    # -- 生命周期 ---------------------------------------------------------

    async def promote(self, insight_id: str) -> Insight:
        """人工确认：置信度 +0.3、转 active，并确保进入召回池。"""
        insight = await self.db.insights.require(insight_id)
        new_conf, new_status = apply_confidence_event(
            confidence=insight.confidence,
            status=insight.status,
            event=ConfidenceEvent.USER_CONFIRM,
        )
        updated = await self.db.insights.update(
            insight_id, InsightUpdate(confidence=new_conf, status=new_status)
        )
        await self._record_event(
            before=insight, after=updated, event=str(ConfidenceEvent.USER_CONFIRM)
        )
        await self._reconcile_vector(insight, updated)
        return updated

    async def archive(self, insight_id: str) -> Insight:
        """归档（软删除）并移出召回池。"""
        insight = await self.db.insights.require(insight_id)
        updated = await self.db.insights.update(insight_id, InsightUpdate(status="archived"))
        await self._record_event(before=insight, after=updated, event="archived_manually")
        await self._remove_vector(insight_id)
        return updated

    # -- 反馈回流 ---------------------------------------------------------

    async def attribute_feedback(self, trace: Trace, kind: FeedbackKind) -> FeedbackAttribution:
        """把一次用户反馈回流到这次回答注入过的经验上。

        注入过哪几条由 ``trace.used_insights`` 给出，这是系统手里唯一硬的事实。
        但「用了这条经验」不等于「这条经验起了作用」：用户点的是整条回答，系统
        无从知道注入的六条里哪一条坏了事。所以这一次反馈的证据按条数均摊
        （``share = 1 / 条数``），既让差评真的落到被注入的经验上，又不至于一次
        误点把六条经验一起推向归档。``success_count`` / ``applied_count`` 照实
        逐条累加，单条归因日后靠这两个计数做统计，而不是靠这一次点击去猜。

        没有注入过经验的回答、以及本身不改变置信度的反馈类别，都只返回空结果。
        """
        event = feedback_event(kind)
        injected = [item for item in trace.used_insights if item]
        if event is None or not injected:
            return FeedbackAttribution(event=event)
        found = await self.db.insights.get_many(injected)
        if not found:
            # 被注入的经验可能已经被删掉了；反馈本身仍然留在库里供蒸馏使用
            return FeedbackAttribution(event=event)

        share = 1 / len(found)
        archived: list[str] = []
        for insight in found:
            updated = await self._apply_event(insight, event, share=share)
            if updated.status != insight.status:
                archived.append(updated.id)
        # 字段名不能叫 event：structlog 的第一个位置参数就是事件名，
        # 再传一个 event= 会直接 TypeError，把整次回流记录成失败
        logger.info(
            "insight_feedback_attributed",
            space_id=self.space_id,
            trace_id=trace.id,
            feedback_kind=kind,
            confidence_event=str(event),
            insights=len(found),
            archived=len(archived),
        )
        return FeedbackAttribution(
            event=event,
            insight_ids=[item.id for item in found],
            archived=archived,
            share=share,
        )

    async def _apply_event(
        self, insight: Insight, event: ConfidenceEvent, *, share: float
    ) -> Insight:
        """施加一个置信度事件并写回；跌破阈值时把经验移出召回池。

        这里不调 ``_reconcile_vector``：反馈不改 ``trigger``，向量本身不需要重算，
        经验能被召回就说明它已经在池子里；只有掉到不可召回状态才需要移除，
        漏掉这一步会留下「已归档却仍被召回」的脏向量。
        """
        new_confidence, new_status = apply_confidence_event(
            confidence=insight.confidence,
            status=insight.status,
            event=event,
            share=share,
        )
        patch = InsightUpdate(confidence=new_confidence, status=new_status)
        if event is ConfidenceEvent.POSITIVE_FEEDBACK:
            patch.success_count = insight.success_count + 1
        updated = await self.db.insights.update(insight.id, patch)
        await self._record_event(
            before=insight,
            after=updated,
            event=str(event),
            share=share,
            reason=f"均摊 1/{round(1 / share)}" if 0 < share < 1 else "独占整份",
        )
        if updated.status != insight.status and updated.status not in RECALLABLE_STATUSES:
            await self._remove_vector(updated.id)
        return updated

    async def _record_event(
        self,
        *,
        before: Insight,
        after: Insight,
        event: str,
        share: float | None = None,
        reason: str | None = None,
    ) -> None:
        """把一次置信度变更记进流水。

        产品的立身之本是「经验可证伪」，而变更此前是就地覆盖：用户只看得到当前
        置信度，看不到它是怎么走到今天的。这里把每一次加减分、每一次状态流转留档，
        「为什么这条经验被淘汰了」才答得出来。
        """
        await self.db.insight_events.add(
            InsightEventCreate(
                insight_id=after.id,
                space_id=after.space_id,
                event=event,
                confidence_before=before.confidence,
                confidence_after=after.confidence,
                status_before=before.status,
                status_after=after.status,
                share=share,
                reason=reason,
            )
        )

    # -- 冲突 -------------------------------------------------------------

    async def list_conflicts(self) -> list[InsightConflictGroup]:
        """列出待裁决的冲突组。

        v1 把当前全部 ``conflicted`` 经验作为**一个**组返回——consolidate 目前
        不持久化「哪几条属于同一组」，精细分组需要一张冲突关系表（Phase 4）。
        没有冲突时返回空列表。
        """
        conflicted = await self.db.insights.list_by_status(self.space_id, "conflicted")
        if not conflicted:
            return []
        return [InsightConflictGroup(group_id="conflicts", insights=conflicted)]

    async def resolve_conflict(
        self, group_id: str, payload: ConflictResolveRequest
    ) -> list[Insight]:
        """裁决冲突：保留一条转 active，归档其余；可选写入合并文本。"""
        keeper = await self.db.insights.require(payload.keep_id)
        patch = InsightUpdate(status="active")
        if payload.merged_text:
            patch = InsightUpdate(status="active", guidance=payload.merged_text)
        kept = await self.db.insights.update(payload.keep_id, patch)
        await self._record_event(
            before=keeper, after=kept, event="conflict_resolved_keep", reason=group_id
        )
        await self._reconcile_vector(keeper, kept)

        for archive_id in payload.archive_ids:
            existing = await self.db.insights.get(archive_id)
            if existing is None:
                continue
            archived = await self.db.insights.update(archive_id, InsightUpdate(status="archived"))
            await self._record_event(
                before=existing, after=archived, event="conflict_resolved_archive", reason=group_id
            )
            await self._remove_vector(archive_id)
        return [kept]

    # -- 溯源 -------------------------------------------------------------

    async def review_candidates(self) -> InsightReviewResponse:
        """挑出值得人看一眼的经验：被应用够多、却很少收到好评。

        这是**统计线索，不是判决**。一次好评/差评会被均摊到当时注入的所有经验上，
        归因本身是粗的；所以这里只列出「应用 ≥ N 次且成功率 ≤ X」的条目，交由人来
        判断是留、是改、还是归档。全部按成功率升序：最可疑的排最前面。
        """
        rows = await self.db.insights.list_for_review(
            self.space_id,
            min_applied=REVIEW_MIN_APPLIED,
            max_success_rate=REVIEW_MAX_SUCCESS_RATE,
            limit=REVIEW_LIMIT,
        )
        items: list[InsightReviewItem] = []
        for insight in rows:
            rate = insight.success_count / insight.applied_count if insight.applied_count else 0.0
            items.append(
                InsightReviewItem(
                    insight=insight,
                    applied_count=insight.applied_count,
                    success_count=insight.success_count,
                    success_rate=round(rate, 4),
                    reason=(
                        f"被注入 {insight.applied_count} 次，"
                        f"只有 {insight.success_count} 次收到好评"
                    ),
                )
            )
        return InsightReviewResponse(
            items=items,
            min_applied=REVIEW_MIN_APPLIED,
            max_success_rate=REVIEW_MAX_SUCCESS_RATE,
        )

    async def confidence_history(self, insight_id: str) -> InsightHistoryResponse:
        """一条经验的置信度变更流水（旧的在前，读下来就是它的经历）。"""
        insight = await self.db.insights.require(insight_id)
        return InsightHistoryResponse(
            insight_id=insight.id,
            insight=insight,
            events=await self.db.insight_events.list_by_insight(insight.id),
        )

    async def lineage(self, insight_id: str) -> InsightLineage:
        """经验溯源：由哪些轨迹/反馈产生，取代了谁、被谁取代。"""
        insight = await self.db.insights.require(insight_id)
        traces: list[Trace] = []
        feedback = []
        for trace_id in insight.source_trace_ids:
            trace = await self.db.traces.get(trace_id)
            if trace is None:
                continue
            traces.append(trace)
            feedback.extend(await self.db.feedback.list_by_trace(trace_id))

        # 被谁取代：其它经验的 supersedes 指向本条
        all_active = await self.db.insights.list_active(
            self.space_id, min_confidence=0.0, limit=1000
        )
        superseded_by = [i.id for i in all_active if i.supersedes == insight_id]

        return InsightLineage(
            insight_id=insight_id,
            source_traces=traces,
            source_feedback=feedback,
            supersedes=insight.supersedes,
            superseded_by=superseded_by,
        )

    # -- 向量同步 ---------------------------------------------------------

    async def _sync_vector(self, insight: Insight) -> None:
        """把一条经验的 trigger 写入 insights_vec（不可召回状态则跳过）。"""
        if insight.status not in RECALLABLE_STATUSES:
            return
        if self.db.vectors is None:
            return
        try:
            embedder = self.registry.embedding(purpose="ingest")
        except ProviderNotConfiguredError as exc:
            logger.warning(
                "insight_embedding_unavailable", space_id=self.space_id, error=exc.message
            )
            return
        vectors = await self.db.require_vectors()
        embeddings = await embedder.embed([insight.trigger], kind="doc")
        if len(embeddings) != 1:
            raise ValidationError("经验向量化返回数量异常", detail={"insight_id": insight.id})
        await vectors.upsert(
            "insights_vec",
            [
                VectorRecord(
                    id=insight.id,
                    space_id=insight.space_id,
                    vector=embeddings[0],
                    embedding_model=embedder.name,
                )
            ],
        )

    async def reconcile_all_vectors(self) -> int:
        """让 insights_vec 与经验的当前状态对齐：可召回的全部写入，其余全部移除。

        蒸馏、整合、评测判决都直接写经验表、不经过本服务，此前它们产出或晋升的经验
        从来不进向量池——而只要池里有过任何一条（人工新建过一次），召回就只认向量，
        于是自动进化出来的经验永远不会被用上。这里在那些流程收尾时统一对一次账。
        一个空间的经验通常只有几十条，一次批量向量化即可。

        Returns:
            写入向量池的条数。
        """
        if self.db.vectors is None:
            return 0
        insights: list[Insight] = []
        cursor: str | None = None
        while True:
            page, _total, cursor = await self.db.insights.list_by_space(
                self.space_id, sort="recent", limit=200, cursor=cursor
            )
            insights.extend(page)
            if not cursor:
                break
        recallable = [item for item in insights if item.status in RECALLABLE_STATUSES]
        stale = [item.id for item in insights if item.status not in RECALLABLE_STATUSES]
        vectors = await self.db.require_vectors()
        if stale:
            await vectors.delete("insights_vec", stale)
        if not recallable:
            return 0
        try:
            embedder = self.registry.embedding(purpose="ingest")
        except ProviderNotConfiguredError as exc:
            logger.warning(
                "insight_embedding_unavailable", space_id=self.space_id, error=exc.message
            )
            return 0
        embeddings = await embedder.embed([item.trigger for item in recallable], kind="doc")
        if len(embeddings) != len(recallable):
            raise ValidationError(
                "经验向量化返回数量异常",
                detail={"expected": len(recallable), "got": len(embeddings)},
            )
        await vectors.upsert(
            "insights_vec",
            [
                VectorRecord(
                    id=item.id,
                    space_id=item.space_id,
                    vector=vector,
                    embedding_model=embedder.name,
                )
                for item, vector in zip(recallable, embeddings, strict=True)
            ],
        )
        return len(recallable)

    async def _remove_vector(self, insight_id: str) -> None:
        if self.db.vectors is None:
            return
        vectors = await self.db.require_vectors()
        await vectors.delete("insights_vec", [insight_id])

    async def _reconcile_vector(self, before: Insight, after: Insight) -> None:
        """根据状态变化决定向量的加入/移除/更新。"""
        now_recallable = after.status in RECALLABLE_STATUSES
        if not now_recallable:
            await self._remove_vector(after.id)
            return
        # 仍可召回：trigger 变了或此前不在池中，都重新写入（upsert 幂等）
        await self._sync_vector(after)
