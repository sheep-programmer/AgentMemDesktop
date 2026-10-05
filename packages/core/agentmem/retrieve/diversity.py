"""检索结果的冗余抑制：近重复识别 + MMR 多样性重排。

检索出来的 top-N 里本来就带着重复内容，来源有两个：相邻切片共享一段重叠文本
（``chunk_overlap`` 的产物），以及跨文档的近重复（同一份资料的转载、多版本、
同一段话在多处出现）。这些重复文字会原样占掉证据区的 token 预算，
把本该进来的第 N+1 条挤出去——预算花在重复文字上，等于白花。

本模块用三个纯函数处理这件事：

    text_similarity      两条文本的重合度，字符 n-gram，纯标准库
    suppress_redundant   贪心丢弃已被前面条目覆盖的近重复
    mmr_rerank           按 MMR 重排，在相关性与多样性之间取平衡

不依赖存储层与 provider：输入输出都是 :class:`ScoredChunk`，不发网络请求，
因此可以脱离检索链路单独测试。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import ScoredChunk

#: 字符 n-gram 的 n。
#: 取 4：3 在中文里会和「的方」「是一个」这类高频搭配撞车，虚高两条无关文本的相似度；
#: 5 对转载后的少量改写太敏感，改几个字就把 n-gram 打散一片，近重复反而漏检。
NGRAM_SIZE = 4

#: 判为近重复的默认重合度。0.85 对应「短的那条八成半以上能在长的那条里找到」，
#: 剩下的一成多差异通常是错别字、标点与转载时的措辞微调。
DEFAULT_DEDUP_THRESHOLD = 0.85

#: MMR 默认 λ：偏相关性，只在相似度很高时才为了多样性让位。
DEFAULT_MMR_LAMBDA = 0.7

#: 名次 → 相关性的偏移量。rank 从 0 起，relevance = 1 / (rank + 2)，
#: 于是首条 0.5，之后 1/3、1/4……衰减快而不陡，排序靠前的那几条仍有明显区分。
RANK_OFFSET = 2.0

_WHITESPACE = re.compile(r"\s+")


def text_similarity(a: str, b: str) -> float:
    """两段文本的重合度，0~1。

    定义为 ``|n-gram(A) ∩ n-gram(B)| / min(|n-gram(A)|, |n-gram(B)|)``，
    语义是「短的那条有多大比例能在长的那条里找到」，而不是 Jaccard
    （分母为并集）。等长的两条文本下两者结果相同，长度悬殊时才有区别，
    而长度悬殊正是检索结果的常态——一条 50 字的结论与一条 2000 字的正文
    讲的是同一件事时，Jaccard 被正文里的其余内容稀释到 0.03 以下，
    于是「短的那条毫无新信息」这件事实被藏起来了。

    为什么要字符级 n-gram 而不是分词或编辑距离：中英混排同一套代码就够用，
    不引入分词器之外的任何依赖，且对局部改动不敏感——转载时改几个词，
    其余 n-gram 全在，重合度只掉几个百分点；反之两条只在虚词上相似的无关文本，
    共享的 n-gram 很少，重合度贴近 0。

    结果对调用方是对称的：``text_similarity(a, b) == text_similarity(b, a)``。
    """
    return _similarity(_ngrams(a), _ngrams(b))


def suppress_redundant(
    chunks: Sequence[ScoredChunk],
    *,
    threshold: float = DEFAULT_DEDUP_THRESHOLD,
    keep_min: int = 1,
) -> list[ScoredChunk]:
    """丢弃与前文重复的条目，返回保留的那几条（保持原有顺序）。

    前提：``chunks`` 已按相关性降序。贪心从左往右扫，每条与**已保留的所有**条目
    比重合度，超过 ``threshold`` 就丢掉。与已保留集合比而不是只比相邻一条，
    是因为重复未必相邻：同一份资料的三个版本可能在名次上被别的文档隔开。

    ``keep_min`` 是保底条数：前 ``keep_min`` 条无条件保留，之后才开始判重。
    阈值调激进时（例如设成 0.0）这个下限能避免结果被清空——检索链路宁可比预期
    少一点多样性，也不能变成「没有证据」。

    为什么丢掉整条而不是裁掉重叠部分：能走到这一步的重复，是**短的那条**
    基本被长的那条覆盖，丢掉它不损失信息。相邻切片之间那种只重叠一小段的情形
    不会被这里命中——丢掉一条共享 15% 的切片，等于连它独有的 85% 一起扔了。
    """
    kept: list[ScoredChunk] = []
    profiles: list[frozenset[str]] = []
    for chunk in chunks:
        profile = _ngrams(chunk.content)
        if len(kept) >= keep_min and any(
            _similarity(profile, other) > threshold for other in profiles
        ):
            continue
        kept.append(chunk)
        profiles.append(profile)
    return kept


def mmr_rerank(
    chunks: Sequence[ScoredChunk],
    *,
    lambda_: float = DEFAULT_MMR_LAMBDA,
    limit: int | None = None,
) -> list[ScoredChunk]:
    """最大边际相关（MMR）重排：每轮选出 ``λ·相关性 − (1−λ)·与已选集合的最大重合度``。

    ``λ = 1.0`` 退化为纯相关性排序（输入已排序，输出即原顺序）；
    ``λ = 0.0`` 只认多样性，第一条之后每次都挑与已选最不相似的那条。
    ``limit`` 为 ``None`` 表示全部返回。

    相关性由**名次**推导（``1/(rank + 2)`` 再 min-max 归一化到 0~1），
    不使用 chunk 自带的分数。各家 rerank 的分不在一个量纲上：Cohere 是 0~1，
    本地 CrossEncoder 直接返回原始 logits（可负、无界），没配 reranker 时
    退回的 RRF 分彼此只差千分之几。拿原始分参与加权，等于让 λ 的含义随
    provider 而变——同一组排序换个 reranker，输出的证据顺序就不一样了。
    名次是这三种情况下唯一可比的信号。

    归一化到 0~1 是为了让 λ 在候选条数变化时保持同一个含义：
    ``1/(rank+2)`` 的绝对值范围随 N 变化（第 16 名的 0.056 与第 8 名的 0.1
    不是一回事），min-max 之后首条恒为 1、末条恒为 0，λ 才是一个稳定的比例。

    候选条数在几十条量级，两两比较是 O(N²) 且每条文本的 n-gram 只算一次，
    开销可以忽略；真正贵的是 rerank 调用，与这里无关。

    返回的条目**不修改分数**：``rerank_score`` / ``rrf`` 原样保留，
    下游按名次分配证据篇幅，分数只用于展示与调试。
    """
    if not chunks:
        return []
    if limit is not None and limit <= 0:
        return []

    count = len(chunks)
    target = count if limit is None else min(limit, count)
    relevance = _normalized_relevance(count)
    profiles = [_ngrams(chunk.content) for chunk in chunks]

    # max_sim[i]：候选 i 与「已选集合」的最大重合度。每选走一条就把新选中那条
    # 的相似度并进去，避免每轮把整个已选集合重算一遍。
    max_sim = [0.0] * count
    remaining = list(range(count))
    selected: list[ScoredChunk] = []

    def mmr_of(index: int) -> float:
        """候选的 MMR 得分：相关性减去与已选集合的重复度。"""
        return lambda_ * relevance[index] - (1.0 - lambda_) * max_sim[index]

    while remaining and len(selected) < target:
        # 并列时取名次靠前的那条（-index 参与比较），同样的输入永远得到同样的输出
        best = max(remaining, key=lambda index: (mmr_of(index), -index))
        selected.append(chunks[best])
        remaining.remove(best)
        for index in remaining:
            similarity = _similarity(profiles[index], profiles[best])
            if similarity > max_sim[index]:
                max_sim[index] = similarity
    return selected


def _normalize(text: str) -> str:
    """折大小写、压空白。

    同一段话在两份资料里的差别常常只是换行、缩进与空格（导出格式不同、
    从网页复制粘贴），大小写差异同理。不归一化会让这些差别全部计入 n-gram，
    把本该判为重复的两条拉开距离。
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _ngrams(text: str) -> frozenset[str]:
    """文本的字符 n-gram 集合。

    短于 n 的文本退化成整体一个元素，这样「一模一样的两条短文本」仍然相似度 1.0，
    不会因为切不出 n-gram 而恒为 0。
    """
    normalized = _normalize(text)
    if not normalized:
        return frozenset()
    if len(normalized) < NGRAM_SIZE:
        return frozenset({normalized})
    return frozenset(
        normalized[start : start + NGRAM_SIZE] for start in range(len(normalized) - NGRAM_SIZE + 1)
    )


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """两个 n-gram 集合的重合度。空集合只与空集合相等。"""
    if not left or not right:
        return 1.0 if left == right else 0.0
    shared = len(left & right)
    if shared == 0:
        return 0.0
    return shared / min(len(left), len(right))


def _normalized_relevance(count: int) -> list[float]:
    """把名次折算成 0~1 的相关性，且严格递减。"""
    raw = [1.0 / (rank + RANK_OFFSET) for rank in range(count)]
    highest, lowest = raw[0], raw[-1]
    if highest <= lowest:
        # 只有一条：归一化无意义，给它满值，λ 加权后与单独保留这条等价
        return [1.0] * count
    span = highest - lowest
    return [(value - lowest) / span for value in raw]
