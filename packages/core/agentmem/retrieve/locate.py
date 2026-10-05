"""把一条引用从「整条切片」收窄到「回答依据的那一句」。

切片按 token 预算打包，一条切片常常跨好几个小节：实测一份 700 字的设备手册
只切出 2 条正文切片，第一条从「产品概述」一直覆盖到「3.1 E07 过温保护」，
``heading_path`` 却只能写切片**开头**所在的章节（文档标题）。用户点开引用看到
「出自：星澜 X3 技术手册」，等于没说在哪。

收窄靠两样东西：

1. **引用它的那句回答**。标记跟在论断后面（``……自动恢复。[^c1]``），往前取到
   上一个句末标点，就是这条引用要支撑的论断；
2. **切片里与论断最像的句子**。用字二元组 + 数字/英文词元算重合度——数字和代号
   （58℃、63A、E07）是最强的定位信号，必须单独成词元，不能被拆成字。

再用文档解析缓存里的标题表与页码表，按句子的**绝对位置**查出它真正所在的章节
和页码。任何一步拿不准（重合度太低、切片正文与原文区间对不上、没有解析缓存）
都退回整条切片的定位，宁可粗也不能指错。
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import structlog

from agentmem.ingest.parse import ParseResult

from .models import ScoredChunk, snippet_of

logger = structlog.get_logger(__name__)

#: 句末标点：中文句号/问叹号/分号，英文句号等后面跟空白，以及换行
_SENTENCE_END = re.compile(r"[。！？；!?;]+|(?<=[.])\s+|\n+")
#: 标记前面可以跳过的字符：句末标点、空白，以及收尾的强调/行内代码/右括号引号
_TRAILING = frozenset("。！？；!?;.，,、 \n\t*_`~)）」』”’】")
#: 论断往前最多回看的字数：再长就不是「一句话」了
CLAIM_LOOKBACK = 160
#: 数字、英文单词、型号代号整体成词元（E07、63A、15.36、kWh）
_WORD = re.compile(r"[A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*")
_CJK = re.compile(r"[㐀-鿿]")
_HEADING_LINE = re.compile(r"^#{1,6}\s")
#: 小节标题对其下句子的加分权重：标题能说明「在讲哪件事」，但不如原句本身可靠
HEADING_WEIGHT = 0.5
#: Markdown 标题行、表格分隔行之类不算可引用的句子
_NOT_A_SENTENCE = re.compile(r"^\s*(#{1,6}\s|\|?\s*:?-{3,})")
#: 论断与句子至少要有这么多分的重合，才敢说「依据就是这一句」
MIN_SCORE = 0.18
#: 命中句离切片开头不到这么多字时，片段照旧从开头截：省掉一个没意义的省略号
FOCUS_LEAD_CHARS = 24
#: 相邻句也够像（不低于最佳的这个比例）就一起框进来：论断常常跨两句原文
NEIGHBOR_RATIO = 0.7


@dataclass(frozen=True)
class Located:
    """收窄后的定位：绝对字符区间、原句、所在章节与页码。"""

    start: int
    end: int
    quote: str
    heading_path: str | None
    page: int | None


def claim_before(answer: str, offset: int) -> str:
    """回答里标记位置之前的那句论断。

    标记常紧跟句号（``恢复。[^c1]``），也可能在句号前（``恢复[^c1]。``）。
    先跳过紧挨着的标点与空白，再往前找上一个句末。
    """
    end = min(max(offset, 0), len(answer))
    # 标记前常挂着句末标点和 Markdown 记号：``……4 欧姆。**[^c2]``（加粗收尾）
    while end > 0 and answer[end - 1] in _TRAILING:
        end -= 1
    start = max(0, end - CLAIM_LOOKBACK)
    window = answer[start:end]
    boundaries = list(_SENTENCE_END.finditer(window))
    if boundaries:
        window = window[boundaries[-1].end() :]
    return window.strip(" *_-#>\n")


def _tokens(text: str) -> set[str]:
    """字二元组 + 整词元。单个汉字太常见，区分不了句子，所以用二元组。"""
    lowered = text.lower()
    tokens = {match.group(0) for match in _WORD.finditer(lowered)}
    han = "".join(_CJK.findall(lowered))
    tokens.update(han[index : index + 2] for index in range(len(han) - 1))
    return tokens


def _score(claim: set[str], sentence: set[str]) -> float:
    if not claim or not sentence:
        return 0.0
    overlap = claim & sentence
    # 数字/代号命中加权：它们几乎唯一地指向某一句
    weight = sum(2.0 if _WORD.fullmatch(token) else 1.0 for token in overlap)
    return weight / math.sqrt(len(claim) * len(sentence))


def _sentences(content: str) -> list[tuple[int, int, str]]:
    """切片里每个可引用句子的 [start, end)（相对切片）及它上方最近的小节标题。

    标题行本身不算句子（引用一个标题没有意义），但它的字面要算进下面每一句的
    分数里：回答常用小节名来指代内容（「E07 是过温保护故障码」），而小节下的
    原句里只写了阈值和动作，单靠句子本身对不上。
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(content):
        end = match.end() if match.group(0).strip() else match.start()
        spans.append((cursor, end))
        cursor = match.end()
    if cursor < len(content):
        spans.append((cursor, len(content)))
    result: list[tuple[int, int, str]] = []
    heading = ""
    for start, end in spans:
        segment = content[start:end]
        stripped = segment.strip()
        if _HEADING_LINE.match(stripped):
            heading = stripped.lstrip("#").strip()
            continue
        if len(stripped) < 4 or _NOT_A_SENTENCE.match(stripped):
            continue
        lead = len(segment) - len(segment.lstrip())
        result.append((start + lead, start + lead + len(stripped), heading))
    return result


