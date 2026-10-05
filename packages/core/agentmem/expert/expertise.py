"""专家度指数 —— 把「有多专业」压成五维分数 + 一个总分。

维度定义（`docs/00-VISION.md` §4）与计算口径：

| 维度            | 含义                       | 计算 |
|-----------------|----------------------------|------|
| coverage        | 领域知识图谱填充率         | 已覆盖大纲节点 / 节点总数（无大纲时退回对数缩放） |
| accuracy        | EvalSet 得分               | 最近一次全量评测的均分 |
| consistency     | 同问多答的稳定性           | 实测优先；没测过退回代理指标 |
| groundedness    | 回答有原文支撑的比例       | 最近若干回答里带引用的句子占比 |
| insight_density | 高置信度经验数量           | 对数缩放到 0~100 |

总分权重（v1，与文档一致）：
``accuracy 0.35 + groundedness 0.25 + coverage 0.20 + consistency 0.10 + insight_density 0.10``

**关于 consistency**：实测口径是「同问多答的语义稳定性」——取最近问过的若干问题，
每个在正常温度下重复回答若干次，答案向量化后算两两余弦相似度的平均
（``POST /spaces/{id}/expertise/consistency`` 触发，结果落库）。它不随页面加载自动跑：
一次探测是「问题数 × 重复次数」次生成调用，不该藏在一次 GET 里。

没测过、或最近一次探测已经超过 ``PROBE_TTL_DAYS`` 天时，退回代理指标——「active 经验
占全部非归档经验的比例」。代理指标量的是规则沉淀多不多，跟回答稳不稳不是一回事，
所以分数上会带 ``consistency_source`` 标明来源（``probe`` / ``proxy``）与实测时间，
界面必须如实标注，不能让用户以为那是测出来的。
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Sequence

import structlog

from agentmem.prompts.budget import split_sentences
from agentmem.store import Database
from agentmem.store.sqlite import now_ms
from agentmem.types import (
    ConsistencySource,
    DomainOutline,
    ExpertiseScore,
    ExpertiseSnapshot,
    ExpertiseSnapshotCreate,
    KnowledgeCard,
    OutlineNode,
    Persona,
)

logger = structlog.get_logger(__name__)


def _is_fresh(created_at: int) -> bool:
    """这次探测是否还在保鲜期内。"""
    return (now_ms() - created_at) <= PROBE_TTL_DAYS * 24 * 60 * 60 * 1000


# 总分权重
WEIGHTS = {
    "accuracy": 0.35,
    "groundedness": 0.25,
    "coverage": 0.20,
    "consistency": 0.10,
    "insight_density": 0.10,
}

#: Markdown 标题行不是论断，不计入 groundedness 的分母
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")

#: insight_density 达到满分所需的高置信度经验数（对数缩放的锚点）
DENSITY_SATURATION = 40

#: 统计 groundedness 时回看的最近 trace 数
GROUNDEDNESS_WINDOW = 50

#: 实测一致性的保鲜期。超过这个天数就当作过期：三个月前测的稳定性说明不了今天
PROBE_TTL_DAYS = 30

#: 计入 insight_density / consistency 的置信度下限
HIGH_CONFIDENCE = 0.7

#: 判定大纲覆盖时最多看多少张卡片（按置信度从高到低取）
COVERAGE_CARD_LIMIT = 1000

#: 卡片与大纲子主题的语义相似度下限（余弦，bge-m3 实测）。
#: 大纲由模型写成概括性的说法（「电池类故障处理（过压、欠压、过温、压差过大）」），
#: 卡片写的是具体条目（「E07 过温保护的触发条件、降额与恢复逻辑」），逐字匹配一条都对不上：
#: 一份储能手册抽出的 7 张卡片对 15 个主题，原规则覆盖 0 个。实测真正对口的最佳子主题在
#: 0.52~0.65，找不到归属的卡片（「安装环境条件要求」）最高只有 0.44。
SEMANTIC_MATCH_MIN = 0.5

#: 文本 → 向量（provider 名打头）。大纲与卡片标题都很少变，进程内缓存住，
#: 图谱页反复打开不用每次重新向量化
_VECTOR_CACHE: dict[str, list[float]] = {}
_VECTOR_CACHE_LIMIT = 8192

#: 拿一批文本换向量的函数；没有可用的 embedding 时传 ``None``，退回逐字匹配
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


# -- 大纲覆盖的关键词判定（专家度与知识图谱共用） -------------------------------
#
# 「某个大纲节点有没有被覆盖」在两处要回答：专家度的 coverage 分子，和知识图谱里
# 主题节点挂了哪几张卡片。两边口径一旦分叉，就会出现图谱上某主题挂着卡片、专家度
# 却说它没覆盖（或反过来）的矛盾，所以判定只写这一份。


def outline_keywords(node: OutlineNode) -> list[str]:
    """一个大纲节点的匹配词：主题本身 + 全部子主题（空串剔除）。"""
    return [keyword for keyword in [node.topic, *node.subtopics] if keyword]


def card_match_text(card: KnowledgeCard) -> str:
    """拿来做覆盖判定的卡片文本：标题 + 别名。

    不看正文：正文里顺口提到一个词不等于这张卡讲的是它，标题和别名才是
    「这张卡是关于什么的」。
    """
    return f"{card.title} {' '.join(card.aliases)}"


def match_outline(
    nodes: Sequence[OutlineNode], cards: Sequence[KnowledgeCard]
) -> list[list[KnowledgeCard]]:
    """逐个大纲节点列出命中的卡片（与 ``nodes`` 一一对应，保持卡片原顺序）。

    命中规则：节点的主题词或任一子主题，原样出现在某张卡片的标题 / 别名里。
    返回空列表的节点就是未覆盖。

    早先是把全部卡片的标题拼成一个大字符串再查子串，结果与这里只差一种情形：
    一个带空格的关键词恰好横跨两张卡片的拼接处——那是拼接的副产品，不是覆盖。
    """
    texts = [card_match_text(card) for card in cards]
    matched: list[list[KnowledgeCard]] = []
    for node in nodes:
        keywords = outline_keywords(node)
        matched.append(
            [
                card
                for card, text in zip(cards, texts, strict=True)
                if any(keyword in text for keyword in keywords)
            ]
        )
    return matched


async def match_outline_semantic(
    nodes: Sequence[OutlineNode],
    cards: Sequence[KnowledgeCard],
    embed: Embedder | None,
    *,
    cache_key: str = "",
) -> list[list[KnowledgeCard]]:
    """逐字匹配之上再按语义归属：每张卡片归到与它最像的那个子主题所在的主题。

    只取「最像的那一个」而不是所有过线的：同一领域的主题彼此都沾边，放开了一张
    卡片会同时点亮三四个主题，覆盖率虚高。逐字命中的照旧算数。向量化失败时
    （模型没配、服务不通）安静退回逐字匹配——覆盖率偏低总比整页报错好。
    """
    matched = match_outline(nodes, cards)
    if embed is None or not nodes or not cards:
        return matched
    units = [
        (index, phrase) for index, node in enumerate(nodes) for phrase in outline_keywords(node)
    ]
    texts = [phrase for _index, phrase in units] + [_card_semantic_text(card) for card in cards]
    try:
        vectors = await _embed_cached(embed, texts, cache_key)
    except Exception as exc:
        logger.info("outline_semantic_match_skipped", error=str(exc)[:160])
        return matched
    unit_vectors = vectors[: len(units)]
    for card, vector in zip(cards, vectors[len(units) :], strict=True):
        scores = [_cosine(vector, other) for other in unit_vectors]
        best = max(range(len(scores)), key=scores.__getitem__)
        if scores[best] < SEMANTIC_MATCH_MIN:
            continue
        bucket = matched[units[best][0]]
        if all(item.id != card.id for item in bucket):
            bucket.append(card)
    order = {card.id: position for position, card in enumerate(cards)}
    return [sorted(bucket, key=lambda card: order[card.id]) for bucket in matched]


def _card_semantic_text(card: KnowledgeCard) -> str:
    """语义归属看标题与别名，理由同 :func:`card_match_text`。"""
    return "。".join([card.title, *card.aliases])


async def _embed_cached(embed: Embedder, texts: list[str], cache_key: str) -> list[list[float]]:
    missing = list(
        dict.fromkeys(text for text in texts if f"{cache_key}\x00{text}" not in _VECTOR_CACHE)
    )
    if missing:
        fresh = await embed(missing)
        if len(_VECTOR_CACHE) + len(fresh) > _VECTOR_CACHE_LIMIT:
            _VECTOR_CACHE.clear()
        for text, vector in zip(missing, fresh, strict=True):
            _VECTOR_CACHE[f"{cache_key}\x00{text}"] = vector
    return [_VECTOR_CACHE[f"{cache_key}\x00{text}"] for text in texts]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def coverage_cards(database: Database, space_id: str) -> list[KnowledgeCard]:
    """参与覆盖判定的卡片：置信度最高的 ``COVERAGE_CARD_LIMIT`` 张。"""
    cards, _total, _cursor = await database.cards.list_by_space(space_id, limit=COVERAGE_CARD_LIMIT)
    return cards


class ExpertiseService:
    """专家度计算与快照。一个 Space 一个实例。"""

    def __init__(
        self,
        database: Database,
        persona: Persona,
        *,
        embed: Embedder | None = None,
        embed_key: str = "",
    ) -> None:
        self.db = database
        self.persona = persona
        self.embed = embed
        self.embed_key = embed_key

    async def compute(self, space_id: str, *, outline_size: int | None = None) -> ExpertiseScore:
        """计算当前五维分数与总分（不落库）。

        Args:
            outline_size: 领域大纲节点总数，用于 coverage 分母；缺省时用
                知识卡片数的启发式估计（见 `_coverage`）。
        """
        coverage = await self._coverage(space_id, outline_size)
        accuracy = await self._accuracy(space_id)
        groundedness, groundedness_samples = await self._groundedness(space_id)
        consistency, consistency_source, consistency_measured_at = await self._consistency(space_id)
        density = await self._insight_density(space_id)

        overall = round(
            accuracy * WEIGHTS["accuracy"]
            + groundedness * WEIGHTS["groundedness"]
            + coverage * WEIGHTS["coverage"]
            + consistency * WEIGHTS["consistency"]
            + density * WEIGHTS["insight_density"],
            2,
        )
        return ExpertiseScore(
            coverage=coverage,
            accuracy=accuracy,
            consistency=consistency,
            consistency_source=consistency_source,
            consistency_measured_at=consistency_measured_at,
            groundedness=groundedness,
            groundedness_samples=groundedness_samples,
            insight_density=density,
            overall=overall,
        )

    async def snapshot(
        self, space_id: str, *, outline_size: int | None = None
    ) -> ExpertiseSnapshot:
        """计算并落库一张专家度快照（供成长曲线）。"""
        score = await self.compute(space_id, outline_size=outline_size)
        return await self.db.expertise.create(
            ExpertiseSnapshotCreate(space_id=space_id, **score.model_dump())
        )

    # -- 各维度 -----------------------------------------------------------

    async def _coverage(self, space_id: str, outline_size: int | None) -> float:
        """知识覆盖率：**有卡片对应的领域大纲节点 / 节点总数**。

        没有大纲（或调用方给了节点数）时才退回旧口径——对卡片数量做对数缩放，
        它只反映「资料越多覆盖越广」的单调趋势，不是覆盖率本身。大纲是落库的，
        所以这里的分子分母都来自真实数据，不再需要调用模型。
        """
        card_count = await self.db.cards.count(space_id)
        outline = await self.db.outlines.latest(space_id)
        nodes = outline.nodes if outline is not None else []
        total = outline_size or len(nodes)
        if total and total > 0:
            covered = await self._covered_nodes(space_id, nodes) if nodes else card_count
            return round(min(1.0, covered / total) * 100, 2)
        # 无大纲的保守估计：40 张卡片视为基本覆盖
        return round(min(1.0, math.log1p(card_count) / math.log1p(40)) * 100, 2)

    async def _covered_nodes(self, space_id: str, nodes: Sequence[OutlineNode]) -> int:
        """有卡片归属的大纲节点个数。

        判定规则见 :func:`match_outline_semantic`，知识图谱的主题节点用的是同一份。
        """
        cards = await coverage_cards(self.db, space_id)
        matches = await match_outline_semantic(nodes, cards, self.embed, cache_key=self.embed_key)
        return sum(1 for hits in matches if hits)

    # -- 领域大纲 ---------------------------------------------------------

    async def outline(self, space_id: str) -> DomainOutline | None:
        """当前落库的领域大纲。"""
        return await self.db.outlines.latest(space_id)

    async def covered_node_count(self, space_id: str) -> tuple[int, int]:
        """``(已覆盖节点数, 节点总数)``；没有大纲时返回 ``(0, 0)``。"""
        outline = await self.db.outlines.latest(space_id)
        if outline is None:
            return 0, 0
        return await self._covered_nodes(space_id, outline.nodes), len(outline.nodes)

    async def _accuracy(self, space_id: str) -> float:
        """最近一次全量评测的均分；从未评测过则为 0。"""
        latest = await self.db.eval_runs.latest(space_id, "with_insights")
        baseline = await self.db.eval_runs.latest(space_id, "baseline")
        # 优先用注入经验后的分数，没有就用 baseline
        run = latest or baseline
        return round(run.score, 2) if run else 0.0

    async def _groundedness(self, space_id: str) -> tuple[float, int]:
        """回答有原文支撑的比例：最近 N 条回答里，带引用的句子占全部句子的比例。

        旧口径是「这次检索到证据了吗」——只要检索返回过任何一条切片就算满分，
        于是「检索到一条不相关切片、然后凭模型自身知识作答」与「每句论断都有出处」
        得到同样的分数。检索到证据只是有据可依的前提，答案到底用没用它，要看引用
        落在哪一句上。

        判定靠 ``Citation.char_offset``：回答正文里引用标记已被剥离（前端渲染的是
        芯片，不是裸标记），所以只有记录下来的位置能说明哪些句子有出处。
        位置缺失的老数据（本字段引入之前入库的回答）不计入分母，避免把「没记位置」
        算成「没引用」。

        标题行不算论断（Markdown 结构，不是结论），其余每行都计入分母：空口作答的
        句子会如实拉低这一维——**一条引用都没有的回答也一样**。此前它和「有引用但没记
        位置」的老数据一起被跳过，凭空作答不进分母，这一维只升不降。

        Returns:
            ``(分数 0~100, 计入统计的回答数)``。样本数要一起给出去：只剩两三条回答时
            算出来的 100% 说明不了什么，界面得让人看见分母。
        """
        traces, _total, _cursor = await self.db.traces.list_by_space(
            space_id, limit=GROUNDEDNESS_WINDOW
        )
        if not traces:
            return 0.0, 0

        cited = 0
        total = 0
        samples = 0
        for trace in traces:
            message = await self.db.messages.get(trace.message_id)
            if message is None or not message.content.strip():
                continue
            offsets = sorted(
                citation.char_offset
                for citation in message.citations
                if citation.char_offset is not None
            )
            if message.citations and not offsets:
                # 有引用、但位置没记下来：老数据，说不清哪句有出处，不进分母
                continue
            cursor = 0
            sentences: list[tuple[int, int]] = []
            for sentence in split_sentences(message.content):
                if sentence.strip() and not _HEADING.match(sentence):
                    sentences.append((cursor, cursor + len(sentence)))
                cursor += len(sentence)
            if not sentences:
                continue
            samples += 1
            total += len(sentences)
            hits: set[int] = set()
            for offset in offsets:
                for index, (start, end) in enumerate(sentences):
                    # 位置正好落在句末时归前一句：标记紧跟句号写，
                    # 位置就是上一句的结尾
                    if start <= offset <= end:
                        hits.add(index)
                        break
            cited += len(hits)
        if total == 0:
            return 0.0, samples
        return round(cited / total * 100, 2), samples

    async def _consistency(self, space_id: str) -> tuple[float, ConsistencySource, int | None]:
        """一致性：优先用实测，没有新鲜的实测就退回代理指标。

        Returns:
            ``(分数 0~100, 来源, 实测时间)``。
        """
        probe = await self.db.consistency.latest(space_id)
        if probe is not None and _is_fresh(probe.created_at):
            return round(probe.similarity * 100, 2), "probe", probe.created_at
        return await self._consistency_proxy(space_id), "proxy", None

    async def _consistency_proxy(self, space_id: str) -> float:
        """代理指标：active 经验占全部非归档经验的比例。

        它量的是「规则沉淀得多不多」，与「同样的问题问两遍会不会得到两个说法」
        不是一回事——只在没有实测时才用，并在响应里标成 ``proxy``。
        """
        active = await self.db.insights.count(space_id, status="active")
        candidate = await self.db.insights.count(space_id, status="candidate")
        conflicted = await self.db.insights.count(space_id, status="conflicted")
        live = active + candidate + conflicted
        if live == 0:
            return 0.0
        return round(active / live * 100, 2)

    async def _insight_density(self, space_id: str) -> float:
        """高置信度经验数量，对数缩放到 0~100。"""
        active = await self.db.insights.list_active(
            space_id, min_confidence=HIGH_CONFIDENCE, limit=1000
        )
        count = len(active)
        return round(min(1.0, math.log1p(count) / math.log1p(DENSITY_SATURATION)) * 100, 2)
