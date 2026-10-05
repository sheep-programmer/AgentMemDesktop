"""零模型调用的上下文压缩；只摘取原文，不生成替代事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ._shapes import Turn
from .budget import DEFAULT_MARKER, estimate_tokens, split_sentences

ContextMode = Literal["standard", "economy"]


@dataclass(frozen=True)
class ContextBudget:
    mode: ContextMode
    evidence_tokens: int
    history_tokens: int
    card_tokens: int
    insight_tokens: int
    compress_evidence: bool = False


STANDARD_BUDGET = ContextBudget("standard", 2000, 2400, 1200, 1200)
ECONOMY_BUDGET = ContextBudget("economy", 1200, 1200, 500, 600, True)


def compact_history(history: list[Turn], token_budget: int) -> list[Turn]:
    """选取连续的最近问答，最近一轮原样保留（预算对该轮是软上限）。

    不裁剪用户指令，不用模型摘要，也不把孤立的助手消息当成对话起点。
    同一份历史的结果确定且幂等，保留的消息逐字不变，便于复用前缀缓存。
    """
    starts = [index for index, turn in enumerate(history) if turn["role"] == "user"]
    if not starts or token_budget <= 0:
        return []
    start = starts[-1]
    used = sum(estimate_tokens(turn["content"]) for turn in history[start:])
    for previous in reversed(starts[:-1]):
        cost = sum(estimate_tokens(turn["content"]) for turn in history[previous:start])
        if used + cost > token_budget:
            break
        used += cost
        start = previous
    return history[start:]


# Lexical matching is deliberately a fallback-friendly alternative to running another
# embedding or LLM compressor. CJK bigrams allow matching without a tokenizer dependency.
_WORDS = re.compile(r"[a-zA-Z][a-zA-Z0-9_+-]*|[\u4e00-\u9fff]+")
_STOP_WORDS = frozenset(
    [
        "what",
        "which",
        "how",
        "does",
        "please",
        "explain",
        "tell",
        "about",
        "with",
        "from",
        "this",
        "that",
        "the",
        "and",
        "for",
        "are",
        "can",
        "could",
        "should",
        "would",
        "into",
        "when",
        "why",
        "where",
    ]
)
_STOP_CJK = frozenset(
    (
        "如何",
        "什么",
        "哪些",
        "怎么",
        "这个",
        "那个",
        "是否",
        "可以",
        "需要",
        "进行",
        "情况",
        "请问",
        "说明",
        "一下",
        "关于",
        "方法",
        "我们",
        "他们",
        "它们",
        "以及",
        "或者",
        "根据",
    )
)
_BROAD_QUERY = re.compile(
    r"原文|逐字|全文|完整|概述|概括|总结|综述|所有|全部|对比|比较|区别|"
    r"变化|趋势|范围|最高|最低|平均|最大|最小|各组|差异|"
    r"\b(?:verbatim|overview|summari[sz]e|compare|comparison|differences|entire|all)\b",
    re.IGNORECASE,
)
_STRUCTURED = re.compile(r"```|~~~|^\s*\|.*\||^\s*(?:[-*+] |\d+[.)] )", re.MULTILINE)


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _WORDS.finditer(text.casefold()):
        word = match.group()
        if word[0].isascii():
            if len(word) >= 2 and word not in _STOP_WORDS:
                terms.add(word)
        else:
            terms.update(word[index : index + 2] for index in range(len(word) - 1))
    return terms - _STOP_CJK


def compact_evidence(text: str, query: str) -> str:
    """选取最多三个命中窗口，保留前后句、原文顺序与显式省略标记。

    短文本、代码/表格/列表、宽泛或原文问题不压缩。词面信号不充分时原样
    返回，交给既有预算裁剪；这不是语义等价保证，故仅用于用户选择的节省模式。
    """
    if estimate_tokens(text) <= 450 or _BROAD_QUERY.search(query) or _STRUCTURED.search(text):
        return text
    terms = _terms(query)
    if not terms:
        return text
    sentences = split_sentences(text)
    if len(sentences) < 8:
        return text
    # Never slice a long sentence to create a window: a formula or an unparsed table
    # should follow the existing conservative path rather than become a new excerpt.
    if any(estimate_tokens(sentence) > 350 for sentence in sentences):
        return text
    matches = [terms & _terms(sentence) for sentence in sentences]
    present = set().union(*matches)
    scores = [len(matched) for matched in matches]
    best = max(scores, default=0)
    # One English/domain term is sufficient, but a lone Chinese bigram is too weak.
    if best == 0 or (best < 2 and not any(term.isascii() for term in present)):
        return text
    threshold = max(1, (best + 1) // 2)
    ranked = sorted(range(len(sentences)), key=lambda index: (-scores[index], index))
    selected: set[int] = {0, len(sentences) - 1}
    covered: set[str] = set()
    anchors = 0
    for index in ranked:
        if scores[index] < threshold or anchors >= 3:
            break
        if matches[index] <= covered:
            continue
        selected.update(range(max(0, index - 1), min(len(sentences), index + 2)))
        covered.update(matches[index])
        anchors += 1
    # Every query term found anywhere in the evidence must remain represented.
    # Multiple dispersed requirements therefore fall back instead of silently losing one.
    kept = set().union(*(matches[index] for index in selected))
    if not present <= kept:
        return text
    parts: list[str] = []
    previous = -1
    for index in sorted(selected):
        if index > previous + 1:
            parts.append(DEFAULT_MARKER)
        parts.append(sentences[index])
        previous = index
    result = "".join(parts)
    return result if estimate_tokens(result) < estimate_tokens(text) * 0.85 else text