def best_span(claim: str, content: str) -> tuple[int, int] | None:
    """切片里与论断最匹配的句子区间（相对切片）；拿不准返回 ``None``。"""
    claim_tokens = _tokens(claim)
    if len(claim_tokens) < 2:
        return None
    sentences = _sentences(content)
    if len(sentences) < 2:
        # 切片只有一句时它本身就是最精确的定位，不必再收窄
        return None
    scores = [
        _score(claim_tokens, _tokens(content[start:end]))
        + HEADING_WEIGHT * _score(claim_tokens, _tokens(heading))
        for start, end, heading in sentences
    ]
    best = max(range(len(scores)), key=scores.__getitem__)
    if scores[best] < MIN_SCORE:
        return None
    first = last = best
    if best > 0 and scores[best - 1] >= scores[best] * NEIGHBOR_RATIO:
        first = best - 1
    elif best + 1 < len(scores) and scores[best + 1] >= scores[best] * NEIGHBOR_RATIO:
        last = best + 1
    return sentences[first][0], sentences[last][1]


class DocumentOutline:
    """一篇文档的标题表与页码表，按绝对字符位置查章节与页码。"""

    def __init__(self, parsed: ParseResult) -> None:
        self._parsed = parsed
        self._heading_starts = [heading.char_start for heading in parsed.headings]

    def heading_at(self, offset: int) -> str | None:
        index = bisect.bisect_right(self._heading_starts, offset) - 1
        return self._parsed.headings[index].heading_path if index >= 0 else None

    def page_at(self, offset: int) -> int | None:
        return self._parsed.page_at(offset)


@lru_cache(maxsize=64)
def _outline_cached(path: str, mtime_ns: int) -> DocumentOutline | None:
    del mtime_ns  # 只参与缓存键：解析缓存重写后自然失效
    try:
        parsed = ParseResult.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.info("citation_outline_unavailable", path=path, error=str(exc)[:120])
        return None
    return DocumentOutline(parsed)


def load_outline(cache_dir: Path, document_id: str) -> DocumentOutline | None:
    """读文档解析缓存里的大纲；缓存不存在时返回 ``None``（退回切片级定位）。"""
    path = cache_dir / f"{document_id}.parse.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return _outline_cached(str(path), mtime)


def locate(
    claim: str,
    content: str,
    char_start: int | None,
    char_end: int | None,
    outline: DocumentOutline | None,
) -> Located | None:
    """把论断落到切片里的具体句子上。

    切片正文必须与原文区间逐字等长，否则相对位置换算不回原文（相邻切片合并、
    经济模式摘录之后可能出现这种情况），此时不收窄。
    """
    if char_start is None or char_end is None or char_end - char_start != len(content):
        return None
    span = best_span(claim, content)
    if span is None:
        return None
    start, end = char_start + span[0], char_start + span[1]
    return Located(
        start=start,
        end=end,
        quote=content[span[0] : span[1]],
        heading_path=outline.heading_at(start) if outline else None,
        page=outline.page_at(start) if outline else None,
    )


def focus_hits(query: str, chunks: list[ScoredChunk], cache_dir: Path) -> dict[str, Located]:
    """检索命中按问题收窄：每条命中里与问题最贴近的句子及其章节。

    检索面板与证据栏原来展示的是切片开头的章节和开头的一段文字——问「E12 怎么处理」，
    命中却显示「产品概述」那一段，用户得自己在几百字里找。同步读解析缓存，
    调用方放进线程里跑。
    """
    outlines: dict[str, DocumentOutline | None] = {}
    focused: dict[str, Located] = {}
    for chunk in chunks:
        if chunk.kind != "body":
            continue
        if chunk.document_id not in outlines:
            outlines[chunk.document_id] = load_outline(cache_dir, chunk.document_id)
        located = locate(
            query, chunk.content, chunk.char_start, chunk.char_end, outlines[chunk.document_id]
        )
        if located is not None:
            focused[chunk.chunk_id] = located
    return focused


def focused_snippet(chunk: ScoredChunk, located: Located | None, limit: int) -> str:
    """从命中句开始截片段；命中句本来就在开头附近时与整条切片的片段一致。"""
    if located is None or chunk.char_start is None:
        return snippet_of(chunk.content, limit)
    local = located.start - chunk.char_start
    if local < FOCUS_LEAD_CHARS:
        return snippet_of(chunk.content, limit)
    return "…" + snippet_of(chunk.content[local:], limit)
