"""AgentMem Prompt 层。

所有与大模型交互的提示词**集中在本包内**，禁止散落到业务逻辑中
（见 `docs/01-ARCHITECTURE.md` §8 编码规范）。这样做的收益：
Prompt 可以单独 diff、单独 A/B、单独测试，而不用在一堆 async 代码里翻找字符串。

本包零依赖：不 import agentmem 的任何其它模块，输入输出都是基本类型与 TypedDict。

模块对应到 `docs/00-VISION.md` 的进化闭环：

    answer       ①Interact  —— 装配 L4/L3/L2/L1 生成回答
    rewrite      ①Interact  —— 检索前的查询改写（指代补全 / 扩展 / HyDE）
    extract      资料侧      —— 从切片抽取 L2 知识卡片与实体关系
    judge        ③Critique  —— LLM-as-Judge 打分（对话评价 + A/B 评测共用）
    distill      ④Distill   —— 从反馈蒸馏 L3 候选经验
    consolidate  ⑤Consolidate —— 去重 / 合并 / 冲突检测
    evalgen      ⑥Evaluate  —— 测验集与领域大纲生成
    persona      L4          —— 专家人格起草
    contextual   资料侧      —— 摄取期的文档级上下文预置（Contextual Retrieval）
"""

from __future__ import annotations

from ._base import extract_json, join, numbered, schema_block, section, truncate
from ._shapes import (
    CardRef,
    ChunkRef,
    InsightRef,
    Message,
    PersonaSpec,
    Turn,
)
from .answer import (
    build_answer_messages,
    build_system_prompt,
    render_evidence,
    render_insights,
    render_persona,
)
from .consolidate import InsightDraft, build_consolidate_messages
from .contextual import build_context_messages, clean_summary
from .distill import FeedbackEpisode, build_distill_messages
from .evalgen import (
    CorrectionSample,
    build_domain_outline_messages,
    build_from_correction_messages,
    build_from_document_messages,
)
from .extract import ExtractSource, build_extract_messages
from .judge import EvalItem, build_eval_judge_messages, build_judge_messages
from .persona import PersonaDraftInput, build_persona_draft_messages
from .rewrite import (
    build_contextualize_messages,
    build_expand_messages,
    build_hyde_messages,
)

__all__ = [
    # 形状
    "CardRef",
    "ChunkRef",
    "CorrectionSample",
    "EvalItem",
    "ExtractSource",
    "FeedbackEpisode",
    "InsightDraft",
    "InsightRef",
    "Message",
    "PersonaDraftInput",
    "PersonaSpec",
    "Turn",
    # ① 问答与检索改写
    "build_answer_messages",
    "build_consolidate_messages",
    "build_context_messages",
    "build_contextualize_messages",
    # ④⑤ 蒸馏与整合
    "build_distill_messages",
    "build_domain_outline_messages",
    "build_eval_judge_messages",
    "build_expand_messages",
    # 资料侧抽取
    "build_extract_messages",
    "build_from_correction_messages",
    # ⑥ 评测集
    "build_from_document_messages",
    "build_hyde_messages",
    # ③ 评价
    "build_judge_messages",
    # L4
    "build_persona_draft_messages",
    "build_system_prompt",
    # 工具
    "clean_summary",
    "extract_json",
    "join",
    "numbered",
    "render_evidence",
    "render_insights",
    "render_persona",
    "schema_block",
    "section",
    "truncate",
]
