"""LLM-as-Judge —— 进化闭环的第 ③ 步（自动评价）与第 ⑥ 步（A/B 评测）共用。

两个用途，两个函数：

- `build_judge_messages`     给一次真实对话打分。用户没给显式反馈时，靠它产生蒸馏原料。
- `build_eval_judge_messages` 给测验集的一道题打分。用于 A/B 对比"注入经验 vs 不注入"。

**打分必须锚定在 Persona 的 quality_bar 上**，而不是模型自己的通用审美。
否则同一个答案今天 0.8 明天 0.6，分数没有可比性，整个进化闭环就失去了度量基准。

评分维度固定为四项，与专家度指数中的 accuracy / groundedness 对齐：
    correctness  事实是否正确
    groundedness 是否有证据支撑、有没有编造
    completeness 是否答全了问题
    standard_fit 是否符合本领域的质量标准（来自 quality_bar）
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import bulleted, join, schema_block, section, truncate
from ._shapes import ChunkRef, Message, PersonaSpec

MAX_EVIDENCE_CHARS = 1200
MAX_ANSWER_CHARS = 4000


class EvalItem(TypedDict):
    """一道测验题。`reference` 可空——空时按 quality_bar 打分。"""

    question: str
    reference: NotRequired[str | None]
    must_include: NotRequired[list[str]]


#: 裁判四个维度的权重，**唯一来源**。提示词里的表格与解析端的兜底计算都以它为准，
#: `tests` 里有一条守护测试确认两边没有漂移。
#:
#: 解析端需要它，是因为裁判偶尔会漏写汇总字段 `score`，只给出四个维度分。
#: 此前那种情况下 `data.get("score", 0.0)` 直接得 0：实测「四维全 1.0、verdict 写着
#: 『全对』」的满分答案被记成了零分。有了权重就能按提示词自己的定义精确算回来。
JUDGE_WEIGHTS: dict[str, float] = {
    "correctness": 0.40,
    "groundedness": 0.30,
    "completeness": 0.20,
    "standard_fit": 0.10,
}


_DIMENSIONS = """\
按以下四个维度各打 0~1 分（可取小数），然后给出加权总分：

| 维度 | 含义 | 权重 |
|---|---|---|
| `correctness`  | 陈述的事实是否正确，有无硬伤 | 0.40 |
| `groundedness` | 论断是否有证据支撑、引用是否对应得上、有无编造 | 0.30 |
| `completeness` | 是否覆盖了问题的全部要点，有无避重就轻 | 0.20 |
| `standard_fit` | 是否满足下方 quality_bar 列出的领域标准 | 0.10 |

`score` = 四项的加权和，保留两位小数。\
"""

_CALIBRATION = """\
分数锚点，请严格对齐，不要滥发高分：

- **0.9~1.0** 专家级。事实无误、证据充分、要点齐全，挑不出实质毛病。
- **0.7~0.9** 合格。方向正确、主要内容可用，但有小疏漏或表述不够精确。
- **0.5~0.7** 勉强。答到了边上，但缺关键信息，或有需要读者自行纠正的错误。
- **0.3~0.5** 不合格。方向偏了，或存在会误导人的错误。
- **0.0~0.3** 有害。编造事实、答非所问，或给出会造成实际损失的建议。

**默认应该给 0.6~0.8。** 一份答案要拿到 0.9 以上，必须是你挑不出任何实质问题。
宽松的评分会让 A/B 评测失去分辨力——所有变体都拿 0.95，就什么都测不出来了。\
"""


def _quality_bar_block(persona: PersonaSpec) -> str:
    bar = persona.get("quality_bar") or []
    if not bar:
        return section(
            "quality_bar",
            f"本领域（{persona['domain']}）未配置专门的质量标准，"
            "`standard_fit` 维度请按该领域的通用专业水准判断。",
        )
    return section(
        "quality_bar",
        f"本领域（{persona['domain']}）的答案质量标准，`standard_fit` 维度据此打分：\n"
        + bulleted(bar),
    )


_SCHEMA: dict[str, Any] = {
    "correctness": "0~1",
    "groundedness": "0~1",
    "completeness": "0~1",
    "standard_fit": "0~1",
    "score": "四项加权总分，0~1，保留两位小数",
    "verdict": "一句话总评",
    "issues": [
        {
            "severity": "critical | major | minor",
            "quote": "答案中有问题的原话片段",
            "problem": "问题是什么",
            "should_be": "正确的说法应该是什么（不确定就写 null）",
        }
    ],
    "strengths": ["答得好的地方，用于从好评中蒸馏经验；没有就空数组"],
}

_EXAMPLE: dict[str, Any] = {
    "correctness": 0.6,
    "groundedness": 0.4,
    "completeness": 0.8,
    "standard_fit": 0.5,
    "score": 0.58,
    "verdict": "整体思路对，但把两个激酶的选择性数据弄反了，且关键结论没有引用支撑",
    "issues": [
        {
            "severity": "major",
            "quote": "该抑制剂对 JAK2 的选择性高于 JAK1",
            "problem": "检索到的资料里是 JAK1 选择性更高，原文数据被弄反",
            "should_be": "该抑制剂对 JAK1 的选择性高于 JAK2",
        },
        {
            "severity": "minor",
            "quote": "建议该化合物直接进入临床试验",
            "problem": "没有说明临床前毒理是否完成，不满足 quality_bar 的要求",
            "should_be": None,
        },
    ],
    "strengths": [],
}

#: 带证据的评测打分才要求审计块。真实对话的打分不需要——它的 groundedness
#: 已经由证据块本身约束，再让模型逐条列论断只会加重负担。
_AUDIT_RULES = """\
除了打分，还要审计这次**检索**的质量。检索好坏与答案好坏是两件事：答案差可能是
证据没捞到，也可能是模型没用上证据，只有分开看才知道该改检索还是改提示词。

