"""摄取期的文档级上下文预置（Contextual Retrieval）。

问题：一段话里的化合物名、标准号、条款号往往只在前一节出现过。这一段被单独
检索时，向量不带那个名字、全文索引里也没有它，于是「XX 化合物的心脏毒性」这类
问法命不中它——检索失败不是因为它不相关，而是因为它离主语太远。

做法：解析完成后给整篇文档生成一段简短的上下文（是什么文档、涉及哪些关键实体与
术语、怎么组织），把这段文字**拼在被索引与嵌入的文本前面**。切片正文一个字符都不动，
`char_start` / `char_end` 仍旧指向原文，引用高亮不受影响。

粒度说明：这是**文档级**上下文，一篇一次调用；Anthropic 的做法是**逐切片**再写一段
上下文（每片一次调用，配合 prompt caching 摊薄成本）。逐切片更准，但本地模型上
成本与延迟都不可忽视，所以先做文档级，并把它做成可关闭的开关。
"""

from __future__ import annotations

from ._base import join, section, truncate

#: 送进摘要 Prompt 的正文上限。摘要要的是「这篇文档讲什么」，不需要读完全文。
MAX_EXCERPT_CHARS = 6000

#: 摘要长度上限（字符），约 100 token 量级——它是给索引用的前缀，
#: 太长会把每个切片的索引文本都撑大，检索时反而稀释关键词权重。
MAX_SUMMARY_CHARS = 400

_SYSTEM = """\
你在为一个本地知识库做「文档级上下文预置」。用户提问时命中的是文档里的**一小段文字**，
那段文字常常缺少主语：化合物名、标准号、产品型号只在前面的章节出现过，单看那一段
根本不知道在说谁。你写的这段上下文会被拼在**每一个切片的索引文本前面**，
作用就是补上那个缺失的主语。

写作要求：

1. **只写材料里明文写着的内容。**不得引入材料之外的任何事实——哪怕是你确信无误的
   常识：地点、机构、人物、前身与沿革、年份、数值，材料没写就不写。
   这段文字会作为一条「知识库证据」被检索和引用，材料外的内容一旦写进来，
   就会被当成知识库的结论引述，而点开原文却找不到。
2. 不要推测、不要评价、不要写「本文档旨在……」这类套话。
3. 必须点名材料里出现的**关键实体与术语**：化合物 / 靶点 / 标准号 / 产品型号 / 章节主题。
   这些词是检索的关键词来源，缺了它们这段上下文就没有价值。
4. 最多 3~5 句话、350 字以内；**材料本身很短时，写一两句即可**，宁可短，
   不要为了凑篇幅补充材料之外的内容。不要分点、不要标题、不要 Markdown。
5. 直接输出这段文字本身，不要任何前后缀。\
"""


def build_context_messages(
    *,
    title: str,
    headings: list[str],
    excerpt: str,
) -> list[dict[str, str]]:
    """构造文档上下文摘要的 Prompt。

    Args:
        title: 文档标题。
        headings: 文档的标题路径列表，用来交代结构。
        excerpt: 正文摘录（会被截断）。
    """
    parts = [section("document_title", title)]
    if headings:
        parts.append(
            section(
                "outline",
                "文档的标题结构如下，它同时也是「关键主题」的候选来源：\n"
                + "\n".join(f"- {item}" for item in headings[:40]),
            )
        )
    parts.append(
        section(
            "excerpt",
            "正文摘录（可能被截断）：\n\n" + truncate(excerpt.strip(), MAX_EXCERPT_CHARS),
        )
    )
    parts.append("请写出这段文档级上下文。")
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": join(*parts)},
    ]


def clean_summary(raw: str) -> str | None:
    """把模型输出收拾成一段可用的上下文。

    去掉代码块围栏与常见的自述式前后缀；空结果返回 ``None``——宁可不加前缀，
    也不要往每个切片的索引文本里塞一段空话。
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    for prefix in ("文档级上下文：", "上下文：", "摘要："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    text = " ".join(text.split())
    if not text:
        return None
    return text[:MAX_SUMMARY_CHARS]
