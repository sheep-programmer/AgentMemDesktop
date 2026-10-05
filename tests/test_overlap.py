"""相邻切片重叠的裁剪：公共前后缀识别、零损失、字符偏移同步与管线接入。

这里守的是三件事，任何一件破了都会让引用链路出错：

1. **只切重复段**。裁掉的文字必须原样留在相邻的已保留条目里——裁错了就是
   把独有正文扔了，而这正是本模块存在的理由（整条丢弃是净亏）。
2. **不切半句话**。切点两侧都落在句子/行边界上。
3. **偏移跟着正文走**。``char_end - char_start == len(content)`` 恒成立，
   且裁剪后的区间仍落在原切片的区间里，否则前端引用高亮会整体错位。
"""

from __future__ import annotations

import itertools
from itertools import pairwise
from pathlib import Path

from agentmem.config import ModelsConfig, Settings
from agentmem.ingest import IngestPipeline
from agentmem.ingest.chunk import split_markdown
from agentmem.prompts.budget import estimate_tokens, split_sentences
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import (
    MIN_OVERLAP_CHARS,
    RetrievalPipeline,
    ScoredChunk,
    common_overlap,
    trim_adjacent_overlap,
)
from agentmem.retrieve.overlap import boundary_cuts
from agentmem.store import Database
from agentmem.types import ProviderConfig, RetrievalSettings, RoleBindings, SpaceCreate

#: 一篇真实规模的正文（约 1700 token），用真实切分器切成若干条相邻切片。
#: 与手写的等长文本相比，它能让「相邻切片共享一段重叠」这个前提真实成立。
SECTIONS = [
    (
        "成药性评估在候选化合物确定之前进行，目的是尽早筛掉那些理化性质或药代行为注定"
        "无法成药的分子。评估围绕溶解度、渗透性、代谢稳定性与安全性展开，四项指标相互"
        "制约，改善其中一项往往要以牺牲另一项为代价。"
    ),
    (
        "药代动力学研究回答药物在体内随时间变化的全过程，包括吸收、分布、代谢与排泄。"
        "吸收环节关注达峰时间与峰浓度，分布环节的核心参数是表观分布容积，代谢环节要"
        "识别参与转化的主要酶亚型，排泄环节则要看肾脏清除与胆汁排泄的比例。"
    ),
    (
        "动物模型的选择直接决定临床前数据的可外推程度。小鼠成本低、遗传背景清晰，"
        "适合机制验证与大规模筛选，但代谢酶谱与人体差异较大。犬与非人灵长类的生理"
        "结构更接近人体，常用于安全性评价，尤其是心血管与神经系统毒性。"
    ),
    (
        "临床试验设计要一开始就明确主要终点与次要终点，并在方案中事先约定统计分析"
        "方法。随机化与盲法是控制偏倚的两条基本手段，样本量估算需要基于预期效应量"
        "与变异度，低估变异度会导致试验把握度不足。"
    ),
    (
        "监管申报的资料准备贯穿研发全程，药学部分要说明原料药与制剂的生产工艺与"
        "质量标准，非临床部分要求药理、药代与毒理数据相互印证，临床部分按适应症与"
        "人群分层呈现安全性与有效性数据。"
    ),
    (
        "生物标志物的验证分为分析验证与临床验证两个层面。分析验证关注检测方法的"
        "准确度、精密度与线性范围，临床验证则要证明该标志物与疾病状态之间存在稳定"
        "关联，并在独立队列中重复。"
    ),
    (
        "药物相互作用研究要覆盖代谢酶与转运体两条路径。代谢层面的相互作用多来自"
        "酶抑制或酶诱导，前者让合并用药的血药浓度骤升，后者则可能让疗效在几天内"
        "消失；转运体的影响常被忽略，却足以改变整个暴露曲线。"
    ),
    (
        "制剂开发要在生物利用度、稳定性与可生产性之间找平衡。难溶性化合物常通过"
        "成盐、共晶或无定形固体分散体提高溶出，但无定形体系本身处于高能态，"
        "存放过程中可能析晶，稳定性研究的取样点必须覆盖这一风险。"
    ),
    (
        "药效学评价要回答暴露量与效应之间的关系，为剂量选择提供依据。量效曲线在"
        "低剂量段常常平缓，剂量递增带来的效应增益有限，而副作用可能已经出现，"
        "因此最大耐受剂量与最小有效剂量之间的窗口才是可用的剂量区间。"
    ),
    (
        "真实世界研究的数据来源包括电子病历、医保结算与可穿戴设备，能补上随机"
        "对照试验在人群代表性上的短板。但混杂控制是难点，倾向性评分只能校正已"
        "观测到的混杂因素，未观测的混杂仍需靠设计上的限制来规避。"
    ),
]


