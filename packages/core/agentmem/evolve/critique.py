"""③ Critique —— 评价一次回答。

两个来源，产物都汇入 ``feedback`` 表，成为后续蒸馏的原料：

- **显式反馈**：用户点 👍 / 👎 / 直接纠正 → 落库，并立刻回流到这次回答注入过的
  L3 经验上（置信度按份额升降、``success_count`` 自增），见
  :meth:`InsightService.attribute_feedback`。
- **自动评分**：LLM-as-Judge 按 Persona 的 quality_bar 打分 → 落库为一条
  ``up`` / ``down`` 反馈承载分数。judge 分数**不**回流置信度：它是无人监督的
  自动判断，只在 EvalSet 的 A/B 路径上生效；直接动经验存留的信号只认用户表态。

判分复用 `agentmem.prompts.build_judge_messages`，绝不在这里拼提示词。
"""

from __future__ import annotations

import structlog

from agentmem.evolve._persona import to_persona_spec, to_provider_messages
from agentmem.evolve.insights import FeedbackAttribution, InsightService
from agentmem.prompts import ChunkRef as PromptChunkRef
from agentmem.prompts import build_judge_messages, extract_json
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import (
    Feedback,
    FeedbackCreate,
    FeedbackKind,
    JudgeResponse,
    Persona,
    Trace,
)

logger = structlog.get_logger(__name__)

#: judge 打分用低温度，要的是稳定判断而非发挥
JUDGE_TEMPERATURE = 0.2

#: judge 认为「合格」的分数线（0~1）；低于此线在无显式反馈时记为负反馈
JUDGE_PASS_LINE = 0.6


class CritiqueService:
    """反馈收集与自动评分。一个 Space 一个实例。"""

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        persona: Persona,
        space_id: str,
    ) -> None:
        self.db = database
        self.registry = registry
        self.persona = persona
        self.space_id = space_id

    # -- 显式反馈 ---------------------------------------------------------

    async def submit_feedback(
        self, trace_id: str, kind: FeedbackKind, comment: str | None = None
    ) -> Feedback:
        """记录用户的显式反馈，并让它回流到当时注入的经验上。

        trace 必须存在（否则反馈无从溯源）；纠错类反馈必须带 comment，
        否则蒸馏时没有可学的内容。

        每条反馈记录都会回流一次，所以同一条回答重复提交同一方向的反馈会重复
        计票——防重放在提交入口（前端按钮进入已提交态），而不是靠这里猜哪次是
        误点。回流失败不影响反馈本身落库：差评的文本证据比一次置信度加减更重要，
        它还要进蒸馏管线。
        """
        trace = await self.db.traces.require(trace_id)
        if kind in ("correction", "edit") and not (comment and comment.strip()):
            from agentmem.errors import ValidationError

            raise ValidationError(
                "纠正类反馈必须说明正确做法", detail={"trace_id": trace_id, "kind": kind}
            )
        feedback = await self.db.feedback.create(
            FeedbackCreate(trace_id=trace_id, kind=kind, comment=comment)
        )
        await self._attribute(trace, kind, feedback.id)
        return feedback

    async def _attribute(self, trace: Trace, kind: FeedbackKind, feedback_id: str) -> None:
        """把反馈回流到这次回答注入过的经验上，失败只记日志。"""
        try:
            insights = InsightService(
                database=self.db, registry=self.registry, space_id=self.space_id
            )
            attribution: FeedbackAttribution = await insights.attribute_feedback(trace, kind)
            logger.info(
                "feedback_attributed",
                space_id=self.space_id,
                trace_id=trace.id,
                feedback_id=feedback_id,
                kind=kind,
                confidence_event=str(attribution.event) if attribution.event else None,
                insights=len(attribution.insight_ids),
                archived=len(attribution.archived),
            )
        except Exception:
            logger.exception(
                "feedback_attribution_failed",
                space_id=self.space_id,
                trace_id=trace.id,
                feedback_id=feedback_id,
            )

    # -- 自动评分 ---------------------------------------------------------

    async def judge_trace(self, trace_id: str) -> JudgeResponse:
        """用 LLM-as-Judge 给一次回答打分，并把分数落库。

        没有可评的助手回答时抛错。评分结果：
        - 更新该 trace 已有反馈的 judge_score（若有）；
        - 否则新建一条反馈（合格→up / 不合格→down）承载分数，
          使它能进入蒸馏管线。
        """
        trace = await self.db.traces.require(trace_id)
        answer = await self._answer_of(trace)

        messages = build_judge_messages(
            persona=to_persona_spec(self.persona),
            question=trace.query,
            answer=answer,
            chunks=await self._chunk_refs(trace),
        )
        route = self.registry.llm("judge", purpose="judge")
        result = await route.chat(to_provider_messages(messages), temperature=JUDGE_TEMPERATURE)

        score, reason = self._parse_judge(result.content)
        feedback_id = await self._record_judge(trace_id, score, reason)
        return JudgeResponse(trace_id=trace_id, score=score, reason=reason, feedback_id=feedback_id)

    async def _answer_of(self, trace: Trace) -> str:
        from agentmem.errors import NotFoundError

        message = await self.db.messages.get(trace.message_id)
        if message is None or not message.content.strip():
            raise NotFoundError("可评分的回答", trace.id)
        return message.content

    async def _chunk_refs(self, trace: Trace) -> list[PromptChunkRef]:
        """把 trace 记录的检索命中还原成 judge 需要的证据形状。

        trace 只存了 chunk_id 与各路分数，没存全文；judge 要看「答案是否有据」，
        因此按 id 回载真实切片内容。切片已被删除的跳过（judge Prompt 会据此
        判断答案是否恰当承认了信息缺失）。
        """
        refs: list[PromptChunkRef] = []
        for index, item in enumerate(trace.retrieved, start=1):
            chunk = await self.db.chunks.get(item.chunk_id)
            if chunk is None:
                continue
            document = await self.db.documents.get(chunk.document_id)
            refs.append(
                PromptChunkRef(
                    marker=f"c{index}",
                    chunk_id=chunk.id,
                    document_title=document.title if document else "资料",
                    content=chunk.content,
                )
            )
        return refs

    @staticmethod
    def _parse_judge(raw: str) -> tuple[float, str]:
        """解析 judge 返回的 JSON，取总分与总评。

        judge Prompt 约定输出 0~1 的 ``score`` 与 ``verdict``；解析失败时
        给一个中性分并记录，绝不让整条评价链路因一次脏输出崩掉。
        """
        try:
            data = extract_json(raw)
            score = float(data.get("score", 0.5))
            reason = str(data.get("verdict") or "").strip()
            return max(0.0, min(1.0, score)), reason
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("judge_parse_failed", error=str(exc), raw=raw[:200])
            return 0.5, "评分解析失败"

    async def _record_judge(self, trace_id: str, score: float, reason: str) -> str:
        from agentmem.types import FeedbackUpdate

        existing = await self.db.feedback.list_by_trace(trace_id)
        if existing:
            # 已有显式反馈：把分数补到最近一条上，不新建，避免重复计入蒸馏
            target = existing[-1]
            await self.db.feedback.update(
                target.id, FeedbackUpdate(judge_score=score, judge_reason=reason)
            )
            return target.id
        kind: FeedbackKind = "up" if score >= JUDGE_PASS_LINE else "down"
        created = await self.db.feedback.create(
            FeedbackCreate(
                trace_id=trace_id,
                kind=kind,
                judge_score=score,
                judge_reason=reason,
            )
        )
        return created.id