三个字段：

- `must_include_covered`：上方 must_include 列表里**确实被证据支撑**的要点，原样抄写要点文本。
  注意区分「答案提到了」与「证据里真有」——答案是模型写的，可能编造。
- `answer_claims`：把答案里的**实质论断**逐条列出（最多 12 条），标明 `supported`：
  该论断能否在证据里找到依据。空话、过渡句、复述问题的话不算论断，不要列。
  答案里没有出处的具体细节（数值、名称、步骤）必须列出来并标 `supported: false`。
- `used_evidence`：对回答**有实质支撑作用**的证据编号（如 "c1"）。只是被检索到、
  但答案根本没用上的不要写。一条都没用上就给空数组。\
"""

_AUDIT_SCHEMA: dict[str, Any] = {
    "evidence_audit": {
        "must_include_covered": ["被证据支撑的要点原文"],
        "answer_claims": [{"claim": "答案里的一句实质论断", "supported": True}],
        "used_evidence": ["c1"],
    }
}

_AUDIT_EXAMPLE: dict[str, Any] = {
    "evidence_audit": {
        "must_include_covered": ["ADMET"],
        "answer_claims": [
            {"claim": "该化合物对 JAK1 的选择性高于 JAK2", "supported": True},
            {"claim": "建议直接进入临床试验", "supported": False},
        ],
        "used_evidence": ["c1", "c3"],
    }
}


def _evidence_block(chunks: list[ChunkRef] | None) -> str:
    if not chunks:
        return section(
            "evidence",
            "本次回答没有检索到资料。`groundedness` 维度请判断答案是否恰当地"
            "承认了信息缺失——如果它在无资料情况下编造了具体细节，应当重罚。",
        )
    blocks = [
        section(
            "evidence",
            truncate(c["content"].strip(), MAX_EVIDENCE_CHARS),
            marker=c["marker"],
            source=c["document_title"],
        )
        for c in chunks
    ]
    return section(
        "retrieved_evidence",
        "回答时可用的原文证据。判断 `groundedness` 时以此为准——"
        "答案里超出这些资料范围的具体论断即为编造：\n\n" + "\n\n".join(blocks),
        escape=False,  # 内层 evidence 已转义
    )


def _judge_system(
    persona: PersonaSpec,
    *,
    schema: dict[str, Any] = _SCHEMA,
    example: dict[str, Any] = _EXAMPLE,
    audit: bool = False,
) -> str:
    blocks = [
        f"你是一名严格的「{persona['domain']}」领域评审，负责给一个 AI 助手的回答打分。",
        "你的评分会被用来驱动这个助手的自我改进，因此**准确比友善重要得多**。"
        "发现问题就直说，不要因为答案看起来完整流畅就给高分。",
        section("dimensions", _DIMENSIONS),
        _quality_bar_block(persona),
        section("calibration", _CALIBRATION),
    ]
    if audit:
        blocks.append(section("retrieval_audit", _AUDIT_RULES))
    blocks.append(schema_block(schema, example))
    return join(*blocks)


def build_judge_messages(
    *,
    persona: PersonaSpec,
    question: str,
    answer: str,
    chunks: list[ChunkRef] | None = None,
) -> list[Message]:
    """给一次真实对话打分（进化闭环第 ③ 步）。"""
    user = join(
        section("question", question),
        _evidence_block(chunks),
        section("answer_to_judge", truncate(answer, MAX_ANSWER_CHARS)),
        "请打分。",
    )
    return [
        {"role": "system", "content": _judge_system(persona)},
        {"role": "user", "content": user},
    ]


def build_eval_judge_messages(
    *,
    persona: PersonaSpec,
    item: EvalItem,
    answer: str,
    evidence: list[ChunkRef] | None = None,
) -> list[Message]:
    """给测验集的一道题打分（进化闭环第 ⑥ 步，A/B 评测）。

    有 `reference` 时以参考答案为准绳；没有时退化为按 quality_bar 打分。
    `must_include` 是硬性要点，缺一个就要在 issues 里明确记一笔。

    传了 `evidence`（本次检索到的原文）时，额外要求一段检索审计：哪些要点真的被
    证据支撑、答案的论断有没有出处、哪几号证据起了作用。分数与审计在同一次调用里
    产出——多给一份证据只增加输入 token，不必为此再跑一次 judge。
    """
    audit = evidence is not None
    blocks = [section("question", item["question"])]

    if ref := item.get("reference"):
        blocks.append(
            section(
                "reference_answer",
                "标准答案。`correctness` 以它为准；"
                "但表述方式不必一致，只要实质内容对得上就算正确：\n\n" + ref,
            )
        )

    if must := item.get("must_include"):
        blocks.append(
            section(
                "must_include",
                "以下要点必须出现在答案中，**每缺一个都要在 issues 里记为 major**，"
                "并相应扣减 completeness：\n" + bulleted(must),
            )
        )

    if audit:
        blocks.append(_evidence_block(evidence))

    blocks.append(section("answer_to_judge", truncate(answer, MAX_ANSWER_CHARS)))
    blocks.append(
        "请打分。注意这是自动评测场景，分数会直接决定某条经验的存留，请务必严格对齐分数锚点。"
    )

    return [
        {
            "role": "system",
            "content": _judge_system(
                persona,
                schema={**_SCHEMA, **_AUDIT_SCHEMA} if audit else _SCHEMA,
                example={**_EXAMPLE, **_AUDIT_EXAMPLE} if audit else _EXAMPLE,
                audit=audit,
            ),
        },
        {"role": "user", "content": join(*blocks)},
    ]
