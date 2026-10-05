"""一致性度量：同一个问题问多遍，答案到底稳不稳。

专家度雷达上的「逻辑一致性」此前是一个**代理指标**（active 经验占非归档经验的比例），
它量的是「沉淀下来的规则多不多」，跟「同样的问题问两遍会不会得到两个说法」毫无关系。
一个指标若与它的标签说的不是一回事，比没有这个指标更糟——用户会照着雷达图做判断。

这里做的是真口径：取最近问过的若干问题，每个问题在**正常温度下重复回答若干次**，
把答案向量化后算两两余弦相似度的平均。相似度低意味着模型对这个问题的回答不稳定，
无论原因在检索（每次捞到的证据不同）还是在生成（自由发挥），对用户都是可感知的不一致。

代价与取舍：一次探测是 ``问题数 × 重复次数`` 次生成调用，所以它**不随页面加载自动跑**，
而是显式触发（``POST /spaces/{id}/expertise/consistency``），结果落库；专家度计算读最近
一次的实测值，没测过时退回代理指标并在响应里说明来源。
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations

import structlog

from agentmem.errors import ProviderNotConfiguredError, ValidationError
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import RetrievalPipeline, assemble_answer_messages
from agentmem.store import Database
from agentmem.types import (
    ConsistencyProbe,
    ConsistencyProbeCreate,
    ConsistencyQuestionScore,
    Insight,
)

logger = structlog.get_logger(__name__)

#: 一次探测最多取几个问题
MAX_QUESTIONS = 10

#: 默认取几个问题、每个问题问几遍
DEFAULT_QUESTIONS = 3
DEFAULT_REPEATS = 3

#: 回答温度与对话链路保持一致：测的是用户实际会遇到的稳定性
ANSWER_TEMPERATURE = 0.3


#: 余弦相似度可能落在 [0,1] 之外（语义相反时为负）。一致性是「像不像」，
#: 负数一律按 0 处理，避免两个完全相反的答案把平均分拉得更低而失去解释力。
def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """两个向量的余弦相似度，夹到 ``0~1``。"""
    if len(left) != len(right) or not left:
        raise ValidationError("向量维度不一致", detail={"left": len(left), "right": len(right)})
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return _clamp(dot / (left_norm * right_norm))


def mean_pairwise_similarity(vectors: Sequence[Sequence[float]]) -> float:
    """两两相似度的平均。

    少于两个向量时没有「两两」可言，返回 0——调用方据此把它当作「没测到」。
    只问一遍的答案当然稳定，但它证明不了任何事。
    """
    if len(vectors) < 2:
        return 0.0
    scores = [cosine_similarity(left, right) for left, right in combinations(vectors, 2)]
    return round(sum(scores) / len(scores), 4)


class ConsistencyService:
    """一致性的探测与读取。一个 Space 一个实例。"""

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        pipeline: RetrievalPipeline,
    ) -> None:
        self.db = database
        self.registry = registry
        self.pipeline = pipeline

    async def latest(self) -> ConsistencyProbe | None:
        """最近一次实测结果；没测过返回 ``None``。"""
        return await self.db.consistency.latest(self.pipeline.space_id)

    async def measure(
        self,
        *,
        questions: int = DEFAULT_QUESTIONS,
        repeats: int = DEFAULT_REPEATS,
        insights: Sequence[Insight] = (),
    ) -> ConsistencyProbe:
        """跑一次一致性探测并把结果落库。

        Args:
            questions: 取几个问题（取最近问过的，去重后按时间倒序）。
            repeats: 每个问题重复回答几遍，至少 2——只问一遍测不出任何东西。
            insights: 回答时注入的经验集合，与真实对话保持一致。
        """
        if repeats < 2:
            raise ValidationError("重复次数至少为 2，只问一遍测不出稳定性")
        repeats = min(repeats, MAX_QUESTIONS)
        probes = await self._pick_questions(min(questions, MAX_QUESTIONS))
        if not probes:
            raise ValidationError("还没有问过任何问题，无法测一致性")

        try:
            embedder = self.registry.embedding(purpose="consistency")
        except ProviderNotConfiguredError as exc:
            raise ValidationError(
                "一致性需要 embedding 角色才能比较答案的语义相似度", detail={"role": "embedding"}
            ) from exc

        scored: list[ConsistencyQuestionScore] = []
        for question in probes:
            answers = [await self._answer(question, insights) for _ in range(repeats)]
            vectors = await embedder.embed(list(answers), kind="doc")
            scored.append(
                ConsistencyQuestionScore(
                    question=question,
                    similarity=mean_pairwise_similarity(vectors),
                    answers=answers,
                )
            )

        overall = round(sum(item.similarity for item in scored) / len(scored), 4)
        probe = await self.db.consistency.create(
            ConsistencyProbeCreate(
                space_id=self.pipeline.space_id,
                questions=len(scored),
                repeats=repeats,
                similarity=overall,
                detail=scored,
            )
        )
        logger.info(
            "consistency_measured",
            space_id=self.pipeline.space_id,
            questions=len(scored),
            repeats=repeats,
            similarity=overall,
        )
        return probe

    async def _pick_questions(self, limit: int) -> list[str]:
        """挑要复问的问题：最近问过的，去重后新的在前。

        用真实提过的问题而不是测验集：一致性量的是「日常使用中回答会不会飘」，
        测验集是出题人挑的，不代表用户实际会问什么。没有历史问题时退回测验集。
        """
        traces, _total, _cursor = await self.db.traces.list_by_space(
            self.pipeline.space_id, limit=200
        )
        seen: dict[str, None] = {}
        for trace in sorted(traces, key=lambda item: item.created_at, reverse=True):
            question = trace.query.strip()
            if question:
                seen.setdefault(question, None)
            if len(seen) >= limit:
                break
        if seen:
            return list(seen)
        items, _total, _cursor = await self.db.eval_items.list_by_space(
            self.pipeline.space_id, limit=limit
        )
        return [item.question for item in items]

    async def _answer(self, question: str, insights: Sequence[Insight]) -> str:
        """按正常对话链路生成一次答案（检索 + 装配 + 生成）。"""
        result = await self.pipeline.retrieve(question, use_insights=False)
        messages, _registry = assemble_answer_messages(
            persona=self.pipeline.persona,
            question=question,
            chunks=result.chunks,
            insights=list(insights),
            cards=result.cards,
        )
        route = self.registry.llm("chat", purpose="consistency")
        completion = await route.chat(messages, temperature=ANSWER_TEMPERATURE)
        return completion.content
