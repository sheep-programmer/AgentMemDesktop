"""④ Distill —— 从反馈中蒸馏候选经验。

把「用户纠错 + 差评 + 有效好评」提炼成三段式经验（场景/做法/依据），
落库为 ``status='candidate'`` 的候选条目（初始置信度 0.3）。

复用 `agentmem.prompts.build_distill_messages`，本模块只负责：
1. 把待处理反馈 + 对应轨迹组装成 Prompt 需要的 FeedbackEpisode 形状；
2. 解析产出、落库为 InsightCandidate；
3. 把已处理的反馈标记 distilled=True，避免下次重复蒸馏。
"""

from __future__ import annotations

from typing import cast

import structlog
from pydantic import BaseModel

from agentmem.evolve._persona import to_persona_spec, to_provider_messages
from agentmem.evolve.insights import reconcile_insight_vectors
from agentmem.prompts import (
    FeedbackEpisode,
    InsightRef,
    build_distill_messages,
    extract_json,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import Feedback, Insight, InsightCreate, InsightKind, Persona

logger = structlog.get_logger(__name__)

#: 蒸馏用低温度：要审慎归纳，不要发散创作
DISTILL_TEMPERATURE = 0.3

#: 一次最多处理多少条反馈，避免上下文超长
MAX_EPISODES_PER_RUN = 20

#: 传给蒸馏 Prompt 作参考的既有经验数（用于去重提示）
EXISTING_HINT_LIMIT = 40

_VALID_KINDS: frozenset[str] = frozenset(
    {"correction", "preference", "heuristic", "constraint", "terminology"}
)


class DistilledInsight(BaseModel):
    """一条蒸馏产出的候选经验（落库前的中间结构）。"""

    trigger: str
    guidance: str
    rationale: str | None = None
    kind: InsightKind
    source_trace_ids: list[str]
    confidence_hint: float = 0.3
    supersedes_hint: str | None = None


class DistillOutcome(BaseModel):
    """一次蒸馏的结果。"""

    candidates: list[Insight]
    processed_feedback_ids: list[str]
    skipped: list[dict[str, str]]


async def distill_feedback(
    *,
    database: Database,
    registry: ProviderRegistry,
    persona: Persona,
    space_id: str,
    limit: int = MAX_EPISODES_PER_RUN,
) -> DistillOutcome:
    """处理待蒸馏反馈，产出候选经验并落库。

    只处理 ``distilled=False`` 的反馈；处理后无论是否产出经验都标记为已处理，
    否则一条无法蒸馏的反馈会每次都被重新捞出来。
    """
    pending = await database.feedback.list_pending(space_id, limit=limit)
    if not pending:
        return DistillOutcome(candidates=[], processed_feedback_ids=[], skipped=[])

    episodes = await _build_episodes(database, pending)
    if not episodes:
        # 反馈全都无法组装成 episode（如对应 trace 已删）——直接标记已处理
        await _mark_processed(database, [fb.id for fb in pending])
        return DistillOutcome(
            candidates=[],
            processed_feedback_ids=[fb.id for fb in pending],
            skipped=[{"reason": "对应轨迹缺失，无法蒸馏"}],
        )

    existing = await _existing_hints(database, space_id)
    messages = build_distill_messages(
        persona=to_persona_spec(persona),
        episodes=episodes,
        existing_insights=existing,
    )
    route = registry.llm("distill", purpose="distill")
    result = await route.chat(to_provider_messages(messages), temperature=DISTILL_TEMPERATURE)

    distilled, skipped = _parse(result.content)
    candidates = await _persist(database, space_id, distilled)

    processed_ids = [fb.id for fb in pending]
    await _mark_processed(database, processed_ids)
    if candidates:
        # 候选也属于可召回状态，要进向量池；不经过 InsightService 就得自己对账
        await reconcile_insight_vectors(database, registry, space_id)

    logger.info(
        "distill_done",
        space_id=space_id,
        processed=len(processed_ids),
        produced=len(candidates),
        skipped=len(skipped),
    )
    return DistillOutcome(
        candidates=candidates, processed_feedback_ids=processed_ids, skipped=skipped
    )


async def _build_episodes(database: Database, feedback: list[Feedback]) -> list[FeedbackEpisode]:
    """把反馈 + 其轨迹 + 助手回答组装成蒸馏 Prompt 的 episode。"""
    episodes: list[FeedbackEpisode] = []
    for item in feedback:
        trace = await database.traces.get(item.trace_id)
        if trace is None:
            continue
        message = await database.messages.get(trace.message_id)
        answer = message.content if message else ""
        episode = FeedbackEpisode(
            trace_id=trace.id,
            question=trace.query,
            answer=answer,
            feedback_kind=item.kind,
            comment=item.comment,
            judge_score=item.judge_score,
            judge_reason=item.judge_reason,
            used_insights=trace.used_insights,
        )
        episodes.append(episode)
    return episodes


async def _existing_hints(database: Database, space_id: str) -> list[InsightRef]:
    """取一批既有经验作为去重提示（活跃 + 候选都算，避免与刚产出的重复）。"""
    active = await database.insights.list_active(
        space_id, min_confidence=0.0, limit=EXISTING_HINT_LIMIT
    )
    candidates = await database.insights.list_by_status(space_id, "candidate")
    combined = {item.id: item for item in [*active, *candidates]}
    return [
        InsightRef(
            insight_id=item.id,
            trigger=item.trigger,
            guidance=item.guidance,
            rationale=item.rationale,
            confidence=item.confidence,
        )
        for item in list(combined.values())[:EXISTING_HINT_LIMIT]
    ]


def _parse(raw: str) -> tuple[list[DistilledInsight], list[dict[str, str]]]:
    """解析蒸馏产出。脏输出不致命：解析失败即当作「本轮无产出」。"""
    try:
        data = extract_json(raw)
    except ValueError as exc:
        logger.warning("distill_parse_failed", error=str(exc), raw=raw[:200])
        return [], [{"reason": "蒸馏结果解析失败"}]

    distilled: list[DistilledInsight] = []
    for entry in data.get("insights", []) or []:
        kind = str(entry.get("kind", "")).strip()
        if kind not in _VALID_KINDS:
            kind = "heuristic"  # 模型给了非法类别时归入通用启发型，不丢弃
        trigger = str(entry.get("trigger", "")).strip()
        guidance = str(entry.get("guidance", "")).strip()
        if not trigger or not guidance:
            continue  # 缺场景或做法的条目无法使用
        try:
            hint = float(entry.get("confidence_hint", 0.3))
        except (TypeError, ValueError):
            hint = 0.3
        distilled.append(
            DistilledInsight(
                trigger=trigger,
                guidance=guidance,
                rationale=(str(entry.get("rationale")).strip() or None)
                if entry.get("rationale")
                else None,
                kind=cast(InsightKind, kind),
                source_trace_ids=[str(t) for t in entry.get("source_trace_ids", []) or []],
                confidence_hint=max(0.0, min(1.0, hint)),
                supersedes_hint=(str(entry.get("supersedes_hint")).strip() or None)
                if entry.get("supersedes_hint")
                else None,
            )
        )

    skipped = [
        {"trace_id": str(s.get("trace_id", "")), "reason": str(s.get("reason", ""))}
        for s in data.get("skipped", []) or []
    ]
    return distilled, skipped


async def _persist(
    database: Database, space_id: str, distilled: list[DistilledInsight]
) -> list[Insight]:
    """把候选经验落库。

    ``confidence_hint`` 只作参考，实际入库一律用规则规定的初始值 0.3——
    置信度必须由后续的用户确认 / A/B 评测挣得，不能让模型自封。
    """
    from agentmem.evolve.confidence import INITIAL_CONFIDENCE

    created: list[Insight] = []
    for item in distilled:
        # supersedes_hint 指向的经验必须真实存在，否则丢弃该指向（模型可能编造 id）
        supersedes = None
        if item.supersedes_hint:
            target = await database.insights.get(item.supersedes_hint)
            supersedes = target.id if target else None
        insight = await database.insights.create(
            InsightCreate(
                space_id=space_id,
                trigger=item.trigger,
                guidance=item.guidance,
                rationale=item.rationale,
                kind=item.kind,
                confidence=INITIAL_CONFIDENCE,
                status="candidate",
                origin="user_correction",
                source_trace_ids=item.source_trace_ids,
                supersedes=supersedes,
            )
        )
        created.append(insight)
    return created


async def _mark_processed(database: Database, feedback_ids: list[str]) -> None:
    from agentmem.types import FeedbackUpdate

    for feedback_id in feedback_ids:
        await database.feedback.update(feedback_id, FeedbackUpdate(distilled=True))
