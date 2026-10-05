"""问答主 Prompt —— 把 L4 人格 + L3 经验 + L2 卡片 + L1 原文装配成对话。

上下文装配顺序（顺序有意义，越靠前的约束力越强）：

    L4 Persona   我是谁、怎么说话、什么算好答案
    L3 Insights  这类问题已知的经验与坑
    L2 Knowledge 相关概念卡片
    L1 Chunks    原文证据（带引用编号）

引用协议：模型在正文里用 ``[^c3]`` 标注，后端据此把 marker 映射回 chunk_id，
前端渲染成可点击芯片并能定位到原文高亮处。协议一旦改动，
必须同步改 `agentmem.retrieve.citations` 的解析正则与前端渲染。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._base import bulleted, join, numbered, section, truncate
from ._shapes import CardRef, ChunkRef, InsightRef, Message, PersonaSpec, Turn
from .budget import allocate, estimate_tokens, trim_to_budget
from .compaction import ContextBudget, compact_evidence, compact_history

#: 证据区**整体**（含标签与来源标注）的 token 预算。原先只有「单条 2400 字符」
#: 的局部上限，最坏情况是条数 × 2400，没有任何全局约束；这里改成总量封顶，
#: 由分配器决定每条拿多少。标签开销先从预算里扣掉，正文用剩下的额度。
EVIDENCE_TOKEN_BUDGET = 2000

#: 单条证据的份额低于此值就整条不要：与其塞一段没有信息量的残片，
#: 不如把位置让给更相关的证据。
MIN_EVIDENCE_TOKENS = 120

#: 证据区的开头说明。标签开销的估算与真正渲染共用它，避免两处措辞漂移。
_EVIDENCE_OPENING = "以下是从知识库检索到的原文片段，每段有唯一的引用编号：\n\n"

#: 名次权重的平滑系数。越小越偏向头部：``k=2`` 时 8 条的份额比约 4.5:1，
#: 既拉开了差距，又不会让末位直接饿死。
_RANK_WEIGHT_K = 2.0

MAX_CARD_CHARS = 800

#: 卡片正文有这么大比例的字符 4-gram 已经出现在本轮证据（或先入选的卡片）里，
#: 就判为重复、不再渲染。卡片多由同一批切片抽取而来，实测 39 个问题里有一半以上的
#: 注入卡片与同轮证据重合 0.85 以上（「关键判定阈值」卡片与 kras 正文重合 1.0）。
#: 证据带引用编号、点开能定位原文，卡片没有；同一段话送两遍，留下能被引用的那份。
#: 取 0.9 比检索去重的 0.85 更严：卡片的措辞改写更常见，宁可漏判。
CARD_COVERED_THRESHOLD = 0.9

#: 与 ``retrieve/diversity.py`` 的 n-gram 取法一致（本包不依赖 agentmem 其它模块，故不复用）
_COVER_NGRAM = 4
_SPACES = re.compile(r"\s+")


def _grams(text: str) -> frozenset[str]:
    """折大小写、压空白后的字符 4-gram 集合。"""
    normalized = _SPACES.sub(" ", text).strip().casefold()
    if len(normalized) < _COVER_NGRAM:
        return frozenset({normalized}) if normalized else frozenset()
    return frozenset(
        normalized[start : start + _COVER_NGRAM]
        for start in range(len(normalized) - _COVER_NGRAM + 1)
    )


def _covered(text: str, seen: frozenset[str]) -> bool:
    """``text`` 是不是基本都已经出现在 ``seen`` 里（有方向：只看 text 的覆盖率）。"""
    grams = _grams(text)
    if not grams or not seen:
        return False
    return len(grams & seen) / len(grams) >= CARD_COVERED_THRESHOLD


def render_persona(persona: PersonaSpec) -> str:
    """把 L4 人格渲染成系统提示的开头部分。"""
    parts: list[str] = [
        f"你是{persona['name']}，专精领域是「{persona['domain']}」。",
    ]

    if desc := persona.get("role_description"):
        parts.append(desc.strip())

    blocks = [join(*parts)]

    if principles := persona.get("principles"):
        blocks.append(
            section(
                "principles",
                "以下是你恒定遵守的方法论，任何情况下都不要违背：\n" + bulleted(principles),
            )
        )

    if glossary := persona.get("glossary"):
        terms = "\n".join(f"- {k}：{v}" for k, v in glossary.items())
        blocks.append(
            section(
                "glossary",
                "本领域术语表。回答时必须使用这里的规范用词，不要换成同义的通俗说法：\n" + terms,
            )
        )

    if bar := persona.get("quality_bar"):
        blocks.append(
            section(
                "quality_bar",
                "一个合格的回答必须满足：\n" + bulleted(bar),
            )
        )

    return join(*blocks)


def render_insights(insights: list[InsightRef]) -> str:
    """渲染 L3 经验。

    带上置信度是有意为之：让模型知道哪些经验是可靠共识、哪些还只是待验证的猜测，
    从而在冲突时有据可依，而不是盲目照搬最后一条。
    """
    return _render_insights(insights)[0]


def _render_insights(
    insights: list[InsightRef], token_budget: int | None = None
) -> tuple[str, list[str]]:
    opening = (
        "以下是你在这个领域积累的经验，来自过往交互中的纠正与验证。\n"
        "**适用时必须遵守**；置信度低于 0.5 的条目可以参考但不要当作定论；\n"
        "若某条经验与下方原文证据直接矛盾，以原文证据为准，并在回答中指出这一矛盾。\n\n"
    )

    lines: list[str] = []
    included: list[str] = []
    for ins in sorted(insights, key=lambda i: i["confidence"], reverse=True):
        required_entry = (
            f"[{ins['insight_id']}] 置信度 {ins['confidence']:.2f}\n"
            f"  场景：{ins['trigger']}\n"
            f"  做法：{ins['guidance']}"
        )
        entry = required_entry
        if rationale := ins.get("rationale"):
            entry += f"\n  依据：{rationale}"
        candidate = section("learned_insights", opening + "\n\n".join([*lines, entry]))
        if token_budget is not None and estimate_tokens(candidate) > token_budget:
            # Never truncate an applicability condition or instruction. Explanatory
            # rationale can be omitted; if the rule itself cannot fit, omit the rule.
            entry = required_entry
            candidate = section("learned_insights", opening + "\n\n".join([*lines, entry]))
            if estimate_tokens(candidate) > token_budget:
                continue
        lines.append(entry)
        included.append(ins["insight_id"])

    return (
        (section("learned_insights", opening + "\n\n".join(lines)), included) if lines else ("", [])
    )


def render_cards(cards: list[CardRef]) -> str:
    """渲染 L2 知识卡片。卡片是派生材料：排在原文证据之后，不能作为引用来源。"""
    return _render_cards(cards)[0]


def _render_cards(
    cards: list[CardRef], token_budget: int | None = None, *, evidence: str = ""
) -> tuple[str, list[str]]:
    """``evidence`` 是本轮实际渲染出的证据区文本：已被它覆盖的卡片不再重复渲染。

    比对的是**渲染后**的证据而不是检索到的切片：证据可能被预算截断，被截掉的那部分
    只留在卡片里时，卡片仍然要送。
    """
    # 卡片是从原文**二次提炼**出来的派生材料：可能概括过头、可能带进抽取模型自己的话，
    # 而且没有引用编号、点不开原文。所以措辞上明确它排在原文证据之后——此前写的是
    # 「可直接引述其结论」，实测回答会把卡片里原文没有的内容当事实写出来，还挂上
    # 证据的引用编号（或编出 [^knowledge_cards] 这种不存在的编号）。
    opening = (
        "以下是从原文提炼出的知识卡片，属于派生材料，不是原文，不能作为引用来源：\n"
        "- 与下方原文证据不一致时，一律以原文证据为准；\n"
        "- 只在卡片里出现、原文证据没有的内容，不要写成知识库的确定结论，"
        "也不要给它挂任何引用编号。\n\n"
    )
    lines: list[str] = []
    included: list[str] = []
    seen = _grams(evidence)
    for card in cards:
        body = truncate(card["body"].strip(), MAX_CARD_CHARS)
        if _covered(body, seen):
            continue
        entry = f"【{card['kind']}】{card['title']}\n{body}"
        rendered = section("knowledge_cards", opening + "\n\n".join([*lines, entry]))
        # Cards are already summaries: retain complete entries rather than summarizing
        # or chopping them again. Raw evidence retains the primary budget and citations.
        if token_budget is not None and estimate_tokens(rendered) > token_budget:
            continue
        lines.append(entry)
        included.append(card["card_id"])
        # 先入选的卡片也算「已经说过」：抽取常把同一结论写成两张标题略异的卡片
        seen = seen | _grams(body)
    return (
        (section("knowledge_cards", opening + "\n\n".join(lines)), included) if lines else ("", [])
    )


def _evidence_location(chunk: ChunkRef) -> str:
    """证据的来源标注：文档标题 › 章节 › 页码。"""
    location = chunk["document_title"]
    if heading := chunk.get("heading_path"):
        location += f" › {heading}"
    if page := chunk.get("page"):
        location += f" › 第 {page} 页"
    return location


def _evidence_overhead(chunks: list[ChunkRef], locations: list[str]) -> int:
    """标签与来源标注自身要占的 token。

    :data:`EVIDENCE_TOKEN_BUDGET` 约束的是**整块证据区**，不只是正文：
    标签同样是送出去的 token，条数一多这部分开销并不小。按全部候选证据估算，
    因此它是实际上限的保守估计（被丢掉的那些不会渲染出来）。
    """
    labels = "".join(
        f'<evidence marker="{chunk["marker"]}" source="{location}">\n\n</evidence>\n\n'
        for chunk, location in zip(chunks, locations, strict=True)
    )
    return estimate_tokens(f"{_EVIDENCE_OPENING}{labels}</retrieved_context>")


def _relevance_weights(count: int) -> list[float]:
    """按**名次**给权重，而不是按分数。

    看起来「按相关性得分按比例分配」更精细，但各家 rerank 的分根本不在一个量纲上，
    照搬会让同一个排序得出完全不同的分配：

    - Cohere 返回 0~1 的 ``relevance_score``；
    - 本地 CrossEncoder 返回的是**原始 logits**，无上下界、可以为负
      （见 `providers/adapters/local_rerank.py`，它直接 ``float(predict(...))``）；
    - 没配 reranker 时退回 RRF 分，各条差距极小（~0.016 上下），
      比例分配会退化成近似均分，等于没有优先级。

    实测同一组排序、只换分数量纲，保留的证据条数在 5~8 之间跳。
    名次是这三种情况下**唯一可比**的信号，所以这里只用名次。

    代价是丢掉了「第 1 名远好于第 2 名」这类强度信息。这是有意的取舍：
    在分数不可比的前提下，那点强度信息并不可信。

    前提：``chunks`` 必须已按相关性降序排列。检索管线的两条路径
    （rerank 与 RRF 兜底）都满足，见 `retrieve/pipeline.py`。
    """
    return [1.0 / (rank + _RANK_WEIGHT_K) for rank in range(count)]


def render_evidence(chunks: list[ChunkRef]) -> str:
    """渲染 L1 原文证据。每块带 marker，供引用标注使用。

    预算分两层：先扣掉标签开销，再把剩下的按**名次**分给各条
    (:func:`allocate`，权重见 :func:`_relevance_weights`)，最后逐条按句子边界
    裁到自己的份额 (:func:`trim_to_budget`)。排名越靠前的证据拿到的篇幅越大；
    份额低于 :data:`MIN_EVIDENCE_TOKENS` 的整条不渲染——
    宁可少给一条证据，也不要给一条被切成残片的证据。

    编号在裁剪前分配，不重编号。上下文装配根据实际保留的 marker 建立引用
    注册表，避免模型沿用历史中的编号去引用本轮没收到的证据。
    """
    return _render_evidence(chunks)[0]


def _render_evidence(
    chunks: list[ChunkRef],
    *,
    token_budget: int = EVIDENCE_TOKEN_BUDGET,
    query: str = "",
    compress: bool = False,
) -> tuple[str, list[str]]:
    if not chunks:
        return "", []

    locations = [_evidence_location(chunk) for chunk in chunks]
    bodies = [chunk["content"].strip() for chunk in chunks]
    if compress:
        bodies = [compact_evidence(body, query) for body in bodies]
    demands = [estimate_tokens(body) for body in bodies]
    weights = _relevance_weights(len(chunks))

    content_budget = token_budget - _evidence_overhead(chunks, locations)
    if content_budget < MIN_EVIDENCE_TOKENS:
        # 标签加一条最小证据都放不下：与其撑破预算，不如本轮不带证据。
        return "", []

    # 每条自己的标签开销：被预算丢掉时要还回去，否则候选越多、留下的证据反而越少
    refunds = [
        estimate_tokens(
            f'<evidence marker="{chunk["marker"]}" source="{location}">\n\n</evidence>\n\n'
        )
        for chunk, location in zip(chunks, locations, strict=True)
    ]
    budgets = allocate(
        weights,
        content_budget,
        min_tokens=MIN_EVIDENCE_TOKENS,
        demands=demands,
        refunds=refunds,
    )

    blocks = [
        section(
            "evidence",
            trim_to_budget(body, token_budget),
            marker=chunk["marker"],
            source=location,
        )
        for chunk, body, token_budget, location in zip(
            chunks, bodies, budgets, locations, strict=True
        )
        # 份额为 0 表示这条被预算丢掉：整条不渲染，绝不留一个空块占位置
        if token_budget > 0
    ]

    # 全部证据都被预算丢掉时不能渲染空壳：外层说「以下是检索到的原文片段」
    # 却一条都没有，会让模型以为资料为空却仍在等它引用。
    if not blocks:
        return "", []

    # escape=False：内层 evidence 块已各自转义过，外层再转义会毁掉它们的标签结构
    rendered = section(
        "retrieved_context",
        _EVIDENCE_OPENING + "\n\n".join(blocks),
        escape=False,
    )
    included = [
        chunk["marker"] for chunk, budget in zip(chunks, budgets, strict=True) if budget > 0
    ]
    return rendered, included


def _citation_rules(must_cite: bool) -> str:
    """引用规则。

    ⚠️ 措辞必须**同时适用于「本轮有资料」与「本轮无资料」两种情况**。
    本块位于跨轮稳定的 system 里，不能再像以前那样按 ``bool(chunks)``
    切换严格程度——那会让 system 随每轮变化，毁掉前缀缓存。
    """
    rules = [
        "回答严格基于本轮消息中提供的资料。资料没有覆盖到的部分，"
        "明确说「知识库中没有相关资料」，**绝不允许编造**。"
        "本轮若未提供任何资料，直接说明这一点，不要凭记忆作答。",
        "引用格式为 `[^编号]`，紧跟在它支撑的那句话末尾，例如：该抑制剂对 JAK1 选择性更高[^c3]。",
        "一句话有多个来源时写成 `[^c1][^c4]`。只能使用本轮 evidence 块中实际出现的编号。",
        "不要在文末另起「参考资料」列表——引用必须内联在正文里，前端会自动渲染。",
    ]
    if must_cite:
        rules.insert(
            1,
            "**提供了资料时，每一个事实性论断都必须带引用**。没有证据支撑的句子只能是"
            "逻辑推理或过渡语，且要用「据此推测」「需要验证」等措辞标明其性质。",
        )
    return section("citation_rules", numbered(rules))


def _style_rules(persona: PersonaSpec) -> str:
    rules = [
        f"使用 {persona.get('language', 'zh-CN')} 回答。",
        # 篇幅跟着问题走：此前固定要求「结论→依据→步骤/注意事项」加「标注不确定」，
        # 实测问「E07 多久恢复」也会写出带四个小标题、列了三条「不确定项」的报告——
        # 输出 token 是延迟和费用的大头，读者也要在里面找那一句答案
        "篇幅与问题相称：事实型问题（是什么、多少、多久）用一到三句直接答完，"
        "不加小标题、不套固定栏目；只有操作步骤、对比分析类问题才分点展开。",
        "第一句就给出答案，依据靠内联引用体现，不必另起「依据」段落复述原文。",
        "不要写「作为一个 AI 模型」「希望对你有帮助」这类客套话，直接进入正题。",
        "资料确实回答不了问题的关键部分时才说明缺什么；不要为凑完整去罗列问题没问到的细节。",
    ]
    if tone := persona.get("tone"):
        rules.insert(1, f"语气风格：{tone}。")
    return section("style_rules", numbered(rules))


def build_system_prompt(persona: PersonaSpec, *, must_cite: bool = True) -> str:
    """装配系统提示 —— **只放跨轮不变的内容**。

    ⚠️ 这里刻意**不接受** chunks / insights / cards：它们随每个问题变化，
    一旦混进 system，整段提示每轮都不同，前缀缓存命中率归零
    （连带后面的历史轮次一起失配）。随问题变化的内容一律走
    :func:`build_turn_message` 放进当轮 user 消息。

    Args:
        must_cite: 该 Space 是否强制引用。它来自 ``space.yaml``，
            跨轮稳定，所以可以安全地留在 system 里。
    """
    return join(
        render_persona(persona),
        _citation_rules(must_cite),
        _style_rules(persona),
    )


def build_turn_message(
    question: str,
    *,
    chunks: list[ChunkRef] | None = None,
    insights: list[InsightRef] | None = None,
    cards: list[CardRef] | None = None,
    budget: ContextBudget | None = None,
    evidence_query: str | None = None,
) -> str:
    """装配当轮的 user 消息：本轮检索到的经验/卡片/证据 + 问题。

    顺序仍是 L3 经验 → L2 卡片 → L1 证据 → 问题，与原先一致，
    只是整体从 system 挪到了当轮消息里。
    """
    evidence_text = _render_evidence(
        chunks or [],
        token_budget=budget.evidence_tokens if budget else EVIDENCE_TOKEN_BUDGET,
        query=evidence_query or question,
        compress=bool(budget and budget.compress_evidence),
    )[0]
    return join(
        _render_insights(insights or [], budget.insight_tokens if budget else None)[0],
        _render_cards(cards or [], budget.card_tokens if budget else None, evidence=evidence_text)[
            0
        ],
        evidence_text,
        section("question", question),
    )


@dataclass(frozen=True)
class AnswerContext:
    messages: list[Message]
    history: list[Turn]
    evidence_markers: list[str]
    insight_ids: list[str]
    card_ids: list[str]
    estimated_tokens: int
    original_estimated_tokens: int


def build_answer_context(
    *,
    persona: PersonaSpec,
    question: str,
    chunks: list[ChunkRef] | None = None,
    insights: list[InsightRef] | None = None,
    cards: list[CardRef] | None = None,
    history: list[Turn] | None = None,
    budget: ContextBudget | None = None,
    evidence_query: str | None = None,
) -> AnswerContext:
    """同时返回实际注入的资料 ID 与估算量，供轨迹和引用采用同一份选择。"""
    recent = (
        compact_history(history or [], budget.history_tokens) if budget else list(history or [])
    )
    insight_text, insight_ids = _render_insights(
        insights or [], budget.insight_tokens if budget else None
    )
    # 证据先渲染：卡片要和「实际送出去的证据」比对去重，顺序上仍是卡片在前
    evidence_text, markers = _render_evidence(
        chunks or [],
        token_budget=budget.evidence_tokens if budget else EVIDENCE_TOKEN_BUDGET,
        query=evidence_query or question,
        compress=bool(budget and budget.compress_evidence),
    )
    card_text, card_ids = _render_cards(
        cards or [], budget.card_tokens if budget else None, evidence=evidence_text
    )
    messages: list[Message] = [
        {
            "role": "system",
            "content": build_system_prompt(persona, must_cite=bool(persona.get("must_cite", True))),
        }
    ]
    messages.extend({"role": turn["role"], "content": turn["content"]} for turn in recent)
    messages.append(
        {
            "role": "user",
            "content": join(insight_text, card_text, evidence_text, section("question", question)),
        }
    )
    estimated = sum(estimate_tokens(message["content"]) for message in messages)
    original = estimated
    if budget is not None:
        original = build_answer_context(
            persona=persona,
            question=question,
            chunks=chunks,
            insights=insights,
            cards=cards,
            history=history,
        ).estimated_tokens
    return AnswerContext(
        messages=messages,
        history=recent,
        evidence_markers=markers,
        insight_ids=insight_ids,
        card_ids=card_ids,
        estimated_tokens=estimated,
        original_estimated_tokens=original,
    )


def build_answer_messages(
    *,
    persona: PersonaSpec,
    question: str,
    chunks: list[ChunkRef] | None = None,
    insights: list[InsightRef] | None = None,
    cards: list[CardRef] | None = None,
    history: list[Turn] | None = None,
    budget: ContextBudget | None = None,
    evidence_query: str | None = None,
) -> list[Message]:
    """生成完整的 messages 列表，可直接喂给 LLMProvider.chat / stream。

    **结构为前缀缓存而设计**：

    ``system`` 只含 L4 人格与固定规则，在同一个 Space 内**逐字节稳定**；
    随问题变化的 L3/L2/L1 与问题本身合并进当轮 user 消息。于是
    ``system + 历史轮次`` 构成一个**只增不改的前缀**——轮数越多，
    可被供应商前缀缓存复用的部分越大。

    反过来说：**任何把易变内容塞回 system 的改动，都会让整段对话的缓存失效**，
    改这里之前请先想清楚这一点（见 `tests/test_prompt_cache.py` 的守护测试）。
    """
    return build_answer_context(
        persona=persona,
        question=question,
        chunks=chunks,
        insights=insights,
        cards=cards,
        history=history,
        budget=budget,
        evidence_query=evidence_query,
    ).messages
