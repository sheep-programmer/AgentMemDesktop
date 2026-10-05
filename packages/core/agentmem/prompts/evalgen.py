"""测验集自动生成 —— 专家度量化的基础设施。

没有 EvalSet，进化闭环就没有裁判：任何经验都无法被证明有用或有害，
系统只能越用越臃肿。因此这一步虽然不直接面向用户，却是整个设计的承重墙。

两个来源，互补：

- `build_from_document_messages` 从资料出题。覆盖面广，用于测「覆盖度 / 准确率」。
- `build_from_correction_messages` 从用户纠错出题。**价值最高**——
  用户纠正过的地方，正是这个 AI 的已知弱点。由它生成的题目能直接检验
  某条经验是否真的修好了那个弱点。

出题的核心约束：**题目必须能区分"学会了"和"没学会"**。
一道无论注入什么经验都答得对的题，在 A/B 评测里是纯噪音。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import bulleted, join, schema_block, section, truncate
from ._shapes import ChunkRef, Message, PersonaSpec

MAX_CHUNK_CHARS = 2500
MAX_ANSWER_CHARS = 2000


class CorrectionSample(TypedDict):
    """一次用户纠错，出题的原料。"""

    question: str
    wrong_answer: str
    correction: str
    insight_id: NotRequired[str | None]  # 由这次纠错产生的经验


_COMMON_RULES = """\
出题规则：

1. **每道题必须有客观的对错判据。** 开放式的"谈谈你对 X 的看法"无法评分，不要出。
2. **`must_include` 是硬性要点**，填 1~3 个必须出现在答案里的关键信息
   （具体的名称、数值、步骤、前提条件）。评分时缺一个扣一笔。
   宽泛的"要提到原理"不算要点，写具体的东西。
3. **`reference` 要完整但简短**，写清正确答案的实质内容即可，不要写成长文。
4. **题目要自包含**，不能出现"根据上文""这份文档中"——
   评测时资料是通过检索找回来的，不会原样摆在模型面前。
5. **难度分布**：约 1/3 简单（单点事实）、1/2 中等（需要综合两处信息）、
   1/6 困难（需要判断适用条件或识别陷阱）。困难题最能拉开差距。
6. **避开无法检索到的内容**：题目涉及的知识必须在知识库里确实存在，
   否则测的是模型的先验知识，不是这个知识库的水平。\
