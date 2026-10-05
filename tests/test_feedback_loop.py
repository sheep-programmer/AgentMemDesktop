"""反馈回流到经验的闭环测试。

一次回答注入了若干条 L3 经验，用户点 👍 / 👎 / 纠错时，这些经验必须被处理：

- 差评要真的把当时注入的经验压下去，不能让一条坏经验常驻注入池；
- 好评要把 ``success_count`` 记上，作为日后单条归因的分母；
- 一次反馈的证据是**总量**：注入六条时按条数均摊，而不是每条都吃满一份，
  否则一次误点就会把六条经验一起推向归档；
- 跌到阈值以下要归档，并且**移出召回池**——否则留下「已归档却仍被召回」的脏状态。

judge 的自动评分不在此列：无人监督的自动判断只走 EvalSet 的 A/B 路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agentmem.config import Settings
from agentmem.evolve import ConfidenceEvent, feedback_event
from agentmem.evolve.confidence import apply_confidence_event
from agentmem.space.runtime import Runtime
from agentmem.types import (
    ConversationCreate,
    Insight,
    InsightCreate,
    MessageCreate,
    SpaceCreate,
    TraceCreate,
    VectorRecord,
)
from apps.api.main import create_app

API = "/api/v1"

MODELS_YAML = """\
providers:
  - id: local-qwen
    kind: llm
    adapter: openai_compatible
    base_url: {base}
    model: mock-chat
  - id: local-embedding
    kind: embedding
    adapter: openai_compatible
    base_url: {base}
    model: mock-embed
    dimension: 8
roles:
  chat: local-qwen
  fast: local-qwen
  distill: local-qwen
  judge: local-qwen
  embedding: local-embedding
fallbacks: {{}}
"""


@pytest.fixture
def settings(tmp_path: Path, mock_server: str) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    return Settings(data_dir=data_dir, models_config=config_path)


@pytest.fixture
async def app_client(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """跑起完整应用（含 lifespan），返回应用与直连 ASGI 的客户端。"""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


async def _seed_insights(
    runtime: Runtime,
    space_id: str,
    *,
    count: int = 1,
    confidence: float = 0.5,
    status: str = "active",
) -> list[Insight]:
    """造若干条已生效的经验。"""
    database = await runtime.space_db(space_id)
    return [
        await database.insights.create(
            InsightCreate(
                space_id=space_id,
                trigger=f"遇到第 {index} 类先导化合物时",
                guidance="先评估 ADMET 再给结论",
                kind="heuristic",
                status=status,
                confidence=confidence,
                origin="manual",
            )
        )
        for index in range(count)
    ]


async def _seed_trace(runtime: Runtime, space_id: str, used_insights: list[str]) -> str:
    """造一条「注入过这些经验」的轨迹，返回 trace_id。"""
    database = await runtime.space_db(space_id)
    conversation = await database.conversations.create(
        ConversationCreate(space_id=space_id, title="成药性讨论")
    )
    message = await database.messages.create(
        MessageCreate(conversation_id=conversation.id, role="assistant", content="直接推进即可")
    )
    trace = await database.traces.create(
        TraceCreate(
            space_id=space_id,
            conversation_id=conversation.id,
            message_id=message.id,
            query="这个先导化合物成药性如何？",
            used_insights=used_insights,
        )
    )
    return trace.id


async def _new_space(runtime: Runtime, name: str = "新药研发") -> str:
    space = await runtime.spaces.create_space(SpaceCreate(name=name, domain="药物发现"))
    return space.id


async def _submit(
    client: httpx.AsyncClient, trace_id: str, kind: str, comment: str | None = None
) -> dict[str, Any]:
    """提交一次反馈，返回响应体。"""
    payload: dict[str, str] = {"kind": kind}
    if comment is not None:
        payload["comment"] = comment
    response = await client.post(f"{API}/traces/{trace_id}/feedback", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# 事件映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "event"),
    [
        ("up", ConfidenceEvent.POSITIVE_FEEDBACK),
        ("down", ConfidenceEvent.NEGATIVE_FEEDBACK),
        ("correction", ConfidenceEvent.NEGATIVE_FEEDBACK),
        ("edit", ConfidenceEvent.NEGATIVE_FEEDBACK),
        ("judge", None),
    ],
)
def test_feedback_kind_maps_to_event(kind: str, event: ConfidenceEvent | None) -> None:
    """纠错与编辑按负反馈处理：它们是「这个回答错了」的最强表态。"""
    assert feedback_event(kind) == event


def test_only_feedback_events_can_be_shared() -> None:
    """确认与评测事件必须整份生效，不能被均摊机制悄悄削弱。"""
    with pytest.raises(ValueError, match="整份生效"):
        apply_confidence_event(
            confidence=0.5,
            status="candidate",
            event=ConfidenceEvent.USER_CONFIRM,
            share=0.5,
        )
    with pytest.raises(ValueError, match="share"):
        apply_confidence_event(
            confidence=0.5,
            status="candidate",
            event=ConfidenceEvent.NEGATIVE_FEEDBACK,
            share=0.0,
        )


def test_share_scales_the_documented_delta() -> None:
    """份额按比例缩放增量。"""
    confidence, _status = apply_confidence_event(
        confidence=0.5,
        status="active",
        event=ConfidenceEvent.NEGATIVE_FEEDBACK,
        share=0.25,
    )

    assert confidence == pytest.approx(0.5 - 0.15 * 0.25)


# ---------------------------------------------------------------------------
# 只有一条经验被注入：增量与规则表逐字一致
# ---------------------------------------------------------------------------


async def test_like_raises_confidence_and_success_count(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """好评：置信度 +0.05，success_count 记一笔。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, [insight.id])

    await _submit(client, trace_id, "up")

    database = await runtime.space_db(space_id)
    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.55)
    assert updated.status == "active"
    assert updated.success_count == 1
    assert updated.applied_count == 0, "注入次数在对话落库时记，不在反馈里记"


