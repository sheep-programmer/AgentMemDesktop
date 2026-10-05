"""知识图谱（知识网络）—— 把「AI 懂了什么、怎么长出来的、掌握得怎样」画成一张图。

一次只读查询拼出前端「知识图谱」页需要的全部数据：

- **网络**：中心是领域（Space），向外是领域大纲的主题、导入的文档、抽出的知识卡片、
  沉淀的经验。连线说明「谁和谁有关」，见下文 *节点与连线*。
- **进化脉络**：文档导入、进化、专家度快照、经验生效，按时间排成一条时间线。
- **掌握程度**：每个大纲主题一个 0~1 的掌握度，外加一个总评。

全部来自落库数据，**不调用任何模型**（没有 LLM / embedding / 重排）：这是一个随手
打开的页面，不该等模型，也不该花钱。

节点与连线
----------

节点 id 带种类前缀（``domain:`` / ``topic:`` / ``doc:`` / ``card:`` / ``insight:``），
保证跨种类不撞；底层实体的原始 id 放在 ``ref_id``，供界面点开详情。

连线一律从「更靠近中心」的一端指向外侧：

| kind      | src → dst          | 含义 |
|-----------|--------------------|------|
| ``topic`` | 领域 → 主题        | 领域大纲的一个节点 |
| ``covers``| 主题 → 卡片        | 卡片标题 / 别名命中了主题或子主题（与专家度 coverage 同一规则） |
| ``covers``| 领域 → 卡片        | 兜底：既没命中主题、也找不到来源文档（如手动新建的卡片） |
| ``source``| 领域 → 文档        | 这份资料属于该领域 |
| ``source``| 文档 → 卡片        | 卡片的来源切片出自这份文档 |
| ``insight``| 卡片 → 经验       | 经验蒸馏自某次回答，那次回答的上下文里用到了这张卡片 |
| ``insight``| 领域 → 经验       | 兜底：找不到上面那种来源（来源对话已删除、或手动新增的经验） |

「卡片 → 经验」走的是真实存在的溯源链：``insights.source_trace_ids`` → ``traces.used_cards``。
它说明的是「这条经验是在用到这些卡片的那次回答上被纠正出来的」，不是语义相似度。

掌握程度
--------

主题掌握度 = ``min(1, 命中卡片数 / CARD_SATURATION) × 命中卡片的平均置信度``。

- 数量项：一张卡片只能说明「提到过」，三张以上才算这个主题有一定厚度，再多不加分——
  否则一个主题堆一百张低质量卡片就能刷满；
- 质量项：卡片置信度反映抽取 / 人工校订的可靠程度，十张都没把握的卡片不等于掌握。

未覆盖的主题掌握度为 0。总评 ``stats.overall`` 是全部主题掌握度的算术平均——每个主题
等权，不按 importance 加权：大纲里的 importance 是模型给的粗标签，拿它当权重会把
一个主观判断放大成分数差异。没有大纲时总评为 ``None``，而不是 0：「还不知道该懂什么」
和「什么都不懂」是两回事。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from agentmem.expert.expertise import Embedder, coverage_cards, match_outline_semantic
from agentmem.store import Database
from agentmem.types import (
    AgentMemModel,
    Document,
    EvolutionRun,
    ExpertiseSnapshot,
    Id,
    Insight,
    InsightEvent,
    KnowledgeCard,
    Space,
    Timestamp,
)

KnowledgeNodeKind = Literal["domain", "topic", "document", "card", "insight"]
KnowledgeEdgeKind = Literal["topic", "covers", "source", "insight"]
MilestoneKind = Literal["document", "evolution", "snapshot", "insight"]

#: ``max_cards`` 的缺省值与上下限
DEFAULT_MAX_CARDS = 300
MAX_CARDS_LIMIT = 1000

#: 一次最多画多少份文档 / 多少条经验；超出时 ``truncated=True``
MAX_DOCUMENTS = 500
MAX_INSIGHTS = 300

#: 时间线只保留最近这么多个里程碑
MAX_MILESTONES = 200

#: 节点 ``detail`` 的最大字符数
DETAIL_CHARS = 120

#: 经验节点的标签长度：触发条件动辄一句长话，图上只放得下开头
INSIGHT_LABEL_CHARS = 48

#: 主题掌握度的数量饱和点：命中这么多张卡片后，数量项就是满分
CARD_SATURATION = 3

#: 进化收尾时会顺手落一张专家度快照（时间只差几毫秒）。这张快照的分数已经作为
#: 进化里程碑的 ``value`` 给出去了，时间线上不必再单独画一个点
SNAPSHOT_DEDUP_MS = 60_000

#: 主题节点的显著度：核心主题画大一点
IMPORTANCE_WEIGHT = {"core": 1.0, "common": 0.7, "advanced": 0.5}
DEFAULT_IMPORTANCE_WEIGHT = 0.6

#: 没产出卡片的文档也要看得见，不能缩成一个点
MIN_DOCUMENT_WEIGHT = 0.2

#: 同一时刻的里程碑按这个顺序排：先有资料，再有进化，最后是结果
_MILESTONE_ORDER: dict[str, int] = {"document": 0, "insight": 1, "evolution": 2, "snapshot": 3}


# -- 响应模型 ----------------------------------------------------------------
#
# 只给这一个接口用，所以和构建逻辑放在一起，不进 ``agentmem.types``。


class KnowledgeMapSpace(AgentMemModel):
    """图谱所属的 Space。"""

    id: Id
    name: str
    domain: str


class KnowledgeMapNode(AgentMemModel):
    """图上的一个节点。"""

    id: str = Field(description="图内唯一 id，带种类前缀，如 'card:01J…'")
    kind: KnowledgeNodeKind
    label: str
    created_at: Timestamp | None = Field(default=None, description="底层实体的创建时间")
    mastery: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="掌握度 0~1：主题=覆盖掌握度，卡片 / 经验=置信度，领域=总评；文档为空",
    )
    status: str | None = Field(
        default=None,
        description=(
            "按种类而定：文档=处理状态，经验=candidate/active/conflicted，"
            "卡片=卡片类型（concept/fact/…），主题=covered/uncovered"
        ),
    )
    weight: float = Field(default=0.0, ge=0.0, le=1.0, description="相对显著度 0~1，用来定节点大小")
    detail: str | None = Field(default=None, description="一句话纯文本说明（≤120 字）")
    ref_id: str | None = Field(
        default=None, description="底层实体 id（文档 / 卡片 / 经验 / Space），主题为空"
    )


class KnowledgeMapEdge(AgentMemModel):
    """一条连线，方向从靠近中心的一端指向外侧。"""

    src: str
    dst: str
    kind: KnowledgeEdgeKind


class KnowledgeMapTopic(AgentMemModel):
    """领域大纲里的一个主题及其掌握情况。"""

    topic: str
    importance: str
    subtopics: list[str] = Field(default_factory=list)
    covered: bool = Field(description="与专家度 coverage 同一判定：至少一张卡片命中")
    card_count: int = Field(ge=0, description="命中该主题的卡片数")
    mastery: float = Field(ge=0.0, le=1.0, description="min(1, 卡片数/3) × 平均置信度")


class KnowledgeMapMilestone(AgentMemModel):
    """进化脉络上的一个点。"""

    at: Timestamp
    kind: MilestoneKind
    label: str
    value: float | None = Field(
        default=None,
        description="进化 / 快照=专家度总分（0~100），经验=生效时的置信度，文档为空",
    )


class KnowledgeMapStats(AgentMemModel):
    """全局计数与掌握度总评。"""

    documents: int = 0
    cards: int = 0
    insights_active: int = 0
    insights_candidate: int = 0
    topics_total: int = 0
    topics_covered: int = 0
    overall: float | None = Field(
        default=None, description="全部主题掌握度的平均（0~1）；没有大纲时为空"
    )


class KnowledgeMapResponse(AgentMemModel):
    """``GET /spaces/{id}/knowledge-map`` 响应。"""

    space: KnowledgeMapSpace
    nodes: list[KnowledgeMapNode] = Field(default_factory=list)
    edges: list[KnowledgeMapEdge] = Field(default_factory=list)
    topics: list[KnowledgeMapTopic] = Field(default_factory=list)
    milestones: list[KnowledgeMapMilestone] = Field(
        default_factory=list, description="按时间正序，最多保留最近 200 个"
    )
    stats: KnowledgeMapStats
    truncated: bool = Field(default=False, description="卡片 / 文档 / 经验是否因数量上限没画全")


# -- 构建 --------------------------------------------------------------------


def clamp_max_cards(value: int) -> int:
    """把 ``max_cards`` 夹到 ``[1, MAX_CARDS_LIMIT]``。"""
    return max(1, min(MAX_CARDS_LIMIT, value))


async def build_knowledge_map(
    database: Database,
    space: Space,
    *,
    max_cards: int = DEFAULT_MAX_CARDS,
    embed: Embedder | None = None,
    embed_key: str = "",
) -> KnowledgeMapResponse:
    """拼出一个 Space 的知识图谱。纯 SQL + 内存计算，不调用生成模型。

    给了 ``embed`` 时卡片按语义归属到主题（见 :func:`match_outline_semantic`），
    向量按文本缓存，只有新卡片、新大纲才需要向量化。

    Args:
        database: 该 Space 的数据库。
        space: Space 本身（名称 / 领域 / 创建时间来自全局库）。
        max_cards: 最多画多少张卡片（按置信度从高到低），超出部分不画但仍计入
            主题掌握度——掌握度不该因为画布放不下而变低。
    """
    limit = clamp_max_cards(max_cards)
    space_id = space.id

    outline = await database.outlines.latest(space_id)
    cards = await coverage_cards(database, space_id)
    card_total = await database.cards.count(space_id)
    documents = await database.documents.list_overview(space_id, limit=MAX_DOCUMENTS)
    document_total = await database.documents.count(space_id)
    insights = await database.insights.list_live(space_id, limit=MAX_INSIGHTS)
    active = await database.insights.count(space_id, status="active")
    candidate = await database.insights.count(space_id, status="candidate")
    conflicted = await database.insights.count(space_id, status="conflicted")

    builder = _GraphBuilder(space)

    # ---- 主题与掌握度 ----
    nodes = outline.nodes if outline is not None else []
    matches = await match_outline_semantic(nodes, cards, embed, cache_key=embed_key)
    topics: list[KnowledgeMapTopic] = []
    for node, hits in zip(nodes, matches, strict=True):
        topics.append(
            KnowledgeMapTopic(
                topic=node.topic,
                importance=node.importance,
                subtopics=list(node.subtopics),
                covered=bool(hits),
                card_count=len(hits),
                mastery=topic_mastery(hits),
            )
        )
    overall = round(sum(t.mastery for t in topics) / len(topics), 3) if topics else None
    builder.add_domain(
        mastery=overall,
        detail=space.description
        or (outline.interpretation if outline is not None else None)
        or space.domain,
    )
    for index, topic in enumerate(topics):
        builder.add_topic(
            index, topic, created_at=outline.created_at if outline is not None else None
        )

    # ---- 文档与卡片 ----
    shown = cards[:limit]
    chunk_ids = [chunk_id for card in cards for chunk_id in card.source_chunks]
    chunk_documents = await database.chunks.document_ids(chunk_ids)
    card_documents = {
        card.id: list(
            dict.fromkeys(
                chunk_documents[chunk_id]
                for chunk_id in card.source_chunks
                if chunk_id in chunk_documents
            )
        )
        for card in cards
    }
    produced: dict[str, int] = {}
    for document_ids in card_documents.values():
        for document_id in document_ids:
            produced[document_id] = produced.get(document_id, 0) + 1
    most = max(produced.values(), default=0)
    for document in documents:
        builder.add_document(document, produced=produced.get(document.id, 0), most=most)
    for card in shown:
        builder.add_card(card)
    for index, hits in enumerate(matches):
        for card in hits:
            builder.link_topic_card(index, card.id)
    for card in shown:
        for document_id in card_documents.get(card.id, []):
            builder.link_document_card(document_id, card.id)
    builder.anchor_orphan_cards()

    # ---- 经验 ----
    used_cards = await database.traces.used_cards_by_ids(
        [trace_id for insight in insights for trace_id in insight.source_trace_ids]
    )
    for insight in insights:
        lineage = [
            card_id
            for trace_id in insight.source_trace_ids
            for card_id in used_cards.get(trace_id, [])
        ]
        builder.add_insight(insight, lineage)

    # ---- 进化脉络 ----
    runs = await database.evolution.list_by_space(space_id, limit=MAX_MILESTONES)
    snapshots = await database.expertise.recent(space_id, limit=MAX_MILESTONES)
    activations = await database.insight_events.list_activations(space_id, limit=MAX_MILESTONES)
    milestones = build_milestones(documents, runs, snapshots, activations)

    truncated = (
        card_total > len(shown)
        or document_total > len(documents)
        or active + candidate + conflicted > len(insights)
    )
    return KnowledgeMapResponse(
        space=KnowledgeMapSpace(id=space.id, name=space.name, domain=space.domain),
        nodes=builder.nodes,
        edges=builder.edges,
        topics=topics,
        milestones=milestones,
        stats=KnowledgeMapStats(
            documents=document_total,
            cards=card_total,
            insights_active=active,
            insights_candidate=candidate,
            topics_total=len(topics),
            topics_covered=sum(1 for t in topics if t.covered),
            overall=overall,
        ),
        truncated=truncated,
    )


def topic_mastery(cards: Sequence[KnowledgeCard]) -> float:
    """主题掌握度：数量饱和项 × 平均置信度（口径见模块说明）。"""
    if not cards:
        return 0.0
    saturation = min(1.0, len(cards) / CARD_SATURATION)
    quality = sum(_unit(card.confidence) for card in cards) / len(cards)
    return round(saturation * quality, 3)


def build_milestones(
    documents: Sequence[Document],
    runs: Sequence[EvolutionRun],
    snapshots: Sequence[ExpertiseSnapshot],
    activations: Sequence[tuple[InsightEvent, str | None]],
) -> list[KnowledgeMapMilestone]:
    """把四类事件并成一条时间线，正序，只留最近 ``MAX_MILESTONES`` 个。"""
    items: list[KnowledgeMapMilestone] = [
        KnowledgeMapMilestone(
            at=document.created_at, kind="document", label=f"导入《{document.title}》"
        )
        for document in documents
        # 失败 / 还在处理的文档没有带来任何知识，不算成长
        if document.status == "ready"
    ]
    for run in runs:
        items.append(
            KnowledgeMapMilestone(
                at=run.created_at,
                kind="evolution",
                label=_evolution_label(run),
                value=run.expertise_after,
            )
        )
    for snapshot in snapshots:
        if any(0 <= run.created_at - snapshot.created_at <= SNAPSHOT_DEDUP_MS for run in runs):
            continue
        items.append(
            KnowledgeMapMilestone(
                at=snapshot.created_at,
                kind="snapshot",
                label=f"专家度 {snapshot.overall:.1f}",
                value=snapshot.overall,
            )
        )
    for event, trigger in activations:
        label = f"经验生效：{_shorten(trigger, 40)}" if trigger else "经验生效"
        items.append(
            KnowledgeMapMilestone(
                at=event.created_at, kind="insight", label=label, value=event.confidence_after
            )
        )
    items.sort(key=lambda item: (item.at, _MILESTONE_ORDER[item.kind]))
    return items[-MAX_MILESTONES:]


class _GraphBuilder:
    """按种类前缀发号、去重连线，并记下哪些卡片已经连上了。"""

    def __init__(self, space: Space) -> None:
        self.space = space
        self.domain_id = f"domain:{space.id}"
        self.nodes: list[KnowledgeMapNode] = []
        self.edges: list[KnowledgeMapEdge] = []
        self._edge_keys: set[tuple[str, str, str]] = set()
        self._node_ids: set[str] = set()
        self._card_ids: list[str] = []
        self._linked_cards: set[str] = set()

    def _add(self, node: KnowledgeMapNode) -> None:
        self.nodes.append(node)
        self._node_ids.add(node.id)

    def _link(self, src: str, dst: str, kind: KnowledgeEdgeKind) -> bool:
        """两端都在图上才连；重复的连线只留一条。"""
        if src not in self._node_ids or dst not in self._node_ids:
            return False
        key = (src, dst, kind)
        if key in self._edge_keys:
            return True
        self._edge_keys.add(key)
        self.edges.append(KnowledgeMapEdge(src=src, dst=dst, kind=kind))
        return True

    def add_domain(self, *, mastery: float | None, detail: str | None) -> None:
        self._add(
            KnowledgeMapNode(
                id=self.domain_id,
                kind="domain",
                label=self.space.name,
                created_at=self.space.created_at,
                mastery=mastery,
                weight=1.0,
                detail=_plain(detail),
                ref_id=self.space.id,
            )
        )

    def add_topic(
        self, index: int, topic: KnowledgeMapTopic, *, created_at: Timestamp | None
    ) -> None:
        node_id = f"topic:{index}"
        self._add(
            KnowledgeMapNode(
                id=node_id,
                kind="topic",
                label=topic.topic,
                created_at=created_at,
                mastery=topic.mastery,
                status="covered" if topic.covered else "uncovered",
                weight=IMPORTANCE_WEIGHT.get(topic.importance, DEFAULT_IMPORTANCE_WEIGHT),
                detail=_plain("、".join(topic.subtopics)),
            )
        )
        self._link(self.domain_id, node_id, "topic")

    def add_document(self, document: Document, *, produced: int, most: int) -> None:
        node_id = f"doc:{document.id}"
        weight = produced / most if most else 0.0
        detail: str | None
        if document.status == "failed":
            detail = document.error or "处理失败"
        else:
            detail = document.meta.context_summary
        self._add(
            KnowledgeMapNode(
                id=node_id,
                kind="document",
                label=document.title,
                created_at=document.created_at,
                status=document.status,
                weight=round(max(MIN_DOCUMENT_WEIGHT, weight), 3),
                detail=_plain(detail),
                ref_id=document.id,
            )
        )
        self._link(self.domain_id, node_id, "source")

    def add_card(self, card: KnowledgeCard) -> None:
        confidence = _unit(card.confidence)
        self._add(
            KnowledgeMapNode(
                id=f"card:{card.id}",
                kind="card",
                label=card.title,
                created_at=card.created_at,
                mastery=round(confidence, 3),
                status=card.kind,
                weight=round(confidence, 3),
                detail=_plain(card.body),
                ref_id=card.id,
            )
        )
        self._card_ids.append(card.id)

    def link_topic_card(self, topic_index: int, card_id: str) -> None:
        if self._link(f"topic:{topic_index}", f"card:{card_id}", "covers"):
            self._linked_cards.add(card_id)

    def link_document_card(self, document_id: str, card_id: str) -> None:
        if self._link(f"doc:{document_id}", f"card:{card_id}", "source"):
            self._linked_cards.add(card_id)

    def anchor_orphan_cards(self) -> None:
        """既没命中主题、也没有可见来源文档的卡片，直接挂到领域上，不让它飘着。"""
        for card_id in self._card_ids:
            if card_id not in self._linked_cards:
                self._link(self.domain_id, f"card:{card_id}", "covers")

    def add_insight(self, insight: Insight, lineage: Sequence[str]) -> None:
        node_id = f"insight:{insight.id}"
        confidence = _unit(insight.confidence)
        self._add(
            KnowledgeMapNode(
                id=node_id,
                kind="insight",
                label=_shorten(insight.trigger, INSIGHT_LABEL_CHARS),
                created_at=insight.created_at,
                mastery=round(confidence, 3),
                status=insight.status,
                weight=round(confidence, 3),
                detail=_plain(insight.guidance),
                ref_id=insight.id,
            )
        )
        linked = False
        for card_id in dict.fromkeys(lineage):
            linked = self._link(f"card:{card_id}", node_id, "insight") or linked
        if not linked:
            self._link(self.domain_id, node_id, "insight")


# -- 文本小工具 --------------------------------------------------------------

_CODE_FENCE = re.compile(r"```[^\n]*")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"(^|\s)#{1,6}\s")
_INLINE = re.compile(r"[*`~]+")
_BLOCK = re.compile(r"[>|]+")
_SPACES = re.compile(r"\s+")


def _plain(text: str | None, limit: int = DETAIL_CHARS) -> str | None:
    """Markdown → 一行纯文本，截到 ``limit`` 字（含省略号）；空文本返回 ``None``。"""
    if not text:
        return None
    value = _CODE_FENCE.sub(" ", text)
    value = _LINK.sub(r"\1", value)
    value = _HEADING.sub(" ", value)
    # 行内强调直接去掉（「**早期**药物」→「早期药物」），引用 / 表格线换成空格
    value = _INLINE.sub("", value)
    value = _BLOCK.sub(" ", value)
    value = _SPACES.sub(" ", value).strip()
    return _shorten(value, limit) or None


def _shorten(text: str, limit: int) -> str:
    """超长就截断并补一个省略号，总长不超过 ``limit``。"""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _evolution_label(run: EvolutionRun) -> str:
    """「进化：新增 2 条候选 · 晋升 1」，什么都没变就照实说。"""
    parts = [
        f"{name} {count}"
        for name, count in (
            ("新增候选", run.produced),
            ("合并", run.merged),
            ("晋升", run.promoted),
            ("淘汰", run.demoted),
            ("冲突", run.conflicts),
        )
        if count
    ]
    return "进化：" + (" · ".join(parts) if parts else "无变化")


def _unit(value: float) -> float:
    """置信度夹到 [0, 1]：老数据里偶有浮点误差越界的值，响应模型会拒收。"""
    return max(0.0, min(1.0, value))
