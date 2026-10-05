"""Prompt 渲染的公共工具。

设计原则：
1. 本模块及同包下所有模块 **不依赖 agentmem 其它模块**，输入输出都是基本类型。
   这样 Prompt 可以被单独测试、单独迭代，甚至拿去别处复用。
2. 所有需要模型返回结构化结果的 Prompt，一律要求输出 **单个 JSON 对象**，
   并在提示词里显式给出 schema 与一个示例。解析失败的重试策略由调用方负责。
3. 用 XML 风格标签分隔上下文块。相比 Markdown 标题，标签边界更清晰，
   模型更不容易把资料内容误当成指令。
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

# ---------------------------------------------------------------- 分块与转义

# 资料可能包含形如 </context> 的字符串，直接拼接会破坏标签边界。
_TAG_LIKE = re.compile(r"</?([a-zA-Z_][\w-]*)\s*/?>")


def _neutralize_tags(text: str) -> str:
    """把正文里形似标签的片段打断，防止污染我们自己的标签结构。

    只在闭合尖括号前插入一个零宽度无意义字符会影响模型阅读，
    因此改为把尖括号替换成全角形式——视觉上等价，不会被当作标签。
    """
    return _TAG_LIKE.sub(lambda m: m.group(0).replace("<", "＜").replace(">", "＞"), text)


def section(tag: str, content: str, *, escape: bool = True, **attrs: Any) -> str:
    """包一个 XML 风格的上下文块。

    >>> section("doc", "hello", id="c1")
    '<doc id="c1">\\nhello\\n</doc>'

    `escape=True`（默认）会中和正文里形似标签的片段，防止资料内容伪造标签边界、
    把后续指令伪装成上下文的一部分。

    ⚠️ **外层包裹已经由 `section()` 构造好的内容时，必须传 `escape=False`**，
    否则会把我们自己生成的内层标签一并转义掉，上下文结构就塌了。
    """
    body = _neutralize_tags(content.strip()) if escape else content.strip()
    if not body:
        return ""
    attr_str = "".join(f' {k}="{_attr(v)}"' for k, v in attrs.items() if v is not None)
    return f"<{tag}{attr_str}>\n{body}\n</{tag}>"


def _attr(value: Any) -> str:
    return str(value).replace('"', "'").replace("\n", " ")


def join(*blocks: str) -> str:
    """拼接若干块，自动丢弃空块并用空行分隔。"""
    return "\n\n".join(b for b in blocks if b and b.strip())


def numbered(items: list[str], *, start: int = 1) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, start=start))


def bulleted(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


# ---------------------------------------------------------------- JSON 约定

JSON_ONLY = (
    "只输出一个 JSON 对象，不要输出任何解释性文字，不要用 ```json 代码块包裹。"
    "如果某个字段无法确定，宁可留空数组或 null，也不要编造。"
)


def schema_block(schema: dict[str, Any], example: dict[str, Any] | None = None) -> str:
    """生成 schema 说明块。schema 用可读的伪 JSON 描述，不走 JSON Schema 那套冗长格式。"""
    parts = [section("output_schema", json.dumps(schema, ensure_ascii=False, indent=2))]
    if example is not None:
        parts.append(section("output_example", json.dumps(example, ensure_ascii=False, indent=2)))
    parts.append(JSON_ONLY)
    return join(*parts)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里尽力提取 JSON 对象。

    依次尝试：整体解析 → 剥离代码围栏 → 截取首个 `{` 到末个 `}` → 截取首个 `[` 到
    末个 `]` → 从被截断的输出里救回完整的数组元素。全都不成则抛 ValueError，由调用方
    决定重试还是放弃。

    对象优先于数组：输出里同时含 `{` 与 `[` 时，按数组截取往往能"成功"解析出一个内层
    列表，调用方拿到的就不是它要的对象了。

    顶层确实是数组时包成 ``{"items": [...]}``：调用方一律按对象取字段，直接返回列表
    会让它们抛 ``AttributeError``——那是崩溃，不是解析失败。
    """
    candidates = [text, _FENCE.sub("", text)]

    stripped = _FENCE.sub("", text).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if start != -1 and end > start:
            if opener == "[" and "{" in stripped[:start]:
                # 已经有对象了，别再退而求其次去截数组
                continue
            candidates.append(stripped[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        return {"items": parsed} if isinstance(parsed, list) else parsed

    salvaged = salvage_truncated_json(text)
    if salvaged is not None:
        return salvaged

    raise ValueError(f"模型输出中未找到可解析的 JSON：{text[:300]!r}")


def salvage_truncated_json(text: str) -> dict[str, list[Any]] | None:
    """从被截断的 JSON 里救回已经完整的数组元素。

    模型输出撞上 ``max_tokens`` 时会在半句话中间断掉，整体解析必然失败——此前整批
    结果就此丢掉（实测一批 19 批里废掉 1 批，白花一次调用）。这里按括号配对扫一遍，
    把每个数组里**已经闭合**的元素收集起来，让这一批至少产出一部分。

    Returns:
        形如 ``{"cards": [...], "entities": [...]}``；一个完整元素都没有时返回 ``None``。
    """
    start = text.find("{")
    if start == -1:
        return None

    found: dict[str, list[Any]] = {}
    key: str | None = None
    depth = 0
    in_string = False
    escaped = False
    item_start: int | None = None
    pending_key: str | None = None
    expecting_key = False

    index = start
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            if depth == 1:
                # 顶层的键名：读到结尾再判断后面是不是数组
                end = index + 1
                escaped_inner = False
                while end < len(text):
                    if escaped_inner:
                        escaped_inner = False
                    elif text[end] == "\\":
                        escaped_inner = True
                    elif text[end] == '"':
                        break
                    end += 1
                try:
                    pending_key = json.loads(text[index : end + 1])
                except ValueError:
                    pending_key = None
                expecting_key = True
                index = end + 1
                continue
            in_string = True
            index += 1
            continue

        if char == "[":
            depth += 1
            if depth == 2 and expecting_key and pending_key:
                key = pending_key
                found.setdefault(key, [])
            index += 1
            continue

        if char == "{":
            if depth == 2 and key is not None and item_start is None:
                item_start = index
            depth += 1
            index += 1
            continue

        if char == "}":
            depth -= 1
            # 数组元素闭合后 depth 回到 2（根对象 1 + 数组 1）
            if depth == 2 and key is not None and item_start is not None:
                # 截断在半途的元素解析不出来，跳过即可：这个函数的用处正是从被截断的
                # 输出里抢救出完整的那几条
                with contextlib.suppress(ValueError):
                    found[key].append(json.loads(text[item_start : index + 1]))
                item_start = None
            elif depth == 0:
                break
            index += 1
            continue

        if char == "]":
            depth -= 1
            if depth == 1:
                key = None
                expecting_key = False
            elif depth == 0:
                break
            index += 1
            continue

        if char == "," and depth == 1:
            expecting_key = False
        index += 1

    cleaned = {name: items for name, items in found.items() if items}
    return cleaned or None


# ---------------------------------------------------------------- 预算控制


def truncate(text: str, max_chars: int, *, marker: str = "\n…（已截断）…\n") -> str:
    """超长文本保头保尾截断——中间省略比直接砍尾更能保住结论部分。"""
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker):
        return text[:max_chars]
    head = (max_chars - len(marker)) * 2 // 3
    tail = max_chars - len(marker) - head
    return text[:head] + marker + text[-tail:]
