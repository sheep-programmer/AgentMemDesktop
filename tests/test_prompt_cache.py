"""前缀缓存不变量的守护测试。

供应商（Anthropic / OpenAI / DeepSeek）都靠**前缀逐字节一致**来命中缓存。
只要 ``system`` 随每轮变化，不仅 system 本身不能缓存，**后面的历史轮次也会全部失配**，
整段对话的复用归零。

这组测试锁死的核心不变量：

    system 只含跨轮不变的内容；随问题变化的 L1/L2/L3 一律进当轮 user 消息。

如果某条测试挂了，说明有人把易变内容塞回了 system——请把它移到
``build_turn_message``，而不是改这里的断言。
"""

from __future__ import annotations

from agentmem.prompts import build_answer_messages
from agentmem.prompts._shapes import CardRef, ChunkRef, InsightRef, PersonaSpec
from agentmem.prompts.answer import build_system_prompt, build_turn_message

PERSONA: PersonaSpec = {
    "name": "新药研发专家",
    "domain": "药物发现与临床开发",
    "role_description": "你是一名药物化学家。",
    "principles": ["构效关系先行"],
    "quality_bar": ["给出具体数值"],
    "glossary": {"ADMET": "吸收分布代谢排泄毒性"},
    "language": "zh-CN",
    "tone": "简洁",
    "must_cite": True,
}


def _chunk(marker: str, content: str) -> ChunkRef:
    return {
        "marker": marker,
        "chunk_id": f"ch-{marker}",
        "document_title": "资料.pdf",
        "page": 3,
        "content": content,
    }


def _insight(identifier: str, confidence: float) -> InsightRef:
    return {
        "insight_id": identifier,
        "trigger": "问成药性时",
        "guidance": "先看 ADMET",
        "confidence": confidence,
    }


def _card(identifier: str) -> CardRef:
    """卡片正文里嵌入 identifier —— 卡片没有引用编号（只有 chunk 有 marker），
    所以 card_id 不会被渲染出来，正文才是可追踪的标记。"""
    return {
        "card_id": f"card-{identifier}",
        "kind": "pitfall",
        "title": f"hERG 心脏毒性 {identifier}",
        "body": "早期就要反筛。",
        "confidence": 0.9,
    }


# ------------------------------------------------------------------ 核心不变量


def test_system_is_identical_across_different_questions() -> None:
    """不同问题 + 不同检索结果，system 必须逐字节一致。"""
    first = build_answer_messages(
        persona=PERSONA,
        question="化合物 A 成药性如何？",
        chunks=[_chunk("c1", "A 的 IC50 是 12 nM")],
        insights=[_insight("i1", 0.9)],
        cards=[_card("k1")],
    )[0]["content"]

    second = build_answer_messages(
        persona=PERSONA,
        question="化合物 B 的选择性窗口多大？",
        chunks=[_chunk("c1", "B 对野生型的选择性是 80 倍"), _chunk("c2", "另一段资料")],
        insights=[_insight("i2", 0.5), _insight("i3", 0.7)],
        cards=[_card("k2"), _card("k3")],
    )[0]["content"]

    assert first == second, "system 随问题变化了——前缀缓存会全部失效"


def test_system_identical_with_and_without_retrieval() -> None:
    """有检索与纯聊天两种模式下，system 也必须一致。

    以前 citation 规则按 ``bool(chunks)`` 切换严格程度，正是这里的隐患。
    """
    with_evidence = build_answer_messages(
        persona=PERSONA, question="q", chunks=[_chunk("c1", "资料")]
    )[0]["content"]
    without_evidence = build_answer_messages(persona=PERSONA, question="q")[0]["content"]

    assert with_evidence == without_evidence


def test_volatile_content_never_appears_in_system() -> None:
    """L1 证据 / L2 卡片 / L3 经验的内容一个字都不能出现在 system 里。"""
    messages = build_answer_messages(
        persona=PERSONA,
        question="q",
        chunks=[_chunk("c1", "这是独一无二的证据正文 UNIQUE_EVIDENCE")],
        insights=[_insight("UNIQUE_INSIGHT_ID", 0.9)],
        cards=[_card("UNIQUE_CARD_ID")],
    )
    system = messages[0]["content"]

    assert "UNIQUE_EVIDENCE" not in system
    assert "UNIQUE_INSIGHT_ID" not in system
    assert "UNIQUE_CARD_ID" not in system
    assert "retrieved_context" not in system
    assert "learned_insights" not in system
    assert "knowledge_cards" not in system


