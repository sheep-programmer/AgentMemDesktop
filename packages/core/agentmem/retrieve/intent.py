"""问题意图的一点点判别：这次到底该不该注入 L3 经验。

经验是「遇到什么场景该怎么做」的规则。用户说「把第三章的原文贴给我」时，这是一次
纯索取：没有场景可言，却照样会被塞进最多 6 条经验，还带着「适用时必须遵守」的措辞——
既占掉当轮 token，又可能把回答的措辞往经验描述的方向拽。

这里只做**保守**判别：命中明确索取原文的说法才跳过注入。判错的代价不对称——
漏判只是维持现状（多注入几条），误判会让本该遵守的规则缺席，所以宁可漏判。
"""

from __future__ import annotations

import re

#: 明确索取原文的说法。匹配到任意一条就认为这次不需要「场景—做法」那类经验。
_VERBATIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"原文"),
    re.compile(r"原句"),
    re.compile(r"原话"),
    re.compile(r"逐字"),
    re.compile(r"一字不差"),
    re.compile(r"照抄"),
    re.compile(
        r"(贴|粘|复制|引用|摘录|摘抄)(一下|下来|出|来)?(这|那|第)?[^。！？]{0,6}(段|条|句|章|节|部分|内容|文字|正文)"
    ),
    re.compile(
        r"(第\s*[一二三四五六七八九十百\d]+\s*[章节条]|[这那]一?[段落章节])"
        r"[^。！？]{0,8}(原文|内容|正文|怎么写的|写了什么|是什么)"
    ),
    re.compile(r"\bquote\b", re.IGNORECASE),
    re.compile(r"\bverbatim\b", re.IGNORECASE),
    re.compile(r"\bexcerpt\b", re.IGNORECASE),
)

#: 出现这些说法时，即使同时命中了上面的模式也不算纯索取——用户在问「怎么做」，
#: 只是顺带要一段原文
_PROCEDURAL_MARKERS: tuple[str, ...] = (
    "怎么",
    "如何",
    "为什么",
    "该注意",
    "应该",
    "建议",
    "步骤",
    "流程",
    "判断",
    "对比",
    "区别",
)


def looks_like_verbatim_request(question: str) -> bool:
    """这句话是不是在索取原文（而不是在问「遇到某场景该怎么做」）。

    Args:
        question: 用户原始问题（用改写前的原话判断：改写可能引入「场景」措辞）。
    """
    text = question.strip()
    if not text:
        return False
    if not any(pattern.search(text) for pattern in _VERBATIM_PATTERNS):
        return False
    # 用户一边要原文一边问做法时，经验仍然有用，不要跳
    return not any(marker in text for marker in _PROCEDURAL_MARKERS)


#: 指代词：出现这些说明问题离开上文读不懂
_ANAPHORA = re.compile(
    r"(它|他|她|它们|其|其中|该|此|本|这个|那个|这些|那些|这种|那种|上述|前面|刚才|之前|上一条|上面|那么|然后|接着|还有)"
)

#: 追问标记：「……呢？」本身就是「刚才那个话题的另一个方面」
_FOLLOWUP = re.compile(r"呢[？?]?\s*$")


def needs_contextualize(question: str) -> bool:
    """这个问题要不要用历史补全指代。

    改写是一次完整的模型调用（实测 4.7 秒），只在问题**依赖上文**时才值得花：
    「那它的半衰期呢？」离开上文读不懂，必须改写；「心脏毒性怎么评估？」本身就完整，
    改写只是把同样的意思再说一遍。

    判别只认两类明确信号——指代词与「呢」式追问。不用「句子短就改写」这种长度启发：
    实测「心脏毒性怎么评估？」（9 字）会被误判成需要改写，而它完全自足。
    """
    text = question.strip()
    if not text:
        return False
    return bool(_ANAPHORA.search(text) or _FOLLOWUP.search(text))
