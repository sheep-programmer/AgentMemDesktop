"""§6 记忆 L2 / L3。

L2（知识卡片 + 实体关系图）已接真实实现；L3 经验部分仍是骨架，
等 ``core/evolve`` 落地后替换。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import ExitStack
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from agentmem.retrieve.chat import aclose_stream
from agentmem.types import (
    CardExtractRequest,
    CardHistoryResponse,
    CardKind,
    ConflictResolveRequest,
    DeleteResponse,
    GraphResponse,
    Insight,
    InsightConflictGroup,
    InsightHistoryResponse,
    InsightKind,
    InsightLineage,
    InsightRequest,
    InsightReviewResponse,
    InsightStatus,
    InsightUpdate,
    KnowledgeCard,
    KnowledgeCardRequest,
    KnowledgeCardUpdate,
    Page,
)

from ..deps import DatabaseDep, RuntimeDep
from ..sse import SseEvent, event, sse_response

router = APIRouter(tags=["memory"])


# -- L2 知识卡片 ------------------------------------------------------------


@router.get("/spaces/{space_id}/cards", response_model=Page[KnowledgeCard], summary="卡片列表")
async def list_cards(
    space_id: str,
    database: DatabaseDep,
    kind: Annotated[CardKind | None, Query(description="卡片类型")] = None,
    q: Annotated[str | None, Query(description="关键词")] = None,
    min_confidence: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[KnowledgeCard]:
    """按类型 / 关键词 / 置信度筛选知识卡片。"""
    items, total, next_cursor = await database.cards.list_by_space(
        space_id,
        kind=kind,
        query=q,
        min_confidence=min_confidence,
        limit=limit,
        cursor=cursor,
    )
    return Page[KnowledgeCard](items=items, total=total, next_cursor=next_cursor)


@router.post(
    "/spaces/{space_id}/cards",
    response_model=KnowledgeCard,
    status_code=201,
    summary="手动新建卡片",
)
async def create_card(
    space_id: str, payload: KnowledgeCardRequest, runtime: RuntimeDep
) -> KnowledgeCard:
    """人工新建知识卡片。"""
    service = await runtime.card_service(space_id)
    return await service.create(payload)


@router.patch(
    "/spaces/{space_id}/cards/{card_id}", response_model=KnowledgeCard, summary="编辑卡片"
)
async def update_card(
    space_id: str, card_id: str, payload: KnowledgeCardUpdate, runtime: RuntimeDep
) -> KnowledgeCard:
    """人工校订：``verified_by='user'``，置信度拉满。"""
    service = await runtime.card_service(space_id)
    return await service.update(card_id, payload)


@router.get(
    "/spaces/{space_id}/cards/{card_id}/versions",
    response_model=CardHistoryResponse,
    summary="卡片版本历史",
)
async def card_versions(space_id: str, card_id: str, runtime: RuntimeDep) -> CardHistoryResponse:
    """当前版本 + 被取代的旧版本（新的在前）。

    卡片是「事实」，事实会变：新版指南覆盖旧值时，这里能看到「什么时候改的、
    原来写的是什么」。
    """
    service = await runtime.card_service(space_id)
    return await service.history(card_id)


@router.delete(
    "/spaces/{space_id}/cards/{card_id}", response_model=DeleteResponse, summary="删除卡片"
)
async def delete_card(space_id: str, card_id: str, runtime: RuntimeDep) -> DeleteResponse:
    """删除知识卡片，并清掉它的向量。"""
    service = await runtime.card_service(space_id)
    return await service.delete(card_id)


@router.post(
    "/spaces/{space_id}/cards/extract",
    summary="批量抽取卡片（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def extract_cards(
    space_id: str, payload: CardExtractRequest, runtime: RuntimeDep
) -> Response:
    """从指定文档批量抽取 L2 知识卡片与实体关系，SSE 推送进度。

    ``document_ids`` 为空表示抽取该 Space 全部已就绪的文档。
    事件：``progress``（逐批进度）→ ``error``（单篇失败，可多条）→ ``done``（汇总报告）。
    """
    extractor = await runtime.knowledge_extractor(space_id)
    documents = await extractor.resolve_documents(payload.document_ids)

    async def generate() -> AsyncIterator[SseEvent]:
        with ExitStack() as reservations:
            gate = runtime.spaces.operation_gate(space_id)
            for document in documents:
                reservations.enter_context(gate.reserve_document(document.id))
            stream = extractor.stream(documents)
            try:
                async for item in stream:
                    yield event(item.name, item.payload)
            finally:
                await aclose_stream(stream)

    return sse_response(generate())


@router.get("/spaces/{space_id}/graph", response_model=GraphResponse, summary="实体关系图")
async def graph(
    space_id: str,
    runtime: RuntimeDep,
    center: Annotated[str | None, Query(description="以某实体为中心")] = None,
    depth: Annotated[int, Query(ge=1, le=4)] = 2,
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> GraphResponse:
    """返回节点与边，供力导向图渲染。"""
    return await runtime.knowledge_graph(space_id, center=center, depth=depth, limit=limit)


# -- L3 经验 ----------------------------------------------------------------


@router.get("/spaces/{space_id}/insights", response_model=Page[Insight], summary="经验列表")
async def list_insights(
    space_id: str,
    runtime: RuntimeDep,
    status: Annotated[InsightStatus | None, Query(description="状态")] = None,
    kind: Annotated[InsightKind | None, Query(description="类型")] = None,
    q: Annotated[str | None, Query(description="关键词")] = None,
    sort: Annotated[str, Query(pattern="^(confidence|recent|applied)$")] = "confidence",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Page[Insight]:
    """列出经验条目。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    items, total, next_cursor = await database.insights.list_by_space(
        space_id, status=status, kind=kind, query=q, sort=sort, limit=limit, cursor=cursor
    )
    return Page[Insight](items=items, total=total, next_cursor=next_cursor)


