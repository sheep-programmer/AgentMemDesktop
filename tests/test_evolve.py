"""进化闭环端到端测试。

用 mock 模型验证「反馈 → 蒸馏 → 候选经验 → 人工确认 → 进召回池」这条主链路
真的能跑通，以及关键的可证伪不变量（无测验集不自动转正、置信度按规则升降）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.evolve import CritiqueService, distill_feedback
from agentmem.evolve.cycle import CycleResult
from agentmem.space.runtime import Runtime
from agentmem.types import (
    ConversationCreate,
    FeedbackCreate,
    MessageCreate,
    Persona,
    SpaceCreate,
    TraceCreate,
)
from apps.api.main import create_app
from conftest import app_runtime

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

# 蒸馏 Prompt 期望的 JSON 产出：一条纠错型经验
_DISTILL_JSON = json.dumps(
    {
        "insights": [
            {
                "trigger": "用户询问某先导化合物的成药性时",
                "guidance": "先评估 ADMET 再给结论，不要只看靶点活性",
                "rationale": "一次用户纠正：通用脚本对 VMP 保护无效",
                "kind": "correction",
                "source_trace_ids": [],
                "confidence_hint": 0.6,
                "supersedes_hint": None,
            }
        ],
        "skipped": [],
    },
    ensure_ascii=False,
)


@pytest.fixture
def settings(tmp_path: Path, mock_server: str) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    return Settings(data_dir=data_dir, models_config=config_path)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


async def _seed_trace_with_correction(runtime: Runtime, space_id: str) -> str:
    """造一条带纠错反馈的轨迹，作为蒸馏原料。返回 feedback_id。"""
    database = await runtime.space_db(space_id)
    conversation = await database.conversations.create(
        ConversationCreate(space_id=space_id, title="成药性讨论")
    )
    message = await database.messages.create(
        MessageCreate(
            conversation_id=conversation.id,
            role="assistant",
            content="这个化合物活性高，直接推进即可",
        )
    )
    trace = await database.traces.create(
        TraceCreate(
            space_id=space_id,
            conversation_id=conversation.id,
            message_id=message.id,
            query="这个先导化合物成药性如何？",
        )
    )
    feedback = await database.feedback.create(
        FeedbackCreate(
            trace_id=trace.id,
            kind="correction",
            comment="该化合物 hERG 抑制强，不能只看活性，应先评估 ADMET",
        )
    )
    return feedback.id


def test_evolve_pending_counts_feedback(client: TestClient) -> None:
    """提交反馈后，待蒸馏计数应 +1。"""
    from anyio.from_thread import start_blocking_portal

    runtime = app_runtime(client)
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "Android"}).json()[
        "id"
    ]

    before = client.get(f"{API}/spaces/{space_id}/evolve/pending").json()["pending_count"]

    with start_blocking_portal() as portal:
        portal.call(_seed_trace_with_correction, runtime, space_id)

    after = client.get(f"{API}/spaces/{space_id}/evolve/pending").json()
    assert after["pending_count"] == before + 1
    assert after["by_kind"] == {"correction": 1}, "按种类的准确计数，不从预览里数"
    assert len(after["preview"]) >= 1


async def test_distill_produces_candidate_and_marks_processed(
    settings: Settings, mock_reply: object
) -> None:
    """蒸馏：把纠错反馈变成候选经验，且反馈被标记为已处理（不会重复蒸馏）。"""
    # 让 distill 角色的调用返回预设 JSON（distill Prompt 里含「经验萃取」字样）
    mock_reply(_DISTILL_JSON)  # type: ignore[operator]

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space = await runtime.spaces.create_space(SpaceCreate(name="新药研发", domain="药物发现"))
        await _seed_trace_with_correction(runtime, space.id)

        database = await runtime.space_db(space.id)
        registry = await runtime.registry_for_space(space.id)

        outcome = await distill_feedback(
            database=database,
            registry=registry,
            persona=Persona(name="新药研发专家", domain="药物发现"),
            space_id=space.id,
        )

        assert len(outcome.candidates) == 1
        candidate = outcome.candidates[0]
        assert candidate.status == "candidate"
        assert candidate.confidence == 0.3  # 入库用规则初始值，不采信模型的 hint
        assert candidate.kind == "correction"
        # 候选是可召回状态，要进向量池：此前蒸馏直接写表、不向量化，只要池里已有别的
        # 经验，召回就只认向量，自动进化出来的经验永远用不上
        vectors = await database.require_vectors()
        assert await vectors.count("insights_vec", space.id) == 1

        # 反馈已标记处理：再次蒸馏应无新产出
        again = await distill_feedback(
            database=database,
            registry=registry,
            persona=Persona(name="新药研发专家", domain="药物发现"),
            space_id=space.id,
        )
        assert again.candidates == []


async def test_judge_records_score(settings: Settings, mock_reply: object) -> None:
    """LLM-as-Judge 给回答打分并落库成反馈。"""
    mock_reply(json.dumps({"score": 0.4, "verdict": "把两个厂商混淆了"}, ensure_ascii=False))  # type: ignore[operator]

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space = await runtime.spaces.create_space(SpaceCreate(name="新药研发", domain="药物发现"))
        feedback_id = await _seed_trace_with_correction(runtime, space.id)
        database = await runtime.space_db(space.id)
        feedback = await database.feedback.get(feedback_id)
        assert feedback is not None

        service: CritiqueService = await runtime.critique_service(space.id)
        result = await service.judge_trace(feedback.trace_id)

        assert result.score == pytest.approx(0.4)
        # 已有的纠错反馈应被补上 judge_score，而不是新建一条
        updated = await database.feedback.get(feedback_id)
        assert updated is not None
        assert updated.judge_score == pytest.approx(0.4)


async def test_cycle_writes_an_honest_history_entry(settings: Settings, mock_reply: Any) -> None:
    """跑一次进化要留下「新增/晋升/淘汰了多少条」，而不是拿评测分数当收益。

    此前 `/evolve/history` 用评测记录近似：with_insights 那一次的分数（71.5 之类的
    绝对分）被当成 eval_delta 返回，而这一轮真正的产出算完就丢了。
    """
    mock_reply(
        "结论[^c1]。",
        [("feedback_episodes", json.dumps({"insights": [], "skipped": []}, ensure_ascii=False))],
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space = await runtime.spaces.create_space(SpaceCreate(name="新药研发", domain="新药研发"))
        database = await runtime.space_db(space.id)
        await _seed_trace_with_correction(runtime, space.id)

        service = await runtime.evolution_service(space.id)
        async for _item in service.run_cycle(space.id):
            pass

        runs = await database.evolution.list_by_space(space.id)
        assert len(runs) == 1
        run = runs[0]
        assert run.produced == 0, "这次反馈蒸馏不出东西，如实记 0 而不是编一个数"
        assert run.merged == 0
        assert run.eval_delta is None, "没跑评测就没有 delta，不能拿分数顶替"
        assert run.expertise_after >= 0

    with TestClient(app) as client:
        history = client.get(f"{API}/spaces/{space.id}/evolve/history").json()["items"]
    assert len(history) == 1
    assert history[0]["produced"] == 0
    assert history[0]["eval_delta"] is None


async def test_cycle_without_evals_reports_pending_candidates(
    settings: Settings, mock_reply: Any
) -> None:
    """没有测验题时候选不会生效，结果里要报出还有几条候选待验证。

    否则用户点完「开始进化」再去提问，发现系统「没学会」，会以为功能坏了。
    """
    mock_reply(_DISTILL_JSON)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        space = await runtime.spaces.create_space(SpaceCreate(name="新药研发", domain="新药研发"))
        await _seed_trace_with_correction(runtime, space.id)

        service = await runtime.evolution_service(space.id)
        final = None
        async for item in service.run_cycle(space.id):
            final = item

        assert isinstance(final, CycleResult)
        assert final.produced == 1
        assert final.eval_delta is None, "没有测验题就不评测"
        assert final.promoted == 0, "没有裁判，候选不能自动转正"
        assert final.pending_candidates == 1
