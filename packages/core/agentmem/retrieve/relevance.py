"""回答前的证据精简：把「检索到了」但「对回答没用」的条目挡在上下文之外。

检索的 top-N 是按名次截出来的，不是按「够不够相关」截出来的：问「长江大学哪一年建立的」，
知识库里真正相关的只有一篇两条，剩下六个名额照样被 EGFR 表格行填满——重排分 0.000，
却吃掉了证据预算的九成，回答模型每次都要读完它们才能开口。

这里只做两件**有据可查**的裁剪，都不调模型、不改写正文：

- :func:`drop_low_relevance`  重排分远低于本轮最高分的条目不进上下文；
- :func:`drop_covered_summaries` 一篇短文档的正文已经全部在场时，它的概要切片是重复。

与同包的 ``diversity`` 一样是纯函数，输入输出都是 :class:`ScoredChunk`。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentmem.prompts.budget import estimate_tokens

from .models import ScoredChunk

#: 默认的相对下限：重排分不到本轮最高分的十分之一就不送给回答模型。
#:
#: 取值依据（本机 bge-reranker-v2-m3，39 个问题、312 条最终证据，其中 29 题来自
#: 黄金评测集）：有用的证据里分数最低的一条约为最高分的 15%（「长江大学哪一年建立的」
#: 那篇的正文 0.152，同篇概要 0.989）；被挡掉的是 0.000~0.06 这一档的无关表格行。
#: 评测集里能逐字核对的要点，在 0.02 / 0.05 / 0.1 / 0.2 四档下都没有一条因此失去支撑。
#: 取 0.1：比最低的有用证据还留出一半余量。
DEFAULT_RELEVANCE_FLOOR = 0.1

#: 本轮最高分低于此值时不裁：重排对哪一条都没把握，相对比例只是噪声。
#: 宽泛的概览题正是这样——「这些文档分别讲了什么」整轮最高 0.007，
#: 各篇概要切片在 0.0006~0.007 之间，按比例裁会把其中一篇文档整个漏掉。
CONFIDENT_TOP_SCORE = 0.5

#: 概要切片判为重复时，同文档正文合计的 token 上限。只对短文档生效：长文档的正文
#: 在证据预算里可能被截断，届时概要里的汇总信息未必还在。
SUMMARY_COVER_MAX_TOKENS = 1000


def drop_low_relevance(
    chunks: Sequence[ScoredChunk], *, floor: float = DEFAULT_RELEVANCE_FLOOR
) -> list[ScoredChunk]:
    """丢掉重排分低于 ``最高分 × floor`` 的条目，保持原有顺序。

    用**相对**下限而不是绝对阈值：各题的分数水平差别很大，「比最高分低一个数量级」
    比任何一个固定分数都更接近「明显不是一回事」。且只在最高分达到
    :data:`CONFIDENT_TOP_SCORE` 时才裁——重排对哪一条都没把握时（宽泛的概览题），
    全部原样保留。

    只在分数确实是 0~1 的相关概率时生效（本地 CrossEncoder 默认过 sigmoid，
    Cohere 返回 ``relevance_score``）。缺分、出现负数或大于 1 的分（原始 logits），
    相对比例没有意义，原样返回——宁可多送几条，也不按一把不可比的尺子裁。

    最高分那条恒被保留，结果不会为空。
    """
    if floor <= 0 or len(chunks) <= 1:
        return list(chunks)
    scores = [chunk.rerank_score for chunk in chunks]
    if any(score is None or not 0.0 <= score <= 1.0 for score in scores):
        return list(chunks)
    top = max(score for score in scores if score is not None)
    if top < CONFIDENT_TOP_SCORE:
        return list(chunks)
    threshold = top * floor
    return [
        chunk
        for chunk in chunks
        if chunk.rerank_score is not None and chunk.rerank_score >= threshold
    ]


def drop_covered_summaries(
    chunks: Sequence[ScoredChunk], *, max_tokens: int = SUMMARY_COVER_MAX_TOKENS
) -> list[ScoredChunk]:
    """同一篇短文档的正文切片已经全部在场时，丢掉它的概要切片。

    概要切片是摄取时由模型从全文写出的一段话（``kind="summary"``，没有原文区间），
    用来回答「跨全文汇总」类问题。可正文全部在场时它就只是同一件事的第二种说法：
    回答里于是同一个事实挂着两处引用（实测「长江大学哪一年建立的」，回答同时引用了
    概要 [1] 与正文 [3]），而概要那处点开定位不到原文。更糟的是概要由模型写成，
    可能夹带原文没有的内容——同一题的概要里就多出了正文没有的校址与前身院校。
    留下正文，引用才落在可以高亮的原文上。

    判定只用切片自身的元数据：摄取时正文的 ``ordinal`` 是 0..N-1，概要排在 N
    （见 ``ingest/pipeline.py``）；兄弟切片合并后的条目覆盖从 ``ordinal`` 起的
    ``len(merged_from)`` 个序号。少一条正文就不算覆盖，概要原样保留。
    """
    covered: dict[str, set[int]] = {}
    body_tokens: dict[str, int] = {}
    for chunk in chunks:
        if chunk.kind != "body" or chunk.ordinal is None:
            continue
        span = max(1, len(chunk.merged_from))
        covered.setdefault(chunk.document_id, set()).update(
            range(chunk.ordinal, chunk.ordinal + span)
        )
        body_tokens[chunk.document_id] = body_tokens.get(chunk.document_id, 0) + estimate_tokens(
            chunk.content
        )

    def redundant(chunk: ScoredChunk) -> bool:
        if chunk.kind != "summary" or not chunk.ordinal:
            return False
        if body_tokens.get(chunk.document_id, 0) > max_tokens:
            return False
        return set(range(chunk.ordinal)) <= covered.get(chunk.document_id, set())

    return [chunk for chunk in chunks if not redundant(chunk)]