@router.post(
    "/spaces/{space_id}/insights",
    response_model=Insight,
    status_code=201,
    summary="手动新增经验",
)
async def create_insight(space_id: str, payload: InsightRequest, runtime: RuntimeDep) -> Insight:
    """人工新增经验（``origin='manual'``，直接 active 并向量化）。"""
    service = await runtime.insight_service(space_id)
    return await service.create_manual(
        trigger=payload.trigger,
        guidance=payload.guidance,
        kind=payload.kind,
        rationale=payload.rationale,
        scope=payload.scope,
        confidence=payload.confidence,
    )


@router.patch(
    "/spaces/{space_id}/insights/{insight_id}", response_model=Insight, summary="编辑经验"
)
async def update_insight(
    space_id: str, insight_id: str, payload: InsightUpdate, runtime: RuntimeDep
) -> Insight:
    """编辑内容或直接调整状态（同步向量召回池）。"""
    service = await runtime.insight_service(space_id)
    return await service.update(insight_id, payload)


@router.delete(
    "/spaces/{space_id}/insights/{insight_id}",
    response_model=DeleteResponse,
    summary="删除经验",
)
async def delete_insight(space_id: str, insight_id: str, runtime: RuntimeDep) -> DeleteResponse:
    """删除经验条目及其向量。"""
    service = await runtime.insight_service(space_id)
    deleted = await service.delete(insight_id)
    return DeleteResponse(deleted=deleted, id=insight_id)


@router.post(
    "/spaces/{space_id}/insights/{insight_id}/promote",
    response_model=Insight,
    summary="人工确认经验",
)
async def promote_insight(space_id: str, insight_id: str, runtime: RuntimeDep) -> Insight:
    """人工确认 → ``active``，置信度 +0.3，进入召回池。"""
    service = await runtime.insight_service(space_id)
    return await service.promote(insight_id)


@router.post(
    "/spaces/{space_id}/insights/{insight_id}/archive",
    response_model=Insight,
    summary="归档经验",
)
async def archive_insight(space_id: str, insight_id: str, runtime: RuntimeDep) -> Insight:
    """归档（软删除，可恢复），移出召回池。"""
    service = await runtime.insight_service(space_id)
    return await service.archive(insight_id)


@router.get(
    "/spaces/{space_id}/insights/conflicts",
    response_model=list[InsightConflictGroup],
    summary="冲突组列表",
)
async def list_conflicts(space_id: str, runtime: RuntimeDep) -> list[InsightConflictGroup]:
    """列出语义冲突、待用户裁决的经验分组。"""
    service = await runtime.insight_service(space_id)
    return await service.list_conflicts()


@router.post(
    "/spaces/{space_id}/insights/conflicts/{group_id}/resolve",
    response_model=list[Insight],
    summary="裁决冲突",
)
async def resolve_conflict(
    space_id: str, group_id: str, payload: ConflictResolveRequest, runtime: RuntimeDep
) -> list[Insight]:
    """保留一条、归档其余，可选合并文本。"""
    service = await runtime.insight_service(space_id)
    return await service.resolve_conflict(group_id, payload)


@router.get(
    "/spaces/{space_id}/insights/review",
    response_model=InsightReviewResponse,
    summary="值得复查的经验",
)
async def insights_for_review(space_id: str, runtime: RuntimeDep) -> InsightReviewResponse:
    """被注入够多次、却很少收到好评的经验，按成功率升序。

    这是统计线索而不是判决：反馈是均摊到当时注入的所有经验上的，归因本身是粗的。
    它的价值在于把「应用 9 次只成功 2 次」这种条目摆到人面前——以前这些计数只是
    显示出来，没人会一条条去比。
    """
    service = await runtime.insight_service(space_id)
    return await service.review_candidates()


@router.get(
    "/spaces/{space_id}/insights/{insight_id}/history",
    response_model=InsightHistoryResponse,
    summary="经验置信度变更流水",
)
async def insight_history(
    space_id: str, insight_id: str, runtime: RuntimeDep
) -> InsightHistoryResponse:
    """一条经验的每一次加减分与状态流转，旧的在前。

    产品讲的是「经验可证伪」，那就得答得出「它为什么变成现在这样」：一次好评 +0.05、
    一次 A/B +0.2、一次均摊的差评 -0.025……这些以前是就地覆盖，看不到轨迹。
    """
    service = await runtime.insight_service(space_id)
    return await service.confidence_history(insight_id)


@router.get(
    "/spaces/{space_id}/insights/{insight_id}/lineage",
    response_model=InsightLineage,
    summary="经验溯源",
)
async def insight_lineage(space_id: str, insight_id: str, runtime: RuntimeDep) -> InsightLineage:
    """由哪些 trace / feedback 产生，取代了谁。"""
    service = await runtime.insight_service(space_id)
    return await service.lineage(insight_id)
