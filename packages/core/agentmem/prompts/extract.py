"""知识抽取 —— 从 L1 切片中提炼 L2 知识卡片与实体关系。

与蒸馏（distill）的分工要分清：

    extract  处理**资料**，产出**事实性知识** → L2 知识卡片
    distill  处理**反馈**，产出**行为规则**   → L3 经验

一份论文里写的"某激酶抑制剂的 IC50 是 12 nM"是知识卡片；
用户纠正你"下次要先问清厂商"是经验。两者互不越界。

抽取是逐批切片进行的（一次喂 3~6 个相邻切片，保留上下文又不至于超长），
因此同一个概念会在不同批次被重复抽出——**去重在 store 层按 title + kind 做**，
这段 Prompt 不负责全局去重，只负责在本批内不重复。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import join, schema_block, section, truncate
from ._shapes import ChunkRef, Message, PersonaSpec

MAX_CHUNK_CHARS = 3000


class ExtractSource(TypedDict):
    document_title: str
    chunks: list[ChunkRef]
    known_entities: NotRequired[list[str]]  # 该 Space 已有的实体名，用于对齐命名


_CARD_KINDS = """\
知识卡片必须归入以下五类之一：

- `concept`   概念定义。"什么是 X"。
- `fact`      具体事实。参数、版本、数值、命名、对应关系。
- `procedure` 操作流程。"怎么做 X"，有明确步骤。
- `pitfall`   坑与注意事项。"做 X 时会遇到 Y 问题"。
- `tool`      工具介绍。用途、用法、适用边界。
"""

_RULES = """\
抽取规则：

1. **只抽资料里明确写了的东西。** 不要用你自己的先验知识补充、扩展、纠正原文。
   资料说错了也照抽——纠错是用户和评测环节的事，不是你的事。

2. **一卡一事。** 一张卡片只讲一个概念/一个事实/一个流程。
   贪多会导致检索时命中一张卡却只有三分之一相关。

3. **卡片要能脱离原文独立看懂。** 不要出现"上文提到的方法""如前所述"这类指代——
   卡片会被单独检索出来注入上下文，那时原文并不在场。

4. **`title` 是检索的钩子**，要用领域内最常见的说法，不要用原文的章节标题。
   ✅ "JAK1 与 JAK2 选择性抑制的区别"
   ❌ "3.2.1 两种模式"

5. **`source_chunk_markers` 必填**，写清这张卡的内容来自哪几个切片。
   没有来源的卡片不可追溯，等于不可信。

6. **宁少勿滥。** 一批切片抽出 0~5 张卡是正常的。
   把每段话都变成一张卡，只会让知识库充满噪音。
   纯过渡段落、目录、版权页、无信息量的套话 —— 一张都不要抽。

实体与关系：

7. 实体是这个领域里**反复出现、值得建立连接**的具体对象（工具、协议、漏洞、厂商、算法…），
   不是随便一个名词。判断标准：它会不会在别的文档里再次出现？不会就别抽。

8. 实体命名优先复用 `known_entities` 里已有的写法，避免"JAK1"和"jak1"变成两个节点。

9. 关系的 `predicate` 用简短的中文动词短语（"用于"、"依赖"、"对抗"、"是…的一种"），
   不要写成句子。同一对实体只抽最重要的一条关系。\
"""

_SCHEMA: dict[str, Any] = {
    "cards": [
        {
            "kind": "concept | fact | procedure | pitfall | tool",
            "title": "检索友好的标题",
            "body": "Markdown 正文，能脱离原文独立看懂",
            "aliases": ["这个概念的其它叫法，没有就空数组"],
            "source_chunk_markers": ["c1", "c2"],
            "confidence": "0~1，原文表述越明确越高",
        }
    ],
    "entities": [
        {
            "name": "实体名",
            "type": "领域自定义类型，如 工具 / 协议 / 厂商 / 漏洞",
            "summary": "一句话说明",
            "source_chunk_markers": ["c1"],
        }
    ],
    "relations": [
        {
            "src": "源实体名（必须出现在 entities 中或 known_entities 中）",
            "dst": "目标实体名",
            "predicate": "简短动词短语",
            "source_chunk_markers": ["c1"],
        }
    ],
}

_EXAMPLE: dict[str, Any] = {
    "cards": [
        {
            "kind": "pitfall",
            "title": "hERG 抑制带来的心脏毒性风险常在先导优化早期被忽视",
            "body": (
                "许多先导化合物在体外活性优异，却因抑制 hERG 钾通道导致 QT 间期延长，"
                "在临床前毒理阶段被淘汰。\n\n"
                "应在优化早期即纳入 hERG 抑制测试，而非只盯着靶点活性。"
            ),
            "aliases": ["hERG 心脏毒性", "QT 间期延长风险"],
            "source_chunk_markers": ["c2", "c3"],
            "confidence": 0.9,
        }
    ],
    "entities": [
        {
            "name": "hERG",
            "type": "靶点",
            "summary": "心肌钾离子通道，被抑制时可致 QT 间期延长与心律失常",
            "source_chunk_markers": ["c2"],
        },
        {
            "name": "QT 间期延长",
            "type": "不良反应",
            "summary": "心电图上复极时间延长，是药物心脏毒性的重要预警",
            "source_chunk_markers": ["c2"],
        },
    ],
    "relations": [
        {
            "src": "hERG",
            "dst": "QT 间期延长",
            "predicate": "导致",
            "source_chunk_markers": ["c2"],
        }
    ],
}


def build_extract_messages(
    *,
    persona: PersonaSpec,
    source: ExtractSource,
) -> list[Message]:
    """构造抽取请求。

    建议温度 0.1~0.2：这一步要的是忠实转录，任何"创造性"都是污染。
    """
    system = join(
        f"你是一名「{persona['domain']}」领域的知识工程师，"
        "正在把资料提炼成结构化的知识卡片与实体关系图。",
        section("card_kinds", _CARD_KINDS),
        section("rules", _RULES),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    evidence = "\n\n".join(
        section(
            "chunk",
            truncate(c["content"].strip(), MAX_CHUNK_CHARS),
            marker=c["marker"],
            heading=c.get("heading_path"),
            page=c.get("page"),
        )
        for c in source["chunks"]
    )

    blocks = [
        section(
            "document",
            f"《{source['document_title']}》的若干相邻切片：\n\n" + evidence,
            escape=False,  # 内层 chunk 已转义
        )
    ]

    if known := source.get("known_entities"):
        blocks.append(
            section(
                "known_entities",
                "本知识库中已存在的实体名。命名请优先与它们保持一致：\n" + "、".join(known),
            )
        )

    blocks.append(
        f"请从以上 {len(source['chunks'])} 个切片中抽取知识。记住：宁少勿滥，"
        "没有值得沉淀的内容就返回空数组。"
    )

    if glossary := persona.get("glossary"):
        blocks.insert(
            1,
            section(
                "glossary",
                "本领域术语表，卡片中请使用这些规范用词：\n"
                + "\n".join(f"- {k}：{v}" for k, v in glossary.items()),
            ),
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": join(*blocks)},
    ]
