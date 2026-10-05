"""Anthropic 前缀缓存断点的位置测试。

OpenAI / DeepSeek 的前缀缓存是自动的，Anthropic 必须显式打 ``cache_control``。
断点打错位置有两种代价，都不会报错、只会悄悄烧钱：

- 打在**本轮独有**的内容上 → 每轮都写一次缓存（1.25x），永远读不到；
- 该打的地方没打 → 稳定前缀白白按原价重复计费。

所以断点位置必须有测试盯着。
"""

from __future__ import annotations

from typing import Any

from agentmem.providers.adapters.anthropic import AnthropicProvider
from agentmem.providers.base import Message
from agentmem.types import ProviderConfig


def _provider(**extra: Any) -> AnthropicProvider:
    return AnthropicProvider(
        ProviderConfig(
            id="anthropic-test",
            kind="llm",
            adapter="anthropic",
            model="claude-sonnet-4-5",
            extra=extra,
        )
    )


def _long(text: str, tokens: int) -> str:
    """造一段约 ``tokens`` 个 token 的中文文本（CJK 粗估 1 字 1 token）。"""
    return (text * ((tokens // len(text)) + 1))[:tokens]


def _messages(system: str, turns: list[tuple[str, str]]) -> list[Message]:
    messages = [Message(role="system", content=system)]
    for role, content in turns:
        messages.append(Message(role=role, content=content))
    return messages


def _breakpoints(kwargs: dict[str, Any]) -> list[str]:
    """列出所有带断点的位置，便于断言「有几个、在哪」。"""
    found: list[str] = []
    system = kwargs.get("system")
    if isinstance(system, list):
        for block in system:
            if block.get("cache_control"):
                found.append("system")
    for index, message in enumerate(kwargs["messages"]):
        content = message["content"]
        if isinstance(content, list):
            for block in content:
                if block.get("cache_control"):
                    found.append(f"messages[{index}]")
    return found


def _build(provider: AnthropicProvider, messages: list[Message]) -> dict[str, Any]:
    return provider._build_kwargs(messages, temperature=0.7, max_tokens=1024, tools=None)


# ------------------------------------------------------------------ 断点位置


def test_never_caches_the_final_turn() -> None:
    """最后一条 user 装的是本轮独有的检索证据，打断点等于每轮白付 1.25x 写入费。"""
    kwargs = _build(
        _provider(),
        _messages(
            _long("你是新药研发专家。", 2500),
            [("user", "第一问"), ("assistant", "第一答"), ("user", "本轮独有的证据与问题")],
        ),
    )
    last = len(kwargs["messages"]) - 1
    assert f"messages[{last}]" not in _breakpoints(kwargs)


def test_caches_system_and_history_prefix() -> None:
    """稳定前缀（system + 全部历史）应当被一并纳入缓存。"""
    kwargs = _build(
        _provider(),
        _messages(
            _long("你是新药研发专家。", 2500),
            [("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")],
        ),
    )
    marks = _breakpoints(kwargs)
    assert "system" in marks
    # 历史的最后一条 = 倒数第二条消息
    assert f"messages[{len(kwargs['messages']) - 2}]" in marks
    assert len(marks) == 2, f"断点数量应恰好为 2，实际 {marks}"


def test_short_prefix_gets_no_breakpoint() -> None:
    """短于官方下限的前缀不可能被缓存，打了只会白付写入费。"""
    kwargs = _build(_provider(), _messages("你是助手。", [("user", "你好")]))
    assert _breakpoints(kwargs) == []
    assert isinstance(kwargs["system"], str), "没有断点时应退回纯字符串形式"


def test_single_turn_marks_only_system() -> None:
    """只有一轮时没有历史可缓存，断点只该落在 system 上。"""
    kwargs = _build(_provider(), _messages(_long("你是新药研发专家。", 2500), [("user", "问题")]))
    assert _breakpoints(kwargs) == ["system"]


def test_can_be_disabled_for_incompatible_relays() -> None:
    """部分第三方中转不认 cache_control 会直接 400，必须能关掉。"""
    kwargs = _build(
        _provider(prompt_cache=False),
        _messages(
            _long("你是新药研发专家。", 2500),
            [("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")],
        ),
    )
    assert _breakpoints(kwargs) == []


# ------------------------------------------------------------------ 内容保真


def test_cache_control_does_not_alter_content() -> None:
    """加断点只是换了个表示形式，正文一个字都不能变。"""
    system_text = _long("你是新药研发专家。", 2500)
    turns = [("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")]
    cached = _build(_provider(), _messages(system_text, turns))
    plain = _build(_provider(prompt_cache=False), _messages(system_text, turns))

    assert cached["system"][0]["text"] == plain["system"]
    for got, want in zip(cached["messages"], plain["messages"], strict=True):
        text = got["content"]
        if isinstance(text, list):
            text = "".join(block["text"] for block in text)
        assert text == want["content"]


# ------------------------------------------------------------------ 用量口径


def test_prompt_tokens_include_cache_tokens() -> None:
    """Anthropic 的 input_tokens **不含**缓存读写，不加回去用量统计会凭空少一截。"""
    from agentmem.providers.adapters.anthropic import _cache_usage, _total_prompt_tokens

    class _Usage:
        input_tokens = 100
        cache_read_input_tokens = 900
        cache_creation_input_tokens = 50

    assert _total_prompt_tokens(_Usage()) == 1050
    assert _cache_usage(_Usage()) == (900, 50)


def test_usage_absent_is_none_not_zero() -> None:
    """取不到就是「不知道」，不能报成 0——排查缓存有没有生效时这两者含义相反。"""
    from agentmem.providers.adapters.anthropic import _cache_usage, _total_prompt_tokens

    assert _total_prompt_tokens(None) is None
    assert _cache_usage(None) == (None, None)


def test_openai_style_cached_tokens() -> None:
    """OpenAI 与 DeepSeek report 缓存命中的字段名不同，两种都要认。"""
    from agentmem.providers.adapters.openai_compatible import _cached_tokens

    class _Details:
        cached_tokens = 512

    class _OpenAIUsage:
        prompt_tokens_details = _Details()

    class _DeepSeekUsage:
        prompt_tokens_details = None
        prompt_cache_hit_tokens = 384

    assert _cached_tokens(_OpenAIUsage()) == 512
    assert _cached_tokens(_DeepSeekUsage()) == 384
    assert _cached_tokens(None) is None
