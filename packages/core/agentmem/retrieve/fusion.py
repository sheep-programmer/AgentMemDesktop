"""Reciprocal Rank Fusion（RRF）——把多路召回融合成一个排名。

公式：``score(d) = Σ w_i / (k + rank_i(d))``，``rank`` 从 1 开始。

之所以用名次而不是原始分数：向量相似度与 BM25 分数量纲完全不同，
直接加权求和需要针对每种模型单独做归一化，换模型就得重调系数。
RRF 只依赖名次，天然免疫量纲差异，是混合检索的默认融合方式。

``k`` 起到「拉平头部优势」的作用：``k`` 越大，第 1 名与第 10 名的差距越小。
60 来自原论文的经验值（Cormack et al., 2009），作为默认常量。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from agentmem.types import AgentMemModel

#: RRF 平滑常数，越大越弱化头部名次的优势
RRF_K = 60


class FusedHit(AgentMemModel):
    """融合后的一条命中。"""

    id: str
    score: float = Field(description="RRF 融合分，越大越相关")
    ranks: dict[str, int] = Field(default_factory=dict, description="来源名 → 1-based 名次")


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    *,
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[FusedHit]:
    """把多路排名融合成单一排名。

    Args:
        rankings: 来源名 → 该来源的名次列表（第一项为第 1 名）。来源名只用于调试展示。
            同一个 id 在同一路里重复出现时只按首次名次计分。
        k: RRF 平滑常数，必须为正。
        weights: 各路权重，缺省为 1.0。用于「更信任向量召回」这类偏置。

    Returns:
        按融合分降序排列的命中；同分时保持首次出现的先后顺序（排序稳定），
        因此结果只取决于入参顺序，可复现。

    Raises:
        ValueError: ``k`` 不是正数。
    """
    if k < 1:
        raise ValueError(f"RRF 常数 k 必须为正整数，收到 {k}")

    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    order: list[str] = []

    for source, ids in rankings.items():
        weight = weights.get(source, 1.0) if weights is not None else 1.0
        for position, doc_id in enumerate(ids, start=1):
            if doc_id not in scores:
                scores[doc_id] = 0.0
                ranks[doc_id] = {}
                order.append(doc_id)
            if source in ranks[doc_id]:
                continue
            ranks[doc_id][source] = position
            scores[doc_id] += weight / (k + position)

    fused = [FusedHit(id=doc_id, score=scores[doc_id], ranks=ranks[doc_id]) for doc_id in order]
    fused.sort(key=lambda hit: hit.score, reverse=True)
    return fused