async def test_dislike_lowers_confidence_without_touching_success_count(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """差评：置信度 -0.15，success_count 不动。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, [insight.id])

    await _submit(client, trace_id, "down")

    database = await runtime.space_db(space_id)
    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.35)
    assert updated.status == "active"
    assert updated.success_count == 0


async def test_correction_is_attributed_as_negative_feedback(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """纠错同样把当时注入的经验压下去。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, [insight.id])

    await _submit(client, trace_id, "correction", comment="该化合物 hERG 抑制强，不能只看活性")

    database = await runtime.space_db(space_id)
    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.35)
    assert updated.success_count == 0


# ---------------------------------------------------------------------------
# 多条经验被注入：证据均摊
# ---------------------------------------------------------------------------


async def test_dislike_is_shared_across_injected_insights(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """注入三条经验时点一次 👎：各降 0.15/3，总量仍是规则表里的一次 -0.15。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insights = await _seed_insights(runtime, space_id, count=3)
    trace_id = await _seed_trace(runtime, space_id, [item.id for item in insights])

    await _submit(client, trace_id, "down")

    database = await runtime.space_db(space_id)
    updated = [await database.insights.require(item.id) for item in insights]
    drops = [0.5 - item.confidence for item in updated]
    assert drops == [pytest.approx(0.05)] * 3
    assert sum(drops) == pytest.approx(0.15), "一次反馈的总证据量不该随注入条数放大"

    # 反过来：好评的总量也不随注入条数放大
    up_trace = await _seed_trace(runtime, space_id, [item.id for item in insights])
    await _submit(client, up_trace, "up")
    after = [await database.insights.require(item.id) for item in insights]
    assert [item.success_count for item in after] == [1, 1, 1]


async def test_one_dislike_does_not_archive_a_whole_batch(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """六条候选经验同时被注入时，一次差评不该把它们一起打进归档。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insights = await _seed_insights(runtime, space_id, count=6, confidence=0.2, status="candidate")
    trace_id = await _seed_trace(runtime, space_id, [item.id for item in insights])

    await _submit(client, trace_id, "down")

    database = await runtime.space_db(space_id)
    updated = [await database.insights.require(item.id) for item in insights]
    assert {item.status for item in updated} == {"candidate"}, "一次点击不该整批淘汰"
    assert all(item.confidence > 0.15 for item in updated)


# ---------------------------------------------------------------------------
# 归档与召回池同步
# ---------------------------------------------------------------------------


async def test_insight_below_threshold_is_archived_and_leaves_the_recall_pool(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """跌到阈值以下：状态转 archived，且向量从 insights_vec 里移除。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id, confidence=0.2))[0]
    database = await runtime.space_db(space_id)
    vectors = await database.require_vectors()
    await vectors.upsert(
        "insights_vec",
        [
            VectorRecord(
                id=insight.id,
                space_id=space_id,
                vector=[0.1] * 8,
                embedding_model="mock-embed",
            )
        ],
    )
    assert await vectors.count("insights_vec", space_id) == 1
    trace_id = await _seed_trace(runtime, space_id, [insight.id])

    await _submit(client, trace_id, "down")

    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.05)
    assert updated.status == "archived"
    assert await vectors.count("insights_vec", space_id) == 0, "归档了却还留在召回池里"


# ---------------------------------------------------------------------------
# 边界：没有注入经验、经验已被删除、judge 不回流
# ---------------------------------------------------------------------------


async def test_feedback_without_injected_insights_still_records(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """没有注入过经验的回答照样能提交反馈，只是没有经验可回流。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    trace_id = await _seed_trace(runtime, space_id, [])

    body = await _submit(client, trace_id, "down")

    assert body["kind"] == "down"
    database = await runtime.space_db(space_id)
    assert len(await database.feedback.list_by_trace(trace_id)) == 1


async def test_deleted_insight_does_not_block_the_rest(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """被注入的经验事后被删掉：反馈照常落库，还活着的经验照常回流。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, ["ins-does-not-exist", insight.id])

    await _submit(client, trace_id, "down")

    database = await runtime.space_db(space_id)
    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.35), "均摊只按实际存在的经验算"


async def test_judge_score_does_not_move_confidence(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """LLM-as-Judge 的自动评分不回流置信度，只有用户表态才算。"""
    app, _client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, [insight.id])
    service = await runtime.critique_service(space_id)

    await service.judge_trace(trace_id)

    database = await runtime.space_db(space_id)
    updated = await database.insights.require(insight.id)
    assert updated.confidence == pytest.approx(0.5)
    assert updated.success_count == 0


async def test_confidence_changes_are_recorded_as_a_journey(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """每一次加减分都要留下流水：产品的立身之本是「经验可证伪」，
    而「它为什么变成现在这样」得答得出来——以前是就地覆盖，看不到轨迹。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id, confidence=0.5))[0]

    # 一次好评、一次差评，再人工确认
    up_trace = await _seed_trace(runtime, space_id, [insight.id])
    await _submit(client, up_trace, "up")
    down_trace = await _seed_trace(runtime, space_id, [insight.id])
    await _submit(client, down_trace, "down")
    service = await runtime.insight_service(space_id)
    await service.promote(insight.id)

    history = await service.confidence_history(insight.id)
    events = history.events

    assert [event.event for event in events] == [
        "positive_feedback",
        "negative_feedback",
        "user_confirm",
    ]
    assert events[0].confidence_before == pytest.approx(0.5)
    assert events[0].confidence_after == pytest.approx(0.55)
    assert events[1].confidence_before == pytest.approx(0.55)
    assert events[1].confidence_after == pytest.approx(0.4)
    assert events[1].reason == "独占整份"
    assert events[-1].status_after == "active"
    assert history.insight.confidence == pytest.approx(0.7)


async def test_review_picks_experiences_that_keep_missing(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """被注入够多、却很少收到好评的经验要能被挑出来。

    反馈是均摊到当时注入的所有经验上的，归因本身是粗的——所以这是**统计线索**，
    不是判决：把「应用 9 次只成功 2 次」摆到人面前，由人决定留、改还是归档。
    """
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    database = await runtime.space_db(space_id)

    async def seeded(confidence: float, applied: int, success: int) -> str:
        insight = await database.insights.create(
            InsightCreate(
                space_id=space_id,
                trigger=f"场景 {applied}-{success}",
                guidance="做法",
                kind="heuristic",
                status="active",
                confidence=confidence,
                origin="manual",
                applied_count=applied,
                success_count=success,
            )
        )
        return insight.id

    bad = await seeded(0.5, applied=9, success=2)  # 22%
    borderline = await seeded(0.5, applied=5, success=3)  # 60%，不该入选
    too_few = await seeded(0.5, applied=3, success=0)  # 样本太小，不该入选

    response = await client.get(f"{API}/spaces/{space_id}/insights/review")
    assert response.status_code == 200
    body = response.json()

    assert [item["insight"]["id"] for item in body["items"]] == [bad]
    assert body["items"][0]["success_rate"] == pytest.approx(0.2222, abs=1e-3)
    assert "9 次" in body["items"][0]["reason"]
    assert body["min_applied"] == 5
    assert borderline not in {item["insight"]["id"] for item in body["items"]}
    assert too_few not in {item["insight"]["id"] for item in body["items"]}


async def test_trace_view_resolves_injected_insights(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """GET /traces/{id} 要把注入的经验按 id 补全，查不到的（已删）不编造。

    此前只回 id：证据栏刷新后把 ULID 当「条件」显示、对策兜底成一句套话、作用域全标「全局」。
    """
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    insight = (await _seed_insights(runtime, space_id))[0]
    trace_id = await _seed_trace(runtime, space_id, [insight.id, "01MISSINGINSIGHT0000000000"])

    body = (await client.get(f"{API}/traces/{trace_id}")).json()
    assert body["used_insights"] == [insight.id, "01MISSINGINSIGHT0000000000"]
    details = body["insight_details"]
    assert [item["id"] for item in details] == [insight.id]
    assert details[0]["trigger"] == insight.trigger
    assert details[0]["scope"] == insight.scope


async def test_conversation_detail_remembers_feedback(
    app_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    """会话详情要带上每条回答收到过的反馈，刷新后赞 / 纠错状态才不会丢。"""
    app, client = app_client
    runtime: Runtime = app.state.runtime
    space_id = await _new_space(runtime)
    trace_id = await _seed_trace(runtime, space_id, [])
    await _submit(client, trace_id, "up")
    await _submit(client, trace_id, "correction", "应先评估 ADMET")

    database = await runtime.space_db(space_id)
    trace = await database.traces.require(trace_id)
    detail = (await client.get(f"{API}/conversations/{trace.conversation_id}")).json()
    answer = next(m for m in detail["messages"] if m["role"] == "assistant")
    assert answer["trace_id"] == trace_id
    assert answer["feedback"] == ["up", "correction"]
