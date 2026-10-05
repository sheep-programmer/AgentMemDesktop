"""§8 专家度与评测。

实现落在 ``core/expert``；本模块只做「解包请求 → 调 core → 包装响应/SSE」。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import APIRouter, Query
from fastapi.responses import Response

from agentmem.errors import AgentMemError, ValidationError
from agentmem.types import (
    ConsistencyProbe,
    DeleteResponse,
    DomainOutline,
    DomainOutlineCreate,
    EvalCompareRequest,
    EvalGenerateRequest,
    EvalItem,
    EvalItemCreate,
    EvalItemRequest,
    EvalItemUpdate,
    EvalRunListResponse,
    EvalRunRequest,
    ExpertiseGap,
    ExpertiseGapsResponse,
    ExpertiseHistoryResponse,
    ExpertiseScore,
    InsightAttribution,
    InsightAttributionRequest,
    OutlineResponse,
    Page,
)

from ..deps import RuntimeDep
from ..sse import SseEvent, event, sse_response

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/spaces/{space_id}", tags=["expert"])


@router.get("/expertise", response_model=ExpertiseScore, summary="当前专家度")
async def expertise(space_id: str, runtime: RuntimeDep) -> ExpertiseScore:
    """五维分数（覆盖度 / 准确率 / 一致性 / 引用率 / 经验密度）与加权总分。"""
    service = await runtime.expertise_service(space_id)
    return await service.compute(space_id)


@router.get(
    "/expertise/history",
    response_model=ExpertiseHistoryResponse,
    summary="专家度成长曲线",
)
async def expertise_history(
    space_id: str,
    runtime: RuntimeDep,
    since: Annotated[int | None, Query(description="起始时间（Unix 毫秒）")] = None,
    bucket: Annotated[str, Query(pattern="^(day|week)$")] = "day",
) -> ExpertiseHistoryResponse:
    """快照序列，用于画成长曲线。

    ``bucket`` 目前不做服务端聚合——快照本身就是按进化次生成的稀疏点，
    前端可自行按天/周归并。参数保留以兼容契约。
    """
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    snapshots = await database.expertise.list_by_space(space_id, since=since)
    return ExpertiseHistoryResponse(snapshots=snapshots)


@router.post(
    "/expertise/consistency",
    response_model=ConsistencyProbe,
    summary="测一次回答一致性",
)
async def measure_consistency(
    space_id: str,
    runtime: RuntimeDep,
    questions: Annotated[int, Query(ge=1, le=10, description="取几个最近问过的问题")] = 3,
    repeats: Annotated[int, Query(ge=2, le=5, description="每个问题重复回答几遍")] = 3,
) -> ConsistencyProbe:
    """同问多答：取最近问过的问题各回答若干遍，比较答案的语义相似度。

    一次探测是 ``questions × repeats`` 次生成调用，所以**不随页面加载自动跑**，
    由用户显式触发。结果落库，专家度里的「一致性」优先读最近一次实测值
    （超过 30 天视为过期，退回代理指标并在响应里标明来源）。
    """
    await runtime.spaces.require_space(space_id)
    with runtime.spaces.operation_gate(space_id).activity("一致性测算"):
        service = await runtime.consistency_service(space_id)
        database = await runtime.space_db(space_id)
        active = await database.insights.list_active(space_id, min_confidence=0.5, limit=6)
        return await service.measure(questions=questions, repeats=repeats, insights=active)


OUTLINE_HINT_DOCS = 20
OUTLINE_HINT_SUMMARY_CHARS = 80


async def _outline_document_hints(space_id: str, runtime: RuntimeDep) -> list[str]:
    """知识库里已有文档的标题与概要：只给模型辨认领域用（领域名常是简称）。"""
    database = await runtime.space_db(space_id)
    documents, _total, _cursor = await database.documents.list_by_space(
        space_id, status="ready", limit=OUTLINE_HINT_DOCS
    )
    hints: list[str] = []
    for document in documents:
        summary = (document.meta.context_summary or "").strip()
        if summary:
            hints.append(f"{document.title}：{summary[:OUTLINE_HINT_SUMMARY_CHARS]}")
        else:
            hints.append(document.title)
    return hints


OUTLINE_AUTO_RETRY_SECONDS = 600
# space_id → (失败时刻, 原因)。只管「打开盲区页时自动生成」这条路：失败后一段时间内
# 不再自动重跑，把原因交给页面，重试交给用户手里的「重新生成」按钮。
_outline_failures: dict[str, tuple[float, str]] = {}


def _recent_outline_failure(space_id: str) -> str | None:
    """冷却期内的上一次自动生成失败原因；过了冷却期返回 ``None``（允许再自动试一次）。"""
    failure = _outline_failures.get(space_id)
    if failure is None:
        return None
    failed_at, reason = failure
    if time.monotonic() - failed_at > OUTLINE_AUTO_RETRY_SECONDS:
        _outline_failures.pop(space_id, None)
        return None
    return reason


async def _outline_or_none(space_id: str, runtime: RuntimeDep) -> DomainOutline | None:
    """取落库的领域大纲；一份都没有时生成一次并落库。

    生成用的是模型的先验知识（要一把外部尺子量「缺了什么」），一次调用几秒，
    所以只在没有大纲时跑一次。失败时记下原因、冷却一段时间：此前失败不落任何记号，
    每次打开盲区页都要再等一轮（带重试就是两轮）模型调用，然后照样是「大纲还没生成」。
    """
    from agentmem.expert.evalgen import generate_outline

    database = await runtime.space_db(space_id)
    stored = await database.outlines.latest(space_id)
    if stored is not None:
        return stored
    if _recent_outline_failure(space_id) is not None:
        return None
    config = await runtime.spaces.read_config(space_id)
    registry = await runtime.registry_for_space(space_id)
    try:
        result = await generate_outline(
            registry=registry,
            persona=config.persona,
            document_hints=await _outline_document_hints(space_id, runtime),
        )
    except AgentMemError as exc:
        logger.warning("outline_generation_failed", space_id=space_id, code=exc.code)
        _outline_failures[space_id] = (time.monotonic(), exc.message)
        return None
    if not result.nodes:
        _outline_failures[space_id] = (
            time.monotonic(),
            result.failure_reason or "模型没有返回大纲",
        )
        return None
    _outline_failures.pop(space_id, None)
    return await database.outlines.create(
        DomainOutlineCreate(
            space_id=space_id,
            domain=config.persona.domain,
            nodes=result.nodes,
            interpretation=result.interpretation,
        )
    )


@router.get("/expertise/gaps", response_model=ExpertiseGapsResponse, summary="知识盲区")
async def expertise_gaps(space_id: str, runtime: RuntimeDep) -> ExpertiseGapsResponse:
    """领域大纲中尚无资料覆盖的节点，引导用户补充投喂。

    大纲是落库的：第一次访问时生成一份，之后直接读，不再每次打开页面都调模型。
    想更新大纲走 ``POST /expertise/outline``。
    """
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    outline = await _outline_or_none(space_id, runtime)
    if outline is None:
        return ExpertiseGapsResponse(outline_error=_recent_outline_failure(space_id))

    service = await runtime.expertise_service(space_id)
    covered, total = await service.covered_node_count(space_id)
    _ = covered
    cards, _card_total, _cursor = await database.cards.list_by_space(space_id, limit=1000)
    covered_text = " ".join(f"{c.title} {' '.join(c.aliases)}" for c in cards)

    gaps: list[ExpertiseGap] = []
    for node in outline.nodes:
        # 主题词或任一子主题在卡片标题/别名中出现即视为已覆盖
        keywords = [node.topic, *node.subtopics]
        if any(kw and kw in covered_text for kw in keywords):
            continue
        gaps.append(
            ExpertiseGap(
                topic=node.topic,
                reason="领域大纲中的该主题尚无对应知识卡片",
                suggested_queries=[f"{node.topic} 是什么", f"{node.topic} 的常见做法"],
            )
        )
    return ExpertiseGapsResponse(
        gaps=gaps,
        outline_size=total,
        outline_generated_at=outline.created_at,
        outline_interpretation=outline.interpretation,
    )


@router.post("/expertise/outline", response_model=OutlineResponse, summary="重新生成领域大纲")
async def refresh_outline(space_id: str, runtime: RuntimeDep) -> OutlineResponse:
    """重新生成领域大纲并落库，返回新大纲与当前覆盖情况。

    大纲决定覆盖率的分子分母与知识盲区的基准，所以要能显式更新——但它是一次
    几秒的模型调用，不该藏在页面加载里。
    """
    from agentmem.expert.evalgen import generate_outline

    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    config = await runtime.spaces.read_config(space_id)
    registry = await runtime.registry_for_space(space_id)

    result = await generate_outline(
        registry=registry,
        persona=config.persona,
        document_hints=await _outline_document_hints(space_id, runtime),
    )
    if not result.nodes:
        _outline_failures[space_id] = (
            time.monotonic(),
            result.failure_reason or "模型没有返回大纲",
        )
        # 原来的大纲不动；把模型原话带回去，用户才知道是该改领域名还是该重试
        raise ValidationError(
            f"模型没能为「{config.persona.domain}」生成大纲（{result.failure_reason}）。"
            "原有大纲保持不变；可以在左上角的空间切换里编辑空间，把领域写具体些，或先导入几份资料后重试。"
        )

    _outline_failures.pop(space_id, None)
    outline = await database.outlines.create(
        DomainOutlineCreate(
            space_id=space_id,
            domain=config.persona.domain,
            nodes=result.nodes,
            interpretation=result.interpretation,
        )
    )
    service = await runtime.expertise_service(space_id)
    covered, total = await service.covered_node_count(space_id)
    return OutlineResponse(
        outline=outline, covered=covered, total=total, interpretation=result.interpretation
    )


@router.get("/evals", response_model=Page[EvalItem], summary="测验集列表")
async def list_evals(
    space_id: str,
    runtime: RuntimeDep,
    tag: Annotated[str | None, Query(description="标签")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> Page[EvalItem]:
    """列出测验题。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    items, total, next_cursor = await database.eval_items.list_by_space(
        space_id, tag=tag, limit=limit, cursor=cursor
    )
    return Page[EvalItem](items=items, total=total, next_cursor=next_cursor)


