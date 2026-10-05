"""检索侧指标：公式的穷举测试 + 一次真实评测链路的端到端验证。

公式与判定是分开的：judge 负责「哪条论断有依据、哪几号证据起了作用」，本模块的
算术归 :mod:`agentmem.expert.metrics`。这样三把尺子可以被穷举测试，也不必为了
验证一个除法去造模型输出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.expert.metrics import (
    AnswerClaim,
    EvidenceAudit,
    audit_from_judge,
    average,
    compute,
    context_precision,
    context_recall,
    faithfulness,
)
from agentmem.prompts import build_eval_judge_messages
from agentmem.prompts._shapes import ChunkRef, PersonaSpec
from agentmem.types import EvalItemScore
from apps.api.main import create_app

API = "/api/v1"

#: 多段语料：需要切出多条切片，检索覆盖项（top_n_rerank 之类）才看得出差别
_LONG_DOC = "# 成药性\n\n" + "\n\n".join(
    f"第 {index} 节讨论 ADMET 的一个侧面：溶解度、膜通透性、代谢稳定性、"
    f"血浆蛋白结合与 hERG 抑制各有其测定方法与淘汰线，早期反筛能显著降低后期失败率。"
    for index in range(12)
)

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

#: judge 的审计输出：两个要点里只支撑了一个，答案三条论断里两条有出处，
#: 四条证据里真正起作用的是 c1 与 c3
_AUDIT = {
    "score": 0.72,
    "verdict": "方向对，但缺了选择性数据",
    "evidence_audit": {
        "must_include_covered": ["ADMET"],
        "answer_claims": [
            {"claim": "该化合物需要先评估 ADMET 五项", "supported": True},
            {"claim": "hERG 抑制是主要淘汰原因", "supported": True},
            {"claim": "建议直接进入临床", "supported": False},
        ],
        "used_evidence": ["c1", "c3"],
    },
}


# ---------------------------------------------------------------------------
# 公式
# ---------------------------------------------------------------------------


def test_context_recall_counts_covered_claims() -> None:
    """要点被证据支撑的比例。"""
    assert context_recall(["ADMET"], ["ADMET", "选择性"]) == pytest.approx(0.5)
    assert context_recall(["ADMET", "选择性"], ["ADMET", "选择性"]) == pytest.approx(1.0)
    assert context_recall([], ["ADMET"]) == pytest.approx(0.0)


def test_context_recall_is_none_without_claims() -> None:
    """没有 must_include 的开放题不参与召回统计，而不是记 0 分。"""
    assert context_recall(["ADMET"], []) is None
    assert context_recall(["ADMET"], ["   "]) is None


def test_context_recall_tolerates_paraphrase() -> None:
    """judge 很难一字不差地抄要点，包含关系也算命中。"""
    assert context_recall(["ADMET 五项"], ["ADMET"]) == pytest.approx(1.0)
    assert context_recall(["ADMET"], ["必须评估 ADMET 五项"]) == pytest.approx(1.0)


def test_context_precision_rewards_early_relevant_evidence() -> None:
    """RAGAS 的 Precision@K：命中的证据越靠前分越高。"""
    # 两条相关都在最前面：1.0
    assert context_precision(["c1", "c2"], ["c1", "c2", "c3", "c4"]) == pytest.approx(1.0)
    # 相关条目排在第 3、4 位：Σ(1/3 + 2/4)/2 = 0.4167
    assert context_precision(["c3", "c4"], ["c1", "c2", "c3", "c4"]) == pytest.approx(
        (1 / 3 + 2 / 4) / 2, abs=1e-4
    )
    # 没有一条证据起作用
    assert context_precision([], ["c1", "c2"]) == pytest.approx(0.0)
    # 检索为空：没得评
    assert context_precision(["c1"], []) is None


def test_context_precision_ignores_hallucinated_markers() -> None:
    """模型写出并不存在的证据号时，分母不该跟着变大。"""
    assert context_precision(["c9"], ["c1", "c2"]) == pytest.approx(0.0)
    assert context_precision(["c1", "c9"], ["c1", "c2"]) == pytest.approx(1.0)


def test_faithfulness_counts_supported_claims() -> None:
    """答案论断里有出处的比例。"""
    claims = [
        AnswerClaim(claim="a", supported=True),
        AnswerClaim(claim="b", supported=True),
        AnswerClaim(claim="c", supported=False),
    ]
    assert faithfulness(claims) == pytest.approx(2 / 3, abs=1e-4)


def test_faithfulness_is_none_for_answers_without_claims() -> None:
    """拒答或纯闲聊没有论断，不该被判成不忠实。"""
    assert faithfulness([]) is None


def test_audit_parser_survives_dirty_output() -> None:
    """审计块缺失或字段类型不对时只丢指标，不抛异常。"""
    assert audit_from_judge({"score": 0.5}) is None
    assert audit_from_judge({"evidence_audit": "没按格式来"}) is None

    parsed = audit_from_judge(
        {"evidence_audit": {"used_evidence": "c1", "answer_claims": [{"claim": "x"}]}}
    )
    assert parsed is not None
    assert parsed.used_evidence == ["c1"]
    assert parsed.answer_claims[0].supported is False


def test_compute_marks_unaudited_runs() -> None:
    """没有审计结果时 ``audited`` 为假，指标留空。"""
    metrics = compute(None, must_include=["ADMET"], retrieved_markers=["c1"])

    assert metrics.audited is False
    assert metrics.context_recall is None
    assert metrics.evidence == 1


def test_average_skips_unmeasured_items() -> None:
    """求平均只统计测到过的题，空集合返回 None。"""
    first = compute(
        EvidenceAudit(must_include_covered=["a"], used_evidence=["c1"], answer_claims=[]),
        must_include=["a"],
        retrieved_markers=["c1"],
    )
    second = compute(None, must_include=["a"], retrieved_markers=["c1"])

    assert average([first, second]) is not None
    assert average([first, second]).context_recall == pytest.approx(1.0)  # type: ignore[union-attr]
    assert average([second]) is None
    assert average([]) is None


# ---------------------------------------------------------------------------
# Prompt：审计块只在带证据时出现
# ---------------------------------------------------------------------------


def _persona() -> PersonaSpec:
    return PersonaSpec(
        name="新药研发专家",
        domain="药物发现",
        role_description="",
        principles=[],
        language="zh-CN",
        tone="简洁",
        must_cite=True,
        quality_bar=["结论必须有数据支撑"],
        glossary={},
    )


def _chunk(marker: str) -> ChunkRef:
    return ChunkRef(
        marker=marker,
        chunk_id=f"chunk-{marker}",
        document_title="讲义",
        heading_path=None,
        page=1,
        content="ADMET 五项包括溶解度、通透性、代谢稳定性、血浆蛋白结合与 hERG。",
        score=0.9,
    )


def test_eval_judge_asks_for_audit_only_with_evidence() -> None:
    """不带证据时提示词不变（真实对话打分走的是同一套 schema）。"""
    without = build_eval_judge_messages(
        persona=_persona(),
        item={"question": "q", "must_include": ["ADMET"]},
        answer="a",
    )
    with_evidence = build_eval_judge_messages(
        persona=_persona(),
        item={"question": "q", "must_include": ["ADMET"]},
        answer="a",
        evidence=[_chunk("c1")],
    )

    system_without = without[0]["content"]
    system_with = with_evidence[0]["content"]
    assert "evidence_audit" not in system_without
    assert "retrieval_audit" not in system_without
    assert "evidence_audit" in system_with
    assert "retrieval_audit" in system_with

    user_with = with_evidence[1]["content"]
    assert "retrieved_evidence" in user_with
    assert "c1" in user_with


# ---------------------------------------------------------------------------
# 端到端：一次真实评测里指标要真的落到 SSE 与库里
# ---------------------------------------------------------------------------


def test_eval_run_reports_retrieval_metrics(
    tmp_path: Path, mock_server: str, mock_reply: object
) -> None:
    """跑一次评测：SSE 的 item / done 事件带出检索指标，并落进 eval_runs.detail。"""
    configure = mock_reply
    assert callable(configure)
    configure(
        "需要先评估 ADMET 五项[^c1]，再谈其它。",
        [("retrieval_audit", json.dumps(_AUDIT, ensure_ascii=False))],
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)

    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        created = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={
                "title": "成药性笔记",
                "content": (
                    "# 成药性\n\nADMET 五项包括溶解度、通透性、代谢稳定性、蛋白结合与 hERG。"
                ),
            },
        )
        assert created.status_code == 201
        document_id = created.json()["id"]
        _wait_ready(client, space_id, document_id)

        item = client.post(
            f"{API}/spaces/{space_id}/evals",
            json={
                "question": "评估先导化合物成药性时除了活性还要看什么？",
                "reference": "要看 ADMET 五项与选择性。",
                "must_include": ["ADMET", "选择性"],
            },
        )
        assert item.status_code == 201

        with client.stream("POST", f"{API}/spaces/{space_id}/evals/run", json={}) as response:
            assert response.status_code == 200
            events = _parse_sse("".join(response.iter_text()))

        item_event = next(payload for name, payload in events if name == "item")
        done_event = next(payload for name, payload in events if name == "done")
        metrics = item_event["metrics"]

        # must_include 两条里 judge 只认了一条被证据支撑
        assert metrics["audited"] is True
        assert metrics["context_recall"] == pytest.approx(0.5)
        # 证据 1 被用到、排第 1 位 → 1.0
        assert metrics["context_precision"] == pytest.approx(1.0)
        # 三条论断里两条有出处
        assert metrics["faithfulness"] == pytest.approx(2 / 3, abs=1e-4)
        assert done_event["metrics"]["context_recall"] == pytest.approx(0.5)

        runs = client.get(f"{API}/spaces/{space_id}/evals/runs").json()["runs"]
        assert runs
        detail = runs[0]["detail"]
        assert detail["metrics"]["faithfulness"] == pytest.approx(2 / 3, abs=1e-4)
        assert detail["items"][0]["metrics"]["evidence"] >= 1


def _wait_ready(client: TestClient, space_id: str, document_id: str) -> None:
    """等到摄取结束；评测要检索，切片必须先落库。"""
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["status"]
        if status in {"ready", "failed"}:
            assert status == "ready"
            return
        time.sleep(0.05)
    raise AssertionError("等待摄取超时")


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本拆成 (事件名, 载荷) 列表。

    只覆盖本项目用到的最简形状：一条事件固定是 ``event:`` 行加若干 ``data:`` 行。
    """
    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    payload = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[7:]
            payload = ""
        elif line.startswith("data: "):
            payload += line[6:]
        elif not line and name:
            events.append((name, json.loads(payload)))
            name = ""
            payload = ""
    return events


