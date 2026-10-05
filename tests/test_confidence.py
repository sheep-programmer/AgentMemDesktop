"""置信度规则的穷举测试。

这是进化闭环里唯一的纯函数决策点，也是「经验可证伪」的落地：
一条经验的升降完全由这张规则表决定，必须可预测、可复现。
对照 `docs/02-DATA-MODEL.md` §4 的规则表逐条验证。
"""

from __future__ import annotations

import pytest

from agentmem.evolve.confidence import (
    ARCHIVE_THRESHOLD,
    INITIAL_CONFIDENCE,
    ConfidenceEvent,
    apply_confidence_event,
    resolve_status,
)
from agentmem.types import InsightStatus


def test_initial_confidence_matches_spec() -> None:
    assert INITIAL_CONFIDENCE == 0.3


@pytest.mark.parametrize(
    ("event", "delta"),
    [
        (ConfidenceEvent.USER_CONFIRM, 0.30),
        (ConfidenceEvent.POSITIVE_FEEDBACK, 0.05),
        (ConfidenceEvent.NEGATIVE_FEEDBACK, -0.15),
        (ConfidenceEvent.EVAL_IMPROVED, 0.20),
        (ConfidenceEvent.EVAL_REGRESSED, -0.20),
    ],
)
def test_confidence_deltas(event: ConfidenceEvent, delta: float) -> None:
    """每个事件的增量与规则表一致。"""
    new_conf, _status = apply_confidence_event(confidence=0.5, status="candidate", event=event)
    assert new_conf == pytest.approx(0.5 + delta)


def test_user_confirm_promotes_to_active() -> None:
    _conf, status = apply_confidence_event(
        confidence=0.3, status="candidate", event=ConfidenceEvent.USER_CONFIRM
    )
    assert status == "active"


def test_eval_improved_promotes_to_active() -> None:
    _conf, status = apply_confidence_event(
        confidence=0.3, status="candidate", event=ConfidenceEvent.EVAL_IMPROVED
    )
    assert status == "active"


def test_positive_feedback_does_not_change_status() -> None:
    """好评只微调置信度，不擅自转正——转正要靠确认或评测。"""
    _conf, status = apply_confidence_event(
        confidence=0.5, status="candidate", event=ConfidenceEvent.POSITIVE_FEEDBACK
    )
    assert status == "candidate"


def test_low_confidence_gets_archived() -> None:
    """置信度跌破阈值 → 自动归档（软删除）。"""
    new_conf, status = apply_confidence_event(
        confidence=0.25, status="active", event=ConfidenceEvent.EVAL_REGRESSED
    )
    assert new_conf == pytest.approx(0.05)
    assert new_conf < ARCHIVE_THRESHOLD
    assert status == "archived"


def test_confidence_clamped_to_unit_interval() -> None:
    high, _ = apply_confidence_event(
        confidence=0.95, status="active", event=ConfidenceEvent.USER_CONFIRM
    )
    assert high == 1.0
    low, _ = apply_confidence_event(
        confidence=0.05, status="active", event=ConfidenceEvent.EVAL_REGRESSED
    )
    assert low == 0.0


def test_conflicted_status_is_never_auto_resolved() -> None:
    """冲突态必须由用户裁决：任何自动事件都不能把它转出 conflicted。"""
    for event in ConfidenceEvent:
        _conf, status = apply_confidence_event(confidence=0.5, status="conflicted", event=event)
        assert status == "conflicted", f"{event} 不该改变冲突态"


def test_conflicted_confidence_still_updates() -> None:
    """冲突态的置信度仍会更新（供裁决时参考），只是状态不变。"""
    new_conf, status = apply_confidence_event(
        confidence=0.5, status="conflicted", event=ConfidenceEvent.EVAL_IMPROVED
    )
    assert new_conf == pytest.approx(0.7)
    assert status == "conflicted"


@pytest.mark.parametrize("status", ["candidate", "active"])
def test_resolve_status_archives_below_threshold(status: InsightStatus) -> None:
    assert resolve_status(0.1, status) == "archived"


def test_resolve_status_keeps_conflicted() -> None:
    assert resolve_status(0.05, "conflicted") == "conflicted"


def test_resolve_status_keeps_healthy() -> None:
    assert resolve_status(0.5, "active") == "active"
    assert resolve_status(0.5, "candidate") == "candidate"


def test_negative_feedback_can_cascade_to_archive() -> None:
    """连续负反馈把候选经验推到归档：可证伪性的体现。"""
    confidence = INITIAL_CONFIDENCE
    status: InsightStatus = "candidate"
    for _ in range(3):
        confidence, status = apply_confidence_event(
            confidence=confidence, status=status, event=ConfidenceEvent.NEGATIVE_FEEDBACK
        )
    # 0.3 → 0.15 → 0.0 → 0.0，跌破阈值后归档
    assert status == "archived"
