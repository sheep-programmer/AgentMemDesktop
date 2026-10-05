"""§7 进化闭环。

实现落在 ``core/evolve``（蒸馏 / 整合 / 评价）与 ``core/expert``（评测）；
本模块只做「解包请求 → 调 core → 包装响应/SSE」。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import Response

from agentmem.evolve.cycle import CycleResult, CycleStage
from agentmem.retrieve.chat import aclose_stream
from agentmem.retrieve.locate import Located, focus_hits, focused_snippet
from agentmem.retrieve.models import SNIPPET_CHARS, ScoredChunk
from agentmem.types import (
    Chunk,
    EvolveHistoryItem,
    EvolveHistoryResponse,
    EvolvePendingResponse,
    Feedback,
    FeedbackRequest,
    JudgeResponse,
    TraceRetrievedItem,
    TraceView,
)

from ..deps import RuntimeDep
from ..sse import SseEvent, event, sse_response

router = APIRouter(tags=["evolve"])


@router.get("/traces/{trace_id}", response_model=TraceView, summary="完整轨迹")
async def get_trace(trace_id: str, runtime: RuntimeDep) -> TraceView:
    """检索明细、注入的经验、token 分布，供「为什么这么答」面板。

    轨迹里只存了切片的 id 与各路分数，这里按 id 回表补上文档标题、片段与切片种类，
    否则证据面板上每条都是「未命名文档」、也没有内容可看。
    """
    space_id = await runtime.find_trace_space(trace_id)
    database = await runtime.space_db(space_id)
    trace = await database.traces.require(trace_id)
    insights = await database.insights.get_many(trace.used_insights) if trace.used_insights else []
    view = TraceView(**trace.model_dump(), insight_details=insights)
    if not trace.retrieved:
        return view

    chunks = await database.chunks.get_many([item.chunk_id for item in trace.retrieved])
    by_id = {chunk.id: chunk for chunk in chunks}
    titles = await database.documents.titles_by_ids(
        [chunk.document_id for chunk in chunks if chunk.document_id]
    )
    # 和问答时的检索事件一致：章节与片段按「与问题最贴近的那句」重新定位
    focus = await _focus_trace_chunks(runtime, space_id, trace.query, chunks)
    return view.model_copy(
        update={
            "retrieved": [
                _enrich(item, by_id[item.chunk_id], titles, focus.get(item.chunk_id))
                if item.chunk_id in by_id
                else item
                for item in trace.retrieved
            ]
        }
    )


async def _focus_trace_chunks(
    runtime: RuntimeDep, space_id: str, query: str, chunks: list[Chunk]
) -> dict[str, Located]:
    scored = [
        ScoredChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            content=chunk.content,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            kind=chunk.kind,
        )
        for chunk in chunks
    ]
    cache_dir = runtime.settings.data_dir / "cache" / space_id
    try:
        return await asyncio.to_thread(focus_hits, query, scored, cache_dir)
    except Exception:  # 定位只是锦上添花，失败就按切片级展示
        return {}


def _enrich(
    item: TraceRetrievedItem,
    chunk: Chunk,
    titles: dict[str, str],
    located: Located | None,
) -> TraceRetrievedItem:
    scored = ScoredChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        content=chunk.content,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
    )
    return item.model_copy(
        update={
            "document_id": chunk.document_id,
            "document_title": titles.get(chunk.document_id),
            "snippet": focused_snippet(scored, located, SNIPPET_CHARS),
            "kind": chunk.kind,
            "page": located.page if located and located.page is not None else chunk.page,
            "heading_path": (located and located.heading_path) or chunk.heading_path,
            "ordinal": chunk.ordinal,
            "quote_start": located.start if located else None,
            "quote_end": located.end if located else None,
        }
    )


@router.post(
    "/traces/{trace_id}/feedback", response_model=Feedback, status_code=201, summary="提交反馈"
)
async def submit_feedback(trace_id: str, payload: FeedbackRequest, runtime: RuntimeDep) -> Feedback:
    """显式反馈：``up`` / ``down`` / ``correction``。"""
    space_id = await runtime.find_trace_space(trace_id)
    service = await runtime.critique_service(space_id)
    return await service.submit_feedback(trace_id, payload.kind, payload.comment)


@router.post("/traces/{trace_id}/judge", response_model=JudgeResponse, summary="LLM-as-Judge 打分")
async def judge_trace(trace_id: str, runtime: RuntimeDep) -> JudgeResponse:
    """按 Persona 的 quality_bar 自动打分。"""
    space_id = await runtime.find_trace_space(trace_id)
    service = await runtime.critique_service(space_id)
    return await service.judge_trace(trace_id)


@router.get(
    "/spaces/{space_id}/evolve/pending",
    response_model=EvolvePendingResponse,
    summary="待蒸馏反馈",
)
async def evolve_pending(
    space_id: str,
    runtime: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> EvolvePendingResponse:
    """待蒸馏的反馈数量与预览。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    count = await database.feedback.count_pending(space_id)
    by_kind = await database.feedback.count_pending_by_kind(space_id)
    preview = await database.feedback.list_pending(space_id, limit=limit)
    return EvolvePendingResponse(pending_count=count, by_kind=by_kind, preview=preview)