def _document() -> str:
    """拼接成一篇完整正文。"""
    return "\n\n".join(SECTIONS)


def _real_chunks(document_id: str = "doc-1") -> list[ScoredChunk]:
    """用真实切分器切开正文，构造带 ordinal 与字符偏移的命中。

    直接用切片产物而不是构造巧合，是为了让重叠段的形态与线上完全一致。
    """
    drafts = split_markdown(_document(), target_tokens=512, overlap_tokens=80)
    return [
        ScoredChunk(
            chunk_id=f"c{draft.ordinal}",
            document_id=document_id,
            document_title="新药研发讲义",
            ordinal=draft.ordinal,
            char_start=draft.char_start,
            char_end=draft.char_end,
            content=draft.content,
            rerank_score=1.0 - draft.ordinal * 0.05,
        )
        for draft in drafts
    ]


def _chunk(
    chunk_id: str,
    content: str,
    *,
    ordinal: int,
    document_id: str = "doc-1",
    char_start: int | None = None,
    char_end: int | None = None,
) -> ScoredChunk:
    """构造一条命中；偏移缺省按正文长度自洽填充。"""
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        ordinal=ordinal,
        char_start=char_start if char_start is not None else 0,
        char_end=char_end if char_end is not None else len(content),
    )


def _ids(chunks: list[ScoredChunk]) -> list[str]:
    return [chunk.chunk_id for chunk in chunks]


# ---------------------------------------------------------------------------
# 句边界与公共前后缀
# ---------------------------------------------------------------------------


def test_boundary_cuts_rejoin_to_original() -> None:
    """切点必须能拼回原文，否则按句裁剪会吃掉字符。"""
    text = _document()

    for cut in boundary_cuts(text):
        assert 0 <= cut <= len(text)
    assert "".join(split_sentences(text)) == text
    assert boundary_cuts(text)[0] == 0
    assert boundary_cuts(text)[-1] == len(text)


def test_overlap_between_real_adjacent_chunks_is_detected() -> None:
    """真实相邻切片的重叠段必须被找出来，且两侧都落在句边界上。

    这是本模块的前提：切分器把上一条的尾部若干整句拼到下一条开头。
    找不到（返回 None）说明判据与切分器的实际行为脱节，后续所有裁剪都失效。
    """
    chunks = _real_chunks()
    assert len(chunks) >= 3, "语料没有切出足够多的相邻切片，用例前提不成立"

    for earlier, later in pairwise(chunks):
        found = common_overlap(earlier.content, later.content)
        assert found is not None, f"{earlier.chunk_id} → {later.chunk_id} 的重叠没被识别出来"
        tail_cut, head_cut = found
        assert tail_cut >= MIN_OVERLAP_CHARS
        # 切点两侧都是句边界：裁掉的部分在两边都是完整的句子序列
        assert len(earlier.content) - tail_cut in boundary_cuts(earlier.content)
        assert head_cut in boundary_cuts(later.content)
        # 裁掉的正是重复的那段文字
        removed = earlier.content[len(earlier.content) - tail_cut :]
        assert later.content[:head_cut].rstrip() == removed


def test_common_overlap_ignores_short_coincidence() -> None:
    """共享一个短句不算切片重叠，否则会把两条无关正文各切掉一句。"""
    earlier = "本节结论：优先选择方案三，理由是成本更低。附录列出了全部原始数据。"
    later = "本节结论：优先选择方案三，理由是成本更低。但该结论依赖 2023 年的数据。"

    assert common_overlap(earlier, later) is None


