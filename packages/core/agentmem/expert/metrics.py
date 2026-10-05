"""检索侧指标 —— 用来区分「证据没捞到」与「模型没用上证据」。

三个口径与 RAGAS 对齐（faithfulness / context recall / context precision@K），
但判定来源换成了我们自己的 judge：它已经看过问题、参考答案与答案，现在再多看
一份被检索到的证据，顺带回答三个问题——哪些必须要点真的被证据支撑了、答案的
每条论断有没有出处、哪几号证据确实起了作用。算术留在本模块，判定留给模型，
公式因此可以被穷举测试，也不需要为了测一个除法去造模型。

指标都取 ``0~1``，与 ``EvalItemScore.score``（0~100 的分数）刻意不同：
这三把尺子量的是比例，不是好坏。拿不到判定时返回 ``None`` 而不是 0 ——
「没测」与「测出来是零」是两件事，混在一起会让 A/B 的对比失真。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from agentmem.types import AgentMemModel, RetrievalMetrics


class AnswerClaim(AgentMemModel):
    """答案里的一条实质论断，以及它在证据里有没有依据。"""

    claim: str
    supported: bool = False


class EvidenceAudit(AgentMemModel):
    """judge 对本次检索的审计输出。"""

    must_include_covered: list[str] = Field(
        default_factory=list, description="must_include 里确实被证据支撑的要点"
    )
    answer_claims: list[AnswerClaim] = Field(default_factory=list, description="答案的逐条论断")
    used_evidence: list[str] = Field(default_factory=list, description="真正起了支撑作用的证据编号")


#: 判定文本比对前的归一化：去掉空白与常见标点，再比小写。
#: judge 被要求「原样抄写」must_include，但模型很难一字不差，直接比字符串会
#: 把「ADMET」与「ADMET 五项」判成不匹配，进而把 context recall 算低。
_PUNCTUATION = " \t\n\r，。、；：！？（）()【】[]「」“”\"'`~!@#$%^&*-_=+|\\/<>.,;:?"


def _normalize(text: str) -> str:
    """比对用的归一化文本。"""
    return "".join(char for char in text.strip().casefold() if char not in _PUNCTUATION)


def _is_covered(covered: Sequence[str], claim: str) -> bool:
    """一条要点是否被判定为已覆盖。

    先精确比对，再退到包含关系——judge 可能把要点扩写或缩写了一点。
    """
    target = _normalize(claim)
    if not target:
        return False
    return any(
        normalized and (normalized == target or normalized in target or target in normalized)
        for normalized in (_normalize(item) for item in covered)
    )


def context_recall(covered: Sequence[str], must_include: Sequence[str]) -> float | None:
    """参考答案的要点里，能被检索到的证据支撑的比例。

    没有配置 must_include（自动出题的早期版本、手工建的开放题）时返回 ``None``：
    没有论断集合就无从谈召回，给 0 会让这类题白白拖低整体。
    """
    claims = [item for item in must_include if item.strip()]
    if not claims:
        return None
    hits = sum(1 for claim in claims if _is_covered(covered, claim))
    return round(hits / len(claims), 4)


def context_precision(used: Sequence[str], retrieved: Sequence[str]) -> float | None:
    """Context Precision@K：被用到的证据排得越靠前，分数越高。

    ``Σ (Precision@k × v_k) / 相关条目数``，其中 ``v_k`` 表示第 k 条检索结果是否
    真的起了作用。分子看排序、分母看条数：一条证据都没用到就是 0，检索为空时
    返回 ``None``（没得评，而不是评了 0）。
    """
    if not retrieved:
        return None
    # 只认「检索到过、且被用到」的编号：模型可能写出并不存在的证据号，
    # 那种编号不该进入分母，否则算出来的精度会凭空虚高
    relevant = {marker for marker in used if marker} & set(retrieved)
    if not relevant:
        return 0.0
    hits = 0
    weighted = 0.0
    for index, marker in enumerate(retrieved, start=1):
        if marker in relevant:
            hits += 1
            weighted += hits / index
    return round(weighted / len(relevant), 4)


def faithfulness(claims: Sequence[AnswerClaim]) -> float | None:
    """答案的论断里，能在检索上下文里找到依据的比例。

    空话、过渡句不算论断（Prompt 里已要求 judge 不要列），因此这里只做除法。
    一条论断都没有时返回 ``None``：拒答或纯闲聊的回答不该被算成 0 分的不忠实。
    """
    if not claims:
        return None
    supported = sum(1 for claim in claims if claim.supported)
    return round(supported / len(claims), 4)


def audit_from_judge(data: Mapping[str, object]) -> EvidenceAudit | None:
    """从 judge 的 JSON 输出里取出审计块。

    字段缺失或类型不对时返回 ``None``——审计是加分项，解析失败只该让指标变成
    「没测到」，不该毁掉整次评测的分数。
    """
    raw = data.get("evidence_audit")
    if not isinstance(raw, Mapping):
        return None
    claims: list[AnswerClaim] = []
    raw_claims = raw.get("answer_claims")
    if isinstance(raw_claims, Sequence) and not isinstance(raw_claims, str):
        for item in raw_claims:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("claim") or "").strip()
            if text:
                claims.append(AnswerClaim(claim=text, supported=bool(item.get("supported"))))
    return EvidenceAudit(
        must_include_covered=_str_list(raw.get("must_include_covered")),
        answer_claims=claims,
        used_evidence=_str_list(raw.get("used_evidence")),
    )


def _str_list(value: object) -> list[str]:
    """把 JSON 里可能是列表、字符串或缺失的字段收敛成字符串列表。"""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def compute(
    audit: EvidenceAudit | None,
    *,
    must_include: Sequence[str],
    retrieved_markers: Sequence[str],
) -> RetrievalMetrics:
    """由审计结果算出三个指标。"""
    if audit is None:
        return RetrievalMetrics(evidence=len(retrieved_markers))
    return RetrievalMetrics(
        context_recall=context_recall(audit.must_include_covered, must_include),
        context_precision=context_precision(audit.used_evidence, retrieved_markers),
        faithfulness=faithfulness(audit.answer_claims),
        claims=len(audit.answer_claims),
        evidence=len(retrieved_markers),
        audited=True,
    )


def average(metrics: Sequence[RetrievalMetrics]) -> RetrievalMetrics | None:
    """把逐题指标平均成整轮指标。

    每项指标只对「测到过」的题目求平均，并把参与计算的题目数一并记下；
    空列表返回 ``None``。
    """
    scored = [item for item in metrics if item.audited]
    if not scored:
        return None

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return RetrievalMetrics(
        context_recall=mean(
            [item.context_recall for item in scored if item.context_recall is not None]
        ),
        context_precision=mean(
            [item.context_precision for item in scored if item.context_precision is not None]
        ),
        faithfulness=mean([item.faithfulness for item in scored if item.faithfulness is not None]),
        claims=sum(item.claims for item in scored),
        evidence=sum(item.evidence for item in scored),
        audited=True,
    )
