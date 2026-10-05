"""Pydantic 模型：JSON 字段强类型与序列化往返。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentmem.types import (
    AgentMemModel,
    Citation,
    Document,
    DocumentMeta,
    DocumentUpdate,
    Feedback,
    Insight,
    InsightCreate,
    InsightUpdate,
    KnowledgeCardCreate,
    Message,
    RetrievalSettings,
    SpaceCreate,
    SpaceYaml,
    Trace,
    TraceCreate,
    TraceRetrievedItem,
)


def test_space_create_roundtrip() -> None:
    """Space 创建模型往返不丢字段。"""
    created = SpaceCreate(name="逆向", domain="Android 逆向工程", icon="🔧")
    dumped = created.model_dump_json()
    assert SpaceCreate.model_validate_json(dumped) == created


def test_json_columns_are_strongly_typed() -> None:
    """数据库里的 JSON 字段在模型侧必须是强类型。"""
    card = KnowledgeCardCreate(
        space_id="s1",
        kind="procedure",
        title="脱壳",
        body="先静态后动态",
        aliases=["unpack", "脱壳"],
        source_chunks=["c1", "c2"],
    )
    assert isinstance(card.aliases, list)
    assert all(isinstance(item, str) for item in card.aliases)

    trace = TraceCreate(
        space_id="s1",
        conversation_id="cv1",
        message_id="m1",
        query="怎么脱壳",
        retrieved=[TraceRetrievedItem(chunk_id="c1", vec_score=0.8, rrf=0.5)],
        used_insights=["i1"],
    )
    assert isinstance(trace.retrieved[0], TraceRetrievedItem)
    payload = json.loads(trace.model_dump_json())
    assert payload["retrieved"][0]["chunk_id"] == "c1"
    assert payload["used_insights"] == ["i1"]


def test_nested_models_validate_from_dict() -> None:
    """从数据库行（dict）构造实体时可以校验嵌套结构。"""
    document = Document.model_validate(
        {
            "id": "d1",
            "space_id": "s1",
            "title": "笔记",
            "source_type": "paste",
            "sha256": "abc",
            "status": "ready",
            "meta": {"authors": ["我"], "tags": ["脱壳"], "page_count": 3},
            "created_at": 1,
            "updated_at": 2,
        }
    )
    assert document.meta.authors == ["我"]
    assert document.meta.page_count == 3


def test_document_meta_keeps_unknown_keys() -> None:
    """meta 允许承载未知键，便于后续扩展。"""
    meta = DocumentMeta.model_validate({"authors": [], "custom_field": "x"})
    assert meta.model_extra is not None
    assert meta.model_extra["custom_field"] == "x"


def test_document_update_partial() -> None:
    """更新模型只序列化显式设置的字段。"""
    update = DocumentUpdate(status="failed", error="解析失败")
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"status": "failed", "error": "解析失败"}


def test_extra_fields_are_rejected() -> None:
    """未声明字段直接拒绝，避免前后端字段漂移。"""
    with pytest.raises(PydanticValidationError):
        SpaceCreate.model_validate({"name": "x", "domain": "y", "unknown": 1})


def test_message_citations_typed() -> None:
    """消息引用是嵌套模型而不是裸 dict。"""
    message = Message(
        id="m1",
        conversation_id="c1",
        role="assistant",
        content="答案[^c1]",
        citations=[Citation(marker="c1", chunk_id="k1", document_id="d1", page=3)],
        created_at=1,
    )
    restored = Message.model_validate_json(message.model_dump_json())
    assert restored.citations[0].page == 3


def test_insight_defaults() -> None:
    """经验条目的默认值符合数据模型约定。"""
    insight = InsightCreate(
        space_id="s1",
        trigger="遇到加固 APK",
        guidance="先脱壳",
        kind="heuristic",
        origin="manual",
    )
    assert insight.confidence == 0.3
    assert insight.status == "candidate"
    assert insight.scope == "space"


def test_confidence_bounds_enforced() -> None:
    """入参模型的置信度必须在 0~1。"""
    with pytest.raises(PydanticValidationError):
        KnowledgeCardCreate(
            space_id="s",
            kind="fact",
            title="t",
            body="b",
            confidence=1.5,
        )
    with pytest.raises(PydanticValidationError):
        InsightUpdate(confidence=-0.1)


def test_space_yaml_structure() -> None:
    """space.yaml 的结构化模型覆盖 persona / models / retrieval。"""
    config = SpaceYaml.model_validate(
        {
            "persona": {"name": "逆向专家", "domain": "Android"},
            "models": {"chat": "local-qwen"},
            "retrieval": {"top_k_vector": 30},
        }
    )
    assert config.persona.name == "逆向专家"
    assert config.models["chat"] == "local-qwen"
    assert config.retrieval.top_k_vector == 30
    assert config.retrieval.top_n_rerank == RetrievalSettings().top_n_rerank


def test_all_entities_have_create_update_triples() -> None:
    """§9 约定：每张表都有 XxxCreate / Xxx / XxxUpdate 三件套。"""
    import agentmem.types as types

    names = {name for name in dir(types) if not name.startswith("_")}
    entities = [
        "Space",
        "Document",
        "Chunk",
        "KnowledgeCard",
        "Entity",
        "Relation",
        "Insight",
        "Conversation",
        "Message",
        "Trace",
        "Feedback",
        "EvalItem",
        "EvalRun",
        "UsageRecord",
    ]
    missing = [
        f"{entity}{suffix}"
        for entity in entities
        for suffix in ("Create", "Update")
        if f"{entity}{suffix}" not in names
    ]
    assert not missing, f"缺少模型：{missing}"


def test_base_model_forbids_extra() -> None:
    """全局基类禁止额外字段。"""
    with pytest.raises(PydanticValidationError):
        Feedback.model_validate(
            {
                "id": "f1",
                "trace_id": "t1",
                "kind": "up",
                "created_at": 1,
                "extra": True,
            }
        )
    assert issubclass(Document, AgentMemModel)
    assert issubclass(Trace, AgentMemModel)
    assert issubclass(Insight, AgentMemModel)