def test_common_overlap_requires_boundary_on_both_sides() -> None:
    """只在长文本中间重合、或切点落在半句话上时，宁可不裁。"""
    # 重合段本身就横跨前一条的句中间，切下去会切出半句话
    earlier = "第一句。第二句讲了溶解度与渗透性的关系，需要同时满足两个条件。"
    later = "渗透性的关系，需要同时满足两个条件。补充：第三句。"

    assert common_overlap(earlier, later) is None


# ---------------------------------------------------------------------------
# trim_adjacent_overlap
# ---------------------------------------------------------------------------


def test_trim_keeps_earlier_chunk_intact_and_cuts_later() -> None:
    """顺序靠前的那条一字不动，重复段从靠后的那条里去掉。"""
    chunks = _real_chunks()
    trimmed = trim_adjacent_overlap(chunks)

    assert _ids(trimmed) == _ids(chunks), "裁剪不应改变顺序或条数"
    assert trimmed[0].content == chunks[0].content
    for before, after in zip(chunks[1:], trimmed[1:], strict=True):
        assert after.content != before.content, f"{before.chunk_id} 的重叠段没有被裁掉"
        assert before.content.endswith(after.content), "裁完的正文应当是原文的一段后缀"
    assert len(trimmed) == len(chunks)


def test_trim_loses_no_text() -> None:
    """裁掉的每一段文字都必须还在相邻的已保留条目里。

    这是「零信息损失」的全部含义：裁掉的是重复，不是内容。把裁掉的部分
    与保留邻居比一遍，任何一段找不到出处都说明裁错了。
    """
    chunks = _real_chunks()
    trimmed = trim_adjacent_overlap(chunks)
    by_ordinal = {chunk.ordinal: chunk for chunk in trimmed}

    for before, after in zip(chunks, trimmed, strict=True):
        removed = _removed_piece(before.content, after.content).strip()
        if not removed:
            continue
        neighbours = [
            by_ordinal[ordinal].content
            for ordinal in ((before.ordinal or 0) - 1, (before.ordinal or 0) + 1)
            if ordinal in by_ordinal and by_ordinal[ordinal].chunk_id != before.chunk_id
        ]
        assert neighbours, f"{before.chunk_id} 被裁了，但两侧都没有保留的邻居"
        assert any(removed in neighbour for neighbour in neighbours), (
            f"{before.chunk_id} 裁掉的文字在相邻条目里找不到：{removed[:40]}…"
        )


def test_trim_keeps_offsets_consistent_with_source() -> None:
    """正文被裁后，字符偏移必须同步前进/回退。

    前端拿 ``char_start`` 去解析后的全文里定位高亮：正文少了一段而偏移不动，
    引用会整体错位。这里直接拿全文切片比对，错一个字符就会失败。
    """
    document = _document()
    chunks = _real_chunks()
    trimmed = trim_adjacent_overlap(chunks)

    for before, after in zip(chunks, trimmed, strict=True):
        assert after.char_start is not None and after.char_end is not None
        assert after.char_end - after.char_start == len(after.content)
        assert document[after.char_start : after.char_end] == after.content
        # 裁剪只会收缩区间，不会跑到别的切片上
        assert before.char_start is not None and before.char_end is not None
        assert before.char_start <= after.char_start
        assert after.char_end <= before.char_end
    # 至少有一条真的被裁了，否则上面的断言是空转
    changed = [
        before
        for before, after in zip(chunks, trimmed, strict=True)
        if before.content != after.content
    ]
    assert changed, "一条都没被裁，上面的断言是空转"


def test_trim_drops_chunk_that_is_almost_all_overlap() -> None:
    """裁完只剩几十个 token 的条目整条丢弃：它已经没有自己的内容了。"""
    sentence = "第{index}组溶解度数据在 pH 6.8 缓冲液中测得，批间差异小于百分之三。"
    shared = "".join(sentence.format(index=index) for index in range(6))
    earlier = _chunk("c1", shared, ordinal=0)
    later = _chunk("c2", shared + "补充：该批样品需避光保存。", ordinal=1)

    trimmed = trim_adjacent_overlap([earlier, later])

    assert _ids(trimmed) == ["c1"], "只剩一句补充说明的切片不该继续占一个引用编号"
    # min_tokens=0 时保留，说明丢弃确实是长度规则而不是匹配失败
    assert _ids(trim_adjacent_overlap([earlier, later], min_tokens=0)) == ["c1", "c2"]


