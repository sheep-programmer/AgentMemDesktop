"""⑤ Consolidate —— 去重 / 合并 / 冲突检测。

没有这一步，L3 会随使用量线性膨胀成一堆语义重复的噪音。本模块对刚蒸馏出的
候选经验做整合：

- **重复**：直接归档被判为冗余的候选（保留更具体的那条）。
- **合并**：把互补的多条并成一条，原条目归档，新条目继承为 candidate。
- **冲突**：**不自动裁决**，只把双方标记为 ``conflicted`` 等用户处理——
  经验是用户领域知识的沉淀，让模型替用户否定规则是危险的。

复用 `agentmem.prompts.build_consolidate_messages`。
"""

from __future__ import annotations

from typing import cast

import structlog
from pydantic import BaseModel

from agentmem.evolve._persona import to_persona_spec, to_provider_messages
from agentmem.evolve.insights import reconcile_insight_vectors
from agentmem.prompts import InsightDraft, build_consolidate_messages, extract_json
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import Insight, InsightCreate, InsightKind, InsightUpdate, Persona

logger = structlog.get_logger(__name__)

CONSOLIDATE_TEMPERATURE = 0.2

#: 参与冲突/重复检测的既有经验上限
EXISTING_LIMIT = 30

_VALID_KINDS: frozenset[str] = frozenset(
    {"correction", "preference", "heuristic", "constraint", "terminology"}
)


class ConsolidationOutcome(BaseModel):
    """一次整合的结果统计。"""

    merged: int = 0
    duplicates_archived: int = 0
    conflict_groups: int = 0
    conflicted_insights: list[str] = []


async def consolidate_insights(
    *,
    database: Database,
    registry: ProviderRegistry,
    persona: Persona,
    space_id: str,
    candidate_ids: list[str],
) -> ConsolidationOutcome:
    """整合刚蒸馏出的候选经验。

    Args:
        candidate_ids: 本轮蒸馏产出的候选经验 id。为空则直接返回空结果。
    """
    candidates = await database.insights.get_many(candidate_ids)
    if not candidates:
        return ConsolidationOutcome()

    existing = await _existing_pool(database, space_id, exclude=set(candidate_ids))
    messages = build_consolidate_messages(
        persona=to_persona_spec(persona),
        candidates=[_to_draft(item) for item in candidates],
        existing=[_to_draft(item) for item in existing],
    )
    route = registry.llm("distill", purpose="consolidate")
    result = await route.chat(to_provider_messages(messages), temperature=CONSOLIDATE_TEMPERATURE)

    plan = _parse(result.content)
    known = {item.id: item for item in [*candidates, *existing]}
    outcome = await _apply(database, space_id, plan, known)
    # 合并新建、归档、标冲突都改了可召回集合
    await reconcile_insight_vectors(database, registry, space_id)

    logger.info(
        "consolidate_done",
        space_id=space_id,
        merged=outcome.merged,
        duplicates=outcome.duplicates_archived,
        conflicts=outcome.conflict_groups,
    )
    return outcome


async def _existing_pool(database: Database, space_id: str, *, exclude: set[str]) -> list[Insight]:
    """取既有 active + candidate 经验，排除本轮刚产出的候选。"""
    active = await database.insights.list_active(space_id, min_confidence=0.0, limit=EXISTING_LIMIT)
    candidates = await database.insights.list_by_status(space_id, "candidate")
    pool = {item.id: item for item in [*active, *candidates] if item.id not in exclude}
    return list(pool.values())[:EXISTING_LIMIT]


def _to_draft(insight: Insight) -> InsightDraft:
    return InsightDraft(
        id=insight.id,
        trigger=insight.trigger,
        guidance=insight.guidance,
        kind=insight.kind,
        confidence=insight.confidence,
        status=insight.status,
    )


class _Plan(BaseModel):
    """解析后的整合方案。"""

    duplicates: list[dict[str, object]] = []
    merges: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []


def _parse(raw: str) -> _Plan:
    try:
        data = extract_json(raw)
    except ValueError as exc:
        logger.warning("consolidate_parse_failed", error=str(exc), raw=raw[:200])
        return _Plan()
    return _Plan(
        duplicates=list(data.get("duplicates", []) or []),
        merges=list(data.get("merges", []) or []),
        conflicts=list(data.get("conflicts", []) or []),
    )


def _str_list(value: object) -> list[str]:
    """把 JSON 里可能是任意类型的字段安全地取成字符串列表。

    模型输出不可信：字段可能缺失、是 null、是标量。统一收窄成 ``list[str]``，
    调用方就不必到处判类型。
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


async def _apply(
    database: Database,
    space_id: str,
    plan: _Plan,
    known: dict[str, Insight],
) -> ConsolidationOutcome:
    """执行整合方案。只操作 known 集合内的 id，模型编造的 id 一律忽略。"""
    outcome = ConsolidationOutcome()
    touched: set[str] = set()  # 已被处理的 id，避免一条经验被多个动作重复操作

    # 1) 重复：归档 drop_ids（保留 keep_id）
    for group in plan.duplicates:
        keep_id = str(group.get("keep_id", ""))
        if keep_id not in known:
            continue
        for drop in _str_list(group.get("drop_ids")):
            if drop in known and drop not in touched and drop != keep_id:
                await _archive(database, drop)
                touched.add(drop)
                outcome.duplicates_archived += 1

    # 2) 合并：新建一条合并经验，把 source_ids 全部归档
    for merge in plan.merges:
        source_ids = _str_list(merge.get("source_ids"))
        valid = [s for s in source_ids if s in known and s not in touched]
        if len(valid) < 2:
            continue  # 少于两条无所谓合并
        trigger = str(merge.get("merged_trigger", "")).strip()
        guidance = str(merge.get("merged_guidance", "")).strip()
        if not trigger or not guidance:
            continue
        kind = str(merge.get("merged_kind", "")).strip()
        if kind not in _VALID_KINDS:
            kind = "heuristic"
        merged_trace_ids = _collect_trace_ids(known, valid)
        await database.insights.create(
            InsightCreate(
                space_id=space_id,
                trigger=trigger,
                guidance=guidance,
                rationale="由多条互补经验合并而来",
                kind=cast(InsightKind, kind),
                confidence=_max_confidence(known, valid),
                status="candidate",
                origin="user_correction",
                source_trace_ids=merged_trace_ids,
            )
        )
        for source in valid:
            await _archive(database, source)
            touched.add(source)
        outcome.merged += 1

    # 3) 冲突：双方标记 conflicted，不自动裁决
    for conflict in plan.conflicts:
        ids = _str_list(conflict.get("ids"))
        valid = [i for i in ids if i in known and i not in touched]
        if len(valid) < 2:
            continue
        for insight_id in valid:
            await database.insights.update(insight_id, InsightUpdate(status="conflicted"))
            touched.add(insight_id)
            outcome.conflicted_insights.append(insight_id)
        outcome.conflict_groups += 1

    return outcome


def _collect_trace_ids(known: dict[str, Insight], ids: list[str]) -> list[str]:
    seen: list[str] = []
    for insight_id in ids:
        for trace_id in known[insight_id].source_trace_ids:
            if trace_id not in seen:
                seen.append(trace_id)
    return seen


def _max_confidence(known: dict[str, Insight], ids: list[str]) -> float:
    return max((known[i].confidence for i in ids), default=0.3)


async def _archive(database: Database, insight_id: str) -> None:
    await database.insights.update(insight_id, InsightUpdate(status="archived"))
