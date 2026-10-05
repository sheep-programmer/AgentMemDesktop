"""查询改写 —— 检索管线的第一步。

三种策略，按场景选用：

- `contextualize` 多轮对话中把指代补全（"它呢？"→"JAK1 抑制剂的选择性呢？"）。
  **这一步是刚需**：不补全指代，向量检索几乎必然召回错误内容。
- `expand`        一个问题扩成多个检索式，覆盖不同措辞与角度。适合宽泛问题。
- `hyde`          先让模型凭先验写一段"假想答案"，用答案去检索而非用问题。
  对于"问题措辞与文档措辞差异大"的场景提升明显，但会引入模型幻觉带来的偏移，
  默认关闭，由 space.yaml 的 `retrieval.hyde` 控制。

三者都是**纯检索辅助**，产物不会进入最终回答的上下文。
"""

from __future__ import annotations

from typing import Any

from ._base import join, schema_block, section
from ._shapes import Message, PersonaSpec, Turn

MAX_HISTORY_TURNS = 6


def _history_block(history: list[Turn]) -> str:
    if not history:
        return ""
    recent = history[-MAX_HISTORY_TURNS:]
    lines = [f"{'用户' if t['role'] == 'user' else '助手'}：{t['content'][:400]}" for t in recent]
    return section("recent_dialogue", "\n".join(lines))


_CONTEXTUALIZE_SCHEMA: dict[str, Any] = {
    "query": "补全后的独立问题",
    "changed": "true / false —— 原问题是否本来就是独立的",
}


def build_contextualize_messages(
    *,
    question: str,
    history: list[Turn],
) -> list[Message]:
    """把依赖上下文的追问改写成可独立检索的问题。

    若历史为空，调用方应直接跳过这一步，不必发起请求。
    """
    system = join(
        "你的任务是把多轮对话中的追问改写成一个**可以脱离上下文独立理解**的问题，用于向量检索。",
        section(
            "rules",
            "1. 补全代词与省略：把「它」「这个」「那种方案」替换成具体所指。\n"
            "2. **只做指代补全，不要扩写、不要回答、不要加入你的推测。**\n"
            "3. 原问题本身已经独立完整时，原样返回并把 `changed` 置为 false。\n"
            "4. 保持用户原本的措辞习惯和术语，不要替换成你认为更专业的说法——"
            "用户的用词往往与他自己投喂的文档一致，改了反而检索不到。",
        ),
        schema_block(
            _CONTEXTUALIZE_SCHEMA,
            {"query": "JAK1 抑制剂如何提高对 JAK2 的选择性", "changed": True},
        ),
    )
    user = join(_history_block(history), section("current_question", question), "请改写。")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_EXPAND_SCHEMA: dict[str, Any] = {
    "queries": ["检索式 1", "检索式 2", "检索式 3"],
}


def build_expand_messages(
    *,
    persona: PersonaSpec,
    question: str,
    n: int = 3,
) -> list[Message]:
    """一问扩多问，覆盖不同措辞与切入角度，召回后用 RRF 融合。"""
    system = join(
        f"你是「{persona['domain']}」领域的检索专家。"
        f"把用户的问题改写成 {n} 条不同的检索式，用于在知识库中并行召回。",
        section(
            "rules",
            f"1. 恰好产出 {n} 条，每条都是完整可独立检索的表述。\n"
            "2. 彼此要有**实质差异**：换同义词不算差异，要换切入角度。\n"
            "   例如原问题问「怎么提高成药性」，三条可以分别侧重：\n"
            "   改善代谢稳定性 / 降低毒性风险 / 提高口服吸收。\n"
            "3. 使用本领域的规范术语——文档里大概率用的是术语而非口语。\n"
            "4. 不要引入原问题中不存在的限定条件（版本号、厂商名等），那会缩小召回范围。",
        ),
        schema_block(
            _EXPAND_SCHEMA,
            {
                "queries": [
                    "如何评估一个先导化合物的代谢稳定性",
                    "常见的降低 hERG 心脏毒性的结构改造策略",
                    "如何提高口服药物的生物利用度",
                ]
            },
        ),
    )
    glossary = persona.get("glossary")
    blocks = [section("question", question)]
    if glossary:
        blocks.insert(
            0,
            section(
                "glossary",
                "领域术语表：\n" + "\n".join(f"- {k}：{v}" for k, v in glossary.items()),
            ),
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": join(*blocks, "请改写。")},
    ]


def build_hyde_messages(
    *,
    persona: PersonaSpec,
    question: str,
) -> list[Message]:
    """HyDE：生成一段假想答案用于向量检索。

    ⚠️ 产出内容**只用于检索**，绝不能出现在最终回答里——它是凭模型先验编的，
    未经知识库证实。调用方必须保证这一点。
    """
    system = join(
        f"你是「{persona['domain']}」领域的专家。针对用户的问题，写一段**假设性的**参考答案。",
        section(
            "rules",
            "1. 200 字以内，用本领域文档惯常的写法与术语，像是从一份专业资料里摘出来的段落。\n"
            "2. 直接写内容，不要有「可能」「据我所知」这类前缀，也不要解释你在做什么。\n"
            "3. 不确定的细节大胆写具体的——这段文字只用于向量匹配，"
            "写得越像真实文档，召回效果越好；它不会被展示给用户。\n"
            "4. 不要输出 JSON，直接输出这段文字。",
        ),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