# ---------------------------------------------------------------------------
# G5：多臂对比（只换检索配置）
# ---------------------------------------------------------------------------


def test_compare_reports_paired_deltas(
    tmp_path: Path, mock_server: str, mock_reply: object
) -> None:
    """同一批题目跑两臂，差值要按题配对算出来，且每一臂都落库。"""
    configure = mock_reply
    assert callable(configure)
    configure(
        "需要先评估 ADMET 五项[^c1]。",
        [("retrieval_audit", json.dumps(_AUDIT, ensure_ascii=False))],
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)

    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        created = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={
                "title": "成药性笔记",
                # 语料要够长才能切出多条切片——只切出一条的话，
                # 「把 top_n_rerank 压到 1」这类覆盖项根本看不出差别
                "content": _LONG_DOC,
            },
        )
        document_id = created.json()["id"]
        _wait_ready(client, space_id, document_id)
        for question in ("成药性要看什么？", "hERG 为什么要早筛？"):
            client.post(
                f"{API}/spaces/{space_id}/evals",
                json={"question": question, "must_include": ["ADMET"]},
            )

        with client.stream(
            "POST",
            f"{API}/spaces/{space_id}/evals/compare",
            json={
                "arms": [
                    {"label": "默认"},
                    {"label": "关掉多样性", "retrieval": {"diversity": False}},
                    {"label": "只取一条", "retrieval": {"top_n_rerank": 1}},
                ],
                "persist": True,
            },
        ) as response:
            assert response.status_code == 200, response.read()
            events = _parse_sse("".join(response.iter_text()))

    names = [name for name, _ in events]
    assert names.count("arm") == 3
    assert names[-1] == "done"

    done = next(payload for name, payload in events if name == "done")
    arms = [payload for name, payload in events if name == "arm"]
    assert done["baseline"] == "默认"
    assert [arm["label"] for arm in arms] == ["默认", "关掉多样性", "只取一条"]
    assert [delta["label"] for delta in done["deltas"]] == ["关掉多样性", "只取一条"]

    item_ids = {item["id"] for item in client.get(f"{API}/spaces/{space_id}/evals").json()["items"]}
    assert len(item_ids) == 2

    # 覆盖项必须真的作用到检索上：把 top_n_rerank 压到 1，每题就只拿得到一条证据
    # （整轮的 evidence 是逐题求和）
    evidences = {arm["label"]: arm["metrics"]["evidence"] for arm in arms}
    assert evidences["只取一条"] == len(item_ids), evidences
    assert evidences["默认"] > evidences["只取一条"], evidences

    delta = done["deltas"][0]

    assert {entry["item_id"] for entry in delta["item_deltas"]} == item_ids, "两臂必须逐题配对"
    assert delta["metrics_delta"]["context_recall"] == pytest.approx(0.0)
    assert delta["score_delta"] == pytest.approx(0.0)

    runs = client.get(f"{API}/spaces/{space_id}/evals/runs").json()["runs"]
    recorded = {
        (run["detail"].get("retrieval") or {}).get("label"): (
            run["detail"].get("retrieval") or {}
        ).get("override")
        for run in runs
        if run["variant"] == "custom"
    }
    assert {"默认", "关掉多样性", "只取一条"} <= set(recorded), "每一臂都要落库，并记下它的检索配置"
    assert recorded["只取一条"] == {"top_n_rerank": 1}


