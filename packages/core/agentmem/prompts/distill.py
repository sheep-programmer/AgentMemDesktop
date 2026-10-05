"""经验蒸馏 —— 进化闭环的第 ④ 步，AgentMem 最核心的一段 Prompt。

输入：一批带反馈的交互轨迹（用户的差评、纠错、好评）。
输出：候选经验条目（InsightCandidate），三段式「场景 / 做法 / 依据」。

**最容易踩的坑，也是这段 Prompt 主要在防的事：**

1. 把「事实」当成「经验」。
   "阿司匹林的分子量是 180.16" 是事实 → 属于 L2 知识卡片，不是经验。
   "用户问剂量换算时要先确认物种，因为大鼠与人的体表面积系数不同" 才是经验。
2. 产出不可证伪的废话。
   "要回答得更准确一些" 无法验证、无法执行，是纯噪音。
3. 过度泛化。
   一次针对某个具体化合物的纠正，被写成"所有化合物都应该……"。
4. 复述这一次的答案。
   经验要能迁移到**下一个相似但不相同**的问题上，否则毫无价值。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import join, schema_block, section, truncate
from ._shapes import InsightRef, Message, PersonaSpec

MAX_ANSWER_CHARS = 3000
MAX_CORRECTION_CHARS = 2000


class FeedbackEpisode(TypedDict):
    """一次带反馈的交互，蒸馏的原料。"""

    trace_id: str
    question: str
    answer: str
    feedback_kind: str  # up | down | correction | edit
    comment: NotRequired[str | None]  # 用户写的差评理由或纠正内容
    judge_score: NotRequired[float | None]
    judge_reason: NotRequired[str | None]
    used_insights: NotRequired[list[str]]  # 本次注入过的 insight_id


_KIND_GUIDE = """\
每条经验必须归入以下五类之一：

- `correction`  —— 纠错型。用户明确指出上次答错了，沉淀正确做法。**优先级最高**。
- `constraint`  —— 约束型。某种情况下绝对不能做什么 / 必须先做什么。
- `heuristic`   —— 启发型。某类问题的有效切入角度或排查顺序。
- `preference`  —— 偏好型。用户希望的回答形式（详略、格式、是否给代码）。
- `terminology` —— 术语型。本领域该用哪个词、不该用哪个词。
"""

_QUALITY_BAR = """\
一条合格的经验必须同时满足全部四条，任何一条不满足就**不要产出这条**：

1. **可迁移**：`trigger` 描述的是一类场景，不是这一个具体问题。
   ✅ "用户询问某先导化合物的成药性时"
   ❌ "用户问 GDC-0994 这个化合物成药性怎样时"

2. **可执行**：`guidance` 是具体动作，不是态度或口号。
   ✅ "先评估 ADMET 五项，任一项红线即先解决再谈活性优化"
   ❌ "要更仔细地分析，给出更准确的答案"

3. **可证伪**：能想象出一个检验它的问题，答对答错有明确区别。
   如果一条经验无论怎样都算"遵守了"，它就是废话。

4. **是行为规则而非事实**：
   陈述客观事实 → 不要产出（那属于知识卡片，由另一条管线处理）。
   规定"遇到什么情况该怎么做" → 才是经验。
   自检方法：能不能改写成「当……时，应当……」？不能就不是经验。
