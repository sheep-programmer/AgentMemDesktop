"""证据 token 预算分配器的不变量测试。

这个模块的全部价值都建立在几条**不变量**上，它们一旦被破坏，故障是静默的：
模型照样收到上下文、照样回答，只是证据被切碎了、或者关键数字被砍成半截。
因此下面每条测试都盯着一条不变量，而不是盯着某个具体数值。

    句子切分可逆        → 裁剪时按句取舍，不靠补标点也能还原
    小数点不是句边界    → IC50 = 12.5 nM 不能被切成 IC50 = 12
    只在句子边界裁剪    → 保留下来的每一段都是完整句子
    同时保头保尾        → 结论常在末尾
    总预算不被突破      → 分配结果之和恒不超过总量
    用不完的份额要回收  → 短证据不该占着配额

最后一条端到端测试把整条链路（分预算 → 裁剪 → 渲染）串起来验一遍。
"""

from __future__ import annotations

import random

from agentmem.prompts._shapes import ChunkRef
from agentmem.prompts.answer import (
    EVIDENCE_TOKEN_BUDGET,
    MIN_EVIDENCE_TOKENS,
    render_evidence,
)
from agentmem.prompts.budget import (
    DEFAULT_MARKER,
    allocate,
    estimate_tokens,
    split_sentences,
    trim_to_budget,
)
from agentmem.retrieve.context import to_chunk_ref
from agentmem.retrieve.models import ScoredChunk

#: 句末标点集合。收尾引号也算，因为 split_sentences 会把它们并入前一句，
#: 于是「…就这样。」的整段以引号结尾。
SENTENCE_ENDINGS = "。！？；.!?」』”’\n"


# ------------------------------------------------------------------ token 估算


