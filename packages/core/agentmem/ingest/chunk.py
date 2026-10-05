"""标题感知切分。

默认 512 token / 80 overlap。每个切片带 ``heading_path``（如 "第3章 > 3.2 脱壳"）、
``page`` 与 ``char_start`` / ``char_end``，便于前端下钻高亮。

**切片与原文的对应关系是本模块的核心契约**，由 ``tests/test_chunk_invariants.py``
逐条断言：

1. 偏移能切回原文。切片正文是**原文的精确片段**，偏移就是它在原文里的位置，
   前端点一条引用，高亮画在正确的地方。
2. 切片大小可控。正文预算先扣掉重叠，因此最终切片（重叠 + 正文）落在目标内；
   只有单个原子单位（一句里没有任何可切位置的超长文本、或表头加一行）真的装不下时
   才允许越界。
3. 切开的表格每一段都带表头，续段的数字才有列名可读。

实现上全程只搬运**区间** ``[start, end)``，正文一律由 ``markdown[start:end]`` 切出，
不做「按句拼串、再拿串长反推偏移」——那条路会同时丢掉句后空格（``in vitro.Selectivity``）、
让偏移逐段漂移，而且拼出来的串再也断不出句，重叠裁剪随之失效。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import structlog
from pydantic import Field

from agentmem.ingest.parse import HeadingSpan, ParseResult
from agentmem.prompts.budget import split_sentences
from agentmem.types import ContentModel

logger = structlog.get_logger(__name__)

#: 字符位置 → 页码
PageLocator = Callable[[int], int | None]

DEFAULT_TARGET_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 80

#: 围栏代码块的起始行。代码块内部不切段：空行与竖线在那里都只是代码。
_FENCE = re.compile(r"^\s*(```|~~~)")
#: 表格行的判据：去掉缩进后以竖线开头。
_TABLE_ROW = re.compile(r"^\s*\|")
#: 表头分隔行只由这些字符组成。
_DELIMITER_CHARS = frozenset("|-: \t")
#: 表头超过单段预算的几分之一就不再重复。表头本身太大时复制到每一段会把每段都拖爆，
#: 那比没有列名更糟——预算被表头吃掉，落到模型眼里的数据行反而更少。
_HEADER_REPEAT_RATIO = 2
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@lru_cache(maxsize=4)
def _encoder(name: str = "cl100k_base") -> Any | None:
    try:  # pragma: no cover - 取决于是否安装 tiktoken
        import tiktoken

        return tiktoken.get_encoding(name)
    except Exception as exc:
        logger.warning("tiktoken_unavailable", error=str(exc))
        return None


def count_tokens(text: str) -> int:
    """统计 token 数。"""
    if not text:
        return 0
    encoder = _encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:
            pass
    return estimate_tokens(text)


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 一字一 token，其余按 4 字符一 token。"""
    cjk = len(_CJK.findall(text))
    rest = max(0, len(text) - cjk)
    return cjk + (rest + 3) // 4


def heading_path_at(headings: list[HeadingSpan], char_offset: int) -> str | None:
    """给定字符位置所处的标题路径。"""
    current: str | None = None
    for heading in headings:
        if heading.char_start <= char_offset:
            current = heading.heading_path
        else:
            break
    return current


class ChunkDraft(ContentModel):
    """切分产物（尚未落库）。

    ``content`` 通常是 ``markdown[char_start:char_end]``。唯一的例外是**表格续段**：
    它的正文是「重复的表头 + ``markdown[char_start:char_end]``」，好让离开表头几百行的
    数据行仍然带着列名。合成前缀的长度可由 ``len(content) - (char_end - char_start)``
    反推，不需要额外字段；偏移始终指向该段自己的那些行，引用高亮不受影响。

    继承 ``ContentModel`` 而不是 ``AgentMemModel``：后者会把字符串首尾空白剪掉，
    而重叠段的前导空格正是原文的一部分，剪掉之后 ``content`` 就不再等于
    ``markdown[char_start:char_end]``，偏移当场失效。
    """

    ordinal: int = Field(ge=0, description="文档内序号")
    content: str
    heading_path: str | None = None
    page: int | None = None
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 区间工具：本模块内部一律用 [start, end) 表示正文，不做字符串重建
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Segment:
    """一段连续正文在原文里的区间。

    ``header_end`` 只对表格段有值，指向「表头行 + 分隔行」的结尾。
    """

    start: int
    end: int
    header_end: int | None = None


@dataclass(frozen=True)
class _Piece:
    """一个不可再分的片段。

    ``prefix`` 是必须跟着它走的合成前缀（重复的表头），只能出现在切片开头。
    """

    start: int
    end: int
    prefix: str = ""


@dataclass(frozen=True)
class _ChunkSpan:
    """一条切片的区间与合成前缀。"""

    start: int
    end: int
    prefix: str = ""


