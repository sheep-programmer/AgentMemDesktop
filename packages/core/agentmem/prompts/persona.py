"""专家人格起草 —— 对应 `POST /spaces/{id}/persona/suggest`。

用户新建 Space 时只填了名字和一句领域描述，让他从零写出一份好的 Persona
（方法论、质量标准、术语表）是不现实的——那需要他先想清楚自己要什么。

这段 Prompt 先读一遍他已投喂的资料，起草一份**具体到能直接用**的人格，
再交给他修改。产出是草稿，不直接落库。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import join, schema_block, section, truncate
from ._shapes import Message

MAX_SAMPLE_CHARS = 1500


class PersonaDraftInput(TypedDict):
    name: str
    domain: str
    description: NotRequired[str | None]
    document_titles: NotRequired[list[str]]
    sample_chunks: NotRequired[list[str]]  # 少量代表性切片，让模型看清资料的实际风格


_SCHEMA: dict[str, Any] = {
    "role_description": "两三句话的角色设定，第二人称写给这个 AI 看",
    "principles": ["3~6 条方法论，恒定生效"],
    "output_style": {
        "language": "zh-CN",
        "tone": "语气风格的简短描述",
        "must_cite": "true / false",
    },
    "quality_bar": ["3~5 条判断答案好坏的具体标准"],
    "glossary": {"术语": "解释"},
    "suggested_types": ["这个领域的知识图谱里应该有哪些实体类型"],
}

_EXAMPLE: dict[str, Any] = {
    "role_description": (
        "你是一名有十年经验的药物化学家，擅长先导化合物优化、构效关系分析与成药性评估。"
        "你面对的是同行，不需要解释基础概念，但每个结论都要给得起实验证据。"
    ),
    "principles": [
        "构效关系（SAR）先行：先厘清活性随结构如何变化，再决定优化方向",
        "任何结论必须附带可引用的实验数据或文献，不接受「一般来说」式的断言",
        "评估化合物时活性与成药性并重，ADMET 任一红线优先于活性提升",
        "遇到无法确定的情况，明确说明还需要哪些数据才能判断，而不是给一个模糊的通用答案",
    ],
    "output_style": {"language": "zh-CN", "tone": "简洁、专业、不废话", "must_cite": True},
    "quality_bar": [
        "给出了具体的靶点/化合物/数值，而不是泛泛描述思路",
        "标注了数据的实验条件与适用的适应症范围",
        "对可能失败的路线说明了风险特征与替代方案",
        "区分了「确定的事实」与「需要验证的推测」",
    ],
    "glossary": {
        "先导化合物": "已显示目标活性、可作为进一步优化起点的化合物",
        "ADMET": "吸收、分布、代谢、排泄与毒性，评估成药性的五个维度",
    },
    "suggested_types": ["靶点", "化合物", "通路", "疾病", "临床试验"],
}

_RULES = """\
起草原则：

1. **具体压倒一切。** 这份人格会被逐字注入每一次对话的系统提示，
   写"要专业、要准确"等于什么都没写。每一条都要能让人看出"换个领域就不适用"。

2. **`principles` 是行为约束，不是形容词。**
   ✅ "任何结论必须附带可复现的验证步骤"
   ❌ "严谨专业"

3. **`quality_bar` 会被评分模型直接当作打分依据**，所以必须是可判定的。
   看着一份答案能明确说出"满足/不满足"的，才算合格的标准。

4. **`glossary` 只收那些"口语说法与专业说法不一致"的词**，
   收多了会挤占上下文。3~10 条为宜。人人都懂的词不要收。

5. 从用户提供的资料标题与样本里**读出这个领域的实际关注点与文风**，
   不要套用你对该领域的刻板印象。资料写得硬核，人格就该硬核。

6. 如果资料太少不足以判断，就基于领域常识起草一份保守的版本，
   并在 `role_description` 里保持克制——用户会在界面上修改它。\
"""


def build_persona_draft_messages(inp: PersonaDraftInput) -> list[Message]:
    """构造人格起草请求。"""
    system = join(
        "你正在为一个 AI 知识库配置「领域专家人格」。"
        "这份配置会被注入到该知识库每一次回答的系统提示中，"
        "决定它以什么身份、什么标准、什么风格回答问题。",
        section("rules", _RULES),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    blocks = [f"知识库名称：{inp['name']}\n领域：{inp['domain']}"]

    if desc := inp.get("description"):
        blocks.append(section("user_description", desc))

    if titles := inp.get("document_titles"):
        shown = titles[:40]
        more = f"\n…（共 {len(titles)} 份资料）" if len(titles) > 40 else ""
        blocks.append(
            section(
                "document_titles",
                "用户已投喂的资料：\n" + "\n".join(f"- {t}" for t in shown) + more,
            )
        )

    if samples := inp.get("sample_chunks"):
        blocks.append(
            section(
                "content_samples",
                "资料内容样本，用于判断这个领域的实际关注点与文风：\n\n"
                + "\n\n---\n\n".join(truncate(s, MAX_SAMPLE_CHARS) for s in samples[:5]),
            )
        )
    else:
        blocks.append("用户还没有投喂资料，请基于领域常识起草一份保守的初始版本。")

    blocks.append("请起草这份人格配置。")

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": join(*blocks)},
    ]
