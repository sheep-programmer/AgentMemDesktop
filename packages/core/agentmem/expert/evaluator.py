"""A/B 评测执行器 —— 进化闭环第 ⑥ 步。

核心能力：给定「一组要注入的经验」和一套测验集，逐题生成答案并用 LLM-as-Judge
打分，得出这组经验下的整体得分（0~100）。

进化闭环靠它做对比：
- ``baseline``      —— 不注入任何经验，作为基准分
- ``with_insights`` —— 注入候选经验集，看分数是否提升

分数提升 → 经验有效 → 晋升；下降 → 有害 → 降级。这就是「经验可证伪」的落地。

复用检索管线：用 ``pipeline.retrieve(use_insights=False)`` 拿到 L1 证据，
再用 ``assemble_answer_messages`` 注入**指定**的经验集合——这样才能精确控制
「这一组经验」而不是「全部活跃经验」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence

import structlog
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from agentmem.errors import ProviderTimeoutError, ProviderUnavailableError, ValidationError
from agentmem.evolve._persona import to_persona_spec, to_provider_messages
from agentmem.expert.metrics import EvidenceAudit, audit_from_judge, average, compute
from agentmem.expert.noise import min_eval_delta, paired_test
from agentmem.prompts import ChunkRef as PromptChunkRef
from agentmem.prompts import EvalItem as PromptEvalItem
from agentmem.prompts import build_eval_judge_messages, extract_json
from agentmem.prompts.judge import JUDGE_WEIGHTS
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import (
    RetrievalPipeline,
    ScoredChunk,
    assemble_answer_messages,
    assign_markers,
)
from agentmem.store import Database
from agentmem.types import (
    EvalArmDelta,
    EvalArmOutcome,
    EvalArmSpec,
    EvalComparison,
    EvalItem,
    EvalItemDelta,
    EvalItemScore,
    EvalRun,
    EvalRunCreate,
    EvalRunDetail,
    EvalVariant,
    Insight,
    InsightAttribution,
    InsightContribution,
    Persona,
    RetrievalMetrics,
    RetrievalMetricsDelta,
    RetrievalSettings,
)

logger = structlog.get_logger(__name__)

ANSWER_TEMPERATURE = 0.3
JUDGE_TEMPERATURE = 0.2

#: 判定单题「通过」的分数线（0~100）
PASS_LINE = 60.0

#: 同时评测的题目并发上限，避免打爆模型服务
EVAL_CONCURRENCY = 4


class EvalRunResult(BaseModel):
    """一次评测的结果（含落库后的记录）。"""

    run: EvalRun
    score: float
    item_scores: list[EvalItemScore]
    metrics: RetrievalMetrics | None = None

    @property
    def measured_count(self) -> int:
        """真正测出分数的题数。

        ``score`` 在一题都没测出来时是 0.0——那只是个占位，**不代表考了 0 分**。
        任何拿 ``score`` 做判断的调用方（尤其是进化闭环的晋升/淘汰）都必须先看这个数，
        否则一次限流就会被读成「这组经验让分数掉到了 0」。
        """
        return sum(1 for item in self.item_scores if item.measured)


class EvaluationService:
    """测验集评测。一个 Space 一个实例。"""

    #: 暂时性故障（限流、超时）的重试等待秒数；用尽后该题记为未测得。
    #: 免费额度的限流窗口通常以分钟计，太短的间隔等于白试。
    retry_delays: tuple[float, ...] = (20.0, 60.0)

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        pipeline: RetrievalPipeline,
        persona: Persona,
    ) -> None:
        self.db = database
        self.registry = registry
        self.pipeline = pipeline
        self.persona = persona

    async def run(
        self,
        space_id: str,
        *,
        variant: EvalVariant = "baseline",
        insight_set: list[str] | None = None,
        persist: bool = True,
        retrieval: Mapping[str, object] | None = None,
        label: str | None = None,
    ) -> EvalRunResult:
        """跑一次全量评测。

        Args:
            variant: baseline（不注入经验）/ with_insights / custom。
            insight_set: 要注入的经验 id；``variant='baseline'`` 时强制为空。
            persist: 是否落库为一条 eval_run（cycle 内部的临时评测可设 False）。
            retrieval: 检索配置的覆盖项（只写要改的旋钮），用于验证「调参到底有没有用」。
            label: 这一轮的展示名，落进 ``detail.retrieval``，多臂对比时用来区分。
        """
        items = await self._items(space_id)
        if not items:
            from agentmem.errors import EvalSetEmptyError

            raise EvalSetEmptyError("测验集为空，无法评测")

        insights = await self._resolve_insights(variant, insight_set)
        return await self._execute(
            space_id,
            items,
            insights,
            variant=variant,
            persist=persist,
            retrieval=retrieval,
            label=label,
        )

    async def compare(
        self,
        space_id: str,
        arms: Sequence[EvalArmSpec],
        *,
        persist: bool = True,
    ) -> EvalComparison:
        """多臂对比：同一批题目、同一批经验，只换检索配置。

        这正是「我方检索改动到底有没有用」的判据所在——分两次跑全量评测再比总分，
        会把题目难度、采样噪声、题目顺序都混进差值里；同一批题目配对比较才谈得上
        可证伪。把同一个配置跑两臂（A/A 对照）得到的就是本轮噪声底。

        经验注入沿用各臂自己的 ``insight_set``；不传就是空集，即只比检索。
        """
        if len(arms) < 2:
            raise ValidationError("多臂对比至少需要两臂", detail={"arms": len(arms)})

        items = await self._items(space_id)
        if not items:
            from agentmem.errors import EvalSetEmptyError

            raise EvalSetEmptyError("测验集为空，无法评测")

        outcomes: list[EvalArmOutcome] = []
        for arm in arms:
            insights = await self._resolve_insights("custom", arm.insight_set)
            result = await self._execute(
                space_id,
                items,
                insights,
                variant="custom",
                persist=persist,
                retrieval=arm.retrieval,
                label=arm.label,
            )
            outcomes.append(
                EvalArmOutcome(
                    label=arm.label,
                    run_id=result.run.id,
                    score=result.score,
                    metrics=result.metrics,
                    item_scores=result.item_scores,
                )
            )
        comparison = EvalComparison(
            space_id=space_id,
            items=len(items),
            baseline=outcomes[0].label,
            arms=outcomes,
            deltas=_deltas(outcomes),
        )
        logger.info(
            "eval_compare_done",
            space_id=space_id,
            arms=[arm.label for arm in outcomes],
            scores=[arm.score for arm in outcomes],
        )
        return comparison

    async def attribute_insights(
        self,
        space_id: str,
        *,
        insight_ids: Sequence[str] | None = None,
        max_insights: int = 5,
        include_noise_floor: bool = True,
        persist: bool = False,
    ) -> InsightAttribution:
        """留一法归因：把每条经验单独拿掉重跑同一批题，看分数掉多少。

        这是「这条经验到底有没有用」唯一站得住脚的答法。反馈回流只能做到把一次 👎
        按注入条数均摊——那是相关性；留一法做的是对照实验，掉下去的那部分才是这条
        经验真正贡献的。

        **代价是 N+1 轮评测**（开了噪声底就是 N+2）：每一轮都要把整个测验集重跑一遍，
        逐题调用模型。所以要归因哪几条必须由调用方挑明，且有上限。

        ``noise_floor`` 是同一配置跑两遍得到的分差绝对值：贡献小于它的，只是这一轮的
        采样抖动，不该当成结论。

        Args:
            space_id: 所属 Space。
            insight_ids: 要归因的经验；留空则取当前生效的那些。
            max_insights: 最多归因几条。
            include_noise_floor: 是否多跑一轮 A/A 求噪声底。
            persist: 每一轮是否落库为评测记录。

        Raises:
            ValidationError: 没有可归因的经验。
        """
        if insight_ids:
            candidates = await self.db.insights.get_many(list(insight_ids))
        else:
            candidates = await self.db.insights.list_by_status(space_id, "active")
        candidates = candidates[:max_insights]
        if not candidates:
            raise ValidationError("没有可归因的经验", detail={"space_id": space_id})

        items = await self._items(space_id)
        if not items:
            from agentmem.errors import EvalSetEmptyError

            raise EvalSetEmptyError("测验集为空，无法归因")

        full = [insight.id for insight in candidates]
        arms = [EvalArmSpec(label="全部经验", insight_set=full)]
        if include_noise_floor:
            # A/A：同一配置再跑一轮，用来量这一轮的采样抖动有多大
            arms.append(EvalArmSpec(label="全部经验（A/A 对照）", insight_set=full))
        arms.extend(
            EvalArmSpec(
                label=f"去掉：{insight.trigger[:24]}",
                insight_set=[other for other in full if other != insight.id],
            )
            for insight in candidates
        )

        comparison = await self.compare(space_id, arms, persist=persist)
        baseline = comparison.arms[0]
        offset = 2 if include_noise_floor else 1
        # 一律用 compare() 算好的**配对**差值（只在两臂都测出分数的题上相减），
        # 不再拿两臂的原始总分相减、也不再逐题比较原始分数：
        # - 原始总分相减：两臂各自排除的未测得题可能不同，等于比两个不同题集的均值；
        # - 逐题比原始分数：「去掉这条经验」那一臂某题撞了限流（占位 0 分），
        #   会被算成「这条经验帮了这道题」。
        by_label = {delta.label: delta for delta in comparison.deltas}

        noise_floor = None
        if include_noise_floor:
            aa = by_label[comparison.arms[1].label]
            # 单次 A/A 只是一个样本，碰巧很小时会让噪声冒充信号。
            # 取它与按题量标定的理论阈值中的较大者（标定见 `expert/noise.py`）。
            noise_floor = round(max(abs(aa.score_delta), min_eval_delta(len(aa.item_deltas))), 2)

        contributions: list[InsightContribution] = []
        for insight, arm in zip(candidates, comparison.arms[offset:], strict=True):
            delta = by_label[arm.label]
            # 去掉它之后分数变化了 delta；这条经验的贡献就是 -delta
            helped = sum(1 for item in delta.item_deltas if item.score_delta < 0)
            hurt = sum(1 for item in delta.item_deltas if item.score_delta > 0)
            contributions.append(
                InsightContribution(
                    insight_id=insight.id,
                    trigger=insight.trigger,
                    score_without=arm.score,
                    contribution=round(-delta.score_delta, 2),
                    items_helped=helped,
                    items_hurt=hurt,
                )
            )
        contributions.sort(key=lambda item: item.contribution, reverse=True)

        logger.info(
            "insight_attribution_done",
            space_id=space_id,
            insights=len(contributions),
            baseline=baseline.score,
            noise_floor=noise_floor,
        )
        return InsightAttribution(
            space_id=space_id,
            items=comparison.items,
            baseline_score=baseline.score,
            noise_floor=noise_floor,
            contributions=contributions,
        )

    async def _execute(
        self,
        space_id: str,
        items: list[EvalItem],
        insights: list[Insight],
        *,
        variant: EvalVariant,
        persist: bool,
        retrieval: Mapping[str, object] | None,
        label: str | None,
    ) -> EvalRunResult:
        """按给定的题目与检索配置跑一轮，落库并算总分与检索指标。"""
        started = time.perf_counter()
        pipeline = self._pipeline_with(retrieval)
        item_scores = await self._score_all(items, insights, pipeline)
        # 只对真的测出分数的题求均值：裁判输出解析不出来的题是「没测到」，
        # 按 0 分计入会把一次格式抖动变成实打实的掉分（8 题里坏 1 题就是 -12.5）
        measured = [s for s in item_scores if s.measured]
        if measured:
            overall = round(sum(s.score for s in measured) / len(measured), 2)
        else:
            # 一题都没测出来：给 0 而不是除零，reason 里已写明原因
            overall = 0.0
            logger.warning(
                "eval_run_all_unmeasured",
                space_id=space_id,
                variant=variant,
                items=len(item_scores),
            )
        metrics = average([s.metrics for s in item_scores if s.metrics is not None])
        duration = int((time.perf_counter() - started) * 1000)

        run = await self._persist(
            space_id,
            variant,
            insights,
            overall,
            item_scores,
            metrics,
            duration,
            persist,
            retrieval=_recorded_retrieval(retrieval, label),
        )
        logger.info(
            "eval_run_done",
            space_id=space_id,
            variant=variant,
            label=label,
            score=overall,
            items=len(item_scores),
        )
        return EvalRunResult(run=run, score=overall, item_scores=item_scores, metrics=metrics)

    def _pipeline_with(self, override: Mapping[str, object] | None) -> RetrievalPipeline:
        """按覆盖项装配一条检索管线；没有覆盖就复用现有实例。

        覆盖项走 ``RetrievalSettings`` 校验（``extra="forbid"``），拼错的旋钮名
        会当场报错，而不是被静默忽略——「改了参数但分数没动」是最难查的一类假象。
        """
        if not override:
            return self.pipeline
        merged = {**self.pipeline.params.model_dump(), **override}
        try:
            params = RetrievalSettings.model_validate(merged)
        except PydanticValidationError as exc:
            raise ValidationError("检索覆盖项不合法", detail={"errors": exc.errors()[:5]}) from exc
        return RetrievalPipeline(
            space_id=self.pipeline.space_id,
            database=self.pipeline.db,
            registry=self.pipeline.registry,
            settings=self.pipeline.settings,
            retrieval=params,
            persona=self.pipeline.persona,
        )

    async def _items(self, space_id: str) -> list[EvalItem]:
        items, _total, _cursor = await self.db.eval_items.list_by_space(space_id, limit=500)
        return items

    async def _resolve_insights(
        self, variant: EvalVariant, insight_set: list[str] | None
    ) -> list[Insight]:
        """baseline 一律空集；其余按传入 id 取（丢弃不存在的）。"""
        if variant == "baseline" or not insight_set:
            return []
        return await self.db.insights.get_many(insight_set)

    async def _score_all(
        self, items: list[EvalItem], insights: list[Insight], pipeline: RetrievalPipeline
    ) -> list[EvalItemScore]:
        """并发评测所有题目（有并发上限）。"""
        semaphore = asyncio.Semaphore(EVAL_CONCURRENCY)

        async def score(item: EvalItem) -> EvalItemScore:
            async with semaphore:
                return await self._score_one(item, insights, pipeline)

        return await asyncio.gather(*(score(item) for item in items))

    async def _score_one(
        self, item: EvalItem, insights: list[Insight], pipeline: RetrievalPipeline
    ) -> EvalItemScore:
        """单题：检索 → 注入指定经验生成答案 → judge 打分并审计检索。

        一道题失败不中断整轮，且记为**未测得**而不是 0 分（见下方 except）。
        模型服务的暂时性故障（限流 429、超时）先退避重试，重试用尽才放弃：
        实测对比跑到一半 agnes 开始返回 429，降级链上其余 provider 又都不可用，
        一轮里 21 道题因此作废——而等几十秒再试，它们本可以测出来。
        检索侧指标失败的代价更小：审计解析不出来就只丢指标，分数照常。
        """
        for delay in (*self.retry_delays, None):
            try:
                return await self._score_one_attempt(item, insights, pipeline)
            except (ProviderUnavailableError, ProviderTimeoutError) as exc:
                if delay is None:
                    return self._unmeasured(item, exc)
                logger.info(
                    "eval_item_retry", item_id=item.id, wait_seconds=delay, error=str(exc)[:120]
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                return self._unmeasured(item, exc)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _score_one_attempt(
        self, item: EvalItem, insights: list[Insight], pipeline: RetrievalPipeline
    ) -> EvalItemScore:
        """单题的一次尝试；异常原样抛给 `_score_one` 决定重试还是放弃。"""
        answer, chunks = await self._answer(item.question, insights, pipeline)
        refs, _registry = assign_markers(chunks)
        score, reason, passed, metrics = await self._judge(item, answer, refs)
        return EvalItemScore(
            item_id=item.id,
            # 未测得时占位 0，但 measured=False 会让它被排除在总分之外
            score=0.0 if score is None else score,
            passed=passed,
            reason=reason,
            metrics=metrics,
            measured=score is not None,
        )

    @staticmethod
    def _unmeasured(item: EvalItem, exc: Exception) -> EvalItemScore:
        """基础设施故障的题记为未测得。

        抛异常的是限流、超时、服务不可用，不是「答得完全错」。此前记的是 score=0
        且 measured 默认 True：「带经验」那一轮撞上限流时每题都是 0 分，总分≈0，
        delta = −基准分，远超阈值——结果是**把所有候选经验一起降级**。
        """
        logger.warning("eval_item_failed", item_id=item.id, error=str(exc))
        return EvalItemScore(
            item_id=item.id,
            score=0.0,
            passed=False,
            reason=f"评测异常（未计入总分）：{exc}",
            measured=False,
        )

    async def _answer(
        self, question: str, insights: list[Insight], pipeline: RetrievalPipeline
    ) -> tuple[str, list[ScoredChunk]]:
        """检索证据 + 注入指定经验集，生成一次（非流式）答案。

        连同检索到的证据一起返回：judge 要用同一批证据做检索审计，而
        ``assign_markers`` 的编号顺序与答案提示词里的完全一致（都是 c1、c2…），
        所以审计里说的 c3 就是答案里 ``[^c3]`` 指的那一条。
        """
        # use_insights=False：不让管线自动召回，由我们注入精确的 insight_set
        result = await pipeline.retrieve(question, use_insights=False)
        messages, _registry = assemble_answer_messages(
            persona=self.persona,
            question=question,
            chunks=result.chunks,
            insights=insights,
            cards=result.cards,
        )
        # assemble_answer_messages 已返回 provider 消息，无需再转
        route = self.registry.llm("chat", purpose="eval")
        completion = await route.chat(messages, temperature=ANSWER_TEMPERATURE)
        return completion.content, list(result.chunks)

    async def _judge(
        self, item: EvalItem, answer: str, evidence: list[PromptChunkRef]
    ) -> tuple[float | None, str, bool, RetrievalMetrics]:
        """用 eval_judge 打分并审计检索，返回 (0~100 分, 理由, 是否通过, 检索指标)。

        分数为 ``None`` 表示裁判输出解析失败、这题没测出来，由调用方标成未测得。
        """
        messages = build_eval_judge_messages(
            persona=to_persona_spec(self.persona),
            item=PromptEvalItem(
                question=item.question,
                reference=item.reference,
                must_include=item.must_include,
            ),
            answer=answer,
            evidence=evidence,
        )
        route = self.registry.llm("judge", purpose="eval_judge")
        completion = await route.chat(to_provider_messages(messages), temperature=JUDGE_TEMPERATURE)
        raw01, reason, audit = self._parse(completion.content)
        score = None if raw01 is None else round(raw01 * 100, 2)
        metrics = compute(
            audit,
            must_include=item.must_include,
            retrieved_markers=[ref["marker"] for ref in evidence],
        )
        return score, reason, score is not None and score >= PASS_LINE, metrics

    @staticmethod
    def _parse(raw: str) -> tuple[float | None, str, EvidenceAudit | None]:
        """解析裁判输出。分数为 ``None`` 表示**没测出来**，不是 0 分。

        这里曾经把解析失败直接记成 0.0，而 0 分的含义是「答得完全错」。
        实测踩到过：裁判给的是 correctness 1.0、score 1.0、issues 为空的满分评价，
        只因为它在中文里用了 ASCII 双引号（``指出"无需外推"的前提``）导致 JSON 非法，
        这道题就从满分变成零分。这个分数会一路流向 A/B 对比 → 经验晋升/淘汰 → 专家度，
        一次格式抖动足以翻转一条经验的去留。
        """
        try:
            data = extract_json(raw)
            reason = str(data.get("verdict") or "").strip()
            raw_score = data.get("score")
            if raw_score is not None:
                score = float(raw_score)
            else:
                # 漏写汇总分：按提示词自己的定义（四维加权和）算回来。
                # **绝不能**默认成 0——此前就是 `data.get("score", 0.0)`，
                # 实测「四维全 1.0、verdict『全对』」的满分答案因此被记成零分，
                # 与上面那个「解析失败记 0 分」是同一类问题的另一条路径。
                score = 0.0
                for key, weight in JUDGE_WEIGHTS.items():
                    value = data.get(key)
                    if value is None:
                        raise ValueError(f"裁判输出既无 score 也缺维度 {key}：{sorted(data)}")
                    score += float(value) * weight
                # 四个权重相加有浮点误差（0.4+0.3+0.2+0.1 = 0.9999999999999999），
                # 提示词本身也要求保留两位小数
                score = round(score, 4)
                logger.info("eval_judge_score_derived", score=score)
            return max(0.0, min(1.0, score)), reason, audit_from_judge(data)
        except (ValueError, TypeError, KeyError) as exc:
            # 整段 JSON 解析不了，但分数多半还在。实测失败样本里**每一条**的 score 都
            # 完整写在开头（0.55 / 0.96 / 0.0 / 0.82 / 0.87），坏的是后面 verdict 里
            # 中文混入的 ASCII 双引号（`不存在的"知识卡片"来源`）。数值字段位于评语之前、
            # 不受评语标点影响，把它们抽回来就不必整条丢弃——两次 A/A 里 32 次评分
            # 有 5 次因此作废，8 题的有效样本掉到 6、7 题。
            salvaged = _salvage_judge_score(raw)
            if salvaged is not None:
                logger.info("eval_judge_score_salvaged", score=salvaged, error=str(exc)[:120])
                return salvaged, "裁判输出格式有瑕疵，分数已从原文抽回", None
            logger.warning("eval_judge_parse_failed", error=str(exc), raw=raw[:200])
            return None, "评分解析失败（未计入总分）", None

    async def _persist(
        self,
        space_id: str,
        variant: EvalVariant,
        insights: list[Insight],
        overall: float,
        item_scores: list[EvalItemScore],
        metrics: RetrievalMetrics | None,
        duration: int,
        persist: bool,
        retrieval: dict[str, object] | None = None,
    ) -> EvalRun:
        create = EvalRunCreate(
            space_id=space_id,
            variant=variant,
            insight_set=[i.id for i in insights],
            score=overall,
            detail=EvalRunDetail(items=item_scores, metrics=metrics, retrieval=retrieval),
            duration_ms=duration,
        )
        if persist:
            return await self.db.eval_runs.create(create)
        # 不落库时构造一个瞬时对象供 cycle 内部对比使用
        from agentmem.store.base import new_id
        from agentmem.store.sqlite import now_ms

        return EvalRun(
            id=new_id(),
            space_id=space_id,
            variant=variant,
            insight_set=create.insight_set,
            score=overall,
            detail=create.detail,
            duration_ms=duration,
            created_at=now_ms(),
        )


def _recorded_retrieval(
    override: Mapping[str, object] | None, label: str | None
) -> dict[str, object] | None:
    """写进评测记录的检索配置：覆盖项 + 这一轮的名字。"""
    if override is None and label is None:
        return None
    record: dict[str, object] = {}
    if label:
        record["label"] = label
    if override:
        record["override"] = dict(override)
    return record


_NUMERIC_FIELD = r'"{key}"\s*:\s*(-?\d+(?:\.\d+)?)'


def _salvage_judge_score(raw: str) -> float | None:
    """整段 JSON 坏掉时，从裁判原文里抽回分数（0~1）。

    优先取 ``score``；没有就按四维加权和算（与 `_parse` 同一套权重）。
    取**第一次**出现的值：顶层分数总写在最前面，后面的 ``evidence_audit``
    之类嵌套块里即使有同名字段也不会被误取。越界的值视为不可信。
    """
    import re

    def first(key: str) -> float | None:
        match = re.search(_NUMERIC_FIELD.format(key=key), raw)
        if match is None:
            return None
        value = float(match.group(1))
        return value if 0.0 <= value <= 1.0 else None

    score = first("score")
    if score is not None:
        return score
    dims = {key: first(key) for key in JUDGE_WEIGHTS}
    if any(value is None for value in dims.values()):
        return None
    return round(sum((dims[key] or 0.0) * weight for key, weight in JUDGE_WEIGHTS.items()), 4)


def _deltas(outcomes: Sequence[EvalArmOutcome]) -> list[EvalArmDelta]:
    """逐臂算相对基准臂的差值：只在**两臂都测出分数**的题上配对相减。

    此前这里完全不看 ``measured``：一边是未测得的占位 0 分、一边测出 97 分，
    就产出一个凭空的 +97。实测 A/A（同配置跑两遍）得到过
    ``[-8, +94, -25, +25, 0, +3, +97, +3]`` 这样的逐题差值，一度被当成
    「裁判噪声约 ±32」——其中的大摆幅相当一部分就是这种假零分造成的。

    总分差同理：两臂各自的均值排除的可能是**不同**的题，直接相减等于拿两个
    不同题集的平均分比较。所以总分差也在共同测得的题集上重算。
    """
    baseline = outcomes[0]
    base_measured = {item.item_id: item.score for item in baseline.item_scores if item.measured}
    deltas: list[EvalArmDelta] = []
    for arm in outcomes[1:]:
        paired = [
            (item.item_id, base_measured[item.item_id], item.score)
            for item in arm.item_scores
            if item.measured and item.item_id in base_measured
        ]
        items = [
            EvalItemDelta(item_id=item_id, score_delta=round(after - before, 2))
            for item_id, before, after in paired
        ]
        test = paired_test([after - before for _, before, after in paired])
        metrics_significant = _metrics_significance(baseline, arm)
        if paired:
            score_delta = round(sum(after - before for _, before, after in paired) / len(paired), 2)
        else:
            # 没有任何一道题两臂都测出来：给不出差值。0 会被读成「没区别」，
            # 所以同时在日志里说清楚，让调用方知道这不是结论
            score_delta = 0.0
            logger.warning("eval_compare_no_paired_items", arm=arm.label)
        deltas.append(
            EvalArmDelta(
                label=arm.label,
                score_delta=score_delta,
                metrics_delta=_metrics_delta(baseline.metrics, arm.metrics),
                item_deltas=items,
                paired_items=test.n,
                std_error=None if test.std_error is None else round(test.std_error, 3),
                t_stat=None if test.t is None else round(test.t, 3),
                significant=test.significant,
                metrics_significant=metrics_significant,
            )
        )
    return deltas


_METRIC_KEYS = ("context_recall", "context_precision", "faithfulness")


def _metrics_significance(baseline: EvalArmOutcome, arm: EvalArmOutcome) -> dict[str, bool]:
    """逐题配对检验各检索指标的差值是否显著。

    只用两臂都测到该指标的题；指标是 0~1 的比例、没有标定过的噪声值，
    所以只用实测标准差（``sd_floor=0``），题太少时 t 临界值会自动抬高。
    """
    base = {item.item_id: item.metrics for item in baseline.item_scores if item.measured}
    result: dict[str, bool] = {}
    for key in _METRIC_KEYS:
        diffs: list[float] = []
        for item in arm.item_scores:
            before_metrics = base.get(item.item_id)
            if not item.measured or before_metrics is None or item.metrics is None:
                continue
            before = getattr(before_metrics, key)
            after = getattr(item.metrics, key)
            if before is None or after is None:
                continue
            diffs.append(float(after) - float(before))
        result[key] = paired_test(diffs, sd_floor=0.0).significant
    return result


def _metrics_delta(
    before: RetrievalMetrics | None, after: RetrievalMetrics | None
) -> RetrievalMetricsDelta | None:
    """检索指标的差值；某项有一侧没测到就留空，不拿 0 顶替。"""
    if before is None or after is None:
        return None

    def diff(first: float | None, second: float | None) -> float | None:
        if first is None or second is None:
            return None
        return round(second - first, 4)

    return RetrievalMetricsDelta(
        context_recall=diff(before.context_recall, after.context_recall),
        context_precision=diff(before.context_precision, after.context_precision),
        faithfulness=diff(before.faithfulness, after.faithfulness),
        claims=after.claims - before.claims,
        evidence=after.evidence - before.evidence,
        audited=before.audited and after.audited,
    )
