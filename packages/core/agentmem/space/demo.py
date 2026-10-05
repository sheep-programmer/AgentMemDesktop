"""示例 Space：不配模型也能看见这个产品的样子。

第一次打开应用时，六个页面全是空态——用户得先配好 provider、投喂资料、等摄取跑完，
才可能看到一点点「进化」的影子。这个模块用一批**写死的、自洽的**示例数据把一个
真实使用过的 Space 铺出来：文档与切片、知识卡片、实体关系图、L4 经验（含一条待裁决
的冲突）、测验集与两次评测记录、一条待蒸馏的纠错反馈、以及一段上扬的专家度曲线。

两条刻意的约束：

1. **不调用任何模型。** 切片走真实的切分器（所以偏移与引用高亮都成立），
   但嵌入与抽取不跑——没有 provider 时向量腿自动退化为全文检索，页面照常可用。
   只在配置了 embedding 时才补一次向量化，那是锦上添花，不是前提。
2. **数据要自洽。** 卡片与经验都挂在真实存在的 chunk 上，评测记录里的分数与
   指标互相对得上。示例数据一旦自相矛盾，用户第一次下钻就会发现，反而更糟。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agentmem.expert.expertise import ExpertiseService
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.store import Database
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    Citation,
    ConversationCreate,
    DocumentUpdate,
    DomainOutlineCreate,
    EntityCreate,
    EvalItem,
    EvalItemCreate,
    EvalItemScore,
    EvalRunCreate,
    EvalRunDetail,
    EvolutionRunCreate,
    ExpertiseSnapshotCreate,
    FeedbackCreate,
    InsightCreate,
    InsightEventCreate,
    KnowledgeCardCreate,
    MessageCreate,
    MessageUpdate,
    OutlineNode,
    RelationCreate,
    RetrievalMetrics,
    Space,
    SpaceCreate,
    TraceCreate,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型标注
    from agentmem.space.runtime import Runtime

logger = structlog.get_logger(__name__)

#: 示例 Space 的名字；同名 Space 已存在时直接复用，不重复创建
DEMO_SPACE_NAME = "示例 · 新药研发"
DEMO_DOMAIN = "小分子药物发现（ADMET 早期评价）"

#: 一天有多少毫秒。示例数据的时间戳要看起来像「过去几周慢慢长出来的」
_DAY_MS = 24 * 60 * 60 * 1000

#: (标题, 正文, 文档级上下文)。第三项写死是刻意的：示例 Space 要在**没有模型**的
#: 情况下也能看到「文档概要」这条能力——真实摄取时它由 LLM 生成，这里给一段人写的。
_DOCUMENTS: list[tuple[str, str, str]] = [
    (
        "先导化合物成药性评价规程.md",
        """# 早期成药性评价规程

## 为什么要早筛

先导化合物优化的早期如果只看靶点活性，后期失败率会显著上升。活性好但溶解度差、
口服吸收不达标的化合物，进入体内实验阶段才暴露问题，代价是整条研发线的推迟。

## 五项基本指标

1. 溶解度（kinetic solubility，pH 7.4）
2. 膜通透性（Caco-2 或 PAMPA）
3. 代谢稳定性（肝微粒体孵育，测 CLint）
4. 血浆蛋白结合率（平衡透析法）
5. hERG 抑制（膜片钳，早期用荧光法预筛）

## 常见的判定线

- 动力学溶解度低于 10 µM 时，口服给药的剂量会迅速变得不可接受。
- 肝微粒体 CLint 高于 50 µL/min/mg 时需谨慎推进，通常提示首过效应显著。
- hERG IC50 与游离暴露量之比低于 30 时，心脏毒性风险需要重点评估。
""",
        "本文档是早期成药性评价的操作规程：规定了先导化合物优化前必须补齐的五项指标"
        "（溶解度、膜通透性、代谢稳定性、血浆蛋白结合与 hERG 抑制），给出各自的判定线"
        "（如溶解度 10 µM、CLint 50 µL/min/mg），并说明为什么只看靶点活性会在后期造成"
        "更大的失败代价。适用于判断一个高活性化合物能否推进到体内实验。",
    ),
    (
        "CYP 抑制与药物相互作用.md",
        """# CYP 抑制与药物相互作用

