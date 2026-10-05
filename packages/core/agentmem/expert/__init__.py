"""专家度与评测 —— 让「这个 AI 有多专业」变成可量化、可验证的数字。

- `evaluator.py`  A/B 评测执行器：给定一组经验，跑测验集打分。进化闭环第 ⑥ 步。
- `evalgen.py`    测验集与领域大纲生成（复用 prompts）。
- `expertise.py`  五维专家度计算 + 快照。
- `consistency.py` 一致性实测：同问多答看答案稳不稳。
- `gaps.py`       知识盲区：领域大纲里尚无资料覆盖的节点。

专家度是整个进化闭环的裁判：没有它，任何经验都无法被证明有用或有害，
系统只能越用越臃肿。
"""

from __future__ import annotations

from agentmem.expert.consistency import ConsistencyService
from agentmem.expert.evaluator import EvalRunResult, EvaluationService
from agentmem.expert.expertise import ExpertiseService

__all__ = [
    "ConsistencyService",
    "EvalRunResult",
    "EvaluationService",
    "ExpertiseService",
]
