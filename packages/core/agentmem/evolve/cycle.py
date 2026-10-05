"""一键进化 —— 把 ④蒸馏 → ⑤整合 → ⑥评测 → ⑦晋升 串成一次完整循环。

这是产品的高光按钮。对应 `docs/03-API-SPEC.md` §7 的 `/evolve/cycle` SSE：
逐阶段推送进度，最后给出「专家度 before → after」。

评测决定经验存留（第 ⑥⑦ 步）的逻辑：
1. 先跑一次 baseline（不注入任何候选），得基准分；
2. 再跑一次 with_insights（注入本轮全部候选 active/candidate），得对比分；
3. 分数提升 → 候选整体 EVAL_IMPROVED（置信度↑、转 active）；
4. 分数下降 → 候选整体 EVAL_REGRESSED（置信度↓，可能被归档）。

**没有测验集时不做评测**，候选留在 candidate 等用户手动确认或后续补测——
绝不在没有裁判的情况下把经验自动转正。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import structlog
from pydantic import BaseModel

from agentmem.evolve.confidence import ConfidenceEvent, apply_confidence_event
from agentmem.evolve.consolidate import consolidate_insights
from agentmem.evolve.distill import distill_feedback
from agentmem.evolve.insights import reconcile_insight_vectors
from agentmem.expert.expertise import Embedder
from agentmem.expert.noise import paired_test
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import RetrievalPipeline
from agentmem.store import Database
from agentmem.types import (
    EvolutionRunCreate,
    Insight,
    InsightEventCreate,
    InsightUpdate,
    Persona,
)

logger = structlog.get_logger(__name__)


class CycleStage(BaseModel):
    """一次进化中某阶段的产出（用于 SSE stage 事件）。"""

    stage: str
    status: str  # running | done
    detail: dict[str, object] = {}


class CycleResult(BaseModel):
    """一次完整进化的结果。"""

    produced: int = 0
    merged: int = 0
    duplicates: int = 0
    conflicts: int = 0
    promoted: int = 0
    demoted: int = 0
    eval_delta: float | None = None
    expertise_before: float = 0.0
    expertise_after: float = 0.0
    #: 本轮结束时仍停在 candidate 的经验数。没有测验题、测得题数不足或差异在噪声内时，
    #: 候选都不会生效；不把这个数报出来，用户会以为「进化过了」就已经学会了
    pending_candidates: int = 0


class EvolutionService:
    """进化闭环编排。一个 Space 一个实例。"""

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        pipeline: RetrievalPipeline,
        persona: Persona,
    ) -> None:
        # 在函数内导入：expert 的评价器要用 evolve._persona 做 Persona → Prompt 映射，
        # 两个包在模块层互相 import 会成环（先 import agentmem.expert 的一方直接报错）。
        # 编排层按需拉取，环就断在这里。
        from agentmem.expert.evaluator import EvaluationService
        from agentmem.expert.expertise import ExpertiseService

        self.db = database
        self.registry = registry
        self.persona = persona
        self.evaluator = EvaluationService(database, registry, pipeline, persona)
        embed, embed_key = _topic_embedder(registry)
        self.expertise = ExpertiseService(database, persona, embed=embed, embed_key=embed_key)

    async def run_cycle(self, space_id: str) -> AsyncIterator[CycleStage | CycleResult]:
        """执行一次进化，逐阶段 yield 进度，最后 yield 一个 CycleResult。

        设计成异步生成器，路由层直接把每个产出映射成 SSE 事件即可。
        """
        started = time.perf_counter()
        before = await self.expertise.compute(space_id)
        result = CycleResult(expertise_before=before.overall)

        # ④ 蒸馏
        yield CycleStage(stage="distill", status="running")
        distilled = await distill_feedback(
            database=self.db,
            registry=self.registry,
            persona=self.persona,
            space_id=space_id,
        )
        result.produced = len(distilled.candidates)
        yield CycleStage(
            stage="distill",
            status="done",
            detail={"produced": result.produced, "skipped": len(distilled.skipped)},
        )

        # ⑤ 整合
        candidate_ids = [c.id for c in distilled.candidates]
        yield CycleStage(stage="consolidate", status="running")
        consolidation = await consolidate_insights(
            database=self.db,
            registry=self.registry,
            persona=self.persona,
            space_id=space_id,
            candidate_ids=candidate_ids,
        )
        result.merged = consolidation.merged
        result.duplicates = consolidation.duplicates_archived
        result.conflicts = consolidation.conflict_groups
        yield CycleStage(
            stage="consolidate",
            status="done",
            detail={
                "merged": result.merged,
                "duplicates": result.duplicates,
                "conflicts": result.conflicts,
            },
        )

        # ⑥ 评测 + ⑦ 晋升
        async for event in self._evaluate_and_promote(space_id, result):
            yield event

        result.pending_candidates = len(await self._pending_candidates(space_id))

        # 收尾：算 after 快照，并把这一轮的产出如实记进进化日志
        after = await self.expertise.snapshot(space_id)
        result.expertise_after = after.overall
        await self.db.evolution.create(
            EvolutionRunCreate(
                space_id=space_id,
                produced=result.produced,
                merged=result.merged,
                duplicates=result.duplicates,
                conflicts=result.conflicts,
                promoted=result.promoted,
                demoted=result.demoted,
                eval_delta=result.eval_delta,
                expertise_before=result.expertise_before,
                expertise_after=result.expertise_after,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        logger.info(
            "evolve_cycle_done",
            space_id=space_id,
            produced=result.produced,
            promoted=result.promoted,
            demoted=result.demoted,
            delta=round(result.expertise_after - result.expertise_before, 2),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        yield result

    async def _evaluate_and_promote(
        self, space_id: str, result: CycleResult
    ) -> AsyncIterator[CycleStage]:
        """A/B 评测决定候选经验的存留。"""
        eval_count = await self.db.eval_items.count(space_id)
        has_evals = eval_count > 0
        pending = await self._pending_candidates(space_id)

        if not has_evals or not pending:
            # 无测验集或无候选：跳过评测，候选留在 candidate 等人工确认
            yield CycleStage(
                stage="evaluate",
                status="done",
                detail={"skipped": True, "reason": "无测验集或无候选经验"},
            )
            return

        yield CycleStage(stage="evaluate", status="running", detail={"variant": "baseline"})
        baseline = await self.evaluator.run(space_id, variant="baseline", persist=False)
        yield CycleStage(
            stage="evaluate",
            status="running",
            detail={"variant": "baseline", "score": baseline.score},
        )

        with_insights = await self.evaluator.run(
            space_id,
            variant="with_insights",
            insight_set=[c.id for c in pending],
            persist=True,
        )
        # 配对比较：只在两轮**都测出分数**的题上相减。直接拿两轮总分相减的话，
        # 两边排除的未测得题可能不同，等于拿两个不同题集的平均分比较。
        base_scores = {i.item_id: i.score for i in baseline.item_scores if i.measured}
        paired = [
            (base_scores[i.item_id], i.score)
            for i in with_insights.item_scores
            if i.measured and i.item_id in base_scores
        ]
        delta = (
            round(sum(after - before for before, after in paired) / len(paired), 2)
            if paired
            else 0.0
        )
        result.eval_delta = delta
        yield CycleStage(
            stage="evaluate",
            status="done",
            detail={
                "baseline": baseline.score,
                "with_insights": with_insights.score,
                "delta": delta,
            },
        )

        # ⑦ 晋升 / 降级
        yield CycleStage(stage="promote", status="running")
        # 两轮里**都测出分数**的题数才是这次对比的有效样本。任一轮测得太少
        # （典型场景：限流、额度耗尽），分数就只是占位，拿它判决等于拿故障去淘汰经验。
        measured = len(paired)
        if measured < max(1, eval_count // 2):
            logger.warning(
                "eval_verdict_skipped_insufficient_measurements",
                baseline_measured=baseline.measured_count,
                with_insights_measured=with_insights.measured_count,
                items=eval_count,
                hint="多半是模型限流或额度耗尽；候选经验保持原状，下次再评",
            )
            yield CycleStage(
                stage="promote",
                status="done",
                detail={
                    "promoted": 0,
                    "demoted": 0,
                    "skipped": True,
                    "reason": (
                        f"有效测得题数不足（基准 {baseline.measured_count}/{eval_count}，"
                        f"带经验 {with_insights.measured_count}/{eval_count}），本轮不做判决"
                    ),
                },
            )
            return
        promoted, demoted = await self._apply_verdict(
            pending, delta, [after - before for before, after in paired]
        )
        result.promoted = promoted
        result.demoted = demoted
        yield CycleStage(
            stage="promote",
            status="done",
            detail={"promoted": promoted, "demoted": demoted},
        )

    async def _pending_candidates(self, space_id: str) -> list[Insight]:
        """本轮参与评测的候选经验（candidate 状态；conflicted 的不参与，等裁决）。"""
        return await self.db.insights.list_by_status(space_id, "candidate")

    async def _apply_verdict(
        self, candidates: list[Insight], delta: float, item_deltas: list[float]
    ) -> tuple[int, int]:
        """根据配对差值的显著性对候选整体升/降置信度。返回 (晋升数, 降级数)。

        与多臂对比界面的红绿配色走同一个判定（`expert.noise.paired_test`）：
        否则会出现界面说「显著提升」、闭环却判「噪声内不动」的口径分裂。
        宁可「这轮看不出差别、不动置信度」，也不要拿噪声去晋升或淘汰经验。
        """
        test = paired_test(item_deltas)
        if not test.significant:
            logger.info(
                "eval_delta_within_noise",
                delta=delta,
                t=None if test.t is None else round(test.t, 2),
                items=test.n,
                hint="题量越少要求越高；想分辨更小的差异需要更大的评测集",
            )
            return 0, 0  # 差异在噪声范围内，不动
        event = ConfidenceEvent.EVAL_IMPROVED if delta > 0 else ConfidenceEvent.EVAL_REGRESSED
        promoted = 0
        demoted = 0
        for insight in candidates:
            new_conf, new_status = apply_confidence_event(
                confidence=insight.confidence, status=insight.status, event=event
            )
            updated = await self.db.insights.update(
                insight.id,
                InsightUpdate(confidence=new_conf, status=new_status, eval_delta=delta),
            )
            # 整批判决也要留档：一次 A/B 把一批经验一起 ±0.2，用户事后要能看出
            # 「这条是被哪一次的评测结果推动的」
            await self.db.insight_events.add(
                InsightEventCreate(
                    insight_id=updated.id,
                    space_id=updated.space_id,
                    event=str(event),
                    confidence_before=insight.confidence,
                    confidence_after=updated.confidence,
                    status_before=insight.status,
                    status_after=updated.status,
                    reason=f"A/B {delta:+.1f}",
                )
            )
            if new_status == "active" and insight.status != "active":
                promoted += 1
            elif new_status == "archived" and insight.status != "archived":
                demoted += 1
        # 晋升 / 淘汰改了可召回集合：向量池跟着对账，否则晋升的经验召回不到
        if candidates:
            await reconcile_insight_vectors(self.db, self.registry, candidates[0].space_id)
        return promoted, demoted


def _topic_embedder(registry: ProviderRegistry) -> tuple[Embedder | None, str]:
    """进化前后的专家度快照要和专家度页口径一致：卡片按语义归属大纲主题。

    缓存键用 provider 名：换了 embedding 模型，旧向量不能拿来和新向量比。
    """
    try:
        route = registry.embedding(purpose="expertise")
    except Exception:
        return None, ""
    if not route.providers:
        return None, ""

    async def embed(texts: list[str]) -> list[list[float]]:
        return await route.embed(texts, kind="doc")

    return embed, route.name