"""

_SCHEMA: dict[str, Any] = {
    "insights": [
        {
            "trigger": "什么场景下适用（一类场景，会被向量化用于匹配后续问题）",
            "guidance": "应该怎么做（具体、可执行）",
            "rationale": "为什么（来自哪次纠正、纠正了什么）",
            "kind": "correction | constraint | heuristic | preference | terminology",
            "source_trace_ids": ["支撑这条经验的 trace_id，至少一个"],
            "confidence_hint": "0.0~1.0，你对这条经验可靠性的初步判断",
            "supersedes_hint": "如果它明显是对某条现有经验的修正，填那条的 id，否则 null",
        }
    ],
    "skipped": [{"trace_id": "被跳过的轨迹 id", "reason": "为什么没能从中提炼出经验"}],
}

_EXAMPLE: dict[str, Any] = {
    "insights": [
        {
            "trigger": "用户询问某先导化合物的成药性，且只给了靶点活性数据时",
            "guidance": ("先索要 ADMET 与选择性数据再下结论，不要仅凭 IC50 判断一个化合物值得推进"),
            "rationale": (
                "2026-03-12 用户纠正：我只看了 IC50 就说该化合物很有前景，"
                "但它 hERG 抑制强、口服生物利用度低，临床前必被淘汰"
            ),
            "kind": "correction",
            "source_trace_ids": ["01HQ...ABC"],
            "confidence_hint": 0.7,
            "supersedes_hint": None,
        }
    ],
    "skipped": [
        {
            "trace_id": "01HQ...XYZ",
            "reason": "用户只点了踩没有说明原因，无法判断问题出在哪里",
        }
    ],
}


def _render_episode(ep: FeedbackEpisode, index: int) -> str:
    feedback_label = {
        "up": "👍 用户认可",
        "down": "👎 用户不满意",
        "correction": "✏️ 用户直接纠正了答案",
        "edit": "✏️ 用户编辑了答案",
    }.get(ep["feedback_kind"], ep["feedback_kind"])

    parts = [
        f"反馈类型：{feedback_label}",
        section("question", ep["question"]),
        section("my_answer", truncate(ep["answer"], MAX_ANSWER_CHARS)),
    ]

    if comment := ep.get("comment"):
        label = "用户的纠正内容" if ep["feedback_kind"] in ("correction", "edit") else "用户的反馈"
        parts.append(section("user_feedback", truncate(comment, MAX_CORRECTION_CHARS), label=label))

    if (score := ep.get("judge_score")) is not None:
        reason = ep.get("judge_reason") or ""
        parts.append(section("auto_judge", f"评分 {score:.2f}\n{reason}"))

    if used := ep.get("used_insights"):
        parts.append(
            section(
                "insights_applied",
                "本次回答注入了这些经验：" + ", ".join(used) + "\n"
                "如果反馈是负面的，重点判断是否某条经验本身就是错的——"
                "这种情况请产出一条 supersedes_hint 指向它的修正经验。",
            )
        )

    # escape=False：parts 里的子块已各自转义
    return section("episode", join(*parts), escape=False, index=index, trace_id=ep["trace_id"])


def _render_existing(existing: list[InsightRef]) -> str:
    if not existing:
        return ""
    lines = [
        f"[{i['insight_id']}] 场景：{i['trigger']}\n            做法：{i['guidance']}"
        for i in existing
    ]
    return section(
        "existing_insights",
        "知识库中已有的经验。**不要重复产出语义相同的条目**；\n"
        "如果新素材是对其中某条的修正或细化，在 `supersedes_hint` 里指出它的 id：\n\n"
        + "\n".join(lines),
    )


def build_distill_messages(
    *,
    persona: PersonaSpec,
    episodes: list[FeedbackEpisode],
    existing_insights: list[InsightRef] | None = None,
) -> list[Message]:
    """构造蒸馏请求。

    建议用推理能力强的模型（`distill` 角色），温度调低（0.2~0.3）——
    这一步要的是审慎归纳，不是发散创作。
    """
    system = join(
        f"你是一名经验萃取专家，正在协助一个「{persona['domain']}」领域的 AI 助手自我改进。",
        "你的工作是：阅读它过往回答收到的反馈，从中提炼出**可迁移、可执行、可验证**的经验规则，"
        "让它下次遇到相似问题时不再犯同样的错。",
        section("insight_kinds", _KIND_GUIDE),
        section("quality_bar", _QUALITY_BAR),
        join(
            "重要原则：**宁缺毋滥。**",
            "产出 2 条扎实的经验，远胜于产出 8 条似是而非的。",
            "如果某条轨迹实在提炼不出有价值的东西（比如用户只点了踩没说原因），"
            "就把它放进 `skipped` 并说明原因——这不是失败，这是正确的判断。",
            "特别地：对于 👍 好评，只有当它揭示了**某种做法确实有效**时才产出经验；"
            "如果只是答案本身正确，那不构成经验。",
        ),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    user = join(
        _render_existing(existing_insights or []),
        section(
            "feedback_episodes",
            "\n\n".join(_render_episode(ep, i) for i, ep in enumerate(episodes, 1)),
        ),
        f"请从以上 {len(episodes)} 条反馈中蒸馏经验。",
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
