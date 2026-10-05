"""压缩必须保留原文事实、完整问答和可引用的实际证据。"""

from __future__ import annotations

import pytest

from agentmem.prompts._shapes import CardRef, ChunkRef, InsightRef, PersonaSpec, Turn
from agentmem.prompts.answer import build_answer_context
from agentmem.prompts.budget import DEFAULT_MARKER, estimate_tokens
from agentmem.prompts.compaction import (
    ECONOMY_BUDGET,
    STANDARD_BUDGET,
    compact_evidence,
    compact_history,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.retrieve.citations import CitationEmitter
from agentmem.retrieve.context import assemble_answer_context
from agentmem.retrieve.models import QueryPlan, RetrievalResult, ScoredChunk
from agentmem.retrieve.pipeline import RetrievalPipeline
from agentmem.store import Database
from agentmem.types import Persona

PERSONA: PersonaSpec = {"name": "研发专家", "domain": "药物发现", "must_cite": True}
FILLER = "实验室需要记录样品批号及实验时间，仪器维护与人员培训应按既定规程执行。"
FACT = "EGFR T790M 的体外 IC50 = 12.5 nM。"
WARNING = "但这并不证明体内有效，也不能据此忽略心脏毒性风险。"


def _prose() -> str:
    return FILLER * 45 + "选择性试验采用同一批细胞。" + FACT + WARNING + FILLER * 45


def _chunk(content: str, marker: str = "c1") -> ChunkRef:
    return {"marker": marker, "chunk_id": marker, "document_title": "试验.md", "content": content}


def _dialogue(count: int = 4) -> list[Turn]:
    return [
        turn
        for index in range(count)
        for turn in (
            Turn(role="user", content=f"讨论第 {index} 组试验。"),
            Turn(role="assistant", content=FILLER * 12),
        )
    ]


def test_query_window_keeps_middle_fact_and_adjacent_qualification() -> None:
    text = _prose()
    result = compact_evidence(text, "EGFR T790M 的 IC50 是多少？")
    assert FACT in result
    assert WARNING in result
    assert DEFAULT_MARKER in result
    assert estimate_tokens(result) < estimate_tokens(text) / 3
    assert result.index(FACT) < result.index(WARNING)


def test_cjk_query_finds_an_internal_window() -> None:
    fact = "选择性窗口达到八十倍，但仍须复测野生型抑制活性。"
    text = FILLER * 45 + fact + FILLER * 45
    result = compact_evidence(text, "选择性窗口有多大？")
    assert fact in result
    assert estimate_tokens(result) < estimate_tokens(text)


@pytest.mark.parametrize(
    "query", ["贴出完整原文", "总结全文", "比较所有试验", "Give a verbatim quote"]
)
def test_broad_or_verbatim_questions_do_not_apply_sentence_extraction(query: str) -> None:
    assert compact_evidence(_prose(), query) == _prose()


@pytest.mark.parametrize(
    "structure",
    [
        "```python\nprint(12.5)\n```",
        "| 指标 | 数值 |\n| IC50 | 12.5 nM |",
        "- 先测活性\n- 再做反筛",
        "1. 测试活性\n2. 测试毒性",
    ],
)
def test_structured_evidence_is_not_rewritten_as_sentence_windows(structure: str) -> None:
    text = _prose() + "\n" + structure
    assert compact_evidence(text, "EGFR T790M IC50") == text


def test_short_or_semantically_unmatched_evidence_is_unchanged() -> None:
    assert compact_evidence(FACT, "EGFR IC50") == FACT
    assert compact_evidence(_prose(), "溶解度测量") == _prose()


def test_unmatched_english_term_does_not_strengthen_a_weak_cjk_match() -> None:
    assert compact_evidence(_prose(), "P53 风险") == _prose()


def test_query_terms_in_distant_sections_are_not_silently_dropped() -> None:
    points = ["EGFR", "hERG", "ADMET", "PPB", "CYP3A4"]
    text = "".join(FILLER * 15 + f"{point} 试验需要独立验证。" for point in points)
    result = compact_evidence(text, "EGFR hERG ADMET PPB CYP3A4")
    assert all(point in result for point in points)


def test_history_keeps_recent_complete_rounds_without_rewriting() -> None:
    history = _dialogue()
    recent = compact_history(history, 1000)
    assert recent == history[-len(recent) :]
    assert recent[-2:] == history[-2:]
    assert len(recent) < len(history)
    assert sum(estimate_tokens(turn["content"]) for turn in recent) <= 1000
    assert recent[0]["role"] == "user"
    assert compact_history(recent, 1000) == recent


def test_latest_round_is_preserved_even_when_it_exceeds_the_soft_budget() -> None:
    history = _dialogue()
    assert compact_history(history, 20) == history[-2:]
    assert compact_history(history, 0) == []


def test_history_does_not_start_with_an_orphaned_assistant_message() -> None:
    history = [Turn(role="assistant", content="遗留回答"), *_dialogue(1)]
    assert compact_history(history, 10000) == history[1:]
    assert compact_history([history[0]], 10000) == []


def test_short_request_is_identical_under_all_budgets() -> None:
    kwargs = {"persona": PERSONA, "question": "EGFR IC50？", "chunks": [_chunk(FACT)]}
    old = build_answer_context(**kwargs)  # type: ignore[arg-type]
    for budget in (STANDARD_BUDGET, ECONOMY_BUDGET):
        current = build_answer_context(**kwargs, budget=budget)  # type: ignore[arg-type]
        assert current.messages == old.messages
        assert current.estimated_tokens == current.original_estimated_tokens


def test_system_and_latest_user_constraints_do_not_change_between_modes() -> None:
    history = _dialogue()
    history[-2]["content"] = "只讨论本地部署，禁止将资料上传第三方。"
    contexts = [
        build_answer_context(persona=PERSONA, question="继续", history=history, budget=budget)
        for budget in (STANDARD_BUDGET, ECONOMY_BUDGET)
    ]
    assert contexts[0].messages[0] == contexts[1].messages[0]
    assert all(context.history[-2:] == history[-2:] for context in contexts)


def test_supplement_budgets_report_only_whole_injected_items() -> None:
    cards = [
        CardRef(
            card_id=f"k{i}", kind="pitfall", title=f"提醒 {i}", body=FILLER * 10, confidence=0.8
        )
        for i in range(4)
    ]
    insights = [
        InsightRef(
            insight_id=f"i{i}",
            trigger=f"讨论第 {i} 组试验时",
            guidance=WARNING * 4,
            rationale=FILLER * 30,
            confidence=0.9,
        )
        for i in range(6)
    ]
    context = build_answer_context(
        persona=PERSONA,
        question="EGFR IC50",
        chunks=[_chunk(FACT)],
        cards=cards,
        insights=insights,
        budget=ECONOMY_BUDGET,
    )
    body = context.messages[-1]["content"]
    assert 0 < len(context.card_ids) < len(cards)
    assert 0 < len(context.insight_ids) < len(insights)
    for insight in insights:
        if insight["insight_id"] in context.insight_ids:
            assert insight["trigger"] in body and insight["guidance"] in body
        else:
            assert f"[{insight['insight_id']}]" not in body
    for card in cards:
        assert (card["title"] in body) == (card["card_id"] in context.card_ids)
    assert context.estimated_tokens < context.original_estimated_tokens


def test_oversized_insight_instruction_is_omitted_instead_of_cut() -> None:
    insight = InsightRef(insight_id="i1", trigger="适用条件", guidance=FILLER * 50, confidence=0.9)
    context = build_answer_context(
        persona=PERSONA, question="q", insights=[insight], budget=ECONOMY_BUDGET
    )
    assert context.insight_ids == []
    assert "learned_insights" not in context.messages[-1]["content"]


def test_references_reject_markers_that_did_not_fit_this_context() -> None:
    chunks = [
        ScoredChunk(
            chunk_id=f"k{i}", document_id="d", document_title="试验.md", content=FILLER * 50
        )
        for i in range(8)
    ]
    original = [chunk.content for chunk in chunks]
    context = assemble_answer_context(
        persona=Persona(), question="q", chunks=chunks, budget=ECONOMY_BUDGET
    )
    assert 0 < len(context.registry) < len(chunks)
    assert context.usage.evidence_count == len(context.registry)
    dropped = {f"c{i + 1}" for i in range(len(chunks))} - context.registry.known()
    assert CitationEmitter(context.registry).accept(dropped) == []
    assert [chunk.content for chunk in chunks] == original
    assert (
        context.usage.saved_estimated_tokens
        == context.usage.original_estimated_tokens - context.usage.estimated_tokens
    )


def test_verbatim_request_uses_standard_budget(
    database: Database, mock_registry: ProviderRegistry
) -> None:
    pipeline = RetrievalPipeline(space_id="s", database=database, registry=mock_registry)
    result = RetrievalResult(
        space_id="s",
        plan=QueryPlan(original="给我原文"),
        chunks=[ScoredChunk(chunk_id="k", document_id="d", content=_prose())],
    )
    context = pipeline.assemble_context_details(result, question="给我原文", context_mode="economy")
    assert context.usage.mode == "standard"