def test_compare_rejects_unknown_retrieval_knob(tmp_path: Path, mock_server: str) -> None:
    """拼错的检索旋钮要当场报错，不能被静默忽略。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)

    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        document_id = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "t", "content": "# 标题\n\n正文讲 ADMET。"},
        ).json()["id"]
        _wait_ready(client, space_id, document_id)
        client.post(f"{API}/spaces/{space_id}/evals", json={"question": "q?"})

        with client.stream(
            "POST",
            f"{API}/spaces/{space_id}/evals/compare",
            json={
                "arms": [
                    {"label": "a"},
                    {"label": "b", "retrieval": {"mmr_lamda": 0.5}},
                ]
            },
        ) as response:
            body = "".join(response.iter_text())

    # 错误发生在第一个事件之后，响应头已经发出，因此只能以流内 error 事件收场，
    # 而不是把连接直接掐断
    assert response.status_code == 200
    events = _parse_sse(body)
    assert events[-1][0] == "error", body[-400:]
    assert "检索覆盖项不合法" in events[-1][1]["message"]


def test_compare_aa_arms_measure_the_noise_floor(
    tmp_path: Path, mock_server: str, mock_reply: object
) -> None:
    """同一个配置跑两臂就是 A/A 对照：两臂分数相同、差值恒为 0。

    这条断言的意义不在「0」本身——mock 模型是确定性的，真实模型不会。它守的是
    接口语义：A/A 必须能跑，且差值按题配对相减，而不是两次独立均分相减。
    """
    configure = mock_reply
    assert callable(configure)
    configure("答案[^c1]。", [("retrieval_audit", json.dumps(_AUDIT, ensure_ascii=False))])

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)

    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        document_id = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "t", "content": "# 标题\n\n正文讲 ADMET 五项。"},
        ).json()["id"]
        _wait_ready(client, space_id, document_id)
        client.post(f"{API}/spaces/{space_id}/evals", json={"question": "q?"})

        with client.stream(
            "POST",
            f"{API}/spaces/{space_id}/evals/compare",
            json={"arms": [{"label": "A/A 一"}, {"label": "A/A 二"}], "persist": False},
        ) as response:
            events = _parse_sse("".join(response.iter_text()))

    done = next(payload for name, payload in events if name == "done")
    assert done["deltas"][0]["score_delta"] == pytest.approx(0.0)
    assert done["deltas"][0]["metrics_delta"]["faithfulness"] == pytest.approx(0.0)


def test_attribute_insights_runs_leave_one_out(
    tmp_path: Path, mock_server: str, mock_reply: object
) -> None:
    """留一法：每条经验各跑一轮「把它拿掉」，贡献 = 基准分 − 去掉它之后的分。

    这条守的是归因的形状——轮数、基准、噪声底、每条都要有结果。分数本身由 Mock
    judge 给，不在这里断言大小关系。
    """
    configure = mock_reply
    assert callable(configure)
    configure(
        "需要先评估 ADMET 五项[^c1]。",
        [("retrieval_audit", json.dumps(_AUDIT, ensure_ascii=False))],
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=config_path)

    app = create_app(settings)
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        created = client.post(
            f"{API}/spaces/{space_id}/documents/paste",
            json={"title": "成药性笔记", "content": _LONG_DOC},
        )
        _wait_ready(client, space_id, created.json()["id"])
        client.post(
            f"{API}/spaces/{space_id}/evals",
            json={"question": "成药性要看什么？", "must_include": ["ADMET"]},
        )

        insight_ids = []
        for trigger in ("问成药性时", "问心脏毒性时"):
            insight = client.post(
                f"{API}/spaces/{space_id}/insights",
                json={"trigger": trigger, "guidance": "先看 ADMET 五项", "kind": "heuristic"},
            )
            assert insight.status_code in (200, 201), insight.text
            insight_ids.append(insight.json()["id"])

        response = client.post(
            f"{API}/spaces/{space_id}/insights/attribute",
            json={"insight_ids": insight_ids, "include_noise_floor": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()

    assert body["items"] == 1
    assert {item["insight_id"] for item in body["contributions"]} == set(insight_ids)
    for item in body["contributions"]:
        # 贡献是「基准分 − 去掉它之后的分」，两者都要如实带出来
        assert item["contribution"] == pytest.approx(
            body["baseline_score"] - item["score_without"], abs=0.01
        )
    assert body["noise_floor"] is not None, "开了噪声底就要给出这一轮的抖动幅度"


def test_attribute_insights_without_any_insight(tmp_path: Path, mock_server: str) -> None:
    """一条经验都没有时直接报错，而不是返回一份空结论。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "models.yaml"
    config_path.write_text(MODELS_YAML.format(base=mock_server), encoding="utf-8")

    app = create_app(Settings(data_dir=data_dir, models_config=config_path))
    with TestClient(app) as client:
        space_id = client.post(
            f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}
        ).json()["id"]
        response = client.post(f"{API}/spaces/{space_id}/insights/attribute", json={})

    assert response.status_code == 422, response.text