@router.post("/evals", response_model=EvalItem, status_code=201, summary="新增测验题")
async def create_eval(space_id: str, payload: EvalItemRequest, runtime: RuntimeDep) -> EvalItem:
    """手动新增题目。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    return await database.eval_items.create(
        EvalItemCreate(space_id=space_id, **payload.model_dump())
    )


@router.post(
    "/evals/generate",
    summary="自动出题（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def generate_evals(
    space_id: str, payload: EvalGenerateRequest, runtime: RuntimeDep
) -> Response:
    """从文档自动生成测验题，SSE 推送进度。"""
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        from agentmem.expert.evalgen import generate_from_documents

        database = await runtime.space_db(space_id)
        registry = await runtime.registry_for_space(space_id)
        config = await runtime.spaces.read_config(space_id)
        yield event("stage", {"stage": "generate", "status": "running"})
        items = await generate_from_documents(
            database=database,
            registry=registry,
            persona=config.persona,
            space_id=space_id,
            document_ids=payload.document_ids,
            count=payload.count,
        )
        for item in items:
            # 整条下发：前端把它直接插进题目列表，只给 id 与题干的话参考答案、标签都是空的
            yield event("item", item.model_dump(mode="json"))
        yield event("stage", {"stage": "generate", "status": "done", "produced": len(items)})
        # 前端靠 done 收尾；此前只发 stage，按钮在出题成功后也一直转圈
        yield event("done", {"count": len(items)})

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("生成测验题")
    )


@router.patch("/evals/{eval_id}", response_model=EvalItem, summary="修改测验题")
async def update_eval(
    space_id: str, eval_id: str, payload: EvalItemUpdate, runtime: RuntimeDep
) -> EvalItem:
    """修改题目内容。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    return await database.eval_items.update(eval_id, payload)


