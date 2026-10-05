"""查询改写：把一次提问加工成更适合检索的检索式。

三种策略（Prompt 来自 ``agentmem.prompts.rewrite``，本模块只负责调用与解析）：

- ``contextualize`` 多轮对话里补全指代，否则向量召回几乎必然跑偏；
- ``expand``        一问扩多问，覆盖不同角度；
- ``hyde``          先写假想答案再用答案检索，产物**只用于召回**。

降级原则：改写是**增值步骤**，任何一步失败（角色未绑定、服务不可用、JSON 解析不了）
都退回原检索式并记日志，绝不让整条检索链因为改写失败而中断。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import structlog

from agentmem.errors import AgentMemError
from agentmem.prompts import Message as PromptMessage
from agentmem.prompts import (
    PersonaSpec,
    Turn,
    build_contextualize_messages,
    build_expand_messages,
    build_hyde_messages,
    extract_json,
)
from agentmem.providers.registry import LLMRoute
from agentmem.types import RoleName

from .context import to_provider_messages

logger = structlog.get_logger(__name__)

#: 改写类任务固定用这个角色：快、便宜，且不占用主对话模型的配额
REWRITE_ROLE: RoleName = "fast"

#: 改写请求的温度。改写要的是稳定复现，不是创造力
REWRITE_TEMPERATURE = 0.0

#: 多查询扩展的条数
EXPAND_COUNT = 3

#: 改写请求的推理强度提示。改写是「照规则改一句话」的机械任务，推理模型默认先想再答：
#: 实测 agnes-2.5-flash 一次指代补全要先吐 75~90 个推理 token，首字 3.6 秒；
#: 要 ``none`` 时 1.2 秒，输出的 JSON 一字不差。不认这个参数的服务端会被适配器自动退回。
REWRITE_REASONING_EFFORT = "none"


class LLMRoleSource(Protocol):
    """能从角色名取到 LLM 的东西——:class:`ProviderRegistry` 满足这个形状。

    检索包只依赖这一点，改写逻辑因此可以脱离真实 provider 单独测试。
    """

    def llm(self, role: RoleName = "chat", *, purpose: str | None = None) -> LLMRoute: ...


class QueryRewriter:
    """查询改写的调用与容错。"""

    def __init__(self, registry: LLMRoleSource) -> None:
        self.registry = registry

    async def contextualize(self, *, question: str, history: Sequence[Turn]) -> str | None:
        """多轮对话中把指代补全成可独立检索的问题。

        Returns:
            改写后的问题；历史为空、模型认为无需改写或调用失败时返回 ``None``。
        """
        if not history:
            return None
        payload = await self._complete_json(
            build_contextualize_messages(question=question, history=list(history)),
            purpose="contextualize",
        )
        if payload is None or payload.get("changed") is False:
            return None
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            return None
        rewritten = query.strip()
        return None if rewritten == question else rewritten

    async def expand(
        self, *, persona: PersonaSpec, question: str, n: int = EXPAND_COUNT
    ) -> list[str]:
        """一问扩多问；失败返回空列表。"""
        payload = await self._complete_json(
            build_expand_messages(persona=persona, question=question, n=n),
            purpose="expand",
        )
        if payload is None:
            return []
        queries = payload.get("queries")
        if not isinstance(queries, list):
            return []
        result: list[str] = []
        for item in queries:
            if not isinstance(item, str):
                continue
            text = item.strip()
            # 提示词要求彼此有实质差异，但模型仍可能重复；重复的检索式
            # 会让同一路召回在 RRF 里被计两次，等于偷偷加权
            if text and text != question and text not in result:
                result.append(text)
        return result[:n]

    async def hyde(self, *, persona: PersonaSpec, question: str) -> str | None:
        """生成假想答案；失败返回 ``None``。

        ⚠️ 返回值只用于向量召回，绝不能进入最终回答的上下文或引用。
        """
        content = await self._complete(
            build_hyde_messages(persona=persona, question=question), purpose="hyde"
        )
        if content is None:
            return None
        text = content.strip()
        return text or None

    async def _complete_json(
        self, messages: list[PromptMessage], *, purpose: str
    ) -> dict[str, Any] | None:
        """要求模型返回 JSON 的改写调用。"""
        content = await self._complete(messages, purpose=purpose)
        if content is None:
            return None
        try:
            payload = extract_json(content)
        except ValueError:
            logger.warning("rewrite_json_invalid", purpose=purpose, preview=content[:200])
            return None
        return payload

    async def _complete(self, messages: list[PromptMessage], *, purpose: str) -> str | None:
        """发起一次改写请求；任何失败都降级为 ``None``。"""
        try:
            route: LLMRoute = self.registry.llm(REWRITE_ROLE, purpose=purpose)
            result = await route.chat(
                to_provider_messages(messages),
                temperature=REWRITE_TEMPERATURE,
                reasoning_effort=REWRITE_REASONING_EFFORT,
            )
        except AgentMemError as exc:
            logger.warning("rewrite_failed", purpose=purpose, code=exc.code, error=exc.message)
            return None
        except Exception:  # pragma: no cover - 适配器层的意外错误
            logger.exception("rewrite_crashed", purpose=purpose)
            return None
        return result.content
