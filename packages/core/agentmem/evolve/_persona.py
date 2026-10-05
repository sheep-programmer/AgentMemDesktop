"""Persona ↔ Prompt 层形状的映射（evolve / expert 共用）。

Prompt 层刻意零依赖，用 TypedDict；core 用 Pydantic。两者之间需要一层映射，
集中放在这里避免各模块各写一份、日后字段漂移。
"""

from __future__ import annotations

from collections.abc import Sequence

from agentmem.prompts import Message as PromptMessage
from agentmem.prompts import PersonaSpec
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.base import MessageRole
from agentmem.types import Persona


def to_persona_spec(persona: Persona) -> PersonaSpec:
    """把 core 的 :class:`Persona` 映射成 Prompt 层的 :class:`PersonaSpec`。"""
    return PersonaSpec(
        name=persona.name,
        domain=persona.domain,
        role_description=persona.role_description,
        principles=persona.principles,
        language=persona.output_style.language,
        tone=persona.output_style.tone,
        must_cite=persona.output_style.must_cite,
        quality_bar=persona.quality_bar,
        glossary=persona.glossary,
    )


def to_provider_messages(messages: Sequence[PromptMessage]) -> list[ProviderMessage]:
    """把 Prompt 层的 TypedDict 消息转成可喂给 LLMProvider 的 Pydantic 消息。

    Prompt 层刻意零依赖，产出的是 ``list[dict]``；provider 层要 Pydantic 模型。
    evolve / expert 调 LLM 前都要过这一层，且不能因此依赖 retrieve。
    """
    result: list[ProviderMessage] = []
    for message in messages:
        role: MessageRole = message["role"]
        result.append(ProviderMessage(role=role, content=message["content"]))
    return result
