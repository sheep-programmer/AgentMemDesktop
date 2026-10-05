"""真实链路的问答计时：对正在运行的后端发问，逐事件记录耗时、用量与模型调用次数。

与 ``benchmark_context.py`` 不同，这里**会真实调用已配置的模型**（含云端 API），
数字受网络与提供方负载影响，需多轮取中位数再比较。

每个场景新建一个会话；带 ``follow_up`` 的场景在同一会话里追问一轮，用来覆盖
「依赖上文 → 查询改写」这条路径。模型调用次数取自全局库的 ``usage_records``
（只数 ``kind='llm'``），因此运行期间不要在同一后端上做别的模型任务。

用法::

    uv run python scripts/benchmark_chat.py --space 01M2NAGHNY53464Q7G9G5JQGFQ \\
        --rounds 3 --output docs/benchmarks/chat-latency.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE = "http://127.0.0.1:8765/api/v1"


@dataclass
class Scenario:
    name: str
    question: str
    #: 回答里必须出现的事实片段（粗粒度正确性检查）
    expect: list[str]
    follow_up: str | None = None
    follow_expect: list[str] = field(default_factory=list)


SCENARIOS = [
    Scenario("短事实", "长江大学哪一年建立的", ["2003"]),
    Scenario(
        "表格参数",
        "CMPD-005 的口服生物利用度和半衰期是多少？",
        ["45%"],
        follow_up="那它在 40℃/75%RH 下 6 个月降解多少？",
        follow_expect=["0%"],
    ),
    Scenario("要点解释", "KRAS G12C 共价抑制剂首选哪种弹头？为什么？", ["丙烯酰胺"]),
    Scenario("跨文档对比", "CMPD-003 的晶型和熔点是什么？", ["Form A", "153"]),
    Scenario("长文段落", "为什么单纯依赖共价弹头无法克服 C797S 突变？", ["C797S"]),
]


@dataclass
class TurnResult:
    scenario: str
    turn: str
    question: str
    retrieval_ms: float | None = None
    context_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_tokens: int | None = None
    evidence_count: int | None = None
    retrieved: int | None = None
    citations: int = 0
    cited_markers: list[str] = field(default_factory=list)
    llm_calls: int = 0
    llm_purposes: list[str] = field(default_factory=list)
    cached_tokens: int | None = None
    rewritten: str | None = None
    facts_ok: bool = False
    answer: str = ""
    error: str | None = None


def _llm_calls(db: Path, since_ms: int) -> tuple[int, list[str], int | None]:
    """自 ``since_ms`` 起落库的 LLM 调用（次数、用途、对话这一路命中的缓存 token）。"""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT purpose, cached_tokens FROM usage_records"
            " WHERE kind = 'llm' AND created_at >= ? ORDER BY created_at",
            (since_ms,),
        ).fetchall()
    cached = next((int(row[1] or 0) for row in rows if row[0] == "chat"), None)
    return len(rows), [str(row[0]) for row in rows], cached


def _ask(
    client: httpx.Client, conversation_id: str, question: str, context_mode: str
) -> TurnResult:
    result = TurnResult(scenario="", turn="", question=question)
    parts: list[str] = []
    started = time.perf_counter()
    elapsed = lambda: round((time.perf_counter() - started) * 1000, 1)  # noqa: E731
    with client.stream(
        "POST",
        f"/conversations/{conversation_id}/chat",
        json={"content": question, "context_mode": context_mode},
        timeout=300,
    ) as response:
        response.raise_for_status()
        name = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload: Any = json.loads(line[5:].strip())
            if name == "retrieval":
                result.retrieval_ms = elapsed()
                result.retrieved = len(payload.get("chunks", []))
            elif name == "rewrite":
                result.rewritten = payload.get("rewritten")
            elif name == "context":
                result.context_ms = elapsed()
                result.estimated_tokens = payload.get("estimated_tokens")
                result.evidence_count = payload.get("evidence_count")
            elif name == "delta":
                if result.first_token_ms is None:
                    result.first_token_ms = elapsed()
                parts.append(payload.get("text", ""))
            elif name == "citation":
                result.citations += 1
                result.cited_markers.append(str(payload.get("marker")))
            elif name == "done":
                result.total_ms = elapsed()
                usage = payload.get("usage", {})
                result.prompt_tokens = usage.get("prompt_tokens")
                result.completion_tokens = usage.get("completion_tokens")
            elif name == "error":
                result.error = f"{payload.get('code')}: {payload.get('message')}"
    result.answer = "".join(parts)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    usage_db = Path(args.data_dir) / "agentmem.db"
    results: list[TurnResult] = []
    with httpx.Client(base_url=args.base) as client:
        for round_index in range(args.rounds):
            for scenario in SCENARIOS:
                created = client.post(
                    f"/spaces/{args.space}/conversations",
                    json={"space_id": args.space, "title": f"bench {scenario.name}"},
                )
                created.raise_for_status()
                conversation_id = created.json()["id"]
                turns = [("first", scenario.question, scenario.expect)]
                if scenario.follow_up:
                    turns.append(("follow", scenario.follow_up, scenario.follow_expect))
                for turn, question, expect in turns:
                    since = int(time.time() * 1000)
                    result = _ask(client, conversation_id, question, args.context_mode)
                    # 用量记录在回答落库之后才写，稍等一下再数
                    time.sleep(0.5)
                    result.llm_calls, result.llm_purposes, result.cached_tokens = _llm_calls(
                        usage_db, since
                    )
                    result.scenario = scenario.name
                    result.turn = turn
                    result.facts_ok = all(item in result.answer for item in expect)
                    results.append(result)
                    sys.stdout.write(
                        f"[r{round_index}] {scenario.name}/{turn}: retrieval={result.retrieval_ms}"
                        f" first={result.first_token_ms} total={result.total_ms}"
                        f" prompt={result.prompt_tokens} completion={result.completion_tokens}"
                        f" cached={result.cached_tokens} llm={result.llm_calls}"
                        f" cites={result.citations} facts={result.facts_ok}"
                        + (f" error={result.error}" if result.error else "")
                        + "\n"
                    )
                    sys.stdout.flush()
                if not args.keep:
                    client.delete(f"/conversations/{conversation_id}")
    return {
        "rounds": args.rounds,
        "summary": summarize(results),
        "turns": [asdict(item) for item in results],
    }


def summarize(results: list[TurnResult]) -> list[dict[str, Any]]:
    """按场景 / 轮次取中位数。"""
    keys: dict[tuple[str, str], list[TurnResult]] = {}
    for item in results:
        keys.setdefault((item.scenario, item.turn), []).append(item)

    def median(values: list[float | int | None]) -> float | None:
        present = [float(value) for value in values if value is not None]
        return round(statistics.median(present), 1) if present else None

    rows = []
    for (scenario, turn), items in keys.items():
        rows.append(
            {
                "scenario": scenario,
                "turn": turn,
                "retrieval_ms": median([i.retrieval_ms for i in items]),
                "first_token_ms": median([i.first_token_ms for i in items]),
                "total_ms": median([i.total_ms for i in items]),
                "prompt_tokens": median([i.prompt_tokens for i in items]),
                "completion_tokens": median([i.completion_tokens for i in items]),
                "cached_tokens": median([i.cached_tokens for i in items]),
                "llm_calls": median([i.llm_calls for i in items]),
                "evidence": median([i.evidence_count for i in items]),
                "citations": median([i.citations for i in items]),
                "facts_ok": f"{sum(i.facts_ok for i in items)}/{len(items)}",
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", required=True)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--context-mode", default="standard", choices=["standard", "economy"])
    parser.add_argument("--keep", action="store_true", help="保留测试会话（默认跑完删除）")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args)
    for row in report["summary"]:
        sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
