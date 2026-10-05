"""经验整合 —— 进化闭环的第 ⑤ 步。

对刚蒸馏出的候选经验做三件事：**去重 / 合并 / 冲突检测**。

为什么必须有这一步：没有它，L3 会随使用量线性膨胀成一堆语义重复的噪音，
最终每次注入的 6 条经验全是同一件事的不同说法，挤掉了真正有用的条目。

冲突不由模型擅自裁决。模型只负责**发现并说清楚分歧**，
最终留哪条由用户在前端点一下决定——经验是用户领域知识的沉淀，
让模型替用户否定自己写下的规则是危险的。
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from ._base import join, schema_block, section
from ._shapes import Message, PersonaSpec


class InsightDraft(TypedDict):
    """待整合的经验（候选的或已有的）。`id` 对候选条目可以是临时编号。"""

    id: str
    trigger: str
    guidance: str
    kind: str
    confidence: NotRequired[float]
    status: NotRequired[str]  # 已有条目才有：candidate / active / ...


_SCHEMA: dict[str, Any] = {
    "duplicates": [
        {
            "keep_id": "保留哪一条的 id（优先保留表述更具体、置信度更高的）",
            "drop_ids": ["语义重复、应当丢弃的 id"],
            "reason": "判定为重复的理由",
        }
    ],
    "merges": [
        {
            "source_ids": ["两条以上互补、应当合并的 id"],
            "merged_trigger": "合并后的场景描述",
            "merged_guidance": "合并后的做法",
            "merged_kind": "合并后的类别",
            "reason": "为什么合并而不是分开保留",
        }
    ],
    "conflicts": [
        {
            "ids": ["互相矛盾的 id，两条或多条"],
            "nature": "分歧点是什么——用一句话说清它们在同一场景下给出了怎样对立的做法",
            "discriminator": "一个能区分谁对谁错的具体问题；用户看到这个问题就该知道选哪条",
            "possible_reconciliation": (
                "是否可能其实不矛盾、只是适用条件不同？如是，说明各自的适用条件；否则 null"
            ),
        }
    ],
    "keep_as_is": ["无需任何处理、可直接入库的 id"],
}

_EXAMPLE: dict[str, Any] = {
    "duplicates": [
        {
            "keep_id": "cand-2",
            "drop_ids": ["cand-5"],
            "reason": "两条都在说评估成药性要先看 ADMET，cand-2 还给出了具体五项，更具体",
        }
    ],
    "merges": [],
    "conflicts": [
        {
            "ids": ["ins-014", "cand-3"],
            "nature": ("同为「口服吸收差怎么改善」场景，ins-014 主张成盐，cand-3 主张前药策略"),
            "discriminator": "化合物是否有可电离基团？没有可成盐基团时成盐路线不适用",
            "possible_reconciliation": (
                "很可能不真冲突：有可电离基团时优先成盐，没有时才用前药。"
                "建议合并为带条件分支的单条经验"
            ),
        }
    ],
    "keep_as_is": ["cand-1", "cand-4"],
}

_RULES = """\
判定标准，请严格执行：

**重复 duplicates**
`trigger` 指向同一类场景，且 `guidance` 的实际动作相同 —— 只是措辞不同。
保留原则：表述更具体的 > 更笼统的；置信度高的 > 低的；已 active 的 > 还是 candidate 的。

**合并 merges**
场景相同但给出的是**互补的不同侧面**（比如一条讲前置检查、一条讲失败回退），
拆成两条会让模型只看到半边。合并成一条带完整流程的经验。
⚠️ 场景不同的经验**绝不能合并**——那会造出一条谁都不适用的四不像。

**冲突 conflicts**
`trigger` 有实质重叠，但 `guidance` 给出了**互斥**的动作。
注意区分「真冲突」和「适用条件不同」：后者远比前者常见。
凡是能用一个前置条件把两者区分开的，都在 `possible_reconciliation` 里写清楚——
这类情况对用户最有价值，往往一合并就得到一条比原来两条都强的经验。

**保留 keep_as_is**
与其它条目无关系的，直接放这里。大多数条目应该落在这一类，这是正常的。

每个 id 只能出现在一个分类中。不要遗漏任何一个输入 id。\
"""


def _render_drafts(tag: str, drafts: list[InsightDraft], note: str) -> str:
    if not drafts:
        return ""
    lines = []
    for d in drafts:
        meta = []
        if (c := d.get("confidence")) is not None:
            meta.append(f"置信度 {c:.2f}")
        if s := d.get("status"):
            meta.append(s)
        suffix = f"  ({', '.join(meta)})" if meta else ""
        lines.append(
            f"[{d['id']}] 类别 {d['kind']}{suffix}\n  场景：{d['trigger']}\n  做法：{d['guidance']}"
        )
    return section(tag, note + "\n\n" + "\n\n".join(lines))


def build_consolidate_messages(
    *,
    persona: PersonaSpec,
    candidates: list[InsightDraft],
    existing: list[InsightDraft] | None = None,
) -> list[Message]:
    """构造整合请求。

    `existing` 只需传入与候选条目**向量相近**的那部分已有经验（通常 Top 20~30），
    不要把整个 L3 塞进来——既浪费 token，也会让模型在无关条目上找出假冲突。
    """
    system = join(
        f"你是一名知识库管理员，负责维护一个「{persona['domain']}」领域 AI 助手的经验库。",
        "刚刚有一批新的候选经验被蒸馏出来。你的任务是在它们入库前做整合，"
        "找出其中的重复、可合并项，以及与既有经验的冲突。",
        section("rules", _RULES),
        join(
            "**你不负责裁决冲突。** 发现冲突时，只需把分歧说清楚、给出一个能区分对错的关键问题，",
            "最终保留哪条由用户决定。不要自作主张删除任何一条已 active 的经验。",
        ),
        schema_block(_SCHEMA, _EXAMPLE),
    )

    user = join(
        _render_drafts(
            "candidates",
            candidates,
            "本轮新蒸馏出的候选经验（id 以 cand- 开头）：",
        ),
        _render_drafts(
            "existing",
            existing or [],
            "知识库中语义相近的既有经验，用于检测重复与冲突：",
        ),
        "请完成整合分析。",
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