def _trim_span(markdown: str, start: int, end: int) -> tuple[int, int]:
    """把区间收缩到去掉首尾空白之后的正文上。

    收缩后的区间依然精确对应原文，因此偏移不需要任何补偿。
    """
    body = markdown[start:end]
    lead = len(body) - len(body.lstrip())
    trail = len(body) - len(body.rstrip())
    return start + lead, end - trail


def _line_spans(markdown: str, start: int, end: int) -> list[tuple[int, int]]:
    """把 ``[start, end)`` 按行切开，返回每行的区间（不含行尾换行）。"""
    spans: list[tuple[int, int]] = []
    cursor = start
    for line in markdown[start:end].splitlines(keepends=True):
        line_end = cursor + len(line)
        body_end = line_end
        while body_end > cursor and markdown[body_end - 1] in "\r\n":
            body_end -= 1
        if body_end > cursor:
            spans.append((cursor, body_end))
        cursor = line_end
    return spans


def _is_table_delimiter(line: str) -> bool:
    """``| --- | :---: |`` 这类表头分隔行。"""
    stripped = line.strip()
    if "|" not in stripped or "-" not in stripped:
        return False
    return all(char in _DELIMITER_CHARS for char in stripped)


def _segments(markdown: str) -> list[_Segment]:
    """切成段落段与表格段，代码块内部不切。

    段落按空行断开；连续以竖线开头的行单独成一段表格——只有把它认出来，
    表头才能被复制到每一段数据行前面。
    """
    segments: list[_Segment] = []
    text_start: int | None = None
    text_end = 0
    table_lines: list[tuple[int, int]] = []
    in_fence = False
    offset = 0

    def flush_text() -> None:
        nonlocal text_start
        if text_start is None:
            return
        start, end = _trim_span(markdown, text_start, text_end)
        if start < end:
            segments.append(_Segment(start, end))
        text_start = None

    def flush_table() -> None:
        nonlocal table_lines
        if not table_lines:
            return
        header_end = table_lines[1][1] if len(table_lines) >= 2 else None
        if header_end is not None:
            second = markdown[table_lines[1][0] : table_lines[1][1]]
            if not _is_table_delimiter(second):
                header_end = None
        start, end = _trim_span(markdown, table_lines[0][0], table_lines[-1][1])
        if start < end:
            usable = header_end if header_end is not None and header_end <= end else None
            segments.append(_Segment(start, end, usable))
        table_lines = []

    for line in markdown.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        body = line.rstrip("\r\n")
        if _FENCE.match(body):
            in_fence = not in_fence
        if in_fence:
            flush_table()
            if text_start is None:
                text_start = line_start
            text_end = offset
        elif not body.strip():
            # 空行是段落分隔，本身不属于任何一段
            flush_table()
            flush_text()
        elif _TABLE_ROW.match(body):
            flush_text()
            table_lines.append((line_start, line_start + len(body)))
        else:
            flush_table()
            if text_start is None:
                text_start = line_start
            text_end = offset
    flush_table()
    flush_text()
    return segments


def _sentence_spans(markdown: str, start: int, end: int) -> list[tuple[int, int]]:
    """``[start, end)`` 内每一句的区间。

    断句直接用 ``prompts.budget.split_sentences``，不复用之外再写一套：证据预算裁剪
    （``prompts/budget.py``）与相邻切片裁剪（``retrieve/overlap.py``）都按句边界动手，
    切分器若用另一套规则，它们就认不出这里切出来的边界，重叠裁剪会整体失效。

    该函数保证 ``"".join(split_sentences(t)) == t``，所以累加各句长度得到的就是真实下标。
    """
    spans: list[tuple[int, int]] = []
    cursor = start
    for sentence in split_sentences(markdown[start:end]):
        if sentence:
            spans.append((cursor, cursor + len(sentence)))
        cursor += len(sentence)
    return spans


