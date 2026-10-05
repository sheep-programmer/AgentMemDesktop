"""Offline input-token comparison using explicit synthetic fixtures; no model calls."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

import tiktoken

from agentmem.prompts._shapes import CardRef, ChunkRef, InsightRef, PersonaSpec, Turn
from agentmem.prompts.answer import AnswerContext, build_answer_context
from agentmem.prompts.compaction import ECONOMY_BUDGET, STANDARD_BUDGET

PERSONA: PersonaSpec = {"name": "研究助手", "domain": "试验方法", "must_cite": True}
BACKGROUND = "实验室应记录样品批号、实验时间和仪器状态，人员培训须依照既定规程执行。"
FACT = "EGFR T790M 的体外 IC50 = 12.5 nM。"
QUALIFICATION = "但这并不证明体内有效，也不能据此忽略心脏毒性风险。"


@dataclass
class Case:
    name: str
    question: str
    chunks: list[ChunkRef]
    history: list[Turn] = field(default_factory=list)
    cards: list[CardRef] = field(default_factory=list)
    insights: list[InsightRef] = field(default_factory=list)
    required: list[str] = field(default_factory=list)


def chunk(text: str, index: int = 1) -> ChunkRef:
    return ChunkRef(
        marker=f"c{index}", chunk_id=f"k{index}", document_title="合成试验资料.md", content=text
    )


def cases() -> list[Case]:
    history = [
        turn
        for index in range(4)
        for turn in (
            Turn(role="user", content=f"讨论第 {index} 组试验。"),
            Turn(role="assistant", content=BACKGROUND * 22),
        )
    ]
    history[-2]["content"] = "只讨论本地试验数据，不外传。"
    cards = [
        CardRef(
            card_id=f"card-{i}",
            title=f"试验提醒 {i}",
            kind="pitfall",
            body=BACKGROUND * 10,
            confidence=0.8,
        )
        for i in range(4)
    ]
    insights = [
        InsightRef(
            insight_id=f"ins-{i}",
            trigger=f"第 {i} 组试验",
            guidance=QUALIFICATION * 3,
            rationale=BACKGROUND * 25,
            confidence=0.9,
        )
        for i in range(6)
    ]
    return [
        Case(
            "short_fact",
            "EGFR T790M 的 IC50？",
            [chunk(FACT + QUALIFICATION)],
            required=[FACT, QUALIFICATION],
        ),
        Case(
            "middle_fact",
            "EGFR T790M 的 IC50？",
            [chunk(BACKGROUND * 45 + FACT + QUALIFICATION + BACKGROUND * 45)],
            required=[FACT, QUALIFICATION],
        ),
        Case(
            "long_history",
            "EGFR T790M 的 IC50？",
            [chunk(FACT + QUALIFICATION)],
            history=history,
            required=[FACT, QUALIFICATION, history[-2]["content"]],
        ),
        Case(
            "large_supplements",
            "EGFR T790M 的 IC50？",
            [chunk(FACT + QUALIFICATION)],
            history=history,
            cards=cards,
            insights=insights,
            required=[FACT, QUALIFICATION, history[-2]["content"]],
        ),
        Case(
            "structured_evidence",
            "IC50 表中是多少？",
            [
                chunk(
                    "| 指标 | 数值 |\n| --- | --- |\n| IC50 | 12.5 nM |\n\n"
                    "```python\nvalue = 12.5\n```"
                )
            ],
            required=["| IC50 | 12.5 nM |", "value = 12.5"],
        ),
    ]


def token_count(context: AnswerContext, encoding: tiktoken.Encoding) -> int:
    return sum(
        len(encoding.encode(message["content"], disallowed_special=()))
        for message in context.messages
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    encodings = {name: tiktoken.get_encoding(name) for name in ("cl100k_base", "o200k_base")}
    results: list[dict[str, object]] = []
    for case in cases():
        variants: dict[str, dict[str, object]] = {}
        for name, budget in (
            ("previous", None),
            ("standard", STANDARD_BUDGET),
            ("economy", ECONOMY_BUDGET),
        ):
            started = time.perf_counter()
            for _ in range(args.iterations):
                context = build_answer_context(
                    persona=PERSONA,
                    question=case.question,
                    chunks=case.chunks,
                    history=case.history,
                    cards=case.cards,
                    insights=case.insights,
                    budget=budget,
                )
            elapsed = (time.perf_counter() - started) * 1000 / args.iterations
            prompt = "\n".join(message["content"] for message in context.messages)
            variants[name] = {
                "estimated_tokens": context.estimated_tokens,
                "content_tokens": {
                    key: token_count(context, encoding) for key, encoding in encodings.items()
                },
                "history_messages": len(context.history),
                "evidence_count": len(context.evidence_markers),
                "card_count": len(context.card_ids),
                "insight_count": len(context.insight_ids),
                "required_spans_kept": sum(span in prompt for span in case.required),
                "required_spans_total": len(case.required),
                "mean_assembly_ms": round(elapsed, 3),
            }
        results.append({"case": case.name, "variants": variants})
    output = {
        "python_version": sys.version.split()[0],
        "tiktoken_version": version("tiktoken"),
        "scope": "synthetic offline fixtures; content tokens exclude chat framing; "
        "not billing or answer-quality evaluation",
        "model_calls": 0,
        "compression_model_calls": 0,
        "iterations": args.iterations,
        "results": results,
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
