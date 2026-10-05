"""经验置信度更新规则 —— 进化闭环第 ⑦ 步的核心。

严格实现 `docs/02-DATA-MODEL.md` §4 的规则表。这是纯函数模块：
输入一条经验的当前状态 + 一个事件，输出新的 (confidence, status)，
不碰数据库、不碰模型。这样规则可以被穷举测试，也让「为什么这条经验被淘汰了」
永远可解释、可复现。

规则表（务必与文档保持一致，改这里必须同步改文档）：

| 事件                     | confidence 变化 | status 变化              |
|--------------------------|-----------------|--------------------------|
| 初次蒸馏产出             | = 0.3           | candidate                |
| 用户显式确认             | +0.3            | active                   |
| 应用后得正反馈           | +0.05           | —                        |
| 应用后得负反馈           | -0.15           | —                        |
| EvalSet A/B 提升         | +0.2            | active                   |
| EvalSet A/B 下降         | -0.2            | —                        |
| 与现有条目语义冲突       | —               | conflicted（双方）        |
| confidence < 0.15        | —               | archived（软删除）        |

反馈事件的两个增量是**一次反馈的总量**，不是每条经验的增量：一次回答可能同时注入了
多条经验，用户点的是整条回答的 👍/👎，系统无从知道其中哪一条起了作用，于是把这一次
的证据按条数均摊（``share = 1 / 注入条数``）。只有一条经验被注入时，``share`` 为 1，
增量与上表逐字一致。这样一次误点的 👎 不会同时把六条经验一起推向归档，而
``success_count / applied_count`` 会逐条累积，日后据此做单条归因。
"""

from __future__ import annotations

from enum import StrEnum

from agentmem.types import InsightStatus

#: 低于此置信度的经验自动归档（软删除，可恢复）
ARCHIVE_THRESHOLD = 0.15

#: 蒸馏产出的候选经验初始置信度
INITIAL_CONFIDENCE = 0.3


class ConfidenceEvent(StrEnum):
    """会改变经验置信度 / 状态的事件。取值即规则表的行。"""

    USER_CONFIRM = "user_confirm"  # 用户点「提升」显式确认
    POSITIVE_FEEDBACK = "positive_feedback"  # 应用后得到 👍
    NEGATIVE_FEEDBACK = "negative_feedback"  # 应用后得到 👎
    EVAL_IMPROVED = "eval_improved"  # A/B 评测证明有效
    EVAL_REGRESSED = "eval_regressed"  # A/B 评测证明有害


#: 每个事件对置信度的增量
_DELTA: dict[ConfidenceEvent, float] = {
    ConfidenceEvent.USER_CONFIRM: +0.30,
    ConfidenceEvent.POSITIVE_FEEDBACK: +0.05,
    ConfidenceEvent.NEGATIVE_FEEDBACK: -0.15,
    ConfidenceEvent.EVAL_IMPROVED: +0.20,
    ConfidenceEvent.EVAL_REGRESSED: -0.20,
}

#: 会把经验直接推进到 active 的事件（前提是它不处于冲突态）
_PROMOTES_TO_ACTIVE = frozenset({ConfidenceEvent.USER_CONFIRM, ConfidenceEvent.EVAL_IMPROVED})

#: 用户反馈的类别 → 置信度事件。纠错与编辑是「这个回答错了」的最强表态，
#: 与点踩同类处理：错的回答会连带把当时注入的经验一起降权。
FEEDBACK_EVENTS: dict[str, ConfidenceEvent] = {
    "up": ConfidenceEvent.POSITIVE_FEEDBACK,
    "down": ConfidenceEvent.NEGATIVE_FEEDBACK,
    "correction": ConfidenceEvent.NEGATIVE_FEEDBACK,
    "edit": ConfidenceEvent.NEGATIVE_FEEDBACK,
}


def feedback_event(kind: str) -> ConfidenceEvent | None:
    """把反馈类别映射成置信度事件；不改变置信度的类别返回 ``None``。"""
    return FEEDBACK_EVENTS.get(kind)


#: 可以按份额施加的事件集合，供 :func:`apply_confidence_event` 校验
_FEEDBACK_EVENT_SET = frozenset(FEEDBACK_EVENTS.values())


def clamp(value: float) -> float:
    """把置信度夹到 [0, 1]。"""
    return max(0.0, min(1.0, value))


def resolve_status(confidence: float, current: InsightStatus) -> InsightStatus:
    """根据置信度决定状态的自动流转。

    只负责「低于阈值 → archived」这条自动规则。冲突态由 consolidate 单独处理，
    这里遇到 ``conflicted`` 一律原样保留——冲突必须由用户裁决，不能因为
    置信度回升就悄悄解除。
    """
    if current == "conflicted":
        return "conflicted"
    if confidence < ARCHIVE_THRESHOLD:
        return "archived"
    return current


def apply_confidence_event(
    *,
    confidence: float,
    status: InsightStatus,
    event: ConfidenceEvent,
    share: float = 1.0,
) -> tuple[float, InsightStatus]:
    """对一条经验应用一个事件，返回更新后的 ``(confidence, status)``。

    纯函数，不产生副作用；调用方负责把结果写回数据库。

    Args:
        confidence: 当前置信度。
        status: 当前状态。
        event: 发生的事件。
        share: 这条经验分到的证据份额，取值 ``(0, 1]``，默认独占整份。
            只有反馈类事件可以均摊（见模块 docstring）；确认与评测事件
            要么整份施加，要么不施加，传其它值会抛 :class:`ValueError`。

    Returns:
        ``(新置信度, 新状态)``。
    """
    if not 0.0 < share <= 1.0:
        raise ValueError("share 必须落在 (0, 1] 区间")
    if share != 1.0 and event not in _FEEDBACK_EVENT_SET:
        raise ValueError(f"{event} 不支持按份额施加，它必须整份生效")

    new_confidence = clamp(confidence + _DELTA[event] * share)

    # 冲突态的经验在用户裁决前，任何事件都不改变它的 conflicted 状态，
    # 只更新置信度（供裁决时参考），避免评测把一条待裁决的经验偷偷转正。
    if status == "conflicted":
        return new_confidence, "conflicted"

    new_status: InsightStatus = status
    if event in _PROMOTES_TO_ACTIVE:
        new_status = "active"

    new_status = resolve_status(new_confidence, new_status)
    return new_confidence, new_status
