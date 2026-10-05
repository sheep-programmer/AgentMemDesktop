"""混合检索 + 重排 + 上下文装配 + 流式问答。

对应 ``docs/01-ARCHITECTURE.md`` §5 的检索管线：

    pipeline   编排：并行召回 → RRF 融合 → 重排 → L3/L2 召回 → 上下文装配
    fusion     Reciprocal Rank Fusion
    diversity  近重复抑制与 MMR 多样性重排
    overlap    相邻切片重叠段的裁剪（只切重复段，修正字符偏移）
    citations  ``[^cN]`` 引用标记的流式解析与注册
    context    core 模型 → Prompt 层形状的映射
    rewriting  查询改写（指代补全 / 多查询扩展 / HyDE）
    chat       对话链路：事件流、消息与轨迹落库
    session    进行中的生成任务登记与中断

Prompt 全部来自 ``agentmem.prompts``，本包只调用、不拼提示词。
"""

from agentmem.retrieve.automerge import merge_siblings
from agentmem.retrieve.chat import ChatService, aclose_stream
from agentmem.retrieve.citations import (
    MARKER_PATTERN,
    CitationEmitter,
    CitationFeed,
    CitationRegistry,
    CitationStreamParser,
    marker_for,
)
from agentmem.retrieve.context import (
    assemble_answer_messages,
    assign_markers,
    to_persona_spec,
    to_retrieved_items,
    to_turns,
)
from agentmem.retrieve.diversity import mmr_rerank, suppress_redundant, text_similarity
from agentmem.retrieve.fusion import RRF_K, FusedHit, reciprocal_rank_fusion
from agentmem.retrieve.intent import looks_like_verbatim_request, needs_contextualize
from agentmem.retrieve.models import (
    ChatEvent,
    DeltaChunk,
    QueryPlan,
    RetrievalResult,
    ScoredChunk,
    SearchOutcome,
)
from agentmem.retrieve.overlap import (
    MIN_OVERLAP_CHARS,
    MIN_REMAINING_TOKENS,
    common_overlap,
    trim_adjacent_overlap,
)
from agentmem.retrieve.pipeline import RetrievalPipeline, document_filter
from agentmem.retrieve.rewriting import QueryRewriter
from agentmem.retrieve.session import (
    GenerationRegistry,
    generation_registry,
    reset_generation_registry,
)

__all__ = [
    "MARKER_PATTERN",
    "MIN_OVERLAP_CHARS",
    "MIN_REMAINING_TOKENS",
    "RRF_K",
    "ChatEvent",
    "ChatService",
    "CitationEmitter",
    "CitationFeed",
    "CitationRegistry",
    "CitationStreamParser",
    "DeltaChunk",
    "FusedHit",
    "GenerationRegistry",
    "QueryPlan",
    "QueryRewriter",
    "RetrievalPipeline",
    "RetrievalResult",
    "ScoredChunk",
    "SearchOutcome",
    "aclose_stream",
    "assemble_answer_messages",
    "assign_markers",
    "common_overlap",
    "document_filter",
    "generation_registry",
    "looks_like_verbatim_request",
    "marker_for",
    "merge_siblings",
    "mmr_rerank",
    "needs_contextualize",
    "reciprocal_rank_fusion",
    "reset_generation_registry",
    "suppress_redundant",
    "text_similarity",
    "to_persona_spec",
    "to_retrieved_items",
    "to_turns",
    "trim_adjacent_overlap",
]