def test_volatile_content_lands_in_turn_message() -> None:
    """易变内容必须确实出现在当轮 user 消息里——不能是被丢了。"""
    messages = build_answer_messages(
        persona=PERSONA,
        question="化合物 A 如何？",
        chunks=[_chunk("c1", "UNIQUE_EVIDENCE")],
        insights=[_insight("UNIQUE_INSIGHT_ID", 0.9)],
        cards=[_card("UNIQUE_CARD_ID")],
    )
    turn = messages[-1]["content"]

    assert "UNIQUE_EVIDENCE" in turn
    assert "UNIQUE_INSIGHT_ID" in turn
    assert "UNIQUE_CARD_ID" in turn
    assert "化合物 A 如何？" in turn


def test_history_forms_strictly_growing_prefix() -> None:
    """多轮对话应形成「只增不改」的前缀：前 N 条消息逐字节相同。

    这是缓存收益随轮数增长的前提。
    """
    turn1 = build_answer_messages(
        persona=PERSONA, question="第一问", chunks=[_chunk("c1", "证据一")]
    )
    history = [
        {"role": "user", "content": turn1[-1]["content"]},
        {"role": "assistant", "content": "第一答"},
    ]
    turn2 = build_answer_messages(
        persona=PERSONA,
        question="第二问",
        chunks=[_chunk("c1", "证据二")],
        history=history,  # type: ignore[arg-type]
    )

    # 第二轮的前 3 条（system + 第一轮 user + 第一轮 assistant）应与第一轮完全一致
    assert turn2[0] == turn1[0], "system 变了"
    assert turn2[1]["content"] == turn1[-1]["content"], "历史里的第一轮 user 被改写了"
    assert turn2[2]["content"] == "第一答"
    assert len(turn2) == 4


def test_persona_change_is_the_only_thing_that_invalidates_system() -> None:
    """system 应当只随 Space 的人格配置变化——这正是我们希望的缓存粒度。"""
    base = build_system_prompt(PERSONA)
    same = build_system_prompt(dict(PERSONA))  # type: ignore[arg-type]
    assert base == same

    changed = build_system_prompt({**PERSONA, "tone": "严肃"})
    assert changed != base


# ------------------------------------------------------------------ 结构与内容


def test_system_keeps_stable_rules() -> None:
    """固定规则（引用格式、风格）必须留在 system 里，这样才值得缓存。"""
    system = build_system_prompt(PERSONA)
    assert "citation_rules" in system
    assert "style_rules" in system
    assert "新药研发专家" in system


def test_citation_rules_cover_both_modes() -> None:
    """规则措辞要同时适用于「有资料」与「无资料」，因为它不能再按轮切换。"""
    system = build_system_prompt(PERSONA, must_cite=True)
    assert "未提供任何资料" in system, "必须交代无资料时怎么办"
    assert "提供了资料时" in system, "严格引用要求应限定在有资料的前提下"


def test_turn_message_layer_order() -> None:
    """当轮消息内部仍保持 L3 → L2 → L1 → 问题 的顺序。"""
    turn = build_turn_message(
        "我的问题",
        chunks=[_chunk("c1", "证据")],
        insights=[_insight("i1", 0.9)],
        cards=[_card("k1")],
    )
    assert turn.index("learned_insights") < turn.index("knowledge_cards")
    assert turn.index("knowledge_cards") < turn.index("retrieved_context")
    assert turn.index("retrieved_context") < turn.index("我的问题")


def test_empty_turn_message_is_just_the_question() -> None:
    """纯聊天时当轮消息不该有空的证据/经验壳。"""
    turn = build_turn_message("你好")
    assert "retrieved_context" not in turn
    assert "learned_insights" not in turn
    assert "你好" in turn