@router.post(
    "/spaces/{space_id}/evolve/distill",
    summary="触发蒸馏（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def evolve_distill(space_id: str, runtime: RuntimeDep) -> Response:
    """从反馈蒸馏候选经验，SSE 推送 stage / candidate 事件。"""
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        from agentmem.evolve import distill_feedback

        database = await runtime.space_db(space_id)
        registry = await runtime.registry_for_space(space_id)
        config = await runtime.spaces.read_config(space_id)
        yield event("stage", {"stage": "distill", "status": "running"})
        outcome = await distill_feedback(
            database=database,
            registry=registry,
            persona=config.persona,
            space_id=space_id,
        )
        for candidate in outcome.candidates:
            yield event(
                "candidate",
                {
                    "id": candidate.id,
                    "trigger": candidate.trigger,
                    "guidance": candidate.guidance,
                    "origin": candidate.origin,
                },
            )
        yield event(
            "stage",
            {
                "stage": "distill",
                "status": "done",
                "produced": len(outcome.candidates),
                "skipped": len(outcome.skipped),
            },
        )

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("进化")
    )


@router.post(
    "/spaces/{space_id}/evolve/consolidate",
    summary="去重与冲突检测（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def evolve_consolidate(space_id: str, runtime: RuntimeDep) -> Response:
    """合并重复经验、标记冲突，SSE 推送 stage 事件。

    对当前全部 candidate 状态的经验做整合（未指定具体 id 时的默认行为）。
    """
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        from agentmem.evolve import consolidate_insights

        database = await runtime.space_db(space_id)
        registry = await runtime.registry_for_space(space_id)
        config = await runtime.spaces.read_config(space_id)
        candidates = await database.insights.list_by_status(space_id, "candidate")
        yield event("stage", {"stage": "consolidate", "status": "running"})
        outcome = await consolidate_insights(
            database=database,
            registry=registry,
            persona=config.persona,
            space_id=space_id,
            candidate_ids=[c.id for c in candidates],
        )
        yield event(
            "stage",
            {
                "stage": "consolidate",
                "status": "done",
                "merged": outcome.merged,
                "duplicates": outcome.duplicates_archived,
                "conflicts": outcome.conflict_groups,
            },
        )

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("进化")
    )


@router.post(
    "/spaces/{space_id}/evolve/cycle",
    summary="一键完整进化（SSE）",
    responses={"200": {"content": {"text/event-stream": {}}}},
    response_class=Response,
)
async def evolve_cycle(space_id: str, runtime: RuntimeDep) -> Response:
    """distill → consolidate → evaluate → promote，全程 SSE 推送。"""
    await runtime.spaces.require_space(space_id)

    async def stream() -> AsyncIterator[SseEvent]:
        service = await runtime.evolution_service(space_id)
        cycle = service.run_cycle(space_id)
        try:
            async for item in cycle:
                if isinstance(item, CycleStage):
                    # 把 detail 拍平进顶层，贴合 `03-API-SPEC.md` §7 的事件形状
                    yield event(
                        "stage", {"stage": item.stage, "status": item.status, **item.detail}
                    )
                elif isinstance(item, CycleResult):
                    yield event(
                        "done",
                        {
                            "delta": round(item.expertise_after - item.expertise_before, 2),
                            "expertise_before": item.expertise_before,
                            "expertise_after": item.expertise_after,
                            "produced": item.produced,
                            "promoted": item.promoted,
                            "demoted": item.demoted,
                            "eval_delta": item.eval_delta,
                            "pending_candidates": item.pending_candidates,
                        },
                    )
        finally:
            await aclose_stream(cycle)

    return sse_response(
        stream(), operation=runtime.spaces.operation_gate(space_id).activity("进化")
    )


@router.get(
    "/spaces/{space_id}/evolve/history",
    response_model=EvolveHistoryResponse,
    summary="进化历史",
)
async def evolve_history(space_id: str, runtime: RuntimeDep) -> EvolveHistoryResponse:
    """历次进化：每次新增/合并/晋升/淘汰了多少条，以及当次评测的分数变化。

    读的是进化日志（``evolution_runs``），不是评测记录。此前用评测近似，会把
    with_insights 那一次的**分数**当成「本次收益」返回——71.5 这种分数被当成
    delta 显示，而一次进化真正的产出算完就丢了。
    """
    await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    runs = await database.evolution.list_by_space(space_id, limit=50)
    return EvolveHistoryResponse(
        items=[
            EvolveHistoryItem(
                run_at=run.created_at,
                produced=run.produced,
                merged=run.merged,
                conflicts=run.conflicts,
                promoted=run.promoted,
                demoted=run.demoted,
                eval_delta=run.eval_delta,
                expertise_before=run.expertise_before,
                expertise_after=run.expertise_after,
                duration_ms=run.duration_ms,
            )
            for run in runs
        ]
    )
