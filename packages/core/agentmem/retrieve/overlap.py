"""相邻切片重叠的裁剪：只切重复的那一段，不整条丢弃。

切分器给相邻切片留了 ``chunk_overlap``（默认 80 token 对 512 token 的切片，
约 15%）的重叠。同一篇文档的相邻两条同时进入 top-N 时，那一段文字会被原样
送进上下文两遍：占掉证据预算，却不带来任何新信息。

``retrieve/diversity.py`` 的整条丢弃处理不了这种情形——那里的前提是「短的那条
基本被长的那条覆盖」。相邻切片只重合一小段，丢整条等于连它独有的 85% 一起扔了。
这里的做法是**只裁掉重复的那一段**：保留顺序靠前那条的完整内容，把靠后那条
与它重合的前缀/后缀切掉。切掉的部分按定义已经在前面那条里，所以除了下面这条
保底规则，这一步不损失任何信息。

    MIN_OVERLAP_CHARS      低于此长度的「重合」按巧合同字处理，不当重叠
    common_overlap         两条正文之间的公共前后缀长度，两侧都对齐到句子边界
    trim_adjacent_overlap  按最终顺序裁剪相邻切片，裁完过短的整条丢弃

**唯一会损失信息的地方**：裁完剩下的正文不足 ``MIN_REMAINING_TOKENS`` 时整条丢弃。
那种情形下这条切片除了重复段几乎什么都没有，而证据区的分配器本来也会把
不足 ``prompts.answer.MIN_EVIDENCE_TOKENS`` 的残片丢掉；留在列表里只是占一个
引用编号。要严格零损失可以把 ``min_tokens`` 设成 0。

偏移的处理要区分两种切片。绝大多数切片的 ``char_end - char_start == len(content)``，
本模块一直按这条等式同步偏移。表格续段是唯一的例外：它的正文以一段重复的表头开头，
``content`` 因此比偏移跨度长（见 ``ingest/chunk.py::ChunkDraft``）。这类切片的重合段
一定落在正文部分，裁剪时前缀原样保留，偏移照旧只跟着正文走。

与同目录其它模块一样：输入输出都是 :class:`ScoredChunk`，不发网络请求、不碰存储，
可以脱离检索链路单独测试。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentmem.prompts.budget import estimate_tokens, split_sentences

from .models import ScoredChunk

#: 判为切片重叠的最短字符数。切分器的重叠是「上一条末尾的若干整句」，长度由
#: 句子的实际长度决定：默认 80 token 的上限下，实测既有 58 字符的，也有 23 字符的
#: （上一条末尾那句很短、再往前一句就超限）。阈值取 16（约一句中文短句）能覆盖
#: 这些真实情形；低于它的匹配省不下什么，却会让「裁过」的标记落到本来就很短的
#: 切片上，触发下面那条长度保底。
MIN_OVERLAP_CHARS = 16

#: 裁完剩下的正文低于此 token 数就整条丢弃。
#: 40 远小于证据区单条下限（120），含义是「这条已经基本没有自己的内容了」。
MIN_REMAINING_TOKENS = 40


def boundary_cuts(text: str) -> list[int]:
    """``text`` 里所有可以安全切开的位置，升序，含 0 与末尾。

    用 :func:`agentmem.prompts.budget.split_sentences` 找边界，与证据预算裁剪
    共用同一套断句规则：两处对「哪里算一句话」的判断必须一致，否则在这边
    认为完整的句子，到了预算裁剪那边会被再切一次。

    该函数保证 ``"".join(split_sentences(t)) == t``，因此累加各句长度得到的
    位置就是真实下标，不需要再校正。
    """
    cuts = [0]
    total = 0
    for sentence in split_sentences(text):
        total += len(sentence)
        cuts.append(total)
    if cuts[-1] != len(text):  # pragma: no cover - split_sentences 恒等于原文，这里只兜底
        cuts.append(len(text))
    return cuts


def common_overlap(
    earlier: str,
    later: str,
    *,
    min_chars: int = MIN_OVERLAP_CHARS,
) -> tuple[int, int] | None:
    """``earlier`` 的末尾与 ``later`` 的开头重合的长度。

    Args:
        earlier: 文档内序号在前的那条正文。
        later: 序号在后的那条正文。
        min_chars: 短于它的重合直接当作巧合同字，返回 ``None``。

    Returns:
        ``(earlier 要裁掉的尾部字符数, later 要裁掉的头部字符数)``；
        没有可用重合时返回 ``None``。

    切点两侧都必须落在句子/行边界上：只在一侧对齐会切出半句话，
    在「IC50 = 12 nM」这类以数值为核心的领域里，切坏的证据比没有证据更危险。

    做法是拿 ``later`` 的句子边界当候选切点、从长到短试：候选取 ``later[:cut]``
    并忽略其尾部空白（切片重叠是若干整句的尾部拼上 ``"\\n\\n"`` 再 strip 出来的，
    与 ``earlier`` 的结尾相比会差一个换行），命中后再要求 ``earlier`` 那一侧的
    切点也是句边界。两侧同时成立才算数。

    为什么不用「最长公共子串」的通用算法：那会找出中间任意位置的重复片段，
    而这里要的只是「前一条的尾巴变成了后一条的开头」这一种关系。按句子候选
    逐个字符串比较即可，切片只有几十条、句子只有几十句，开销可以忽略。
    """
    if not earlier or not later:
        return None
    body = earlier.rstrip()
    boundaries = set(boundary_cuts(body))
    for cut in reversed(boundary_cuts(later)):
        if cut <= 0:
            break
        piece = later[:cut].rstrip()
        if len(piece) < min_chars:
            # 候选随 cut 减小而单调变短，再往下试只会更短
            break
        if not body.endswith(piece):
            continue
        remainder = len(body) - len(piece)
        if remainder in boundaries:
            return (len(earlier) - remainder, cut)
    return None


def trim_adjacent_overlap(
    chunks: Sequence[ScoredChunk],
    *,
    min_chars: int = MIN_OVERLAP_CHARS,
    min_tokens: int = MIN_REMAINING_TOKENS,
) -> list[ScoredChunk]:
    """按顺序裁掉相邻切片的重合段，返回裁剪后的命中列表。

    调用前 ``chunks`` 必须已经是**最终进入上下文的顺序**（名次即篇幅：
    证据区按名次分配 token，排在前面的那条拿到的篇幅更大），因此这里保留
    靠前那条的完整内容，只裁靠后那条。顺序与条数（除被丢弃的）不变。

    只与 ``ordinal`` 相差 1 且**已被保留**的切片比对，两个后果都是刻意的：

    - 前一条被裁过（它自己是靠后的那条）时，它的正文仍然是原文的一段后缀或
      前缀，重合段依然完整地留在里面，裁本条不会丢信息；
    - 前一条被整条丢弃时，本条不再与它比对——那段的文字此时只存在于本条里，
      再裁就真的丢信息了。

    与序号在**后**的邻居比对的同理：重叠段是本条的尾部，只要对端（已保留）
    的正文里确实有这段文字才裁。

    Args:
        chunks: 已按最终顺序排好的命中。
        min_chars: 传给 :func:`common_overlap` 的最短重合长度。
        min_tokens: 裁完剩余正文低于此 token 数就丢弃（0 表示永不丢）。
            只对真的被裁过的条目生效——本来就没有重叠的短切片不归这里管。

    Returns:
        裁剪后的列表；``ordinal`` / ``char_start`` / ``char_end`` 已随正文同步修正。
    """
    kept: list[ScoredChunk] = []
    # (document_id, ordinal) → 已保留的那条（正文可能已被裁过）
    retained: dict[tuple[str, int], ScoredChunk] = {}
    for chunk in chunks:
        current = _trim_against(chunk, retained, min_chars, min_tokens)
        if current is None:
            continue
        kept.append(current)
        if current.ordinal is not None:
            retained[(current.document_id, current.ordinal)] = current
    return kept


def _trim_against(
    chunk: ScoredChunk,
    retained: dict[tuple[str, int], ScoredChunk],
    min_chars: int,
    min_tokens: int,
) -> ScoredChunk | None:
    """把 ``chunk`` 与相邻的已保留切片比一遍，裁掉重合段。

    Returns:
        裁剪后的条目；裁完只剩一段没有信息量的残片时返回 ``None``。

    ⚠️ 长度下限**只对真的被裁过的条目生效**。本来就很短的切片（例如文档末尾
    的一小段）如果没有重叠，就不该因为「短」被丢掉——那是证据预算分配器的判断，
    不是这里的事：分配器会按名次给它份额，份额不够时它自己会被丢掉。
    """
    if chunk.ordinal is None:
        return chunk

    head = 0
    tail = 0
    # 序号在前的那条：重合段落在本条的开头
    previous = retained.get((chunk.document_id, chunk.ordinal - 1))
    if previous is not None:
        found = common_overlap(previous.content, chunk.content, min_chars=min_chars)
        if found is not None:
            head = found[1]
    # 序号在后（但名次更靠前）的那条：重合段落在本条的末尾
    following = retained.get((chunk.document_id, chunk.ordinal + 1))
    if following is not None:
        found = common_overlap(chunk.content, following.content, min_chars=min_chars)
        if found is not None:
            tail = found[0]

    if head == 0 and tail == 0:
        return chunk
    current = _reslice(chunk, head, len(chunk.content) - tail)
    if current is None:
        return None
    # 裁完剩下的部分太短，说明这条本来就是上一段的尾巴，没有自己的内容
    return current if _long_enough(current, min_tokens) else None


def _prefix_len(chunk: ScoredChunk) -> int:
    """``content`` 开头那段不属于 ``[char_start, char_end)`` 的合成前缀长度。

    只有表格续段带前缀（切分器把表头复制到每一段，好让数据行有列名可读），
    长度可由「正文长度 − 偏移跨度」反推，不需要额外的字段。偏移缺失或本身就
    不自洽时返回 0，退化成按原样处理。
    """
    if chunk.char_start is None or chunk.char_end is None:
        return 0
    prefix = len(chunk.content) - (chunk.char_end - chunk.char_start)
    return prefix if 0 < prefix <= len(chunk.content) else 0


def _reslice(chunk: ScoredChunk, start: int, end: int) -> ScoredChunk | None:
    """把正文换成 ``content[start:end]``，并同步修正字符偏移。

    ⚠️ 偏移必须与正文一起改。前端拿 ``char_start`` 去解析后的全文里定位高亮，
    正文少了一段而偏移不动，引用就会整体错位（这个坑在
    ``ingest/parse.py::split_pages`` 已经踩过一次）。

    切点两侧可能残留换行（句子边界刚好落在换行上），这里连同空白一起去掉，
    并把去掉的字符数一并计入偏移，保证 ``char_end - char_start == len(content)``
    这个不变量在裁剪前后都成立。

    合成前缀（表格续段重复的表头）不参与偏移，也不该被裁掉：它不是重复内容，
    是这一段的列名。切点先换算到正文坐标，前缀原样留在结果里。
    """
    prefix_len = _prefix_len(chunk)
    prefix = chunk.content[:prefix_len]
    body = chunk.content[prefix_len:]
    start = max(0, min(start - prefix_len, len(body)))
    end = max(0, min(end - prefix_len, len(body)))
    text = body[start : max(start, end)]
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    kept = text.strip()
    if not kept:
        return None
    update: dict[str, object] = {"content": f"{prefix}{kept}"}
    if chunk.char_start is not None:
        update["char_start"] = chunk.char_start + start + lead
    if chunk.char_end is not None:
        # 尾部去掉的 = 切掉的尾巴 + 切点处残留的空白
        update["char_end"] = chunk.char_end - (len(body) - end) - trail
    return chunk.model_copy(update=update)


def _long_enough(chunk: ScoredChunk, min_tokens: int) -> bool:
    """剩余正文是否还值得占一个引用编号。"""
    return min_tokens <= 0 or estimate_tokens(chunk.content) >= min_tokens
