"""派生文本的落地检查：模型写的概要 / 卡片里，原文找不到依据的小句不留。

概要切片与知识卡片都是模型从原文「写」出来的，提示词里说了「只写材料里有的」，
模型照样会补：「长江大学建立时间」这篇原文只有一句「2003年5月，校庆是5月22日」，
概要却写出了「位于湖北省荆州市」「由原荆州师范学院、江汉石油学院、湖北农学院等合并组建」，
再被抽成一张知识卡片，回答时被当成知识库的结论引述出来——引用看上去有出处，
其实点开原文并没有这些话。

这里不调模型，只做确定性的比对：把派生文本切成小句，看每个小句的**关键词**
（专有名词、名词、英文与编号、数字）在原文里有没有出现。缺失的关键词至少两个、
且超过一半时，这一小句就是在讲原文之外的事。

刻意放过的情形（宁可漏判，也不把正常的归纳当编造丢掉）：

- 动词、形容词、量词不算关键词：「递增」「一致」「上升」是对原文的归纳，不是新事实；
- 只缺一个关键词的小句保留：「每号递增约 0.10 nM」里的 0.10 是从原文数据算出来的步长；
- 原文基本是外文、派生文本是中文时（翻译），中文词无从逐字比对，只查数字与外文词。

实测这批数据里，170 张卡片与 6 段概要，被整段判为原文之外的只有长江大学那一段
概要的前三个小句及由它抽出的卡片（见 ``tests/test_grounding.py``）。
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field

with warnings.catch_warnings():
    # jieba.posseg 的源码里有几处无效转义，首次编译时会刷 SyntaxWarning
    warnings.simplefilter("ignore", SyntaxWarning)
    import jieba.posseg as posseg

#: 一个小句里缺失的关键词超过这个比例（且至少 :data:`MIN_MISSING_TERMS` 个），
#: 这一小句就算「原文之外」
UNSUPPORTED_TERM_RATIO = 0.5
MIN_MISSING_TERMS = 2

#: 原文里汉字占字母类字符的比例低于它，就当作外文原文：中文派生文本无法逐字比对
FOREIGN_SOURCE_CJK_SHARE = 0.2

#: 句末与小句分隔。按小句判：一整句里常常一半是原文、一半是补充，
#: 按整句丢会把原文那一半也丢掉。
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")
_CLAUSE_SEP = re.compile(r"[，,、]")
_SPACES = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")

#: 计为关键词的词性：名词与各类专名、外文词、数词（jieba 的 ICTCLAS 标注）
_KEY_FLAGS = ("n", "eng", "m")

#: 单位与通用缩写：出现在派生文本里不代表引入了新事实
_UNITS = frozenset({"nm", "μm", "um", "mm", "mg", "ml", "kg", "h", "x", "min", "pm", "md"})

#: 描述「文档本身」的词。原文里通常不会写「本文档」「共 N 节」，
#: 但这类话不是事实性的补充，不该把整句判成编造。
_META_TERMS = frozenset(
    {
        "文档",
        "本文",
        "本文档",
        "文件",
        "资料",
        "正文",
        "标题",
        "章节",
        "记录",
        "内容",
        "结构",
        "格式",
        "系列",
        "数据",
        "数值",
        "规律",
        "特征",
        "指标",
        "参数",
        "结论",
        "主语",
        "字段",
        # 归纳用语：「步长」「区间」「趋势」说的是对原文数据的概括，不是新事实
        "步长",
        "区间",
        "范围",
        "趋势",
        "水平",
        "差值",
        "幅度",
        "梯度",
        "起点",
        "终点",
        "整体",
        "两者",
    }
)


@dataclass
class GroundedText:
    """落地检查的结果。"""

    text: str
    dropped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.dropped)


def _compact(text: str) -> str:
    """NFKC + 折大小写 + 去空白：原文里的「IC50 = 12 nM」与派生文本的「IC50=12nM」视为同一处。"""
    return _SPACES.sub("", unicodedata.normalize("NFKC", text)).casefold()


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _foreign(source: str) -> bool:
    """原文是不是基本是外文（汉字占比很低）。"""
    letters = [char for char in source if char.isalpha()]
    if not letters:
        return False
    return sum(1 for char in letters if _is_cjk(char)) / len(letters) < FOREIGN_SOURCE_CJK_SHARE


def _key_terms(clause: str, *, skip_cjk: bool) -> set[str]:
    """小句里的关键词：名词与专名（两字以上）、外文词、数字。"""
    terms: set[str] = set()
    for item in posseg.cut(clause):
        word = item.word.strip()
        if not word or not item.flag.startswith(_KEY_FLAGS):
            continue
        lowered = word.casefold()
        if item.flag == "m":
            # 只认带阿拉伯数字的数词：「一个」「一所」这类不是数据
            if _DIGIT.search(word):
                terms.add(lowered)
            continue
        if lowered in _UNITS or lowered in _META_TERMS:
            continue
        if any(_is_cjk(char) for char in word):
            if skip_cjk or len(word) < 2:
                continue
        elif len(word) < 2:
            continue
        terms.add(lowered)
    return terms


def _supported(clause: str, source: str, *, skip_cjk: bool) -> bool:
    """一个小句的关键词在原文（已 :func:`_compact`）里站不站得住。"""
    terms = _key_terms(unicodedata.normalize("NFKC", clause), skip_cjk=skip_cjk)
    if not terms:
        return True
    missing = [term for term in terms if _compact(term) not in source]
    return not (
        len(missing) >= MIN_MISSING_TERMS and len(missing) / len(terms) > UNSUPPORTED_TERM_RATIO
    )


def ground_text(text: str, sources: Iterable[str]) -> GroundedText:
    """只留下原文站得住的小句，返回保留的文本与被丢掉的小句。

    句子结构尽量保持：同一句里被丢掉的小句直接去掉，句末标点留给最后一个保留的小句。
    整句都站不住就整句去掉。全部站不住时 ``text`` 为空串，由调用方决定丢弃还是不用。
    """
    raw = "\n".join(sources)
    source = _compact(raw)
    skip_cjk = _foreign(raw)
    kept_sentences: list[str] = []
    dropped: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if not sentence.strip():
            continue
        body = sentence.rstrip()
        ending = ""
        while body and body[-1] in "。！？!?；;":
            ending = body[-1] + ending
            body = body[:-1]
        clauses = [item for item in _CLAUSE_SEP.split(body) if item.strip()]
        kept = []
        for clause in clauses:
            if _supported(clause, source, skip_cjk=skip_cjk):
                kept.append(clause.strip())
            else:
                dropped.append(clause.strip())
        if not kept:
            continue
        if len(kept) == len(clauses):
            # 一个小句都没丢：原样保留，连分隔符与换行都不动
            kept_sentences.append(sentence)
        else:
            kept_sentences.append("，".join(kept) + (ending or "。"))
    return GroundedText(text="".join(kept_sentences).strip(), dropped=dropped)
