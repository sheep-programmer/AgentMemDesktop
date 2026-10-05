"""兄弟切片合并：同一节里连着命中的几条，并回它们的父节点。

检索命中的常常不是「一段话」而是「同一节里连着的两三条切片」——切分器是按 token 数
切的，而回答需要的往往是整节。把它们各自当独立证据送进去有三个坏处：

1. 同一个标题路径被重复渲染多遍，证据预算花在重复的抬头上；
2. 引用编号被同一节占掉三个，回答里挤满 ``[^c1][^c2][^c3]``，读起来像三处出处；
3. 被切开的那句话跨在两条之间，模型两边都只看到半句。

合并只在**确实相邻**时做：同一篇文档、同一条 ``heading_path``、``ordinal`` 连号。
不相邻的两条即便同属一节也不合并——中间那条没被检索到，正文接起来是断的，
拼在一起会造出一段原文里不存在的文字。

与 RAPTOR 那类「预先建树、逐层摘要」的做法比，这里不预建任何结构，也不额外调模型：
父节点就是切分时已经记下的 ``heading_path``，合并在检索之后按需发生。代价是只能
合并同一节内的相邻切片，做不到跨节的层层上卷。

偏移与正文一起维护（``char_start`` 取最小、``char_end`` 取最大），因此前端拿合并后的
区间照样能在原文里高亮出一整节。引用锚点用**排名最高**的那条切片的 id：引用点进去
定位到的是真正命中的那一段，而不是合并后的起点。
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from .models import ScoredChunk

logger = structlog.get_logger(__name__)

#: 同一节里至少要有这么多条相邻切片才合并。2 条就合：那正是「一节被切成两半」的常态。
MIN_SIBLINGS = 2


def _plain(chunk: ScoredChunk) -> bool:
    """正文与偏移一一对应的普通切片。

    表格续段不满足这条：它的正文以一段重复的表头开头，``content`` 比偏移跨度长
    （见 ``ingest/chunk.py::ChunkDraft``）。把这种切片拼进来，
    ``char_end - char_start == len(content)`` 这条不变量就会被破坏，
    前端拿偏移去原文里高亮会整体错位。
    """
    return (
        chunk.char_start is not None
        and chunk.char_end is not None
        and chunk.char_end - chunk.char_start == len(chunk.content)
    )


def _mergeable(left: ScoredChunk, right: ScoredChunk) -> bool:
    """两条是不是同一节里**紧挨着**的邻居。

    除了同文档、同标题路径、ordinal 连号，还要求区间首尾相接：中间哪怕只差一个字符，
    拼出来的正文都不再等于原文的那一段，偏移就不能用了。
    """
    if left.document_id != right.document_id:
        return False
    if (left.heading_path or "") != (right.heading_path or ""):
        return False
    if left.kind != "body" or right.kind != "body":
        # 概要切片没有原文区间，合进来会让偏移失去意义
        return False
    if left.ordinal is None or right.ordinal is None or right.ordinal != left.ordinal + 1:
        return False
    if not _plain(left) or not _plain(right):
        return False
    return left.char_end == right.char_start


def _merge(group: list[ScoredChunk]) -> ScoredChunk:
    """把一组相邻切片并成一条。

    正文按 ordinal 顺序拼接；锚点、各路分数取排名最高的那条——它才是真正被检索
    命中的那一段，引用点进去应该落在它身上。
    """
    ordered = sorted(group, key=lambda chunk: chunk.ordinal or 0)
    best = max(group, key=lambda chunk: chunk.score)
    starts = [chunk.char_start for chunk in ordered if chunk.char_start is not None]
    ends = [chunk.char_end for chunk in ordered if chunk.char_end is not None]
    return best.model_copy(
        update={
            # 直接相接，不加分隔符：这几条的区间首尾相连（见 _mergeable），
            # 拼起来正好是原文的那一段，`char_end - char_start == len(content)` 仍然成立
            "content": "".join(chunk.content for chunk in ordered),
            "char_start": min(starts) if starts else None,
            "char_end": max(ends) if ends else None,
            "ordinal": ordered[0].ordinal,
            "page": ordered[0].page,
            "merged_from": [chunk.chunk_id for chunk in ordered],
        }
    )


def merge_siblings(
    chunks: Sequence[ScoredChunk], *, min_siblings: int = MIN_SIBLINGS
) -> list[ScoredChunk]:
    """把同一节里相邻的命中合并，返回新的证据列表。

    输入顺序即证据顺序（已经过重排与多样性处理），输出保持这个顺序：合并后的条目
    占据该组中**排名最靠前**那条的位置，其余位置让出来——空出来的名额由后面的证据
    顶上，这正是合并想要的效果：同样的预算装下更多不同的内容。

    ``min_siblings`` 是成组的最小条数，设成很大的数等于关掉这一步。
    """
    if len(chunks) < 2:
        return list(chunks)

    # 先按「文档 + 标题路径」分桶，桶内按 ordinal 找连号段
    buckets: dict[tuple[str, str], list[ScoredChunk]] = {}
    for chunk in chunks:
        buckets.setdefault((chunk.document_id, chunk.heading_path or ""), []).append(chunk)

    merged_by_anchor: dict[str, ScoredChunk] = {}
    absorbed: set[str] = set()
    for bucket in buckets.values():
        if len(bucket) < min_siblings:
            continue
        ordered = sorted(bucket, key=lambda chunk: chunk.ordinal or 0)
        run: list[ScoredChunk] = [ordered[0]]
        for chunk in ordered[1:]:
            if _mergeable(run[-1], chunk):
                run.append(chunk)
                continue
            _close(run, min_siblings, chunks, merged_by_anchor, absorbed)
            run = [chunk]
        _close(run, min_siblings, chunks, merged_by_anchor, absorbed)

    if not merged_by_anchor:
        return list(chunks)

    result: list[ScoredChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in merged_by_anchor:
            result.append(merged_by_anchor[chunk.chunk_id])
        elif chunk.chunk_id not in absorbed:
            result.append(chunk)
    logger.debug(
        "automerge_done",
        before=len(chunks),
        after=len(result),
        groups=len(merged_by_anchor),
    )
    return result


def _close(
    run: list[ScoredChunk],
    min_siblings: int,
    original: Sequence[ScoredChunk],
    merged_by_anchor: dict[str, ScoredChunk],
    absorbed: set[str],
) -> None:
    """一段连号结束：够长就合并，锚点记在它排名最靠前的那条上。"""
    if len(run) < min_siblings:
        return
    order = {chunk.chunk_id: index for index, chunk in enumerate(original)}
    anchor = min(run, key=lambda chunk: order.get(chunk.chunk_id, len(order)))
    merged_by_anchor[anchor.chunk_id] = _merge(run)
    absorbed.update(chunk.chunk_id for chunk in run if chunk.chunk_id != anchor.chunk_id)