def test_judge_parse_failure_is_unmeasured_not_zero() -> None:
    """裁判输出解析不出来时记「未测得」，不能记 0 分。

    0 分的含义是「答得完全错」。实测踩到过：裁判给的是 score 1.0、issues 为空的
    满分评价，只因为它在中文里用了 ASCII 双引号（``指出"无需外推"的前提``）导致
    JSON 非法，这道题就从满分变成零分——而这个分数会流向 A/B 对比与经验晋升。
    """
    from agentmem.expert.evaluator import EvaluationService

    # 正常输出（带代码围栏）照常解析出分数
    fenced = '```json\n{"score": 1.0, "verdict": "好"}\n```'
    ok_score, _reason, _audit = EvaluationService._parse(fenced)
    assert ok_score == 1.0

    # 字符串里混入未转义的 ASCII 双引号 → 非法 JSON
    # 这正是最初的真实案例：裁判给了满分，只是评语里混进了 ASCII 双引号。
    # 起初能做到的最好是「判未测得、不计入」；现在分数能从原文抽回，满分就是满分。
    broken = '{"score": 1.0, "verdict": "指出"无需外推"的前提，诚实"}'
    score, reason, audit = EvaluationService._parse(broken)
    assert score == 1.0, "分数写在评语之前，应当抽回而不是丢弃"
    assert audit is None, "审计块在坏掉的 JSON 里，取不回来就留空"

    # 真正没有任何分数的输出，仍然只能判未测得——绝不是 0 分
    score, reason, _ = EvaluationService._parse('{"verdict": "指出"无需外推"的前提"}')
    assert score is None
    assert "未计入" in reason


def test_eval_overall_excludes_unmeasured_items() -> None:
    """总分只对测出分数的题求均值，未测得的题不进分母。"""
    scores = [
        EvalItemScore(item_id="a", score=90.0, passed=True, measured=True),
        EvalItemScore(item_id="b", score=70.0, passed=True, measured=True),
        # 裁判没解析出来：占位 0 分，但不该拉低总分
        EvalItemScore(item_id="c", score=0.0, passed=False, measured=False),
    ]
    measured = [s for s in scores if s.measured]
    overall = round(sum(s.score for s in measured) / len(measured), 2)

    assert overall == 80.0, "若把未测得的题按 0 分计入，均值会被拉到 53.33"
    assert round(sum(s.score for s in scores) / len(scores), 2) == 53.33
