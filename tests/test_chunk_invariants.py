"""切分器的三条硬不变量。

切分器是整条引用链路的源头：检索按切片召回，回答按切片引用，前端按
``char_start`` / ``char_end`` 在解析后的全文里画高亮。任何一条不变量破掉，
坏掉的都不是「切得不够漂亮」，而是**证据本身是错的**：

1. **分句无损**。``"".join(split_sentences(t)) == t`` 恒成立。按句拼串会吃掉句后空格
   （``in vitro.Selectivity``），拼出来的串也再断不出句，重叠裁剪随之失效。
2. **偏移可切回原文**。``markdown[char_start:char_end]`` 就是切片正文（表格续段多
   一段重复的表头前缀，见下）。偏一个字符，用户点引用就会跳到错误的位置。
3. **切片大小可控**。p95 落在目标内，超长无空行块与 200 行表格都不例外。

三条都在真实语料上验：中文长文、**英文长文（无空行）**、200 行 Markdown 表格、中英混排。
表头那一节在 200 行表格上额外要求：切开的每一段都重复表头与分隔行。
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Protocol

import pytest

from agentmem.ingest import chunk as chunk_module
from agentmem.ingest.chunk import ChunkDraft, count_tokens, split_markdown
from agentmem.prompts import budget
from agentmem.retrieve import ScoredChunk, trim_adjacent_overlap
from agentmem.retrieve.overlap import boundary_cuts

TARGET = 512
OVERLAP = 80
#: 切片大小的验收线。只有「一个原子单位本身就装不下」时才允许越界。
SIZE_SLACK = 1.1

TABLE_HEADER = "| 化合物 | IC50 (nM) | 溶解度 (mg/mL) | 半衰期 (h) | 清除率 (mL/min/kg) |"
TABLE_DELIMITER = "| --- | --- | --- | --- | --- |"


# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------


def chinese_text() -> str:
    """中文长文：段落各自成块，句子长度正常。"""
    paragraphs = [
        f"第{index}节讨论药代动力学的一个环节：吸收、分布、代谢与排泄各有其参数。"
        f"清除率与半衰期共同决定给药间隔，表观分布容积则反映药物在组织中的分布广度；"
        f"这四项指标相互制约，改善其中一项往往要以牺牲另一项为代价，因此需要在早期就"
        f"把目标区间定下来。"
        for index in range(40)
    ]
    return "\n\n".join(paragraphs)


def english_text() -> str:
    """英文长文：整篇没有一个空行，只能靠句子边界切开。"""
    sentence = "Sentence number {} describes the pharmacokinetic profile in detail."
    return " ".join(sentence.format(index) for index in range(200))


def table_text(rows: int = 198) -> str:
    """200 行参数的 Markdown 表格：除了表头没有一句可断句的正文。"""
    body = [
        f"| CMPD-{index:03d} | {index * 3 % 900 + 5} | {round((index % 17) * 0.37 + 0.1, 2)} "
        f"| {round((index % 23) * 0.9 + 1.2, 1)} | {round((index % 11) * 1.7 + 0.4, 2)} |"
        for index in range(rows)
    ]
    return "\n".join([TABLE_HEADER, TABLE_DELIMITER, *body])


def mixed_text() -> str:
    """中英混排：同一段里来回切换语言，句末标点两种都有。"""
    paragraphs = [
        f"第{index}组 Group {index} 的 IC50 是 {index * 7 % 300 + 3} nM，"
        f"Selectivity was measured in vitro. 结论：该化合物值得继续推进。"
        for index in range(60)
    ]
    return "\n\n".join(paragraphs)


CORPORA = {
    "中文长文": chinese_text,
    "英文长文（无空行）": english_text,
    "200 行表格": table_text,
    "中英混排": mixed_text,
}


class Located(Protocol):
    """带字符偏移的切片正文（切分草稿与检索命中共用）。

    三个成员都声明成只读属性：可变属性在 Protocol 里是不变的，写成 ``char_start: int | None``
    会把 ``ChunkDraft``（偏移必有值，类型是 ``int``）挡在门外，而这里要同时接住它和
    检索命中（偏移可空）。
    """

    @property
    def content(self) -> str: ...

    @property
    def char_start(self) -> int | None: ...

    @property
    def char_end(self) -> int | None: ...


def body_of(chunk: Located) -> str:
    """切片的正文部分：去掉开头的合成前缀（重复的表头）。"""
    assert chunk.char_start is not None and chunk.char_end is not None
    prefix_len = len(chunk.content) - (chunk.char_end - chunk.char_start)
    assert 0 <= prefix_len <= len(chunk.content)
    return chunk.content[prefix_len:]


def drafts_of(text: str, target: int = TARGET, overlap: int = OVERLAP) -> list[ChunkDraft]:
    return split_markdown(text, target_tokens=target, overlap_tokens=overlap)


def percentile(values: list[int], ratio: float) -> float:
    """最近秩百分位：样本少时不会被插值抹平。"""
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(ratio * len(ordered)) - 1)])


# ---------------------------------------------------------------------------
# 不变量 1：分句无损
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This is one sentence. Here is another one. A third follows.",
        "化合物 A 的 IC50 是 12 nM. Selectivity was measured in vitro. 结论如上。",
        "12.5 nM 与 IC50 = 3.4 µM 里的小数点不是句末。",
        "带\n换\n行\n的\n中\n文。",
        "Mixed 混排 text. 第二句 Second one. 第三句。",
        "```python\nprint('a. b')\n```\n后面还有一句。",
        "",
        "   ",
        "没有句末标点的一整行",
    ],
)
def test_split_sentences_is_lossless(text: str) -> None:
    """拼回原文必须逐字节相同——句后空格也是原文的一部分。"""
    assert "".join(budget.split_sentences(text)) == text


def test_the_splitter_is_shared_with_the_budget_layer() -> None:
    """切分器与预算裁剪共用同一个断句实现，不各写一套。

    两处对「哪里算一句话」的判断必须一致：证据预算裁剪与相邻切片裁剪都按句边界
    动手，切分器若用另一套规则，它们就认不出切分器切出来的边界。
    """
    # 取模块字典而不是属性：切分器是 from ... import 进来的，严格模式下不算再导出
    assert vars(chunk_module)["split_sentences"] is budget.split_sentences


def test_chunk_starts_land_on_sentence_boundaries() -> None:
    """普通正文里，每一段的起点都落在原文的句边界上。"""
    text = english_text()
    cuts = set(boundary_cuts(text))

    for draft in drafts_of(text):
        assert draft.char_start in cuts, f"ordinal={draft.ordinal} 切在半句话上"


# ---------------------------------------------------------------------------
# 不变量 2：偏移可切回原文
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["中文长文", "英文长文（无空行）", "中英混排"])
def test_offsets_slice_back_to_content(name: str) -> None:
    """非表格语料上，``markdown[char_start:char_end]`` 必须逐字节等于正文。"""
    text = CORPORA[name]()
    drafts = drafts_of(text)

    assert drafts
    for draft in drafts:
        assert body_of(draft) == draft.content, f"{name} 的 ordinal={draft.ordinal} 带了前缀"
        assert text[draft.char_start : draft.char_end] == draft.content


def test_table_chunks_point_at_their_own_rows() -> None:
    """表格续段：偏移指向该段自己的行区间，前缀正是那段重复的表头。

    前端拿偏移画高亮，指向的必须是这一段的正文位置；表头是给模型读的列名，
    不属于偏移覆盖的区间。
    """
    text = table_text()
    drafts = drafts_of(text)

    assert drafts
    for draft in drafts:
        assert text[draft.char_start : draft.char_end] == body_of(draft)
        prefix = draft.content[: len(draft.content) - len(body_of(draft))]
        if prefix:
            assert prefix.startswith(TABLE_HEADER)
            assert not body_of(draft).startswith(TABLE_HEADER), "前缀不该混进正文区间"


def test_offsets_are_monotonic_and_within_document() -> None:
    """偏移随序号前进，且始终落在原文范围内。"""
    for name, build in CORPORA.items():
        text = build()
        drafts = drafts_of(text)

        assert [draft.ordinal for draft in drafts] == list(range(len(drafts)))
        for draft in drafts:
            assert 0 <= draft.char_start <= draft.char_end <= len(text), name
        for previous, current in pairwise(drafts):
            assert current.char_start >= previous.char_start, name
            assert current.char_end > previous.char_end, name


def test_overlap_trim_keeps_offsets_for_table_chunks() -> None:
    """接进相邻裁剪之后，不变量仍然成立，带合成前缀的表格续段也不例外。

    裁剪会同时改正文与偏移，这里正是切片器与检索侧最容易脱节的地方。
    """
    text = table_text()
    hits = [
        ScoredChunk(
            chunk_id=f"c{draft.ordinal}",
            document_id="doc-1",
            ordinal=draft.ordinal,
            char_start=draft.char_start,
            char_end=draft.char_end,
            content=draft.content,
        )
        for draft in drafts_of(text)
    ]

    trimmed = trim_adjacent_overlap(hits)

    assert trimmed
    for chunk in trimmed:
        assert text[chunk.char_start : chunk.char_end] == body_of(chunk)


def test_overlap_trim_keeps_offsets_for_prose_chunks() -> None:
    """表格之外仍然严格成立：``char_end - char_start == len(content)``。"""
    text = chinese_text()
    hits = [
        ScoredChunk(
            chunk_id=f"c{draft.ordinal}",
            document_id="doc-1",
            ordinal=draft.ordinal,
            char_start=draft.char_start,
            char_end=draft.char_end,
            content=draft.content,
        )
        for draft in drafts_of(text)
    ]

    for chunk in trim_adjacent_overlap(hits):
        assert chunk.char_start is not None and chunk.char_end is not None
        assert chunk.char_end - chunk.char_start == len(chunk.content)
        assert text[chunk.char_start : chunk.char_end] == chunk.content


# ---------------------------------------------------------------------------
# 不变量 3：切片大小可控
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(CORPORA))
def test_chunk_size_p95_within_target(name: str) -> None:
    """p95 切片不超过目标的 1.1 倍，四种语料都要成立。"""
    text = CORPORA[name]()
    drafts = drafts_of(text)
    tokens = [draft.token_count for draft in drafts]

    assert drafts
    assert all(draft.token_count == count_tokens(draft.content) for draft in drafts)
    assert percentile(tokens, 0.95) <= TARGET * SIZE_SLACK, f"{name} 的分布：{sorted(tokens)}"


@pytest.mark.parametrize("name", ["中文长文", "英文长文（无空行）", "200 行表格", "中英混排"])
def test_no_chunk_is_empty_or_oversize(name: str) -> None:
    """没有空切片，也没有明显越界的切片。"""
    drafts = drafts_of(CORPORA[name]())

    assert all(draft.content.strip() for draft in drafts)
    assert max(draft.token_count for draft in drafts) <= TARGET * SIZE_SLACK


def test_run_on_english_text_is_split_by_sentences() -> None:
    """整篇没有空行的英文长文也必须切成多条，且各自在目标内。"""
    text = english_text()
    drafts = drafts_of(text)

    assert len(drafts) > 5, "无空行长段被当成一整块，没有切开"
    assert max(draft.token_count for draft in drafts) <= TARGET


def test_blank_line_runs_stay_byte_exact() -> None:
    """文档里有连续多个空行时，切片正文仍是原文的一段，不重新拼换行。"""
    text = "第一段正文，讲的是溶解度。\n\n\n\n第二段正文，讲的是渗透性。"

    drafts = drafts_of(text)

    assert len(drafts) == 1
    assert text[drafts[0].char_start : drafts[0].char_end] == drafts[0].content


def test_small_target_still_respects_the_budget() -> None:
    """目标被调小时同样成立：正文预算扣掉重叠之后仍然装得下。"""
    text = chinese_text()
    for target, overlap in ((200, 40), (128, 0), (64, 32)):
        drafts = drafts_of(text, target=target, overlap=overlap)
        tokens = [draft.token_count for draft in drafts]

        assert drafts
        assert percentile(tokens, 0.95) <= target * SIZE_SLACK, f"target={target}：{tokens}"


@pytest.mark.parametrize("name", list(CORPORA))
def test_no_blank_slice_even_below_the_configured_floor(name: str) -> None:
    """目标小于配置下限（极端调用）时也不产出只有空白的切片。

    ``chunk_size`` 的下限是 64，走配置到不了这里；但切片器是公开函数，
    预算小到装不下一个原子单位时，重叠拼接可能凑出一条纯空白切片——它进不了
    检索，只会白白占一次 embedding。
    """
    for target, overlap in ((8, 80), (8, 0), (1, 2)):
        drafts = drafts_of(CORPORA[name](), target=target, overlap=overlap)

        assert drafts, f"{name} target={target} 切出了空列表"
        assert all(draft.content.strip() for draft in drafts), (
            f"{name} target={target} overlap={overlap} 出现空白切片"
        )


@pytest.mark.parametrize("name", list(CORPORA))
def test_no_visible_character_is_lost(name: str) -> None:
    """原文里每一个非空白字符都必须落在某条切片的偏移区间里。"""
    text = CORPORA[name]()
    drafts = drafts_of(text)
    covered = bytearray(len(text))

    for draft in drafts:
        for index in range(draft.char_start, draft.char_end):
            covered[index] = 1
    missing = [index for index, char in enumerate(text) if char.strip() and not covered[index]]

    assert not missing, f"{name} 丢了 {len(missing)} 个字符，首个位置 {missing[:5]}"


# ---------------------------------------------------------------------------
# 表格：切开的每一段都要能独立读懂
# ---------------------------------------------------------------------------


def test_every_table_chunk_repeats_the_header() -> None:
    """200 行表格切出来的每一段都带表头与分隔行。

    否则后半段的数字没有列名可读——「IC50 最小的是哪个」这种问题只能靠猜列。
    """
    text = table_text()
    drafts = drafts_of(text)

    assert len(drafts) > 5, "200 行表格没有切开"
    for draft in drafts:
        lines = draft.content.splitlines()
        assert lines[0] == TABLE_HEADER, f"ordinal={draft.ordinal} 的第一行不是表头"
        assert lines[1] == TABLE_DELIMITER, f"ordinal={draft.ordinal} 缺分隔行"


def test_table_rows_are_covered_exactly_once() -> None:
    """每一行数据都出现在某一段的正文里，且只出现一次。"""
    text = table_text()
    drafts = drafts_of(text)
    expected = [line for line in text.splitlines()[2:] if line]

    covered: list[str] = []
    for draft in drafts:
        lines = draft.content.splitlines()
        if lines[:2] == [TABLE_HEADER, TABLE_DELIMITER]:
            lines = lines[2:]
        covered.extend(lines)

    assert covered == expected


def test_table_row_without_delimiter_line_is_not_a_table() -> None:
    """没有分隔行的伪表格按普通正文切，不把首行当成表头复制。"""
    text = "\n".join(f"| CMPD-{index:03d} | {index} |" for index in range(200))
    first_line = text.splitlines()[0]

    drafts = drafts_of(text)

    assert len(drafts) > 1
    assert sum(draft.content.count(first_line) for draft in drafts) == 1


# ---------------------------------------------------------------------------
# 与检索侧配合
# ---------------------------------------------------------------------------


def test_adjacent_chunks_still_share_a_boundary_aligned_overlap() -> None:
    """改成区间搬运之后，相邻切片的重叠仍然落在句边界上，裁剪才认得出来。"""
    text = chinese_text()
    drafts = drafts_of(text)

    assert len(drafts) > 3
    for earlier, later in pairwise(drafts):
        assert later.char_start < earlier.char_end, "相邻切片没有重叠"
        shared = text[later.char_start : earlier.char_end]
        assert earlier.content.endswith(shared)
        assert later.content.startswith(shared)
        # 重叠段在两条里都从整句开头算起，裁剪按句边界比对才认得出它
        assert later.char_start - earlier.char_start in boundary_cuts(earlier.content)
        assert 0 in boundary_cuts(shared)
