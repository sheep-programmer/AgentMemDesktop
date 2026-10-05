"""端到端冒烟：把产品的主路径完整走一遍。

存在的理由：单点测试覆盖不到「端口之间的契约」——SSE 事件的形状、
轨迹与经验的引用关系、评测事件里的字段名。曾经有一个 bug 让所有用字典发事件的
SSE 路由在第一个事件就断流，而每个服务自己的单元测试都是绿的：问题只在整个链路
串起来时才会现形。所以这里不追求覆盖分支，只求**把用户真正走的那条路走通**：

    建 Space → 投喂文档 → 提问（带引用的流式回答）→ 提交反馈
    → 自动出题 → 跑评测（拿到检索指标）→ 一键进化
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
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

#: 一次评测的审计输出：两条论断有出处、一条没有
_AUDIT = {
    "score": 0.7,
    "verdict": "方向正确，细节略缺",
    "evidence_audit": {
        "must_include_covered": ["ADMET"],
        "answer_claims": [
            {"claim": "先导化合物需评估 ADMET", "supported": True},
            {"claim": "hERG 抑制是常见淘汰原因", "supported": False},
        ],
        "used_evidence": ["c1"],
    },
}

_EVALGEN = json.dumps(
    {
        "items": [
            {
                "question": "评估先导化合物成药性时除了靶点活性还要看什么？",
                "reference": "要看 ADMET 五项与靶点选择性。",
                "must_include": ["ADMET"],
                "difficulty": "medium",
                "tags": ["成药性"],
                "rationale": "考察是否理解成药性的多维评估",
            }
        ]
    },
    ensure_ascii=False,
)

_DOC = "# 成药性\n\n" + "\n\n".join(
    f"第 {index} 节讨论 ADMET 的一个侧面：溶解度、膜通透性、代谢稳定性、"
    f"血浆蛋白结合与 hERG 抑制各有其测定方法与淘汰线，早期反筛能显著降低后期失败率。"
    for index in range(12)
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


def _parse_sse(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    name = ""
    data = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name, data = line[7:], ""
        elif line.startswith("data: "):
            data += line[6:]
        elif not line and name:
            events.append((name, json.loads(data)))
            name, data = "", ""
    return events


def _names(events: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in events]


def _payload(events: list[tuple[str, Any]], name: str) -> Any:
    return next(payload for event, payload in events if event == name)


def test_full_product_path(client: TestClient, mock_reply: Any) -> None:
    """建库 → 投喂 → 问答 → 反馈 → 出题 → 评测 → 进化，一条路走到底。"""
    mock_reply(
        "先导化合物需要先评估 ADMET 五项[^c1]。",
        [
            ("出题规则", _EVALGEN),
            ("retrieval_audit", json.dumps(_AUDIT, ensure_ascii=False)),
            ("feedback_episodes", json.dumps({"insights": [], "skipped": []}, ensure_ascii=False)),
        ],
    )

    # ① 建 Space 并投喂文档
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    created = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "成药性笔记", "content": _DOC},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]
    _wait_ready(client, space_id, document_id)

    chunks = client.get(f"{API}/spaces/{space_id}/documents/{document_id}/chunks?limit=500").json()[
        "items"
    ]
    assert len(chunks) > 1, "语料要能切出多条切片，后面的引用才有意义"

    # ② 提问：流式回答里要有检索、引用，最后落到轨迹
    # 路径与请求体都带 space_id：路由会校验两者一致，防止会话建到别的 Space
    conversation_id = client.post(
        f"{API}/spaces/{space_id}/conversations",
        json={"space_id": space_id, "title": "成药性讨论"},
    ).json()["id"]
    chat_events = _stream(
        client, f"{API}/conversations/{conversation_id}/chat", {"content": "成药性要看什么？"}
    )
    assert set(_names(chat_events)) >= {"trace_start", "retrieval", "insights", "delta", "done"}

    citation = _payload(chat_events, "citation")
    assert citation["chunk_id"] in {chunk["id"] for chunk in chunks}, "引用必须指向真实切片"

    done = _payload(chat_events, "done")
    assert done["usage"]["prompt_tokens"] > 0, "用量要如实回传（前端轨迹面板依赖它）"
    trace_response = client.get(f"{API}/traces/{done['trace_id']}")
    assert trace_response.status_code == 200, trace_response.text
    trace = trace_response.json()
    assert trace["retrieved"], "轨迹里要有检索明细"
    assert trace["message_id"] == done["message_id"]

    # ③ 反馈：落库即可（置信度回流已由 tests/test_feedback_loop.py 覆盖）
    feedback = client.post(
        f"{API}/traces/{done['trace_id']}/feedback",
        json={"kind": "down", "comment": "没有提到选择性数据"},
    )
    assert feedback.status_code == 201
    assert client.get(f"{API}/spaces/{space_id}/evolve/pending").json()["pending_count"] >= 1

    # ④ 自动出题：SSE 要真的把题目推出来，并且落库
    generated = _stream(client, f"{API}/spaces/{space_id}/evals/generate", {})
    assert "item" in _names(generated)
    items = client.get(f"{API}/spaces/{space_id}/evals").json()["items"]
    assert items, "自动出题没有落库"

    # ⑤ 评测：分数与检索指标都要出现
    evaluation = _stream(client, f"{API}/spaces/{space_id}/evals/run", {})
    run_done = _payload(evaluation, "done")
    assert run_done["score"] > 0
    # 自动出的题只有一个 must_include（ADMET），审计说它被证据支撑 → 召回 1.0
    assert run_done["metrics"]["context_recall"] == pytest.approx(1.0)
    item_event = _payload(evaluation, "item")
    assert item_event["metrics"]["audited"] is True
    assert item_event["metrics"]["faithfulness"] == pytest.approx(0.5)

    # ⑥ 一键进化：全流程必须能跑完并给出 done
    cycle = _stream(client, f"{API}/spaces/{space_id}/evolve/cycle", {})
    assert _names(cycle)[-1] == "done"
    assert "stage" in _names(cycle)

    # 收尾：专家度可算，且引用率不再是「检索到过就算满分」
    score = client.get(f"{API}/spaces/{space_id}/expertise").json()
    assert 0 <= score["overall"] <= 100
    assert score["groundedness"] > 0, "回答里有引用，引用句占比不该是 0"


def _stream(client: TestClient, path: str, payload: dict[str, Any]) -> list[tuple[str, Any]]:
    body = ""
    with client.stream("POST", path, json=payload) as response:
        assert response.status_code == 200, response.read()
        body = "".join(response.iter_text())
    assert "event: error" not in body, body[-500:]
    return _parse_sse(body)


def _wait_ready(client: TestClient, space_id: str, document_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()
        if detail["status"] in {"ready", "failed"}:
            assert detail["status"] == "ready", detail.get("error")
            return
        time.sleep(0.05)
    raise AssertionError("等待摄取超时")