def test_trim_only_pairs_adjacent_ordinals_of_same_document() -> None:
    """序号不相邻、或不属于同一篇文档的命中一律不动。"""
    sentence = "第{index}节的结论是优先选择方案 {index}，理由是成本更低。"
    body = "".join(sentence.format(index=index) for index in range(6))
    gap = _chunk("gap", body, ordinal=0)
    jumped = _chunk("jumped", body + "尾巴。", ordinal=5)
    other_doc = _chunk("other", body + "尾巴。", ordinal=1, document_id="doc-2")

    trimmed = trim_adjacent_overlap([gap, jumped, other_doc])

    assert _ids(trimmed) == ["gap", "jumped", "other"]
    assert trimmed[1].content == jumped.content
    assert trimmed[2].content == other_doc.content


def test_trim_does_not_trim_against_dropped_neighbour() -> None:
    """相邻条目被整条丢弃后，本条不再与它比对——那段文字此时只在本条里。

    否则就会出现真实的丢信息：前一条因为「只剩重复内容」被丢掉，
    后一条却还按重叠去裁，两边都没留下那段正文。
    """
    shared = "".join(f"第{index}组数据在缓冲液中测得，批间差异小于百分之三。" for index in range(6))
    first = _chunk("c1", shared, ordinal=0)
    second = _chunk("c2", shared + "补充说明一句话。", ordinal=1)
    third = _chunk("c3", shared + "补充说明一句话。后一条的独有内容在这里。", ordinal=2)

    trimmed = trim_adjacent_overlap([first, second, third])

    assert _ids(trimmed) == ["c1", "c3"], "c2 应当因只剩重复内容被丢弃"
    assert "后一条的独有内容在这里。" in trimmed[1].content


def test_trim_handles_missing_ordinal_and_empty_input() -> None:
    """没有 ordinal 的老数据不参与裁剪，空输入不崩。"""
    plain = ScoredChunk(chunk_id="c1", document_id="doc-1", content="一段正文。")
    duplicated = ScoredChunk(chunk_id="c2", document_id="doc-1", content="一段正文。又一段正文。")

    assert trim_adjacent_overlap([]) == []
    assert _ids(trim_adjacent_overlap([plain, duplicated])) == ["c1", "c2"]


def test_trim_saves_tokens_on_real_document() -> None:
    """真实相邻切片上确实省下 token，且省下的量级与重叠量相符。

    这是本模块的存在理由：同样一批证据，去掉重复段之后占用的预算更少。
    省下的量应当接近「重叠段 × 相邻对数」，只找到一两处说明检测退化了。
    """
    chunks = _real_chunks()
    before = sum(estimate_tokens(chunk.content) for chunk in chunks)
    trimmed = trim_adjacent_overlap(chunks)
    after = sum(estimate_tokens(chunk.content) for chunk in trimmed)
    removed = sum(
        estimate_tokens(_removed_piece(original.content, kept.content))
        for original, kept in zip(chunks, trimmed, strict=True)
    )
    pairs = len(chunks) - 1

    assert removed > 0, "一条都没裁掉，重叠检测失效"
    assert after == before - removed, "省下的 token 与裁掉的文字对不上"
    assert removed >= pairs * 20, f"省下的 token 明显偏少：{removed}（相邻对 {pairs} 组）"


def _removed_piece(original: str, kept: str) -> str:
    """被裁掉的那一段（前缀或后缀）。"""
    if original.endswith(kept):
        return original[: len(original) - len(kept)]
    if original.startswith(kept):
        return original[len(kept) :]
    return ""


# ---------------------------------------------------------------------------
# 接进检索管线
# ---------------------------------------------------------------------------


async def _seed(database: Database, settings: Settings, registry: ProviderRegistry) -> str:
    """写入两篇长文档，跑到全文索引就绪，返回 Space id。

    刻意只写两篇：候选池里必须出现同一篇文档的相邻切片，否则裁剪无从验证。
    """
    space = await database.spaces.create(SpaceCreate(name="新药研发", domain="新药研发"))
    pipeline = IngestPipeline(database, registry, settings)
    for index, extra in enumerate((" ".join(SECTIONS[:3]), " ".join(SECTIONS[3:]))):
        document = await pipeline.register_text(
            space_id=space.id, title=f"讲义{index}", content=f"{extra}\n\n{_document()}"
        )
        await pipeline.run(document.id, stages=["parsing", "chunking"])
    return space.id


