"""一致性度量：同问多答的稳定性。

雷达图上的「逻辑一致性」此前是个代理指标（active 经验占比），与它的标签说的不是
一回事。这里守真口径：重复提问 → 比较答案语义 → 取平均相似度；以及「没测过 /
测得太久」时如实退回代理指标并标明来源，而不是拿一个假数字冒充实测。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from agentmem.expert.consistency import cosine_similarity, mean_pairwise_similarity
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


# ---------------------------------------------------------------------------
# 纯函数：相似度与两两平均
# ---------------------------------------------------------------------------


def test_cosine_similarity_bounds() -> None:
    """方向相同为 1，正交为 0，相反夹到 0（一致性只问「像不像」）。"""
    assert cosine_similarity([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(0.0)


def test_cosine_similarity_rejects_mismatched_dimensions() -> None:
    from agentmem.errors import ValidationError

    with pytest.raises(ValidationError, match="维度"):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_mean_pairwise_similarity() -> None:
    """三个向量取三对，平均；少于两个没有「两两」可言。"""
    same = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    mixed = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    assert mean_pairwise_similarity(same) == pytest.approx(1.0)
    assert mean_pairwise_similarity(mixed) == pytest.approx(1 / 3, abs=1e-4)
    assert mean_pairwise_similarity([[1.0, 0.0]]) == pytest.approx(0.0)
    assert mean_pairwise_similarity([]) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 端到端：探测落库、专家度优先用实测
# ---------------------------------------------------------------------------


def _seed_history(client: TestClient) -> tuple[str, str]:
    """建库、投喂一篇文档并问两次同一个问题（一致性的输入是真实问过的问题）。"""
    space_id = client.post(f"{API}/spaces", json={"name": "新药研发", "domain": "药物发现"}).json()[
        "id"
    ]
    document_id = client.post(
        f"{API}/spaces/{space_id}/documents/paste",
        json={"title": "成药性笔记", "content": "# 成药性\n\n先导化合物需要评估 ADMET 五项。"},
    ).json()["id"]
    detail = ""
    for _ in range(60):
        detail = client.get(f"{API}/spaces/{space_id}/documents/{document_id}").json()["status"]
        if detail in {"ready", "failed"}:
            break
        import time

        time.sleep(0.1)
    assert detail == "ready"

    conversation = client.post(
        f"{API}/spaces/{space_id}/conversations",
        json={"space_id": space_id, "title": "讨论"},
    ).json()["id"]
    for _ in range(2):
        with client.stream(
            "POST",
            f"{API}/conversations/{conversation}/chat",
            json={"content": "成药性要先看什么？"},
        ) as response:
            "".join(response.iter_text())
    return space_id, conversation


def test_probe_records_measured_similarity(client: TestClient) -> None:
    """探测落库，并把实测值带进专家度（来源标成 probe）。"""
    space_id, _conversation = _seed_history(client)

    before = client.get(f"{API}/spaces/{space_id}/expertise").json()
    assert before["consistency_source"] == "proxy", "没测过时如实标成代理指标"

    measured = client.post(f"{API}/spaces/{space_id}/expertise/consistency")
    assert measured.status_code == 200, measured.text
    probe = measured.json()

    assert probe["questions"] >= 1
    assert probe["repeats"] >= 2
    assert 0.0 <= probe["similarity"] <= 1.0
    assert probe["detail"], "逐题明细要留下，人工才能核对模型到底飘没飘"
    assert all(item["answers"] for item in probe["detail"])

    after = client.get(f"{API}/spaces/{space_id}/expertise").json()
    assert after["consistency_source"] == "probe"
    assert after["consistency"] == pytest.approx(probe["similarity"] * 100, abs=0.01)
    assert after["consistency_measured_at"] is not None


def test_probe_refuses_a_single_repeat(client: TestClient) -> None:
    """只问一遍测不出稳定性：直接拒绝，而不是返回一个恒为 1 的假数字。"""
    space_id, _conversation = _seed_history(client)

    rejected = client.post(f"{API}/spaces/{space_id}/expertise/consistency?repeats=1")

    assert rejected.status_code == 422


def test_probe_without_history_is_rejected(client: TestClient) -> None:
    """一个问题都没问过时，明确报错而不是编一个分数。"""
    space_id = client.post(f"{API}/spaces", json={"name": "空库", "domain": "药物发现"}).json()[
        "id"
    ]

    response = client.post(f"{API}/spaces/{space_id}/expertise/consistency")

    assert response.status_code == 422
    assert "还没有问过任何问题" in response.text


def test_stale_probe_falls_back_to_proxy(client: TestClient) -> None:
    """超过保鲜期的实测值不再代表今天，退回代理指标并标明来源。"""

    from agentmem.expert.expertise import PROBE_TTL_DAYS

    space_id, _conversation = _seed_history(client)
    client.post(f"{API}/spaces/{space_id}/expertise/consistency")
    assert client.get(f"{API}/spaces/{space_id}/expertise").json()["consistency_source"] == "probe"

    runtime = app_runtime(client)
    stale = PROBE_TTL_DAYS * 24 * 60 * 60 * 1000 + 60_000

    async def age_it() -> None:
        database = await runtime.space_db(space_id)
        await database.sqlite.execute(
            "UPDATE consistency_probes SET created_at = created_at - ?", (stale,)
        )

    with __import__("anyio").from_thread.start_blocking_portal() as portal:
        portal.call(age_it)

    assert client.get(f"{API}/spaces/{space_id}/expertise").json()["consistency_source"] == "proxy"
