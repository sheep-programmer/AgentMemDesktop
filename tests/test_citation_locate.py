"""引用收窄：一条跨多个小节的切片，要定位到回答依据的那一句和它真正所在的章节。"""

from __future__ import annotations

from pathlib import Path

from agentmem.ingest.parse import ParseResult, extract_headings
from agentmem.retrieve.locate import (
    DocumentOutline,
    best_span,
    claim_before,
    load_outline,
    locate,
)

MANUAL = """# 星澜 X3 技术手册

## 一、产品概述

星澜 X3 额定容量 15.36 kWh，由 3 个电池模块串联组成。

## 二、安装要求

### 2.2 接线规范

交流输出端必须配置 63A 的空气断路器。接地电阻应小于 4 欧姆。

## 三、故障代码

### 3.1 E07 过温保护

当电芯温度超过 58℃ 时系统报 E07 并自动降额至 50% 功率。温度回落到 50℃ 以下后 10 分钟自动恢复。
"""


def _outline() -> DocumentOutline:
    return DocumentOutline(
        ParseResult(markdown=MANUAL, parser="markitdown", headings=extract_headings(MANUAL))
    )


def test_claim_is_the_sentence_right_before_the_marker() -> None:
    answer = "这是概述。E07 表示电芯超过 58℃ 的过温保护，系统会降额到 50%。"
    assert claim_before(answer, len(answer)) == "E07 表示电芯超过 58℃ 的过温保护，系统会降额到 50%"
    # 标记写在句号前面时也一样
    assert claim_before("前文。断路器要 63A", len("前文。断路器要 63A")) == "断路器要 63A"
    # 加粗收尾：标记挂在 ** 后面，不能把论断截成空串
    bold = "**结论：交流输出端必须配置 63A 空气断路器；接地电阻应小于 4 欧姆。**"
    assert claim_before(bold, len(bold)).endswith("接地电阻应小于 4 欧姆")


def test_locate_pins_the_sentence_and_its_real_section() -> None:
    """切片从文档开头覆盖到 3.1，切片级 heading_path 只能写文档标题。"""
    located = locate(
        "E07 是过温保护：电芯超过 58℃ 时报警并降额到 50% 功率",
        MANUAL,
        0,
        len(MANUAL),
        _outline(),
    )
    assert located is not None
    assert "58℃" in located.quote and "E07" in located.quote
    assert MANUAL[located.start : located.end] == located.quote
    assert located.heading_path == "星澜 X3 技术手册 > 三、故障代码 > 3.1 E07 过温保护"


def test_locate_uses_absolute_offsets_for_chunks_in_the_middle() -> None:
    start = MANUAL.index("## 二、安装要求")
    content = MANUAL[start:]
    located = locate("交流输出端要配 63A 断路器", content, start, len(MANUAL), _outline())
    assert located is not None
    assert MANUAL[located.start : located.end] == located.quote
    assert "63A" in located.quote
    assert located.heading_path and located.heading_path.endswith("2.2 接线规范")


def test_unrelated_claim_keeps_chunk_level_location() -> None:
    assert best_span("今天天气怎么样", MANUAL) is None


def test_mismatched_offsets_are_not_trusted() -> None:
    """正文和原文区间长度对不上（相邻切片合并过）时不收窄，免得指错位置。"""
    assert locate("58℃ 过温保护 E07", MANUAL, 0, len(MANUAL) + 5, _outline()) is None


def test_outline_loads_from_parse_cache(tmp_path: Path) -> None:
    parsed = ParseResult(markdown=MANUAL, parser="markitdown", headings=extract_headings(MANUAL))
    (tmp_path / "doc1.parse.json").write_text(parsed.model_dump_json(), encoding="utf-8")
    outline = load_outline(tmp_path, "doc1")
    assert outline is not None
    heading = outline.heading_at(MANUAL.index("63A"))
    assert heading is not None
    assert heading.endswith("2.2 接线规范")
    assert load_outline(tmp_path, "missing") is None


def test_claim_naming_only_the_section_still_finds_its_sentences() -> None:
    """回答用小节名指代内容（「E07 是过温保护故障码」），原句里却只有阈值和动作。

    标题行不能当引用句，但标题的字面要帮它下面的句子加分，否则这类论断一律对不上。
    """
    span = best_span("E07 是**过温保护**故障码", MANUAL)
    assert span is not None
    assert "58℃" in MANUAL[span[0] : span[1]]