def _bisect_fit(markdown: str, start: int, end: int, budget: int) -> int:
    """在 ``[start, end)`` 里找出装得进预算的最长前缀的终点。

    单个字符就超预算（预算被设得极小）时返回 ``start + 1``：宁可越界一个字符，
    也不能让切分原地打转。
    """
    lo, hi = start + 1, end
    if count_tokens(markdown[start:lo]) > budget:
        return lo
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if count_tokens(markdown[start:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return lo


def _force_split(markdown: str, start: int, end: int, budget: int) -> list[tuple[int, int]]:
    """把装不进预算的原子区间按字符硬切开。

    只有整段找不到任何句边界时才会走到这里（例如被压成一行的长文本）。
    切点优先落在换行或空白上：切在词中间会把一个数值或术语劈成两半。
    """
    pieces: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        if count_tokens(markdown[cursor:end]) <= budget:
            pieces.append((cursor, end))
            break
        cut = _bisect_fit(markdown, cursor, end, budget)
        window = max(cursor + 1, cut - (cut - cursor) // 5)
        newline = markdown.rfind("\n", window, cut)
        if newline >= window:
            cut = newline + 1
        else:
            space = max(markdown.rfind(" ", window, cut), markdown.rfind("\t", window, cut))
            if space >= window:
                cut = space + 1
        if cut <= cursor:  # pragma: no cover - _bisect_fit 至少前进一个字符
            cut = cursor + 1
        pieces.append((cursor, cut))
        cursor = cut
    return pieces


def _pack_units(
    units: Sequence[tuple[int, int]], markdown: str, budget: int
) -> list[tuple[int, int]]:
    """把相邻区间贪心合并成不超过预算的片段。"""
    if not units:
        return []
    packed: list[tuple[int, int]] = []
    start, end = units[0]
    for unit_start, unit_end in units[1:]:
        if count_tokens(markdown[start:unit_end]) > budget:
            packed.append((start, end))
            start, end = unit_start, unit_end
        else:
            end = unit_end
    packed.append((start, end))
    return packed


def _text_pieces(markdown: str, start: int, end: int, budget: int) -> list[_Piece]:
    """把一段普通正文按句子边界打包成不超过预算的片段。"""
    units: list[tuple[int, int]] = []
    for unit_start, unit_end in _sentence_spans(markdown, start, end):
        if count_tokens(markdown[unit_start:unit_end]) > budget:
            units.extend(_force_split(markdown, unit_start, unit_end, budget))
        else:
            units.append((unit_start, unit_end))
    return [
        _Piece(unit_start, unit_end)
        for unit_start, unit_end in _pack_units(units, markdown, budget)
    ]


def _table_pieces(markdown: str, segment: _Segment, budget: int) -> list[_Piece]:
    """按行切表格，并把表头复制到每一段。

    表头只出现在整张表的最前面，第 3 段的数据行离它有几百行远——一个连续区间不可能
    同时包含两者。所以续段带一个合成前缀（表头行 + 分隔行），正文仍然是原文的精确片段。
    表头进了正文，也就进了 embedding 与全文索引，续段才可能被检索到；不这么做，
    只有第一段能被「IC50」这类列名命中，后面的行等于不存在。

    正常表头下 ``_HEADER_REPEAT_RATIO`` 那条闸门不会触发；表头本身就占掉大半预算时
    放弃复制，宁可少一点可读性也不让每一段都越界。
    """
    header_end = segment.header_end
    if header_end is None:  # pragma: no cover - 调用方只在表格段上传入
        return [_Piece(segment.start, segment.end)]
    header = markdown[segment.start : header_end]
    rows = _line_spans(markdown, header_end, segment.end)
    if not rows:
        return [_Piece(segment.start, segment.end)]
    separator = markdown[header_end : rows[0][0]]
    repeated = f"{header}{separator}"
    overhead = count_tokens(repeated)
    has_header_prefix = overhead * _HEADER_REPEAT_RATIO <= budget
    row_budget = max(1, budget - (overhead if has_header_prefix else 0))

    units: list[tuple[int, int]] = []
    for row_start, row_end in rows:
        if count_tokens(markdown[row_start:row_end]) > row_budget:
            units.extend(_force_split(markdown, row_start, row_end, row_budget))
        else:
            units.append((row_start, row_end))

    pieces: list[_Piece] = []
    # 第一段的正文从表头开始：表头本来就是原文的一部分，用不着合成
    start, end = segment.start, units[0][1]
    prefix = ""
    for unit_start, unit_end in units[1:]:
        if count_tokens(f"{prefix}{markdown[start:unit_end]}") > budget:
            pieces.append(_Piece(start, end, prefix))
            start, end = unit_start, unit_end
            prefix = repeated if has_header_prefix else ""
        else:
            end = unit_end
    pieces.append(_Piece(start, end, prefix))
    return pieces


def _tail_candidates(markdown: str, chunk: _ChunkSpan, overlap_tokens: int) -> list[int]:
    """上一条切片尾部可作为重叠起点的句首位置，升序。

    越靠前的候选重叠越长。只取句首：切在句子中间的话，相邻切片裁剪按句边界比对，
    认不出这段重复，重叠就会白占预算。至少给一个候选，哪怕它比 ``overlap_tokens`` 大——
    一句长句做重叠总好过完全没有上下文。
    """
    if overlap_tokens <= 0 or chunk.end <= chunk.start:
        return []
    candidates: list[int] = []
    total = 0
    for unit_start, unit_end in reversed(_sentence_spans(markdown, chunk.start, chunk.end)):
        tokens = count_tokens(markdown[unit_start:unit_end])
        if candidates and total + tokens > overlap_tokens:
            break
        candidates.append(unit_start)
        total += tokens
        if total >= overlap_tokens:
            break
    candidates.reverse()
    return candidates


def _fit_overlap(
    markdown: str, previous: _ChunkSpan, piece: _Piece, target_tokens: int, overlap_tokens: int
) -> int | None:
    """给这一条挑一个装得进目标的重叠起点，装不下就不重叠。"""
    if previous.end > piece.start:
        return None
    for candidate in _tail_candidates(markdown, previous, overlap_tokens):
        if count_tokens(markdown[candidate : piece.end]) <= target_tokens:
            return candidate
    return None


def _pack(
    markdown: str, pieces: Sequence[_Piece], target_tokens: int, overlap_tokens: int, budget: int
) -> list[_ChunkSpan]:
    """把片段合并成切片：贪心装满预算，并在句边界上接上一条的重叠。

    重叠会**向前扩**这条切片的起点——它落在上一条的区间里，两条之间的原文因此仍然
    是一段连续区间，偏移照旧精确。
    """
    chunks: list[_ChunkSpan] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        start = piece.start
        # 合成表头必须落在切片开头，所以带前缀的片段不与上一条合并
        ceiling = budget if chunks else target_tokens
        if chunks and not piece.prefix:
            fitted = _fit_overlap(markdown, chunks[-1], piece, target_tokens, overlap_tokens)
            if fitted is not None:
                start = fitted
        end = piece.end
        index += 1
        while index < len(pieces) and not pieces[index].prefix:
            if count_tokens(f"{piece.prefix}{markdown[start : pieces[index].end]}") > ceiling:
                break
            end = pieces[index].end
            index += 1
        chunks.append(_ChunkSpan(start, end, piece.prefix))
    return chunks


def split_markdown(
    markdown: str,
    *,
    headings: list[HeadingSpan] | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    page_at: PageLocator | None = None,
) -> list[ChunkDraft]:
    """把 Markdown 切成标题感知的切片。

    Args:
        markdown: 已清洗的 Markdown 正文。
        headings: 标题位置信息，用于生成 ``heading_path``。
        target_tokens: 单切片目标 token 数。
        overlap_tokens: 相邻切片的重叠 token 数。
        page_at: 可选的位置 → 页码函数，签名 ``(char_offset: int) -> int | None``。

    Returns:
        切片草稿列表，``ordinal`` 从 0 开始连续编号；``char_start`` / ``char_end``
        是该切片正文在 ``markdown`` 里的精确位置。
    """
    heading_list = headings or []
    if not markdown.strip():
        return []
    if target_tokens <= 0:
        target_tokens = DEFAULT_TARGET_TOKENS
    if overlap_tokens < 0:
        overlap_tokens = 0
    overlap_tokens = min(overlap_tokens, max(0, target_tokens // 2))
    # 正文预算先扣掉重叠：切片最终是「重叠 + 正文」，不扣的话每条都会超出目标一截
    budget = max(1, target_tokens - overlap_tokens)

    pieces: list[_Piece] = []
    for segment in _segments(markdown):
        if count_tokens(markdown[segment.start : segment.end]) <= budget:
            pieces.append(_Piece(segment.start, segment.end))
        elif segment.header_end is not None:
            pieces.extend(_table_pieces(markdown, segment, budget))
        else:
            pieces.extend(_text_pieces(markdown, segment.start, segment.end, budget))

    drafts: list[ChunkDraft] = []
    for span in _pack(markdown, pieces, target_tokens, overlap_tokens, budget):
        content = f"{span.prefix}{markdown[span.start : span.end]}"
        # 预算小到装不下一个原子单位时（目标远小于配置下限的极端调用），重叠拼接
        # 可能凑出一条只有空白的切片。它进不了检索、只会白白占一次 embedding，
        # 直接丢掉；空白不属于任何有意义的正文，丢它不影响覆盖。
        if not content.strip():
            continue
        drafts.append(
            ChunkDraft(
                ordinal=len(drafts),
                content=content,
                heading_path=heading_path_at(heading_list, span.start),
                page=page_at(span.start) if page_at is not None else None,
                char_start=span.start,
                char_end=span.end,
                token_count=count_tokens(content),
            )
        )
    return drafts


#: 切片算法版本。改动切片逻辑（区间口径、表格处理、重叠裁法）时必须 +1——
#: 重建索引靠它判断哪些文档的切片还是旧算法产出的，从而只重做该重做的。
CHUNKER_VERSION = 2


def split_parse_result(
    parsed: ParseResult,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[ChunkDraft]:
    """按解析结果切分，自动带上标题路径与页码。"""
    return split_markdown(
        parsed.markdown,
        headings=parsed.headings,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        page_at=parsed.page_at,
    )
