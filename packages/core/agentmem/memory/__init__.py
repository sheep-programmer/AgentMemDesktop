"""L2 记忆层：知识卡片抽取、实体关系图谱与卡片读写。

对外入口：

    KnowledgeExtractor  从 L1 切片抽取卡片 / 实体 / 关系
    CardService         卡片的去重、人工校订与向量同步
    build_graph         实体关系图聚合查询
"""

from __future__ import annotations

from .cards import CardService, embedding_text
from .extract import (
    DEFAULT_EXTRACT_ROLE,
    KnowledgeExtractor,
    batch_chunks,
    batch_refs,
    load_persona_spec,
    parse_payload,
    resolve_markers,
)
from .graph import build_graph
from .models import (
    ExtractedCard,
    ExtractedEntity,
    ExtractedRelation,
    ExtractionFailure,
    ExtractionPayload,
    ExtractionProgressCallback,
    ExtractionReport,
    ExtractionStats,
    MemoryEvent,
    VectorSyncResult,
)

__all__ = [
    "DEFAULT_EXTRACT_ROLE",
    "CardService",
    "ExtractedCard",
    "ExtractedEntity",
    "ExtractedRelation",
    "ExtractionFailure",
    "ExtractionPayload",
    "ExtractionProgressCallback",
    "ExtractionReport",
    "ExtractionStats",
    "KnowledgeExtractor",
    "MemoryEvent",
    "VectorSyncResult",
    "batch_chunks",
    "batch_refs",
    "build_graph",
    "embedding_text",
    "load_persona_spec",
    "parse_payload",
    "resolve_markers",
]
