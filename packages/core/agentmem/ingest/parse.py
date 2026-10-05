"""文档解析链：docling 为主，失败回退 markitdown。

两者都是可选导入：没装 docling 时自动只用 markitdown；两者都没有时抛
``PARSE_FAILED``。输出统一为 Markdown 加结构信息（标题层级、页码映射）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import structlog
from pydantic import Field

from agentmem.errors import ParseFailedError, ValidationError
from agentmem.security import MAX_REDIRECTS, resolve_public_url
from agentmem.types import AgentMemModel, ContentModel

logger = structlog.get_logger(__name__)

#: docling 导出 Markdown 时插入的页码占位符
PAGE_BREAK_PLACEHOLDER = "<!-- agentmem:page-break -->"

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class HeadingSpan(AgentMemModel):
    """Markdown 标题及其在全文中的字符位置。"""

    level: int = Field(ge=1, le=6)
    text: str
    char_start: int = Field(ge=0)
    heading_path: str = Field(description="如 '第3章 > 3.2 脱壳'")


class PageSpan(AgentMemModel):
    """页码与字符区间的映射。"""

    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)


class ParseResult(ContentModel):
    """解析结果。"""

    markdown: str
    parser: str = Field(description="docling | markitdown")
    title: str | None = None
    headings: list[HeadingSpan] = Field(default_factory=list)
    pages: list[PageSpan] = Field(default_factory=list)
    page_count: int | None = None

    def page_at(self, char_offset: int) -> int | None:
        """给定字符位置落在第几页。"""
        for span in self.pages:
            if span.char_start <= char_offset < span.char_end:
                return span.page
        if self.pages and char_offset >= self.pages[-1].char_start:
            return self.pages[-1].page
        return None


def docling_available() -> bool:
    """是否安装了 docling。"""
    return importlib.util.find_spec("docling") is not None


def markitdown_available() -> bool:
    """是否安装了 markitdown。"""
    return importlib.util.find_spec("markitdown") is not None


def extract_headings(markdown: str) -> list[HeadingSpan]:
    """扫描 Markdown 的 ATX 标题，代码块内的 ``#`` 不算标题。"""
    headings: list[HeadingSpan] = []
    stack: list[tuple[int, str]] = []
    offset = 0
    in_fence = False
    for line in markdown.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if _FENCE.match(stripped):
            in_fence = not in_fence
        elif not in_fence:
            match = _ATX_HEADING.match(stripped)
            if match:
                level = len(match.group(1))
                text = match.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, text))
                headings.append(
                    HeadingSpan(
                        level=level,
                        text=text,
                        char_start=offset,
                        heading_path=" > ".join(item[1] for item in stack if item[1]),
                    )
                )
        offset += len(line)
    return headings


def split_pages(markdown: str) -> tuple[str, list[PageSpan]]:
    """移除页码占位符，返回清洗后的 Markdown 与页码字符区间。

    两次清洗（去占位符、去首尾空白）都会同步修正偏移量，保证返回的区间
    可以直接用于在清洗后的正文上定位。
    """
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split(PAGE_BREAK_PLACEHOLDER)
    spans: list[PageSpan] = []
    cursor = 0
    for index, segment in enumerate(segments):
        spans.append(PageSpan(page=index + 1, char_start=cursor, char_end=cursor + len(segment)))
        cursor += len(segment)
    joined = "".join(segments)
    text = joined.strip()
    lead = len(joined) - len(joined.lstrip())
    end = len(joined.rstrip())
    adjusted = [
        PageSpan(
            page=span.page,
            char_start=max(0, span.char_start - lead),
            char_end=max(0, min(span.char_end, end) - lead),
        )
        for span in spans
    ]
    return text, [span for span in adjusted if span.char_end > span.char_start]


def _parse_with_docling(path: Path) -> tuple[str, int | None]:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(path))
    document = result.document
    try:
        markdown = document.export_to_markdown(page_break_placeholder=PAGE_BREAK_PLACEHOLDER)
    except TypeError:  # 老版本不支持页码占位符
        markdown = document.export_to_markdown()
    page_count: int | None = None
    pages = getattr(document, "pages", None)
    if isinstance(pages, (dict, list)):
        page_count = len(pages)
    return markdown, page_count