## 为什么关心 CYP

CYP450 家族负责大多数小分子药物的氧化代谢。化合物若能抑制主要亚型，与其它药物
联用时就会抬高对方的暴露量，风险在窄治疗窗药物上尤其明显。

## 需要测的亚型

CYP3A4、2D6、2C9、2C19、1A2 是常规组合。3A4 的底物最广，优先排除时间依赖性抑制。

## 判定口径

以 IC50 与游离 Cmax 的比值作为初筛依据。比值低于 10 时需要做更细的机制实验，
包括 IC50 shift 与 NADPH 依赖性检查，用来区分可逆抑制与时间依赖性抑制。
""",
        "本文档讲 CYP450 抑制与药物相互作用：需要常规测哪些亚型（3A4 / 2D6 / 2C9 / 2C19 /"
        "1A2）、以 IC50 与游离 Cmax 的比值作为初筛依据、以及如何用 IC50 shift 与 NADPH"
        "依赖性实验区分可逆抑制与时间依赖性抑制（TDI）。适用于评估联用药物的暴露量风险。",
    ),
    (
        "hERG 心脏毒性反筛.md",
        """# hERG 心脏毒性反筛

## 靶点是 hERG 钾通道

hERG 阻断会延长 QT 间期，是药物撤市最常见的原因之一。在早期反筛能显著降低
后期失败代价。

## 分级策略

第一级：荧光法或结合实验，通量高，用于快速淘汰。
第二级：膜片钳（手动或全自动），给出可靠的 IC50。

## 判定要点

