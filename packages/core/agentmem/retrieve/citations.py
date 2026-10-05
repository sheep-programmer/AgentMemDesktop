"""引用标记解析：正文里的 ``[^c3]`` → 可下钻的原文定位。

协议见 ``agentmem.prompts.answer``：模型在正文里用 ``[^编号]`` 标注引用，
后端把编号映射回 chunk_id，前端渲染成可点击芯片。

两个必须小心的地方：

1. **流式边界**。标记可能被切成 ``[^c`` + ``3]`` 两个 delta 到达，解析器因此带缓冲：
   任何「可能是一个标记开头」的尾巴都留到下一片再判断，既不能漏检也不能重复发。
2. **模型幻觉**。模型可能编出 evidence 块里不存在的编号，这类标记必须丢弃——
   写进 citations 会让前端渲染出点不开的死链。

第三种跑偏是**丢了字母**：只有一条证据时，模型常把 ``[^c1]`` 写成 ``[^1]``
（实测 agnes-2.5-flash，「长江大学哪一年建立的」三次里有一次），引用于是整条丢失。
纯数字与真正的脚注长得一样，所以只在解析器拿到了本轮的证据编号表、且
``c{数字}`` 确实在表里时才认作引用；其余 ``[^1]`` 一律当普通脚注原样输出。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

import structlog
from pydantic import Field

from agentmem.types import Citation, ContentModel

from .models import ScoredChunk, snippet_of

logger = structlog.get_logger(__name__)

#: 完整的引用标记，编号形如 c1 / c12。
#:
#: 前缀放宽成任意单个字母并在解析时归一到 ``c``：提示词规定的是 ``[^cN]``，
#: 但模型会跑偏——实测 agnes-2.5-flash 把它写成 ``[^e2]``（大概是 evidence），
#: 此前这类标记既不解析也不从正文剥离，引用就此静默丢失。一次实测统计里
#: 11 个标记丢了 2 个（18%），而「每个回答都能下钻到原文」是产品的核心承诺。
#:
#: 放宽是安全的：编号仍要在证据表里查得到，查不到照旧按
#: ``citation_marker_hallucinated`` 丢弃。
#:
#: 字母可以缺省（``[^1]``），但缺字母的那种要过一道更严的关：见
#: :class:`CitationStreamParser` 的 ``known`` 参数——没有证据编号表、或 ``c1``
#: 不在表里时，``[^1]`` 按普通脚注原样输出。文字脚注 ``[^note]`` 不匹配。
MARKER_PATTERN = re.compile(r"\[\^([A-Za-z]?)([0-9]+)\]")

#: 可能是标记前缀的尾巴：``[`` / ``[^`` / ``[^c`` / ``[^c1`` / ``[^1``
#: 命中它时必须把这段留在缓冲区里，等后续 delta 到齐再判断。
_MARKER_PREFIX = re.compile(r"\[(?:\^(?:[A-Za-z]?[0-9]*)?)?\Z")

_OPEN_BRACKET = "["


def marker_for(index: int) -> str:
    """按序号生成引用标记：1 → ``c1``。"""
    return f"c{index}"


class CitationFeed(ContentModel):
    """一次 :meth:`CitationStreamParser.feed` 的产物。"""

    text: str = Field(default="", description="可以安全输出给前端的正文（标记已剥离）")
    markers: list[str] = Field(default_factory=list, description="本次解析出的标记编号")
    offsets: list[int] = Field(
        default_factory=list,
        description="每个标记在 ``text`` 里的字符位置（标记被剥离处），与 markers 一一对应",
    )


class CitationStreamParser:
    """增量解析正文里的引用标记。

    用法::

        parser = CitationStreamParser()
        for delta in stream:
            feed = parser.feed(delta)
            emit(feed.text, feed.markers)
        emit(parser.flush())          # 收尾：把不完整的尾巴当普通正文输出
    """

    def __init__(self, known: Collection[str] | None = None) -> None:
        """
        Args:
            known: 本轮实际注入的证据编号（``c1`` …）。给了它，缺字母的 ``[^1]``
                在 ``c1`` 登记在册时也认作引用；不给（或不在表里）就按普通脚注输出。
                带字母的标记不看这张表，幻觉编号仍由 :class:`CitationEmitter` 过滤。
        """
        self._buffer = ""
        self._known = frozenset(known or ())

    @property
    def pending(self) -> str:
        """尚在缓冲区里、还没决定归属的尾巴。"""
        return self._buffer

    def feed(self, chunk: str) -> CitationFeed:
        """喂入一段增量，返回可输出的正文与本次识别出的标记。

        标记本身不会出现在返回的 ``text`` 里——前端不渲染 ``[^c3]`` 这种裸标记，
        它渲染的是 citation 事件对应的芯片。
        """
        if not chunk:
            return CitationFeed()
        self._buffer += chunk

        parts: list[str] = []
        markers: list[str] = []
        offsets: list[int] = []
        cursor = 0
        length = len(self._buffer)

        while cursor < length:
            index = self._buffer.find(_OPEN_BRACKET, cursor)
            if index < 0:
                parts.append(self._buffer[cursor:])
                cursor = length
                break
            if index > cursor:
                parts.append(self._buffer[cursor:index])

            match = MARKER_PATTERN.match(self._buffer, index)
            if match is not None and (match.group(1) or f"c{match.group(2)}" in self._known):
                prefix, number = match.group(1), match.group(2)
                if prefix.lower() != "c":
                    # 归一但留痕：跑偏本身是提示词或模型的问题，值得能被查出来
                    logger.info(
                        "citation_marker_normalized", wrote=f"{prefix}{number}", used=f"c{number}"
                    )
                markers.append(f"c{number}")
                # 标记在输出文本里的位置：此刻已经攒下的字符数
                offsets.append(sum(len(part) for part in parts))
                cursor = match.end()
                continue

            if _MARKER_PREFIX.match(self._buffer[index:]):
                # 可能是被切断的标记，留到下一片再判断
                cursor = index
                break

            # 只是个普通方括号（如 ``[1]`` / ``[注]``），原样输出
            parts.append(_OPEN_BRACKET)
            cursor = index + 1

        self._buffer = self._buffer[cursor:]
        return CitationFeed(text="".join(parts), markers=markers, offsets=offsets)

    def flush(self) -> str:
        """收尾，返回缓冲区里剩下的正文并清空。

        流结束时残留的 ``[^c`` 是模型没写完的标记，按普通正文输出，
        不做臆测补全。
        """
        text = self._buffer
        self._buffer = ""
        return text


def citation_of(marker: str, chunk: ScoredChunk) -> Citation:
    """由命中构造一条引用记录（落进 ``messages.citations``）。"""
    return Citation(
        marker=marker,
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_title=chunk.document_title or None,
        page=chunk.page,
        heading_path=chunk.heading_path,
        ordinal=chunk.ordinal,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        kind=chunk.kind,
        snippet=snippet_of(chunk.content),
    )


class CitationRegistry:
    """``marker → 引用`` 的映射，由检索结果构造，生成时用来解析标记。"""

    def __init__(self) -> None:
        self._citations: dict[str, Citation] = {}
        self._chunks: dict[str, ScoredChunk] = {}

    def add(self, marker: str, chunk: ScoredChunk) -> Citation:
        """登记一个标记对应的命中。"""
        citation = citation_of(marker, chunk)
        self._citations[marker] = citation
        self._chunks[marker] = chunk
        return citation

    def chunk(self, marker: str) -> ScoredChunk | None:
        """标记对应的完整命中（含正文），收窄引用定位时要用。"""
        return self._chunks.get(marker)

    def get(self, marker: str) -> Citation | None:
        """按标记取引用，未登记返回 ``None``。"""
        return self._citations.get(marker)

    def known(self) -> frozenset[str]:
        """全部已登记的标记。"""
        return frozenset(self._citations)

    def ordered(self) -> list[Citation]:
        """按登记顺序返回全部引用。"""
        return list(self._citations.values())

    def __len__(self) -> int:
        return len(self._citations)


class CitationEmitter:
    """把流式解析出的标记转成引用事件，并过滤模型幻觉编号。

    两条约束：
    - 同一个标记只发一次（正文里多次引用同一篇资料是常态）；
    - 编号不在 :class:`CitationRegistry` 里的直接丢弃并记日志，不写进 citations。
    """

    def __init__(self, registry: CitationRegistry) -> None:
        self.registry = registry
        self._emitted: set[str] = set()

    @property
    def emitted(self) -> list[str]:
        """已发出的标记，按首次出现顺序。"""
        return list(self._emitted)

    def accept(
        self,
        markers: Iterable[str],
        offsets: Iterable[int] | None = None,
        *,
        base: int = 0,
    ) -> list[Citation]:
        """接收本次解析出的标记，返回需要推送的引用。

        Args:
            markers: 本次解析出的标记编号。
            offsets: 与 ``markers`` 一一对应的位置（相对本次输出的正文）。
            base: 本次输出正文在整个回答里的起始下标，用来换算成绝对位置。
        """
        accepted: list[Citation] = []
        positions = list(offsets) if offsets is not None else []
        for index, marker in enumerate(markers):
            if marker in self._emitted:
                continue
            citation = self.registry.get(marker)
            if citation is None:
                logger.warning("citation_marker_hallucinated", marker=marker)
                continue
            self._emitted.add(marker)
            if index < len(positions):
                citation = citation.model_copy(update={"char_offset": base + positions[index]})
            accepted.append(citation)
        return accepted