def test_estimate_tokens_counts_cjk_per_char() -> None:
    """中文一字约一 token、西文四字符约一 token——预算是保守估计，不能偏小。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("新药研发") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2, "不足四个字符也要占一个 token"
    assert estimate_tokens("IC50 = 12 nM") == 3


# ------------------------------------------------------------------ 句子切分


def test_split_sentences_round_trips_mixed_text() -> None:
    """切分必须可逆：拼回去要与原文逐字节相同，否则裁剪会吃掉换行或标点。"""
    text = (
        "第一句结论：IC50 = 12.5 nM，选择性良好。\n"
        "第二句 The assay ran at 25.0 °C overnight.\n"
        "\n"
        "第三句：注意 hERG 风险！第四句结束；\n"
    )
    parts = split_sentences(text)

    assert "".join(parts) == text
    assert len(parts) > 1, "混排文本应该被切成多句"
    assert all(part for part in parts), "不应切出空片段"


def test_split_sentences_round_trips_arbitrary_text() -> None:
    """任意文本都要可逆——包括空串与没有句末标点的单句。"""
    for text in ("", "没有标点的一整段文字", "a\nb", "……"):
        assert "".join(split_sentences(text)) == text


def test_decimal_points_are_not_sentence_boundaries() -> None:
    """小数点和单位不能把数值劈开：12.5 nM 被切成 12 和 5 是灾难性的。"""
    text = "抑制剂的 IC50 = 12.5 nM。另一组的 IC50 = 3.4 µM，需要复测。"
    parts = split_sentences(text)

    assert "12.5 nM" in parts[0]
    assert "3.4 µM" in parts[1]
    assert "".join(parts) == text


def test_sentence_end_marker_must_be_followed_by_space() -> None:
    """英文句号只有后面跟空白或到末尾才算句末；缩写点则会被误当成句末。

    这是刻意选择的简单规则：宁可多切一刀，也不维护一张缩写表。
    多切的后果只是切点不理想（``See Fig.`` 与 ``3`` 被分开），
    绝不会把数值或单位切开——小数点后面跟的是数字，不满足断句条件。
    """
    assert split_sentences("It worked. Then retest.") == ["It worked.", " Then retest."]
    assert split_sentences("See Fig. 3 for details.") == ["See Fig.", " 3 for details."]


def test_newlines_are_boundaries_and_merge_with_preceding_sentence() -> None:
    """换行是边界，但紧跟句末标点的换行并入前一句，不产生只含换行的碎片。"""
    parts = split_sentences("第一句。\n\n第二句\n")

    assert parts == ["第一句。\n\n", "第二句\n"]


# ------------------------------------------------------------------ 预算裁剪


def test_trim_noop_when_within_budget() -> None:
    """放得下就原样返回，不做无谓的改写。"""
    text = "短句一。短句二。"
    assert trim_to_budget(text, 1000) == text


def test_trim_returns_empty_on_non_positive_budget() -> None:
    """预算非正时返回空串，而不是让调用方拿到一个 marker 壳。"""
    assert trim_to_budget("任意内容。", 0) == ""
    assert trim_to_budget("任意内容。", -5) == ""


def test_trim_never_cuts_inside_a_sentence() -> None:
    """裁剪点必须落在句子边界上：每一段留下来的是完整句子，不是残片。"""
    sentences = [f"第{i}条结论是 IC50 = {i}.5 nM。" for i in range(1, 60)]
    text = "".join(sentences)
    out = trim_to_budget(text, 200)

    assert out != text, "这段文本明显超预算，必须被裁"
    head, marker, tail = out.partition(DEFAULT_MARKER)
    assert marker, "裁剪后必须留下截断标记，让模型知道内容不完整"

    for piece in (head, tail):
        assert piece, "头尾都要有内容，不能只保一边"
        assert piece[-1] in SENTENCE_ENDINGS, f"裁剪点不在句子边界上：{piece[-15:]!r}"

    # 更强的断言：切点在原文里的偏移量必须正好是某个句子边界
    boundaries = {0}
    offset = 0
    for sentence in split_sentences(text):
        offset += len(sentence)
        boundaries.add(offset)
    assert len(head) in boundaries, "头部切点没有落在句子边界"
    assert len(text) - len(tail) in boundaries, "尾部切点没有落在句子边界"


def test_trim_keeps_head_and_tail_of_the_text() -> None:
    """保尾是刻意的：结论常写在末尾，只保头会把它扔掉。"""
    text = "开头唯一标记 AAA。" + "中间填充的句子。" * 300 + "结尾唯一标记 ZZZ。"
    out = trim_to_budget(text, 200)

    assert "AAA" in out, "头部丢了"
    assert "ZZZ" in out, "结论所在的尾部丢了"
    assert estimate_tokens(out) <= 200


def test_trim_falls_back_to_char_cut_for_oversized_sentence() -> None:
    """整段只有一个超长句时句子级切分无能为力，必须退回字符截断且不崩溃。"""
    text = "这是一段没有任何句末标点的超长正文" * 200
    out = trim_to_budget(text, 100)

    assert out, "不能返回空结果"
    assert out.startswith("这是一段"), "退化路径也应尽量保住开头"
    assert estimate_tokens(out) <= 100, "折算比例取自文本自身，不应超出预算"


def test_trim_respects_budget_across_sizes() -> None:
    """各种预算下都不得超出，且结果必须是原文的「头 + marker + 尾」组合。"""
    text = "".join(f"第{i}句：该化合物在体外稳定。" for i in range(1, 80))

    for budget in (10, 30, 60, 120, 400, 2000):
        out = trim_to_budget(text, budget)
        assert estimate_tokens(out) <= budget, f"预算 {budget} 被突破了：{estimate_tokens(out)}"
        head, _, tail = out.partition(DEFAULT_MARKER)
        assert text.startswith(head)
        assert text.endswith(tail)


# ------------------------------------------------------------------ 预算分配


def test_allocate_favours_higher_weight() -> None:
    """份额与权重成正比：高相关的证据拿到更多篇幅。"""
    result = allocate([0.9, 0.3], 1200, min_tokens=100, demands=[10_000, 10_000])

    assert result[0] > result[1]
    assert result[0] + result[1] <= 1200


def test_allocate_recycles_unused_share_to_long_evidence() -> None:
    """注水：短证据用不完的份额要流给长证据，而不是压在自己手里浪费掉。"""
    weights = [0.5, 0.5]
    demands = [40, 100_000]
    result = allocate(weights, 1000, min_tokens=100, demands=demands)

    assert result[0] == 40, "短证据只需要这么多，多一分都不该占"
    assert result[1] > 500, "纯按比例它只应得 500，多出来的是回收的余量"
    assert sum(result) <= 1000


def test_allocate_splits_evenly_when_all_weights_are_zero() -> None:
    """权重全为零（没配重排）时退化为均分，且绝不除零。"""
    result = allocate([0.0] * 4, 400, min_tokens=50, demands=[10_000] * 4)

    assert result == [100, 100, 100, 100]


def test_allocate_drops_the_weakest_when_budget_is_tight() -> None:
    """预算只够两条时，最弱的六条整条丢弃，而不是各留一段残片。"""
    weights = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]
    result = allocate(weights, 400, min_tokens=MIN_EVIDENCE_TOKENS, demands=[10_000] * 8)

    assert len(result) == 8
    assert result[2:] == [0] * 6, "弱的那几条应整条出局"
    assert result[0] > 0 and result[1] > 0
    assert sum(result) <= 400


def test_allocate_keeps_short_evidence_that_fits_entirely() -> None:
    """份额低于下限但**能完整放下**的证据要保留：60 token 的全文全是信息，不是残片。"""
    result = allocate([0.5, 0.5], 1000, min_tokens=120, demands=[60, 10_000])

    assert result[0] == 60, "短证据应被完整保留，而不是因为低于下限被丢掉"
    assert sum(result) <= 1000


def test_allocate_without_demands_only_checks_the_floor() -> None:
    """不给 demands 时没有「用不完」可言，只按下限丢弃。"""
    result = allocate([1.0, 1.0], 500, min_tokens=200, demands=None)

    assert result == [250, 250]


def test_allocate_invariants_hold_for_random_inputs() -> None:
    """随机输入下两条硬不变量必须恒成立：长度一致、总和不超过预算。"""
    rng = random.Random(20240917)

    for _ in range(300):
        count = rng.randint(0, 12)
        weights = [
            rng.choice([0.0, rng.random(), rng.random() * 12 - 6, 1e-9]) for _ in range(count)
        ]
        demands = [rng.randint(0, 5000) for _ in range(count)] if rng.random() < 0.7 else None
        budget = rng.randint(0, 5000)
        min_tokens = rng.choice([0, 1, 60, 120, 500])

        result = allocate(weights, budget, min_tokens=min_tokens, demands=demands)

        assert len(result) == count, "返回值长度必须与输入等长"
        assert all(item >= 0 for item in result), "份额不能为负"
        assert sum(result) <= budget, "总和必须落在预算内"
        if demands is not None:
            assert all(share <= demand for share, demand in zip(result, demands, strict=True)), (
                "分到的份额不该超过该条的实际需要"
            )


def test_allocate_rejects_mismatched_demands() -> None:
    """demands 与 weights 不等长是调用方的错，早点炸比静默错配好。"""
    try:
        allocate([0.5, 0.5], 1000, min_tokens=10, demands=[100])
    except ValueError:
        return
    raise AssertionError("长度不匹配时应抛出 ValueError")


# ------------------------------------------------------------------ 端到端

_PERSONA = {
    "name": "新药研发专家",
    "domain": "药物发现",
    "role_description": "你是一名药物化学家。",
    "must_cite": True,
    "language": "zh-CN",
}


def _evidence(marker: str, score: float, repeat: int = 40) -> ChunkRef:
    """构造一条远超总预算的证据，逼出真正的预算裁剪。"""
    body = f"{marker} 号证据的开头结论。" + "该化合物在体外表现出良好的选择性。" * repeat
    return ChunkRef(
        marker=marker,
        chunk_id=f"chunk-{marker}",
        document_title="新药研发资料.pdf",
        heading_path="第 3 章 › 3.2 成药性",
        page=3,
        content=body,
        score=score,
    )


def _body_tokens_by_marker(rendered: str) -> dict[str, int]:
    """从渲染结果里取出每个编号实际拿到的正文字数，用于比较篇幅。"""
    sizes: dict[str, int] = {}
    for block in rendered.split("<evidence ")[1:]:
        marker = block.split('marker="', 1)[1].split('"', 1)[0]
        body = block.split(">\n", 1)[1].split("\n</evidence>", 1)[0]
        sizes[marker] = estimate_tokens(body)
    return sizes


def test_render_evidence_stays_within_budget_and_favours_high_scores() -> None:
    """端到端：渲染结果不超总预算（留标签开销的余量），且高分证据篇幅更大。"""
    chunks = [
        _evidence(f"c{index}", score)
        for index, score in enumerate([0.95, 0.90, 0.85, 0.80, 0.72, 0.64, 0.56, 0.30], start=1)
    ]

    rendered = render_evidence(chunks)
    sizes = _body_tokens_by_marker(rendered)

    assert estimate_tokens(rendered) <= EVIDENCE_TOKEN_BUDGET * 1.15, (
        "标签与来源标注的开销之外，证据正文不该超出总预算"
    )
    assert sizes, "至少要留下一条证据"
    assert sizes["c1"] >= max(sizes.values()), "得分最高的那条应该拿到最多篇幅"
    assert all(size >= 0 for size in sizes.values())
    assert len(sizes) < len(chunks), "8 条内容量都远超预算，不可能全部塞进 2000 token"

    ordered = sorted(sizes.items(), key=lambda item: -item[1])
    assert ordered[0][0] == "c1", f"篇幅排序应与得分一致，实际是 {list(sizes)}"


def test_render_evidence_preserves_format_and_drops_by_budget() -> None:
    """输出格式一个字都不能变：引用协议与前端解析都绑在 evidence 块上。"""
    chunks = [_evidence("c1", 0.9), _evidence("c2", 0.9)]

    rendered = render_evidence(chunks)

    assert rendered.startswith("<retrieved_context>")
    assert rendered.endswith("</retrieved_context>")
    assert '<evidence marker="c1" source="新药研发资料.pdf › 第 3 章 › 3.2 成药性 › 第 3 页">' in (
        rendered
    )
    assert rendered.count("<evidence ") == 2
    assert rendered.count("</evidence>") == 2


def test_render_evidence_keeps_at_least_one_when_budget_is_tight() -> None:
    """丢到只剩最后一条时不能再丢：预算够放一条，就必须给模型留下一条证据。"""
    chunks = [_evidence(f"c{index}", 0.01) for index in range(1, 21)]

    rendered = render_evidence(chunks)
    sizes = _body_tokens_by_marker(rendered)

    # 预算 / 单条下限 = 可容纳的条数上限，每条都需要完整内容，必然要丢掉一部分
    assert 1 <= len(sizes) <= EVIDENCE_TOKEN_BUDGET // MIN_EVIDENCE_TOKENS
    assert estimate_tokens(rendered) <= EVIDENCE_TOKEN_BUDGET * 1.15


def test_render_evidence_returns_empty_for_blank_content() -> None:
    """正文只有空白时不渲染证据区：不能留下「以下是资料」的空壳，
    那会让模型以为本轮有资料可以引用。"""
    chunks = [
        ChunkRef(
            marker="c1",
            chunk_id="chunk-1",
            document_title="笔记.md",
            content="   \n  ",
            score=0.5,
        )
    ]

    assert render_evidence(chunks) == ""


def test_render_evidence_keeps_short_chunks_in_full() -> None:
    """远低于总预算的一组短证据应原样全部进入上下文。"""
    chunks = [
        ChunkRef(
            marker=f"c{index}",
            chunk_id=f"chunk-{index}",
            document_title="笔记.md",
            content=f"第 {index} 条结论：12.5 nM。",
            score=0.5,
        )
        for index in range(1, 4)
    ]

    rendered = render_evidence(chunks)

    for index in range(1, 4):
        assert f'<evidence marker="c{index}"' in rendered
        assert f"第 {index} 条结论：12.5 nM。" in rendered
    assert "已截断" not in rendered


def test_to_chunk_ref_carries_the_relevance_score() -> None:
    """分数要跟着证据一路传到 Prompt 层，否则预算只能均分。"""
    reranked = ScoredChunk(
        chunk_id="chunk-1",
        document_id="doc-1",
        document_title="资料.pdf",
        content="正文",
        rrf=0.02,
        rerank_score=0.87,
    )
    fused = ScoredChunk(
        chunk_id="chunk-2",
        document_id="doc-1",
        document_title="资料.pdf",
        content="正文",
        rrf=0.02,
    )
    bare = ScoredChunk(
        chunk_id="chunk-3", document_id="doc-1", document_title="资料.pdf", content="正文"
    )

    assert to_chunk_ref(reranked, "c1")["score"] == 0.87
    assert to_chunk_ref(fused, "c2")["score"] == 0.02
    assert to_chunk_ref(bare, "c3")["score"] == 0.0


# ------------------------------------------------ 分配对分数量纲不敏感（后补的守护）


def _scored_evidence(
    count: int, scores: list[float], body_chars: int = 900
) -> list[dict[str, object]]:
    return [
        {
            "marker": f"c{i + 1}",
            "chunk_id": f"ch{i}",
            "document_title": "文献.pdf",
            "content": "这是一段示例证据正文。" * (body_chars // 11),
            "score": scores[i],
        }
        for i in range(count)
    ]


def test_allocation_is_invariant_to_score_scale() -> None:
    """同一个排序，换一种 rerank 分的量纲，分配结果必须不变。

    各家 rerank 的分根本不可比：Cohere 是 0~1，本地 CrossEncoder 是原始 logits
    （可负、无界），没有 reranker 时退回的 RRF 分彼此只差千分之几。
    曾经按分数比例分配，实测同一排序保留的条数在 5~8 之间跳——
    检索质量没变，喂给模型的证据却变了，这种不确定性必须堵死。
    """
    from agentmem.prompts.answer import render_evidence

    cohere = [0.95, 0.88, 0.80, 0.72, 0.64, 0.55, 0.40, 0.31]
    logits = [6.2, 5.1, 3.4, 2.0, 0.5, -1.2, -3.4, -5.0]
    rrf = [0.0164, 0.0161, 0.0159, 0.0156, 0.0154, 0.0152, 0.0150, 0.0148]

    renders = [render_evidence(_scored_evidence(8, scores)) for scores in (cohere, logits, rrf)]  # type: ignore[arg-type]
    assert renders[0] == renders[1] == renders[2]


def test_higher_ranked_evidence_gets_more_room() -> None:
    """排名靠前的证据应当拿到更大的篇幅，否则排序就白做了。"""
    from agentmem.prompts.answer import render_evidence

    rendered = render_evidence(_scored_evidence(6, [0.9] * 6))  # type: ignore[arg-type]
    blocks = rendered.split("<evidence ")[1:]
    lengths = [len(block) for block in blocks]
    assert lengths == sorted(lengths, reverse=True), f"篇幅未随名次递减：{lengths}"


def test_negative_scores_do_not_break_allocation() -> None:
    """负分（CrossEncoder logits 常见）不能让分配崩掉或饿死所有条目。"""
    from agentmem.prompts.answer import render_evidence

    rendered = render_evidence(_scored_evidence(5, [-1.0, -2.0, -3.0, -4.0, -5.0]))  # type: ignore[arg-type]
    assert rendered.count("<evidence ") >= 2