@router.delete("/evals/{eval_id}", response_model=DeleteResponse, summary="删除测验题")
async def delete_eval(space_id: str, eval_id: str, runtime: RuntimeDep) -> DeleteResponse:
    """删除题目。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    deleted = await database.eval_items.delete(eval_id)
    return DeleteResponse(deleted=deleted, id=eval_id)


@router.post(
    "/evals/run",
    summary="执行评测（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def run_evals(space_id: str, payload: EvalRunRequest, runtime: RuntimeDep) -> Response:
    """按 variant 执行评测，SSE 推送结果。"""
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        service = await runtime.evaluation_service(space_id)
        yield event("stage", {"stage": "evaluate", "status": "running", "variant": payload.variant})
        result = await service.run(
            space_id, variant=payload.variant, insight_set=payload.insight_set
        )
        for score in result.item_scores:
            yield event(
                "item",
                {
                    "item_id": score.item_id,
                    "score": score.score,
                    "passed": score.passed,
                    # 检索侧指标与分数分开下发：分数掉了但召回没掉，
                    # 说明是模型没用上证据，而不是证据没捞到
                    "metrics": score.metrics.model_dump() if score.metrics else None,
                },
            )
        yield event(
            "done",
            {
                "variant": payload.variant,
                "score": result.score,
                "run_id": result.run.id,
                "metrics": result.metrics.model_dump() if result.metrics else None,
            },
        )

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("评测")
    )


@router.post(
    "/evals/compare",
    summary="多臂对比检索配置（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def compare_evals(
    space_id: str, payload: EvalCompareRequest, runtime: RuntimeDep
) -> Response:
    """同一批题目、同一批经验，只换检索配置，逐臂推送结果与差值。

    分两次跑全量评测再比总分，会把题目难度与采样噪声混进差值；配对比较才谈得上
    可证伪。第一臂是基准，``deltas`` 里给出其余各臂相对它的总分差、检索指标差与
    逐题差值。把同一个配置跑两臂就是 A/A 对照，那个差值就是本轮噪声底。
    """
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        service = await runtime.evaluation_service(space_id)
        for arm in payload.arms:
            yield event("stage", {"stage": "compare", "status": "running", "label": arm.label})
        comparison = await service.compare(space_id, payload.arms, persist=payload.persist)
        for outcome in comparison.arms:
            yield event(
                "arm",
                {
                    "label": outcome.label,
                    "run_id": outcome.run_id,
                    "score": outcome.score,
                    "metrics": outcome.metrics.model_dump() if outcome.metrics else None,
                },
            )
        yield event(
            "done",
            {
                "space_id": comparison.space_id,
                "items": comparison.items,
                "baseline": comparison.baseline,
                "deltas": [item.model_dump() for item in comparison.deltas],
            },
        )

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("评测")
    )


@router.post(
    "/insights/attribute",
    response_model=InsightAttribution,
    summary="留一法：单条经验到底贡献了多少",
)
async def attribute_insights(
    space_id: str, payload: InsightAttributionRequest, runtime: RuntimeDep
) -> InsightAttribution:
    """把每条经验单独拿掉重跑同一批题，分数掉多少就是它的贡献。

    ⚠️ **很贵**：要跑 N+1 轮评测（开了噪声底是 N+2），每轮都把整个测验集重跑一遍。
    所以要归因哪几条必须显式挑明，``max_insights`` 默认只取 5 条。

    这是反馈回流做不到的那一半：一次 👎 只能按注入条数均摊到每条经验头上，那是
    相关性；留一法是对照实验，才谈得上因果。``noise_floor`` 是同一配置跑两遍的分差，
    贡献小于它的不该当结论。
    """
    await runtime.spaces.require_space(space_id)
    with runtime.spaces.operation_gate(space_id).activity("评测"):
        service = await runtime.evaluation_service(space_id)
        return await service.attribute_insights(
            space_id,
            insight_ids=payload.insight_ids or None,
            max_insights=payload.max_insights,
            include_noise_floor=payload.include_noise_floor,
            persist=payload.persist,
        )


@router.get("/evals/runs", response_model=EvalRunListResponse, summary="历次评测结果")
async def list_eval_runs(space_id: str, runtime: RuntimeDep) -> EvalRunListResponse:
    """列出评测记录。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    runs, _cursor = await database.eval_runs.list_by_space(space_id, limit=50)
    return EvalRunListResponse(runs=runs)
