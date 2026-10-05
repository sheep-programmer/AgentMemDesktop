"""进化闭环 —— AgentMem 的灵魂。

对应 `docs/00-VISION.md` §3 的七步循环：

    ① Interact    用户提问，Agent 基于 L1~L4 作答（由 retrieve 层完成）
    ② Trace       记录轨迹（由 retrieve 层落库）
    ③ Critique    评价：用户反馈 + LLM-as-Judge          → feedback.py
    ④ Distill     从差评/纠错中蒸馏候选经验               → distill.py
    ⑤ Consolidate 去重 / 合并 / 冲突检测                   → consolidate.py
    ⑥ Evaluate    用 EvalSet 跑 A/B（在 expert 层）
    ⑦ Promote     置信度升降、晋升/淘汰                    → confidence.py

`cycle.py` 把 ④⑤⑥⑦ 串成「一键进化」，是产品的高光按钮。

**核心设计原则：经验必须可证伪。** 任何一条 Insight 都要能被 EvalSet 验证；
学不到东西的经验会被自动降级淘汰，而不是无限堆积成噪音。
"""

from __future__ import annotations

from agentmem.evolve.confidence import (
    ConfidenceEvent,
    apply_confidence_event,
    feedback_event,
    resolve_status,
)
from agentmem.evolve.consolidate import ConsolidationOutcome, consolidate_insights
from agentmem.evolve.critique import CritiqueService
from agentmem.evolve.cycle import EvolutionService
from agentmem.evolve.distill import DistillOutcome, distill_feedback
from agentmem.evolve.insights import FeedbackAttribution, InsightService

__all__ = [
    "ConfidenceEvent",
    "ConsolidationOutcome",
    "CritiqueService",
    "DistillOutcome",
    "EvolutionService",
    "FeedbackAttribution",
    "InsightService",
    "apply_confidence_event",
    "consolidate_insights",
    "distill_feedback",
    "feedback_event",
    "resolve_status",
]