"""

_SCHEMA: dict[str, Any] = {
    "items": [
        {
            "question": "题目，自包含、有客观判据",
            "reference": "参考答案，简短完整",
            "must_include": ["必须出现的关键信息点"],
            "difficulty": "easy | medium | hard",
            "tags": ["主题标签，用于按维度统计"],
            "rationale": "这道题在考什么能力",
        }
    ]
}

_EXAMPLE: dict[str, Any] = {
    "items": [
        {
            "question": "评估某先导化合物成药性时，除了靶点活性还必须看哪些数据？为什么？",
            "reference": (
                "必须评估 ADMET 五项与靶点选择性。高活性但 hERG 抑制强、"
                "口服生物利用度低的化合物在临床前常被淘汰，故不能只看 IC50。"
            ),
            "must_include": ["ADMET", "选择性"],
            "difficulty": "medium",
            "tags": ["成药性", "ADMET"],
            "rationale": "考察是否理解成药性的多维评估，而非只记住一个活性指标",
        }
    ]
}


def build_from_document_messages(
    *,
    persona: PersonaSpec,
    document_title: str,
    chunks: list[ChunkRef],
    n: int = 5,
) -> list[Message]:
    """从一批资料切片出题。"""
    system = join(
        f"你是一名「{persona['domain']}」领域的命题专家，"
        "正在为一个 AI 知识库编写测验题，用于量化评估它的专业水平。",
        section("rules", _COMMON_RULES),
        join(
            "**宁少勿滥。** 如果这批资料内容稀薄（目录、前言、版权页、泛泛的概述），"
            f"出不满 {n} 道就少出几道，返回空数组也是正确的答案。",
            "凑数的题目会污染评测基准，让后续所有的 A/B 对比失去意义。",
        ),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    evidence = "\n\n".join(
        section(
            "chunk",
            truncate(c["content"].strip(), MAX_CHUNK_CHARS),
            marker=c["marker"],
            heading=c.get("heading_path"),
        )
        for c in chunks
    )

    user = join(
        section("source", f"《{document_title}》\n\n" + evidence, escape=False),
        f"请基于以上资料出最多 {n} 道题。",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_from_correction_messages(
    *,
    persona: PersonaSpec,
    samples: list[CorrectionSample],
) -> list[Message]:
    """从用户纠错出题 —— 针对性最强的一类题目。

    每个纠错样本出 1 道题，题目要**换个问法考同一个知识点**：
    原样重复用户的问题只能测出模型有没有背下那次对话，
    换一个角度问才能验证它是真的理解了。
    """
    system = join(
        f"你是一名「{persona['domain']}」领域的命题专家。",
        "下面是这个 AI 助手过去答错、并被用户纠正过的记录。"
        "请针对每一处错误出一道题，用来检验它是否真正改正了。",
        section("rules", _COMMON_RULES),
        section(
            "critical",
            "**最关键的一条：不要照抄用户原来的问题。**\n"
            "换一个场景、换一个问法，但考同一个知识点或同一种判断能力。\n"
            "照抄原问题只能测出它有没有记住那次对话；换个问法才能测出它是否真的学会了。\n\n"
            "举例：用户问的是「化合物 A 成药性如何」并纠正了你只看了活性，\n"
            "✅ 好题：「拿到一个高活性先导化合物，判断能否推进前必须先查什么？为什么？」\n"
            "❌ 差题：「化合物 A 成药性如何？」",
        ),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    blocks = []
    for i, s in enumerate(samples, 1):
        blocks.append(
            section(
                "correction_case",
                join(
                    section("original_question", s["question"]),
                    section("wrong_answer", truncate(s["wrong_answer"], MAX_ANSWER_CHARS)),
                    section("user_correction", truncate(s["correction"], MAX_ANSWER_CHARS)),
                ),
                escape=False,  # 内层子块已转义
                index=i,
                insight_id=s.get("insight_id"),
            )
        )

    user = join(
        "\n\n".join(blocks),
        f"共 {len(samples)} 处纠错，请每处出 1 道题。记住换个问法考同一个点。",
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_OUTLINE_SCHEMA: dict[str, Any] = {
    "domain_interpretation": "你把这个领域理解成了什么（一句话）",
    "outline": [
        {
            "topic": "一级主题",
            "subtopics": ["二级主题"],
            "importance": "core | common | advanced",
        }
    ],
}


def build_domain_outline_messages(
    *, persona: PersonaSpec, document_hints: list[str] | None = None
) -> list[Message]:
    """生成领域知识大纲 —— 专家度「覆盖度」维度与「知识盲区」功能的基准。

    只在 Space 创建时跑一次，之后由用户手动维护。
    注意：这一步**故意**用模型的先验知识而非知识库内容——
    我们要的正是一把外部尺子，用来量出知识库缺了什么。

    ``document_hints`` 是知识库里已有文档的标题与概要，只用来**辨认领域**：
    用户常把空间起成「长大助手」这类简称，单凭名字模型会乱猜或干脆拒答。
    """
    system = join(
        f"你是「{persona['domain']}」领域的资深从业者。请列出这个领域一名专家应当掌握的知识大纲。",
        section(
            "rules",
            "1. 一级主题 8~15 个，每个下面 3~8 个二级主题。\n"
            "2. 按 `importance` 标注：`core` 入门必备、`common` 日常常用、`advanced` 进阶深入。\n"
            "3. 覆盖要全面但不要无限细分——这是一把用来量「知识库缺了什么」的尺子，"
            "太细会让覆盖率永远接近 0，失去指示意义。\n"
            "4. 用本领域的规范术语命名，便于与知识库中的内容做匹配。\n"
            "5. 这一步不依赖任何资料，凭你对该领域的常识来列；没有资料不是拒绝的理由。\n"
            "6. 领域名可能是简称、昵称或产品名。结合领域描述与已有文档推断最合理的含义，"
            "把你的理解写进 `domain_interpretation`，然后照常列大纲。"
            "不要拒绝、不要反问、不要输出 JSON 以外的任何文字。",
        ),
        schema_block(_OUTLINE_SCHEMA),
    )

    blocks = [f"领域：{persona['domain']}"]
    if desc := persona.get("role_description"):
        # 角色描述是给问答写的，常带「只依据资料回答、资料不足就说不知道」——
        # 原样喂进来，模型会以「没有相关资料」为由拒绝生成大纲
        blocks.append(
            section(
                "context",
                "以下是该空间问答助手的角色设定，仅供理解领域；"
                "其中关于「依据资料作答」的要求针对问答，不适用于本任务：\n" + desc,
            )
        )
    if document_hints:
        blocks.append(
            section(
                "existing_documents",
                "知识库里已有的文档（仅用于辨认领域，大纲不要局限于这些内容）：\n"
                + bulleted(document_hints),
            )
        )
    if glossary := persona.get("glossary"):
        blocks.append(
            section("glossary", "用户已定义的术语，请纳入大纲：\n" + "、".join(glossary.keys()))
        )
    if principles := persona.get("principles"):
        blocks.append(
            section("principles", "用户强调的方法论，可据此判断侧重：\n" + bulleted(principles))
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": join(*blocks, "请生成大纲。")},
    ]