def test_space_yaml_without_overlap_keys_still_loads(tmp_path: Path) -> None:
    """裁剪没有引入新的 space.yaml 字段，旧配置照常加载（用 diversity 总开关控制）。"""
    from agentmem.config import load_space_yaml, save_space_yaml

    path = tmp_path / "space.yaml"
    path.write_text("persona:\n  name: 新药研发专家\n", encoding="utf-8")

    config = load_space_yaml(path)

    assert config.retrieval.diversity is True
    save_space_yaml(path, config)
    assert load_space_yaml(path) == config


async def test_pipeline_trims_overlap_end_to_end(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """管线里真的裁了：同一批命中，正文总量比库里的原文少，且偏移自洽。

    只跑全文召回：本用例关心的是裁剪本身，向量与重排都不参与。
    """
    config = ModelsConfig(
        providers=[
            ProviderConfig(
                id="mock-llm",
                kind="llm",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-chat",
            )
        ],
        roles=RoleBindings(chat="mock-llm", fast="mock-llm"),
    )
    registry = ProviderRegistry(config, usage=database.usage, space_id=database.space_id)
    space_id = await _seed(database, settings, registry)
    pipeline = RetrievalPipeline(
        space_id=space_id,
        database=database,
        registry=registry,
        settings=settings,
        # 这条用例量的是「相邻切片的重叠段有没有被裁掉」，所以把兄弟合并关掉：
        # 合并会把相邻的两条并成一条，裁剪的痕迹（以及「相邻」本身）就都看不见了。
        # 合并自己的行为由 tests/test_automerge.py 钉。
        retrieval=RetrievalSettings(top_k_fts=50, top_n_rerank=8, auto_merge=False),
    )
    query = "溶解度 渗透性 代谢 稳定性 临床试验 样本量 申报 生物标志物"

    outcome = await pipeline.search(query, mode="fts", top_k=8)

    assert len(outcome.chunks) >= 2, "没召回到同一篇文档的相邻切片，用例失效"
    rows = await database.chunks.get_many([chunk.chunk_id for chunk in outcome.chunks])
    originals = {row.id: row for row in rows}
    trimmed_tokens = 0
    original_tokens = 0
    trimmed_any = False
    adjacent_pairs = 0
    for chunk in outcome.chunks:
        row = originals[chunk.chunk_id]
        original_tokens += estimate_tokens(row.content)
        trimmed_tokens += estimate_tokens(chunk.content)
        assert chunk.char_start is not None and chunk.char_end is not None
        assert chunk.char_end - chunk.char_start == len(chunk.content), "偏移与正文长度不一致"
        assert (row.char_start or 0) <= chunk.char_start
        assert chunk.char_end <= (row.char_end or len(row.content))
        assert chunk.content in row.content, "裁完的正文不是原文的一段"
        if chunk.content != row.content:
            trimmed_any = True
    # ⚠️ 必须看**所有**两两组合，不能用 itertools.pairwise。
    # pairwise 只比较结果列表里位置相邻的两条，而 MMR 的职责恰恰是打散顺序——
    # ordinal 相邻的两条几乎不会正好排在一起，用 pairwise 会恒为 0，
    # 把「裁剪没生效」误报成「用例前提不成立」。
    for left, right in itertools.combinations(outcome.chunks, 2):
        if left.document_id != right.document_id:
            continue
        if abs((left.ordinal or 0) - (right.ordinal or 0)) == 1:
            adjacent_pairs += 1

    assert adjacent_pairs >= 1, "top-N 里没有相邻切片，裁剪无从谈起"
    assert trimmed_any, "管线没有裁剪任何一条相邻切片"
    assert trimmed_tokens < original_tokens


def test_trimming_is_not_gated_by_diversity_flag() -> None:
    """关掉 diversity 开关时，重叠裁剪**仍然**要生效。

    那个开关管的是 MMR——「牺牲一点相关性换多样性」是一种策略口味，关掉合理。
    但裁掉相邻切片的重复段是零信息损失的：被裁的文字完整留在前一条里，
    没有任何理由因为用户不想要 MMR 就把同一段话送两遍。
    """
    from agentmem.retrieve.pipeline import RetrievalPipeline

    shared = "药物代谢动力学参数需要在早期就测定。清除率与半衰期决定给药间隔。"
    earlier = "化合物 A 的合成路线共七步，总收率 12%。" + shared
    # ⚠️ 独有部分必须明显超过 MIN_REMAINING_TOKENS（40），否则这条会因为
    # 「裁完没剩什么」被整条丢弃——那是正确行为，但会让本用例测不到想测的东西。
    later = (
        shared
        + "在犬类模型中半衰期为 4.2 小时，支持每日一次给药。"
        + "非人灵长类的暴露量与犬接近，但清除率略低，提示种属间差异主要来自肝脏代谢。"
        + "基于上述数据，首次人体试验的起始剂量按体表面积折算后取 10 mg。"
    )

    chunks = [
        ScoredChunk(
            chunk_id="c1",
            document_id="d1",
            ordinal=0,
            content=earlier,
            char_start=0,
            char_end=len(earlier),
            rerank_score=0.9,
        ),
        ScoredChunk(
            chunk_id="c2",
            document_id="d1",
            ordinal=1,
            content=later,
            char_start=len(earlier) - len(shared),
            char_end=len(earlier) - len(shared) + len(later),
            rerank_score=0.8,
        ),
    ]

    pipeline = RetrievalPipeline.__new__(RetrievalPipeline)
    pipeline.params = RetrievalSettings(diversity=False, top_n_rerank=8, auto_merge=False)

    out = pipeline._diversify(list(chunks), 8)

    assert len(out) == 2, "两条都该留下——裁的是重复段，不是整条"
    assert shared not in out[1].content, "关掉 diversity 后重叠段没有被裁掉"
    for chunk in out:
        assert chunk.char_end is not None and chunk.char_start is not None
        assert chunk.char_end - chunk.char_start == len(chunk.content), "偏移与正文长度不一致"


async def test_pipeline_merges_siblings_end_to_end(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """管线里真的合了：同一节里连号的命中并成一条，偏移仍能在原文里切出同样的文字。

    这条与上面那条互为对照——上面关掉合并量裁剪，这里打开合并量「条数变少、
    信息没丢」。偏移自洽是重点：错位不会报错，只会让引用高亮悄悄指到别的段落。
    """
    config = ModelsConfig(
        providers=[
            ProviderConfig(
                id="mock-llm",
                kind="llm",
                adapter="openai_compatible",
                base_url=mock_server,
                model="mock-chat",
            )
        ],
        roles=RoleBindings(chat="mock-llm", fast="mock-llm"),
    )
    registry = ProviderRegistry(config, usage=database.usage, space_id=database.space_id)
    space_id = await _seed(database, settings, registry)
    query = "溶解度 渗透性 代谢 稳定性 临床试验 样本量 申报 生物标志物"

    def _pipeline(auto_merge: bool) -> RetrievalPipeline:
        return RetrievalPipeline(
            space_id=space_id,
            database=database,
            registry=registry,
            settings=settings,
            retrieval=RetrievalSettings(top_k_fts=50, top_n_rerank=8, auto_merge=auto_merge),
        )

    without = await _pipeline(False).search(query, mode="fts", top_k=8)
    with_merge = await _pipeline(True).search(query, mode="fts", top_k=8)

    merged = [chunk for chunk in with_merge.chunks if chunk.merged_from]
    assert merged, "这批命中里没有可合并的兄弟切片，用例失效"

    for chunk in merged:
        assert chunk.char_start is not None and chunk.char_end is not None
        assert chunk.char_end - chunk.char_start == len(chunk.content), "偏移与正文长度不一致"
        assert len(chunk.merged_from) >= 2

    # 合并腾出来的名额由后面的证据顶上：同样是 8 条，覆盖的原文更多
    covered_without = sum(len(chunk.content) for chunk in without.chunks)
    covered_with = sum(len(chunk.content) for chunk in with_merge.chunks)
    assert covered_with >= covered_without, "合并之后覆盖的原文不该变少"