关注游离药物浓度而不是总浓度。血浆蛋白结合率高时，总 Cmax 达标并不代表安全，
需要把游离分数代入计算。
""",
        "本文档讨论 hERG 心脏毒性反筛：hERG 阻断会延长 QT 间期，是药物撤市最常见的原因"
        "之一；给出两级反筛策略（荧光法 / 结合实验做高通量淘汰，膜片钳给可靠 IC50），"
        "并强调判定必须用游离药物浓度而不是总 Cmax。适用于早期排除心脏毒性风险。",
    ),
]

_CARDS: list[tuple[str, str, str, str]] = [
    (
        "procedure",
        "早期成药性五项反筛",
        "先做溶解度与通透性，再做肝微粒体稳定性，最后补 hERG 与血浆蛋白结合。"
        "五项里任何一项明显不达标，都应先回到结构优化，不要急着推进体内实验。",
        "第 0 号切片",
    ),
    (
        "fact",
        "溶解度阈值 10 µM",
        "动力学溶解度低于 10 µM 时，达到治疗暴露所需的剂量往往超出可接受范围。",
        "第 0 号切片",
    ),
    (
        "pitfall",
        "只比较总浓度会漏掉心脏毒性",
        "hERG 风险要按游离药物浓度评估。血浆蛋白结合率高的化合物，总 Cmax 看着安全，"
        "游离浓度却可能已经超过 IC50 的警戒线。",
        "第 2 号切片",
    ),
    (
        "concept",
        "时间依赖性抑制（TDI）",
        "与可逆抑制不同，TDI 在预孵育后抑制强度会上升，单次 IC50 测不出来，"
        "需要做 IC50 shift 实验确认。",
        "第 1 号切片",
    ),
]

_ENTITIES: list[tuple[str, str, str]] = [
    ("hERG", "靶点", "钾离子通道，阻断会延长 QT 间期"),
    ("CYP3A4", "酶", "主要药物代谢酶之一，底物谱最广"),
    ("溶解度", "理化性质", "决定口服给药可及剂量的上限"),
    ("膜通透性", "理化性质", "Caco-2 / PAMPA 测得的跨膜能力"),
    ("血浆蛋白结合率", "理化性质", "影响游离药物暴露量"),
    ("肝微粒体稳定性", "实验", "用 CLint 表示代谢快慢"),
]

_RELATIONS: list[tuple[str, str, str]] = [
    ("hERG", "血浆蛋白结合率", "受其影响"),
    ("溶解度", "膜通透性", "共同决定"),
    ("CYP3A4", "肝微粒体稳定性", "决定"),
    ("hERG", "心脏毒性", "导致"),
    ("CYP3A4", "药物相互作用", "引起"),
]

_INSIGHTS: list[tuple[str, str, str, str, str, float, int, int]] = [
    (
        "询问某先导化合物能否直接推进时",
        "先要求 ADMET 五项数据，任何一项缺失都先补测再给结论",
        "一次纠错：通用建议让用户以为活性达标即可推进，忽略了 hERG 风险",
        "correction",
        "active",
        0.72,
        9,
        8,
    ),
    (
        "评估心脏毒性风险时",
        "用游离药物浓度而不是总 Cmax 去和 hERG IC50 比",
        "血浆蛋白结合率高的化合物，用总浓度会低估风险",
        "heuristic",
        "active",
        0.65,
        6,
        5,
    ),
    (
        "回答涉及 CYP 抑制的问题时",
        "先问清楚是否做了 IC50 shift，时间依赖性抑制不能用单次 IC50 下结论",
        "可逆抑制与 TDI 的处理方式不同，混淆会导致错误的联用结论",
        "constraint",
        "active",
        0.58,
        4,
        3,
    ),
    (
        "问到具体的给药方案时",
        "直接给出一个经验剂量区间",
        "被注入 7 次只有 1 次收到好评：剂量需要按暴露量与肝肾功能个体化，助手不该给单点值",
        "heuristic",
        "active",
        0.42,
        7,
        1,
    ),
    (
        "用户要求推荐具体化合物时",
        "只给筛选标准与判据，不替用户选定化合物",
        "领域责任边界：选品决策需要实验数据支撑，助手不宜越界",
        "preference",
        "candidate",
        0.4,
        2,
        1,
    ),
    (
        "讨论溶解度与通透性优先级时",
        "先解决溶解度",
        "与另一条经验冲突：通透性差的化合物同样难以达到有效暴露",
        "heuristic",
        "conflicted",
        0.35,
        1,
        0,
    ),
]

_EVAL_ITEMS: list[tuple[str, str, list[str], list[str]]] = [
    (
        "评估一个高活性先导化合物能否推进时，除了靶点活性还必须看哪些数据？",
        "必须评估 ADMET 五项与靶点选择性。只盯 IC50 会把高活性但成药性差的化合物推进到体内实验。",
        ["ADMET", "选择性"],
        ["成药性", "流程"],
    ),
    (
        "血浆蛋白结合率高的化合物，评估 hERG 风险时要注意什么？",
        "要用游离药物浓度而不是总 Cmax 计算安全边界，否则会低估心脏毒性风险。",
        ["游离浓度"],
        ["心脏毒性", "hERG"],
    ),
    (
        "某化合物单次测得的 CYP3A4 IC50 很高，能否据此认为没有相互作用风险？",
        "不能。需要做 IC50 shift 排除时间依赖性抑制，TDI 在预孵育后抑制会明显增强。",
        ["IC50 shift", "时间依赖性"],
        ["代谢", "CYP"],
    ),
]


async def seed_demo_space(runtime: Runtime, *, name: str = DEMO_SPACE_NAME) -> Space:
    """创建一个铺满示例数据的 Space；同名 Space 已存在时直接返回它。

    Returns:
        创建或复用的 Space。
    """
    existing = await _find_by_name(runtime, name)
    if existing is not None:
        logger.info("demo_space_reused", space_id=existing.id, name=name)
        return existing

    space = await runtime.spaces.create_space(SpaceCreate(name=name, domain=DEMO_DOMAIN))
    database = await runtime.spaces.space_db(space.id)
    pipeline = IngestPipeline(
        database, await runtime.registry_for_space(space.id), runtime.settings
    )

    base = now_ms() - 21 * _DAY_MS

    # ① 文档与切片：走真实切分器（偏移因此与正文严格对应，引用高亮可用）
    chunk_ids: list[str] = []
    documents: list[str] = []
    for title, content, summary in _DOCUMENTS:
        document = await pipeline.register_text(space_id=space.id, title=title, content=content)
        # 先写入写死的文档级上下文：解析阶段在拿不到模型时会保留既有值，
        # 切分阶段据此生成概要切片——「文档概要」这条能力因此不依赖模型
        meta = document.meta.model_copy(update={"context_summary": summary})
        await database.documents.update(document.id, DocumentUpdate(meta=meta))
        # 只跑解析与切分：没有 provider 也能得到全文索引与切片
        await pipeline.run(document.id, stages=("parsing", "chunking"))
        chunks = await database.chunks.list_by_document(document.id)
        documents.append(document.id)
        chunk_ids.append(chunks[0].id if chunks else "")

    # ② 知识卡片，挂在真实切片上
    for kind, title, body, source in _CARDS:
        index = int(source.removeprefix("第 ").removesuffix(" 号切片"))
        await database.cards.create(
            KnowledgeCardCreate(
                space_id=space.id,
                kind=kind,
                title=title,
                body=body,
                confidence=0.8,
                verified_by="user",
                source_chunks=[chunk_ids[index]] if index < len(chunk_ids) else [],
            )
        )

    # ③ 实体关系图：Memory 页的图谱要能画出来
    entities: dict[str, str] = {}
    for entity_name, entity_type, summary in _ENTITIES:
        entity = await database.entities.create(
            EntityCreate(
                space_id=space.id,
                name=entity_name,
                type=entity_type,
                summary=summary,
                mention_count=3,
            )
        )
        entities[entity_name] = entity.id
    for src, dst, predicate in _RELATIONS:
        # 「心脏毒性」「药物相互作用」只作为关系端点出现，按需补建
        for endpoint in (src, dst):
            if endpoint not in entities:
                entity = await database.entities.create(
                    EntityCreate(space_id=space.id, name=endpoint, type="概念", mention_count=1)
                )
                entities[endpoint] = entity.id
        await database.relations.create(
            RelationCreate(
                space_id=space.id,
                src_id=entities[src],
                dst_id=entities[dst],
                predicate=predicate,
                weight=0.7,
                source_chunks=chunk_ids[:2],
            )
        )

    # ④ L4 经验：三条生效、一条候选、一条待裁决的冲突
    for offset, (
        trigger,
        guidance,
        rationale,
        kind,
        status,
        confidence,
        applied,
        success,
    ) in enumerate(_INSIGHTS):
        insight = await database.insights.create(
            InsightCreate(
                space_id=space.id,
                trigger=trigger,
                guidance=guidance,
                rationale=rationale,
                kind=kind,
                status=status,
                confidence=confidence,
                origin="user_correction",
                applied_count=applied,
                success_count=success,
            )
        )
        await _backdate(database, "insights", insight.id, base + (7 + offset * 2) * _DAY_MS)

    # ④b 置信度流水：让「它是怎么走到今天的」在界面上有轨迹可画。
    # 数值必须**走到这条经验的当前置信度**：流水终点与卡片上的数字对不上，
    # 用户第一次点开就会发现示例自相矛盾。
    journey = (
        ("distilled", None, "初次蒸馏产出"),
        ("positive_feedback", None, "独占整份"),
        ("eval_improved", None, "A/B +4.1"),
        ("negative_feedback", 1 / 6, "均摊 1/6"),
        ("positive_feedback", None, "独占整份"),
    )
    shape = (0.30, 0.35, 0.55, 0.45, 0.62)
    stored_insights = await database.insights.list_by_status(space.id, "active")
    if stored_insights:
        target = stored_insights[0]
        trail = [*shape[:-1], round(target.confidence, 2)]
        for offset, (event, share, reason) in enumerate(journey):
            before = None if offset == 0 else trail[offset - 1]
            record = await database.insight_events.add(
                InsightEventCreate(
                    insight_id=target.id,
                    space_id=space.id,
                    event=event,
                    confidence_before=before,
                    confidence_after=trail[offset],
                    status_before=None
                    if offset == 0
                    else ("candidate" if offset == 1 else "active"),
                    status_after="candidate" if offset < 2 else "active",
                    share=share,
                    reason=reason,
                )
            )
            await _backdate(database, "insight_events", record.id, base + (6 + offset) * _DAY_MS)

    # ⑤ 测验集与两次评测：Expertise 页的「准确率」与新的检索指标都有真实数字
    items = [
        await database.eval_items.create(
            EvalItemCreate(
                space_id=space.id,
                question=question,
                reference=reference,
                must_include=must_include,
                tags=tags,
                source="auto_from_doc",
            )
        )
        for question, reference, must_include, tags in _EVAL_ITEMS
    ]
    await _record_eval(
        database,
        space_id=space.id,
        variant="baseline",
        score=58.5,
        metrics=RetrievalMetrics(
            context_recall=0.5,
            context_precision=0.62,
            faithfulness=0.6,
            claims=9,
            evidence=12,
            audited=True,
        ),
        items=items,
        created_at=base + 12 * _DAY_MS,
    )
    await _record_eval(
        database,
        space_id=space.id,
        variant="with_insights",
        score=71.5,
        metrics=RetrievalMetrics(
            context_recall=0.78,
            context_precision=0.74,
            faithfulness=0.82,
            claims=10,
            evidence=12,
            audited=True,
        ),
        items=items,
        created_at=base + 13 * _DAY_MS,
    )

    # ⑥ 一条待蒸馏的纠错反馈：Evolve 页的「一键进化」有原料可跑
    conversation = await database.conversations.create(
        ConversationCreate(space_id=space.id, title="这个化合物能不能直接推进？")
    )
    answer = "活性数据不错，但还不能直接推进。先补齐 ADMET 五项。hERG 抑制要优先反筛。"
    message = await database.messages.create(
        MessageCreate(conversation_id=conversation.id, role="assistant", content=answer)
    )
    # 正文里不放 [^cN]（线上也是剥离后入库的），引用位置靠 char_offset 记录——
    # 「依归度」这一维就是按位置算「哪一句有出处」的，示例数据也得如实给出
    await database.messages.update(
        message.id,
        MessageUpdate(
            content=answer,
            citations=[
                Citation(
                    marker="c1",
                    chunk_id=chunk_ids[0],
                    document_id=documents[0],
                    snippet="先导化合物需先评估 ADMET 五项",
                    char_offset=answer.index("项。") + 2,
                ),
                Citation(
                    marker="c2",
                    chunk_id=chunk_ids[2],
                    document_id=documents[2],
                    snippet="hERG 阻断会延长 QT 间期",
                    char_offset=len(answer) - 1,
                ),
            ],
        ),
    )
    trace = await database.traces.create(
        TraceCreate(
            space_id=space.id,
            conversation_id=conversation.id,
            message_id=message.id,
            query="这个先导化合物能不能直接推进？",
            used_insights=[],
        )
    )
    await database.feedback.create(
        FeedbackCreate(
            trace_id=trace.id,
            kind="correction",
            comment="只看了活性就下结论是错的：该化合物 hERG 抑制强，必须先做心脏毒性反筛。",
        )
    )

    # ⑦b 领域大纲：覆盖率的分母与知识盲区的基准。写死一份，示例不依赖模型
    persona = (await runtime.spaces.read_config(space.id)).persona
    await database.outlines.create(
        DomainOutlineCreate(
            space_id=space.id,
            domain=persona.domain,
            nodes=[
                OutlineNode(topic="成药性评价", subtopics=["溶解度", "通透性"], importance="core"),
                OutlineNode(topic="代谢稳定性", subtopics=["肝微粒体", "CLint"], importance="core"),
                OutlineNode(topic="心脏毒性", subtopics=["hERG", "QT 间期"], importance="core"),
                OutlineNode(topic="药物相互作用", subtopics=["CYP 抑制", "TDI"], importance="core"),
                OutlineNode(topic="血浆蛋白结合", subtopics=["游离分数"], importance="common"),
                OutlineNode(
                    topic="体内药代", subtopics=["半衰期", "生物利用度"], importance="common"
                ),
                OutlineNode(topic="制剂与晶型", subtopics=["盐型筛选"], importance="common"),
                OutlineNode(
                    topic="毒理与安全评价", subtopics=["Ames", "重复给药毒性"], importance="common"
                ),
            ],
        )
    )

    # ⑧ 专家度曲线：终点用**实测值**，前面几条按比例回推。
    # 全写死的曲线会与页面顶部的实时分数对不上（一个显示 55、一个显示 41），
    # 用户第一次打开就发现示例自相矛盾。以实测值收尾，既一致又讲得通。
    live = await ExpertiseService(database, persona).compute(space.id)
    ramp = (0.34, 0.52, 0.66, 0.8, 0.92, 1.0)
    for index, ratio in enumerate(ramp):
        snapshot = await database.expertise.create(
            ExpertiseSnapshotCreate(
                space_id=space.id,
                coverage=round(live.coverage * ratio, 2),
                accuracy=round(live.accuracy * ratio, 2),
                consistency=round(live.consistency * ratio, 2),
                groundedness=round(live.groundedness * ratio, 2),
                insight_density=round(live.insight_density * ratio, 2),
                overall=round(live.overall * ratio, 2),
            )
        )
        await _backdate(
            database,
            "expertise_snapshots",
            snapshot.id,
            base + (index * 4 + (2 if index == len(ramp) - 1 else 0)) * _DAY_MS,
        )

    # ⑧ 进化历史：两次闭环的产出，曲线也才有解释得通的那一段
    for index, (produced, promoted, demoted, delta, day) in enumerate(
        ((5, 3, 0, 6.4, 13), (3, 1, 1, 4.1, 19))
    ):
        run = await database.evolution.create(
            EvolutionRunCreate(
                space_id=space.id,
                produced=produced,
                merged=produced - promoted - demoted,
                conflicts=1 if index == 0 else 0,
                promoted=promoted,
                demoted=demoted,
                eval_delta=delta,
                expertise_before=round(live.overall * ramp[-3 + index], 2),
                expertise_after=round(live.overall * ramp[-2 + index], 2),
                duration_ms=8200,
            )
        )
        await _backdate(database, "evolution_runs", run.id, base + day * _DAY_MS)

    # ⑨ 配了 embedding 就顺手把向量补上；没有就退化为全文检索，不影响演示
    await _try_embed(runtime, space.id, pipeline)

    logger.info(
        "demo_space_seeded",
        space_id=space.id,
        documents=len(_DOCUMENTS),
        insights=len(_INSIGHTS),
        eval_items=len(items),
    )
    return space


async def _backdate(database: Database, table: str, row_id: str, created_at: int) -> None:
    """把某一行的 ``created_at`` 改到过去。

    仓储的 ``create`` 一律盖当前时间戳，而示例数据要能画出「三周里慢慢长起来」的
    曲线，所以这里显式回填。只动示例 Space 自己的记录。
    """
    await database.sqlite.execute(
        f"UPDATE {table} SET created_at = ? WHERE id = ?",
        (created_at, row_id),
    )


async def _find_by_name(runtime: Runtime, name: str) -> Space | None:
    """按名字找现有 Space（示例 Space 不该建出第二个）。"""
    for space in await runtime.spaces.list_spaces():
        if space.name == name:
            return space
    return None


async def _record_eval(
    database: Database,
    *,
    space_id: str,
    variant: str,
    score: float,
    metrics: RetrievalMetrics,
    items: list[EvalItem],
    created_at: int,
) -> None:
    """写入一条评测记录（含逐题分数与整轮检索指标）。"""
    item_scores = [
        EvalItemScore(
            item_id=item.id,
            score=round(score + offset, 2),
            passed=(score + offset) >= 60,
            reason="示例数据：分数与理由为预置内容",
        )
        for item, offset in zip(items, (2.5, -3.0, 0.5), strict=False)
    ]
    run = await database.eval_runs.create(
        EvalRunCreate(
            space_id=space_id,
            variant=variant,
            insight_set=[],
            score=score,
            detail=EvalRunDetail(items=item_scores, metrics=metrics),
            duration_ms=4200,
        )
    )
    await _backdate(database, "eval_runs", run.id, created_at)


async def _try_embed(runtime: Runtime, space_id: str, pipeline: IngestPipeline) -> bool:
    """能嵌入就嵌入，不能就算了。

    示例 Space 的卖点是「不配模型也能看」，所以这里所有失败都只记日志：
    没有 embedding 时检索会自动走全文那条腿，页面照常可用。
    """
    from agentmem.errors import AgentMemError

    database = await runtime.spaces.space_db(space_id)
    documents, _total, _cursor = await database.documents.list_by_space(space_id, limit=50)
    try:
        for document in documents:
            await pipeline.run(document.id, stages=("embedding",))
    except AgentMemError as exc:
        logger.info("demo_space_embedding_skipped", space_id=space_id, reason=exc.message)
        return False
    except Exception as exc:  # pragma: no cover - 适配器层意外错误
        logger.warning("demo_space_embedding_failed", space_id=space_id, error=str(exc))
        return False
    return True
