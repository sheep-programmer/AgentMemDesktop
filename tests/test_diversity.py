"""检索结果的冗余抑制：相似度、近重复丢弃、MMR 多样性重排与管线接入。

这里守的是同一件事的多个侧面：**别把重复文字当成两条证据送进上下文，
也别为了去重把独有内容一起丢掉**。数值断言都写在用例里，
阈值或相似度定义被改动时会直接失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentmem.config import ModelsConfig, Settings, load_space_yaml, save_space_yaml
from agentmem.ingest import IngestPipeline
from agentmem.ingest.chunk import split_markdown
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve import (
    RetrievalPipeline,
    ScoredChunk,
    mmr_rerank,
    suppress_redundant,
    text_similarity,
)
from agentmem.store import Database
from agentmem.types import ProviderConfig, RetrievalSettings, RoleBindings, SpaceCreate

#: 十段彼此独立的正文，每段约 250~350 token，模拟知识库里的真实段落
PARAGRAPHS = [
    (
        "成药性评估通常在先导化合物优化之后、候选化合物确定之前进行，目的是尽早筛掉那些"
        "理化性质或药代行为注定无法成药的分子。评估围绕四个方面展开：溶解度、渗透性、"
        "代谢稳定性与安全性。溶解度决定了药物能否在胃肠道中溶出并被吸收，一般要求在生理"
        "pH 范围内达到足够的浓度，否则即便靶点活性再好，口服吸收也会受限。渗透性关系到"
        "药物能否穿过肠上皮细胞进入血液循环，Caco-2 单层细胞模型是体外评价的常用手段，"
        "结果通常与人体吸收分数做相关性分析。代谢稳定性关注化合物在肝微粒体或肝细胞中的"
        "清除速率，半衰期过短意味着首过效应严重，需要提高给药剂量才能维持有效血药浓度。"
        "安全性方面则重点看脱靶活性与结构警示，例如 hERG 通道抑制与 Ames 致突变性。"
        "这四项指标相互制约，改善其中一项往往要以牺牲另一项为代价，因此评估的目标不是"
        "每一项都做到最好，而是在可接受的范围内找到平衡点。"
    ),
    (
        "药代动力学研究回答的是药物在体内随时间变化的全过程，包括吸收、分布、代谢与排泄。"
        "吸收环节关注达峰时间与峰浓度，缓释制剂会刻意拉长达峰时间以平抑血药浓度波动。"
        "分布环节的核心参数是表观分布容积，数值大说明药物广泛分布到组织，数值小则提示"
        "药物主要停留在血液中。血浆蛋白结合率高的化合物，游离药物浓度低，药效可能不足，"
        "同时也会影响药物相互作用的风险评估。代谢环节要区分肝代谢与肝外代谢，并识别"
        "参与转化的主要酶亚型，CYP3A4、CYP2D6 这类多态性明显的酶往往带来个体差异。"
        "排泄环节则要看肾脏清除与胆汁排泄的比例。整体上，药代研究要回答的是给药方案"
        "能否支撑目标适应症的暴露水平，暴露不足则疗效无从谈起。"
    ),
    (
        "动物模型的选择直接决定了临床前数据的可外推程度。小鼠模型成本低、遗传背景清晰，"
        "适合做机制验证与大规模筛选，但代谢酶谱与人体差异较大，药代数据外推需要谨慎。"
        "大鼠体型更大，便于反复采血，是药代研究的常规选择。犬与非人灵长类的生理结构更"
        "接近人体，常用于安全性评价，尤其是心血管与神经系统毒性。疾病模型则要区分"
        "同种移植、异种移植与基因工程模型，各自的免疫背景不同，对药效评价的干扰也不同。"
        "值得注意的是，模型动物上的有效剂量不能直接换算成人体剂量，体表面积换算只是"
        "粗略估计，最终仍需依靠人体首次试验的剂量递增来确认。"
    ),
    (
        "临床试验设计要在一开始就明确主要终点与次要终点，并在方案中事先约定统计分析"
        "方法，否则事后挑选终点会让结论失去可信度。随机化与盲法是控制偏倚的两条基本"
        "手段，开放标签设计只适用于那些无法设盲的场景，例如手术与器械类干预。样本量"
        "估算需要基于预期效应量与变异度，低估变异度会导致试验把握度不足，最终得出"
        "无效的结论，而真实差异其实存在。期中分析可以提前终止无效或显著有效的试验，"
        "但会消耗总体一类错误率，必须按预先规定的消耗函数执行。入排标准过严会让结论"
        "难以推广到真实人群，过宽则会引入大量混杂因素，需要在方案阶段权衡。"
    ),
    (
        "监管申报的资料准备贯穿研发全程，而不是在提交前集中补齐。药学部分要说明原料药"
        "与制剂的生产工艺、质量标准与方法学验证，任何工艺变更都可能需要补充稳定性数据。"
        "非临床部分要求药理、药代与毒理数据相互印证，毒理试验的剂量设置要有暴露量依据，"
        "不能只按毫克每千克简单递增。临床部分则按适应症与人群分层呈现安全性与有效性"
        "数据。沟通交流是降低申报风险的有效手段，关键节点前与监管机构确认技术要求，"
        "可以避免在完整资料提交后才发现设计缺陷。申报资料的撰写质量直接影响审评周期，"
        "数据可追溯、结论有依据的申报材料往往能显著缩短问询轮次。"
    ),
    (
        "生物标志物的验证分为分析验证与临床验证两个层面。分析验证关注检测方法的准确度、"
        "精密度、灵敏度与线性范围，方法学不过关，后续所有结论都失去基础。临床验证则要"
        "证明该标志物与疾病状态、疗效或安全性之间存在稳定关联，并在独立队列中重复。"
        "富集人群设计可以提高试验效率，前提是标志物的阳性预测值足够高，否则会排除掉"
        "本可获益的患者。替代终点能否被接受取决于它与临床终点的关联强度以及生物学合理性，"
        "监管机构对替代终点的接受度在不同适应症之间差别很大。"
    ),
    (
        "药物相互作用研究要覆盖代谢酶与转运体两条路径。代谢层面的相互作用多来自酶抑制"
        "或酶诱导，前者让合并用药的血药浓度骤升，后者则可能让疗效在几天内消失。"
        "转运体的影响常被忽略，P-糖蛋白与 OATP 底物在肠道与肝脏的处置差异，足以改变"
        "整个暴露曲线。研究方法上，先做体外抑制筛选，再按抑制常数与临床暴露量的比值"
        "判断是否需要开展临床试验。诱导作用无法用体外数据直接外推，通常要依靠人体"
        "试验中的探针底物来确认。合并用药普遍的患者人群，相互作用的风险评估必须前置。"
    ),
    (
        "制剂开发要在生物利用度、稳定性与可生产性之间找平衡。难溶性化合物常通过成盐、"
        "共晶或无定形固体分散体提高溶出，但无定形体系本身处于高能态，存放过程中可能"
        "析晶，稳定性研究的取样点必须覆盖这一风险。缓释制剂可以减少给药频次，"
        "但一旦出现剂量倾泻，安全性风险远高于普通制剂，因此体外释放曲线的相似性评价"
        "与酒精诱导释放试验都不能省。生产工艺的放大效应同样关键，实验室规模的搅拌与"
        "干燥条件放到生产线上往往不再等效，关键工艺参数需要重新确认。"
    ),
    (
        "药效学评价要回答暴露量与效应之间的关系，为剂量选择提供依据。量效曲线在低剂量段"
        "常常平缓，剂量递增带来的效应增益有限，而副作用可能已经出现，因此最大耐受剂量"
        "与最小有效剂量之间的窗口才是可用的剂量区间。效应指标要区分替代指标与临床结局，"
        "替代指标变化明显但临床结局无改善的情况并不罕见。给药方案的优化还要考虑"
        "谷浓度是否持续覆盖靶点，暴露量波动大的药物即便平均浓度达标，疗效也可能不稳定。"
    ),
]


def repost(text: str, replacements: tuple[tuple[str, str], ...]) -> str:
    """模拟转载：只改几处措辞，其余原样照抄。"""
    for source, target in replacements:
        text = text.replace(source, target)
    return text


#: 默认阈值，用例直接引用配置默认值，避免同一件事在测试里再写一个数
DEFAULT_THRESHOLD = RetrievalSettings().dedup_threshold


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _chunk(
    chunk_id: str, content: str, *, score: float = 1.0, document_id: str = "doc-1"
) -> ScoredChunk:
    """构造一条命中；分数只用于验证它不影响重排。"""
    return ScoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        document_title=f"文档 {document_id}",
        content=content,
        rerank_score=score,
    )


def _ids(chunks: list[ScoredChunk]) -> list[str]:
    """取命中 id 列表，用于断言顺序。"""
    return [chunk.chunk_id for chunk in chunks]


def _worst_pair(chunks: list[ScoredChunk]) -> float:
    """命中集合里最相似的一对的重合度（少于两条时为 0）。"""
    worst = 0.0
    for index, chunk in enumerate(chunks):
        for other in chunks[index + 1 :]:
            worst = max(worst, text_similarity(chunk.content, other.content))
    return worst


def _split_corpus(paragraphs: list[str]) -> list[ScoredChunk]:
    """用真实切片器把正文切开，模拟检索命中的候选。

    直接用切片产物而不是手写的等长文本，是为了让「相邻切片共享一段重叠」
    这个前提真实成立，而不是靠构造巧合。
    """
    drafts = split_markdown("\n\n".join(paragraphs), target_tokens=512, overlap_tokens=80)
    return [
        _chunk(f"c{index}", draft.content, score=1.0 - index * 0.05)
        for index, draft in enumerate(drafts)
    ]


# ---------------------------------------------------------------------------
# text_similarity
# ---------------------------------------------------------------------------


def test_similarity_is_reflexive_and_symmetric() -> None:
    """自己跟自己恒为 1.0，且与参数顺序无关。"""
    left = PARAGRAPHS[0]
    right = PARAGRAPHS[2]

    assert text_similarity(left, left) == 1.0
    assert text_similarity(left, right) == pytest.approx(text_similarity(right, left))


def test_unrelated_texts_stay_below_point_two() -> None:
    """两段不相关的正文重合度必须很低，否则去重会误杀正常证据。"""
    similarity = text_similarity(PARAGRAPHS[0], PARAGRAPHS[2])

    assert similarity < 0.2, f"不相关文本被判得过高：{similarity}"


def test_adjacent_chunk_overlap_is_not_a_duplicate() -> None:
    """相邻切片的重叠只是各自正文的一小部分，绝不能整条丢掉。

    切片默认带 80 token 重叠，两条相邻切片因此共享一小段文字；但按整条丢弃，
    等于连它们各自独有的正文一起扔了。这里用真实切片产物断言：
    相似度远低于阈值，两条都保留。
    """
    chunks = _split_corpus(PARAGRAPHS[:2])
    assert len(chunks) >= 2, "语料没有切出两条切片，用例前提不成立"

    similarity = text_similarity(chunks[0].content, chunks[1].content)
    assert 0.05 < similarity < DEFAULT_THRESHOLD, f"相邻切片的重合度落在意料之外：{similarity}"

    kept = suppress_redundant(chunks, threshold=DEFAULT_THRESHOLD, keep_min=1)
    assert _ids(kept) == _ids(chunks), "相邻切片被当成重复丢掉了"


def test_overlap_dominated_short_chunks_are_duplicates() -> None:
    """重叠占满整条正文时就该判为重复：两条短切片共享 80 字尾首。

    切片很短时（例如文档由大量短段落组成），80 token 的重叠能覆盖后一条的大半，
    它几乎没有新信息。这与上一条并不矛盾：判据是「后一条有多少比例已经被前一条
    覆盖」，而不是「有没有重叠」。
    """
    first = "".join(
        f"第{index}组溶解度数据在 pH 6.8 缓冲液中测得，批间差异小于百分之三。" for index in range(2)
    )
    second = first[10:] + "补充：该批样品需在四摄氏度下避光保存。"
    chunks = [_chunk("c1", first), _chunk("c2", second)]

    similarity = text_similarity(first, second)
    assert similarity > DEFAULT_THRESHOLD, f"重叠占主导的两条短切片未被判为重复：{similarity}"
    assert _ids(suppress_redundant(chunks, threshold=DEFAULT_THRESHOLD, keep_min=1)) == ["c1"]


# ---------------------------------------------------------------------------
# suppress_redundant
# ---------------------------------------------------------------------------


def test_suppress_drops_duplicates_and_keeps_order() -> None:
    """丢掉近重复，保留的条目顺序不变。"""
    chunks = [
        _chunk("c1", PARAGRAPHS[0]),
        _chunk(
            "c2",
            repost(PARAGRAPHS[0], (("成药性评估", "成药性评价"), ("首过效应严重", "首过效应明显"))),
        ),
        _chunk("c3", PARAGRAPHS[1]),
        _chunk("c4", PARAGRAPHS[1]),
        _chunk("c5", PARAGRAPHS[2]),
    ]

    kept = suppress_redundant(chunks, threshold=DEFAULT_THRESHOLD, keep_min=1)

    assert _ids(kept) == ["c1", "c3", "c5"]


def test_suppress_keeps_keep_min_even_at_zero_threshold() -> None:
    """阈值激进到 0.0 时仍然有保底条数，结果不会被清空。"""
    chunks = [_chunk(f"c{index}", PARAGRAPHS[0]) for index in range(8)]

    kept = suppress_redundant(chunks, threshold=0.0, keep_min=3)

    assert _ids(kept) == ["c0", "c1", "c2"]


# ---------------------------------------------------------------------------
# mmr_rerank
# ---------------------------------------------------------------------------


def _ranked_chunks(scores: list[float]) -> list[ScoredChunk]:
    """一组内容固定、分数可换的候选（顺序即相关性顺序）。"""
    return [
        _chunk(f"c{index}", content, score=score)
        for index, (content, score) in enumerate(
            zip(PARAGRAPHS[: len(scores)], scores, strict=True)
        )
    ]


def test_mmr_is_invariant_to_score_scale() -> None:
    """同一组排序、换一种分数量纲，MMR 的输出顺序必须完全相同。

    各家 rerank 的分不可比：Cohere 是 0~1，本地 CrossEncoder 是原始 logits
    （可负、无界），没配 reranker 时退回的 RRF 分彼此只差千分之几。
    相关性一律由名次推导，因此下面三组候选的结果必须逐条相同——
    这条挂了，说明重排里又混进了原始分数。
    """
    cohere = [0.95, 0.88, 0.80, 0.72, 0.64, 0.55]
    logits = [6.2, 5.1, 3.4, 2.0, 0.5, -1.2]
    rrf = [0.0164, 0.0161, 0.0159, 0.0156, 0.0154, 0.0152]

    orders = [
        _ids(mmr_rerank(_ranked_chunks(scores), lambda_=0.7, limit=4))
        for scores in (cohere, logits, rrf)
    ]

    assert orders[0] == orders[1] == orders[2] == ["c0", "c1", "c2", "c3"]


def test_mmr_with_lambda_one_keeps_relevance_order() -> None:
    """λ=1.0 时只剩相关性，输出就是输入顺序（重复也不让位）。"""
    chunks = _ranked_chunks([0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    chunks[1] = _chunk(
        "c1",
        repost(PARAGRAPHS[0], (("溶解度", "溶解性"), ("Caco-2 单层细胞模型", "Caco-2 细胞模型"))),
        score=0.8,
    )

    assert _ids(mmr_rerank(chunks, lambda_=1.0, limit=None)) == _ids(chunks)


def test_mmr_with_lambda_zero_prefers_diversity() -> None:
    """λ=0.0 时第二条换成与首条最不相似的那条。"""
    chunks = [
        _chunk("near", PARAGRAPHS[0]),
        _chunk("other", PARAGRAPHS[3]),
        _chunk(
            "copy", repost(PARAGRAPHS[0], (("渗透性", "透过性"), ("首过效应严重", "首过效应明显")))
        ),
    ]

    assert _ids(mmr_rerank(chunks, lambda_=0.0, limit=3)) == ["near", "other", "copy"]


def test_mmr_handles_empty_single_and_zero_limit() -> None:
    """空输入、单条输入、limit=0 都不崩。"""
    single = _chunk("c1", PARAGRAPHS[0])

    assert mmr_rerank([], lambda_=0.7, limit=5) == []
    assert _ids(mmr_rerank([single], lambda_=0.7, limit=5)) == ["c1"]
    assert mmr_rerank([single], lambda_=0.7, limit=0) == []
    assert suppress_redundant([], threshold=DEFAULT_THRESHOLD, keep_min=3) == []


# ---------------------------------------------------------------------------
# 配置：旧 space.yaml 必须照常加载
# ---------------------------------------------------------------------------


def test_space_yaml_without_diversity_keys_still_loads(tmp_path: Path) -> None:
    """旧 space.yaml 没有新字段，加载时落到默认值，写回后能原样往返。

    space.yaml 是用户手上的文件，升级后不能因为多了几个字段就打不开——
    那等于把整个 Space 变成不可用。
    """
    path = tmp_path / "space.yaml"
    path.write_text(
        "persona:\n  name: 新药研发专家\nretrieval:\n  top_k_vector: 30\n  hyde: true\n",
        encoding="utf-8",
    )

    config = load_space_yaml(path)

    assert config.retrieval.top_k_vector == 30
    assert config.retrieval.hyde is True
    assert config.retrieval.diversity is True
    assert config.retrieval.mmr_lambda == 0.7
    assert config.retrieval.dedup_threshold == 0.85

    save_space_yaml(path, config)
    assert load_space_yaml(path) == config


def test_retrieval_settings_validates_diversity_values() -> None:
    """显式给出的多样性参数被接受，越界值被拒绝。"""
    params = RetrievalSettings(diversity=False, mmr_lambda=0.0, dedup_threshold=1.0)

    assert params.diversity is False
    assert params.mmr_lambda == 0.0
    assert params.dedup_threshold == 1.0
    with pytest.raises(ValidationError):
        RetrievalSettings(mmr_lambda=1.5)
    with pytest.raises(ValidationError):
        RetrievalSettings(dedup_threshold=-0.1)


# ---------------------------------------------------------------------------
# 接进检索管线
# ---------------------------------------------------------------------------


def _config(*, mock_server: str, rerank_base: str | None = None) -> ModelsConfig:
    """构造检索用的模型配置：llm 必配，rerank 按需绑定。

    不绑 embedding：用例只走全文召回，候选顺序完全由 BM25 / rerank 决定，
    不掺入常量向量带来的并列。
    """
    providers = [
        ProviderConfig(
            id="mock-llm",
            kind="llm",
            adapter="openai_compatible",
            base_url=mock_server,
            model="mock-chat",
        )
    ]
    roles = RoleBindings(chat="mock-llm", fast="mock-llm")
    if rerank_base is not None:
        providers.append(
            ProviderConfig(
                id="mock-rerank",
                kind="rerank",
                adapter="cohere_rerank",
                base_url=rerank_base,
                model="mock-rerank",
            )
        )
        roles.rerank = "mock-rerank"
    return ModelsConfig(providers=providers, roles=roles)


async def _seed_documents(
    database: Database, settings: Settings, registry: ProviderRegistry, items: dict[str, str]
) -> str:
    """写入文档并跑到全文索引就绪，返回 Space id。

    只跑解析与切片：本文件关心的是候选顺序与去重，向量化既不影响全文检索的
    名次，也不需要真的调 embedding 服务。
    """
    space = await database.spaces.create(SpaceCreate(name="新药研发", domain="新药研发"))
    pipeline = IngestPipeline(database, registry, settings)
    for title, content in items.items():
        document = await pipeline.register_text(space_id=space.id, title=title, content=content)
        await pipeline.run(document.id, stages=["parsing", "chunking"])
    return space.id


def _pipeline(
    database: Database,
    registry: ProviderRegistry,
    settings: Settings,
    *,
    space_id: str,
    params: RetrievalSettings,
) -> RetrievalPipeline:
    """构造检索管线。"""
    return RetrievalPipeline(
        space_id=space_id,
        database=database,
        registry=registry,
        settings=settings,
        retrieval=params,
    )


async def test_pipeline_dedups_rrf_fallback_without_losing_slots(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """回退分支（没配 reranker）也要去重，且条数由后续候补顶上。

    RRF 只看名次，没有任何语义去重能力：同一段话的三个版本在它眼里是三条
    高分命中。这里让同一段正文以三个版本进入候选，断言最终 8 条里它只出现
    一次，且**条数仍是 8**——腾出来的位置由后面的候选补上，而不是让结果变短。
    """
    duplicated = PARAGRAPHS[0]
    dup_titles = {"原始笔记", "转载存档", "内部讲义"}
    corpus = {
        "原始笔记": duplicated,
        "转载存档": repost(
            duplicated, (("成药性评估", "成药性评价"), ("Caco-2 单层细胞模型", "Caco-2 细胞模型"))
        ),
        "内部讲义": repost(duplicated, (("脱靶活性", "非靶点活性"), ("结构警示", "结构警告"))),
        **{f"资料{index}": text for index, text in enumerate(PARAGRAPHS[1:], start=1)},
    }
    registry = ProviderRegistry(
        _config(mock_server=mock_server), usage=database.usage, space_id=database.space_id
    )
    space_id = await _seed_documents(database, settings, registry, corpus)
    params = RetrievalSettings(top_k_fts=50, top_n_rerank=8)
    query = "药物 研究 数据 评估 剂量 临床 试验 模型"

    # 先确认候选池里确实有重复：全量召回一次，否则后面的断言可能只是在测空气
    widest = await _pipeline(
        database,
        registry,
        settings,
        space_id=space_id,
        params=params.model_copy(update={"diversity": False}),
    ).search(query, mode="fts", top_k=50)

    with_diversity = await _pipeline(
        database, registry, settings, space_id=space_id, params=params
    ).search(query, mode="fts", top_k=8)
    without_diversity = await _pipeline(
        database,
        registry,
        settings,
        space_id=space_id,
        params=params.model_copy(update={"diversity": False}),
    ).search(query, mode="fts", top_k=8)

    assert _worst_pair(widest.chunks) > DEFAULT_THRESHOLD, "语料没有把近重复召回来，用例失效"
    assert len(without_diversity.chunks) == 8
    assert len(with_diversity.chunks) == 8, "去重后条数变少了，候补没有补上位置"
    assert len({chunk.chunk_id for chunk in with_diversity.chunks}) == 8
    assert _worst_pair(with_diversity.chunks) <= DEFAULT_THRESHOLD
    assert sum(chunk.document_title in dup_titles for chunk in with_diversity.chunks) == 1, (
        "同一段正文的三个版本没有被收敛成一条"
    )


async def test_pipeline_dedups_after_rerank(
    database: Database, settings: Settings, mock_server: str
) -> None:
    """重排分支同样要去重：重排只改顺序，不会让重复内容自己消失。"""
    duplicated = PARAGRAPHS[0] + PARAGRAPHS[1]
    corpus = {
        "原始笔记": duplicated,
        "转载存档": repost(
            duplicated,
            (
                ("成药性评估", "成药性评价"),
                ("Caco-2 单层细胞模型", "Caco-2 细胞模型"),
                ("排泄", "消除"),
            ),
        ),
        **{f"资料{index}": text for index, text in enumerate(PARAGRAPHS[2:], start=2)},
    }
    registry = ProviderRegistry(
        _config(mock_server=mock_server, rerank_base=mock_server),
        usage=database.usage,
        space_id=database.space_id,
    )
    space_id = await _seed_documents(database, settings, registry, corpus)
    params = RetrievalSettings(top_k_fts=50, top_n_rerank=8)
    # 查询词覆盖全部段落，保证候选池够宽；重排由 mock 服务按长度降序给分，
    # 因此那两条最长的重复正文会被排到最前面，正好用来验证重排后仍然去重
    query = "药物 研究 数据 评估 剂量 临床 试验 模型"

    with_diversity = await _pipeline(
        database, registry, settings, space_id=space_id, params=params
    ).search(query, mode="hybrid", top_k=8)
    without_diversity = await _pipeline(
        database,
        registry,
        settings,
        space_id=space_id,
        params=params.model_copy(update={"diversity": False}),
    ).search(query, mode="hybrid", top_k=8)

    assert with_diversity.reranked is True and without_diversity.reranked is True
    assert _worst_pair(without_diversity.chunks) > DEFAULT_THRESHOLD, (
        "语料没把近重复送进 top-8，用例失效"
    )

    assert len(with_diversity.chunks) == len(without_diversity.chunks) == 8
    assert _worst_pair(with_diversity.chunks) <= DEFAULT_THRESHOLD