def _parse_with_markitdown(path: Path) -> tuple[str, int | None]:
    from markitdown import MarkItDown

    converter = MarkItDown()
    result = converter.convert(str(path))
    return result.text_content, None


def _parse_sync(path: Path, prefer: str | None = None) -> ParseResult:
    errors: list[str] = []
    order = ["markitdown", "docling"] if prefer == "markitdown" else ["docling", "markitdown"]
    for name in order:
        if name == "docling":
            if not docling_available():
                errors.append("docling 未安装")
                continue
            try:
                markdown, page_count = _parse_with_docling(path)
            except Exception as exc:
                logger.warning("docling_parse_failed", path=str(path), error=str(exc))
                errors.append(f"docling: {exc}")
                continue
        else:
            if not markitdown_available():
                errors.append("markitdown 未安装")
                continue
            try:
                markdown, page_count = _parse_with_markitdown(path)
            except Exception as exc:
                logger.warning("markitdown_parse_failed", path=str(path), error=str(exc))
                errors.append(f"markitdown: {exc}")
                continue
        text, pages = split_pages(markdown)
        return ParseResult(
            markdown=text,
            parser=name,
            title=path.stem,
            headings=extract_headings(text),
            pages=pages,
            page_count=page_count,
        )
    raise ParseFailedError(
        f"无法解析文档：{path.name}", detail={"path": str(path), "errors": errors}
    )


async def parse_document(path: Path, *, prefer: str | None = None) -> ParseResult:
    """解析文档为 Markdown + 结构信息。

    Args:
        path: 原始文件路径。
        prefer: 强制优先使用的解析器（``docling`` | ``markitdown``）。
    """
    if not await asyncio.to_thread(path.is_file):
        raise ParseFailedError("文件不存在", detail={"path": str(path)})
    return await asyncio.to_thread(_parse_sync, path, prefer)


async def parse_text(content: str, *, title: str | None = None) -> ParseResult:
    """解析直接粘贴的纯文本。"""
    markdown, pages = split_pages(content)
    return ParseResult(
        markdown=markdown,
        parser="paste",
        title=title,
        headings=extract_headings(markdown),
        pages=pages,
    )


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")


def _html_to_markdown(payload: bytes) -> str:
    """HTML 转 Markdown：优先 markitdown，缺失时做一次朴素清洗。"""
    if markitdown_available():
        try:  # pragma: no cover - 取决于可选依赖
            import io

            from markitdown import MarkItDown

            result = MarkItDown().convert_stream(io.BytesIO(payload), file_extension=".html")
            return str(result.text_content)
        except Exception as exc:
            logger.warning("markitdown_html_failed", error=str(exc))
    text = payload.decode("utf-8", errors="ignore")
    text = _SCRIPT.sub(" ", text)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


async def fetch_url_markdown(url: str, *, timeout_seconds: float = 30.0) -> tuple[str, str]:
    """抓取网页并转为 Markdown。

    Args:
        url: 目标地址。
        timeout_seconds: 请求超时（秒）。

    Returns:
        ``(title, markdown)``。
    """
    import httpx

    # SSRF 守卫：先确认目标是公网地址，再发请求。
    # 这里**不能**交给 httpx 自动跟随重定向——那样只有首个 URL 被校验过，
    # 一个 302 到 169.254.169.254 的公网页面就能把守卫绕干净。
    # 因此关掉 follow_redirects，手动逐跳校验。
    current = resolve_public_url(url)

    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "AgentMem/0.1 (+local knowledge base)"},
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                response = await client.get(current)
                if not response.is_redirect:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                # 相对跳转要先补全成绝对地址，否则校验的是个残缺 URL
                current = resolve_public_url(str(response.url.join(location)))
            else:
                raise ParseFailedError(f"重定向次数超过 {MAX_REDIRECTS} 次", detail={"url": url})
            response.raise_for_status()
            payload = response.content
    except (ParseFailedError, ValidationError):
        raise  # 守卫与重定向上限的报错要原样透出，不要被包成“抓取失败”
    except Exception as exc:
        raise ParseFailedError(f"网页抓取失败：{exc}", detail={"url": url}) from exc
    markdown = await asyncio.to_thread(_html_to_markdown, payload)
    html_head = payload[:4096].decode("utf-8", errors="ignore")
    match = _TITLE.search(html_head)
    title = match.group(1).strip() if match else url
    return title, markdown
