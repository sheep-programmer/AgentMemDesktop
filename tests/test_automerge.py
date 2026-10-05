"""兄弟切片合并（轻量层次化检索）。

钉三件事：什么时候该合、合出来的偏移还对不对、不该合的时候一条都不许动。
最后一条尤其重要——偏移错位不会报错，只会让前端的引用高亮悄悄指到别的段落。
"""

from __future__ import annotations

from agentmem.retrieve import ScoredChunk, merge_siblings
from agentmem.types import ChunkKind

DOC = "doc-1"

#: 一篇假文档的正文：合并后的偏移必须仍然能在它上面切出同样的文字
TEXT = "".join(f"第{index}段正文内容。" for index in range(1, 9))


def _chunk(
    index: int,
    *,
    start: int,
    end: int,
    heading: str | None = "第 1 章 › 1.1 小节",
    document_id: str = DOC,
    kind: ChunkKind = "body",
    rerank: float | None = None,
    content: str | None = None,
) -> ScoredChunk:
    """按区间造一条切片，正文默认就是该区间对应的原文。"""
    return ScoredChunk(
        chunk_id=f"c{index}",
        document_id=document_id,
        document_title="讲义",
        heading_path=heading,
        ordinal=index,
        char_start=start,
        char_end=end,
        content=content if content is not None else TEXT[start:end],
        kind=kind,
        rerank_score=rerank,
    )


def _span(chunk: ScoredChunk) -> str:
    """按偏移从原文里取出这条证据应该覆盖的文字。"""
    assert chunk.char_start is not None and chunk.char_end is not None
    return TEXT[chunk.char_start : chunk.char_end]


def test_merges_adjacent_siblings_into_one() -> None:
    """同一节里连号且首尾相接的两条并成一条，正文与偏移都还对得上原文。"""
    first = _chunk(1, start=0, end=9)
    second = _chunk(2, start=9, end=18)

    merged = merge_siblings([first, second])

    assert len(merged) == 1
    item = merged[0]
    assert item.content == TEXT[0:18]
    assert (item.char_start, item.char_end) == (0, 18)
    assert item.char_start is not None and item.char_end is not None
    assert item.char_end - item.char_start == len(item.content), "偏移与正文长度必须一致"
    assert _span(item) == item.content
    assert item.merged_from == ["c1", "c2"]


def test_merged_item_keeps_the_best_ranked_anchor() -> None:
    """锚点（chunk_id 与分数）取组里排名最高的那条：引用点进去要落在真正命中的那段。"""
    weak = _chunk(1, start=0, end=9, rerank=0.2)
    strong = _chunk(2, start=9, end=18, rerank=0.9)

    # 输入顺序即证据顺序：强的排在前面
    merged = merge_siblings([strong, weak])

    assert len(merged) == 1
    assert merged[0].chunk_id == "c2"
    assert merged[0].rerank_score == 0.9
    assert merged[0].content == TEXT[0:18], "正文仍按原文顺序拼，不跟着排名走"


def test_merged_item_takes_the_position_of_its_best_member() -> None:
    """合并后的条目占据组里最靠前那条的位置，其余名额让给后面的证据。"""
    other = _chunk(7, start=54, end=63, document_id="doc-2")
    first = _chunk(1, start=0, end=9)
    second = _chunk(2, start=9, end=18)

    merged = merge_siblings([first, other, second])

    assert [item.chunk_id for item in merged] == ["c1", "c7"]


def test_does_not_merge_across_a_gap() -> None:
    """中间缺了一条就不合：正文接起来是断的，拼出来等于造了一段原文里没有的文字。"""
    first = _chunk(1, start=0, end=9)
    third = _chunk(3, start=18, end=27)

    merged = merge_siblings([first, third])

    assert [item.chunk_id for item in merged] == ["c1", "c3"]
    assert all(item.merged_from == [] for item in merged)


def test_does_not_merge_non_contiguous_offsets() -> None:
    """ordinal 连号但区间不相接（中间有被切掉的空白）时也不合。"""
    first = _chunk(1, start=0, end=9)
    second = _chunk(2, start=12, end=21)

    assert [item.chunk_id for item in merge_siblings([first, second])] == ["c1", "c2"]


def test_does_not_merge_across_documents_or_sections() -> None:
    """跨文档、跨章节都不合——它们本来就不是一段连续的原文。"""
    same_ordinal_other_doc = [
        _chunk(1, start=0, end=9),
        _chunk(2, start=9, end=18, document_id="doc-2"),
    ]
    other_section = [
        _chunk(1, start=0, end=9),
        _chunk(2, start=9, end=18, heading="第 2 章 › 2.1 小节"),
    ]

    assert len(merge_siblings(same_ordinal_other_doc)) == 2
    assert len(merge_siblings(other_section)) == 2


def test_does_not_merge_summary_chunks() -> None:
    """概要切片没有原文区间，合进来偏移就没有意义了。"""
    summary = _chunk(1, start=0, end=0, kind="summary", content="全文概要")
    body = _chunk(2, start=0, end=9)

    assert len(merge_siblings([summary, body])) == 2


def test_does_not_merge_table_continuations() -> None:
    """表格续段的正文带着重复表头，比偏移跨度长，拼进来会让偏移整体错位。"""
    header_prefixed = _chunk(1, start=0, end=9, content="| 列 | 值 |\n" + TEXT[0:9])
    plain = _chunk(2, start=9, end=18)

    merged = merge_siblings([header_prefixed, plain])

    assert len(merged) == 2, "宁可不合，也不能破坏偏移不变量"


def test_merges_three_in_a_row() -> None:
    """连着三条一起合，偏移覆盖整段。"""
    chunks = [_chunk(1, start=0, end=9), _chunk(2, start=9, end=18), _chunk(3, start=18, end=27)]

    merged = merge_siblings(chunks)

    assert len(merged) == 1
    assert merged[0].merged_from == ["c1", "c2", "c3"]
    assert _span(merged[0]) == merged[0].content == TEXT[0:27]


def test_min_siblings_can_disable_merging() -> None:
    """阈值调高即可关掉这一步（管线里的开关走的也是这条路）。"""
    chunks = [_chunk(1, start=0, end=9), _chunk(2, start=9, end=18)]

    assert len(merge_siblings(chunks, min_siblings=3)) == 2


def test_empty_and_single_input() -> None:
    """空输入与单条输入原样返回。"""
    assert merge_siblings([]) == []
    single = [_chunk(1, start=0, end=9)]
    assert merge_siblings(single) == single
