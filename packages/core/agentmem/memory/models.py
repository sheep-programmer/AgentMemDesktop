"""L2 知识抽取与图谱流程使用的数据模型。

模型返回的结构不可信，因此这一层的模型一律**容忍**多余键（``extra="ignore"``）、
对列表字段接受标量、对置信度做钳位，坏掉的**单个条目**由调用方丢弃并计数，
不牵连整批。

``ExtractedCard`` 承载 Markdown 正文，按全局约定继承 ``ContentModel``，不裁剪空白。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentmem.types import AgentMemModel, CardKind, ContentModel, Id, ProgressEvent

#: 抽取进度回调：接收进度事件（供 SSE 转发）
ExtractionProgressCallback = Callable[[ProgressEvent], Awaitable[None]]

_ModelT = TypeVar("_ModelT", bound=BaseModel)

#: 模型可能用同义词或中文表述给出卡片类型，统一归一到五个规范值
KIND_ALIASES: dict[str, str] = {
    "concept": "concept",
    "概念": "concept",
    "定义": "concept",
    "fact": "fact",
    "事实": "fact",
    "数值": "fact",
    "参数": "fact",
    "procedure": "procedure",
    "流程": "procedure",
    "步骤": "procedure",
    "操作": "procedure",
    "pitfall": "pitfall",
    "坑": "pitfall",
    "注意事项": "pitfall",
    "陷阱": "pitfall",
    "tool": "tool",
    "工具": "tool",
}

#: 实体没有给出类型时的兜底分类
DEFAULT_ENTITY_TYPE = "其他"


class _TolerantModel(AgentMemModel):
    """模型输出的基类：忽略多余键，缺键由默认值兜底。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class _TolerantContentModel(ContentModel):
    """承载正文的模型输出：忽略多余键，且不裁剪空白。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


def _as_str_list(value: object) -> list[str]:
    """把可能是标量、``None`` 或混合列表的字段归一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
        return [item for item in items if item]
    return []


class ExtractedCard(_TolerantContentModel):
    """模型抽出的一张知识卡片。"""

    kind: CardKind
    title: str = Field(min_length=1)
    body: str = Field(min_length=1, description="Markdown 正文")
    aliases: list[str] = Field(default_factory=list)
    source_chunk_markers: list[str] = Field(default_factory=list, description="如 c1 / c2")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> object:
        """把同义词与中英文混写归一到规范值；仍不认识的值交给校验报错。"""
        if isinstance(value, str):
            key = value.strip().lower()
            return KIND_ALIASES.get(key, key)
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> object:
        """钳位到 0~1；无法解析的值按默认置信度处理。"""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return 0.5
        try:
            number = float(value)
        except ValueError:
            return 0.5
        return min(1.0, max(0.0, number))

    @field_validator("aliases", "source_chunk_markers", mode="before")
    @classmethod
    def _coerce_lists(cls, value: object) -> object:
        """接受标量或 ``None``，避免整张卡片因为一个列表字段被丢弃。"""
        return _as_str_list(value)


class ExtractedEntity(_TolerantModel):
    """模型抽出的一个实体。"""

    name: str = Field(min_length=1)
    type: str = Field(default=DEFAULT_ENTITY_TYPE, min_length=1, description="领域自定义类型")
    summary: str | None = None
    source_chunk_markers: list[str] = Field(default_factory=list)

    @field_validator("source_chunk_markers", mode="before")
    @classmethod
    def _coerce_markers(cls, value: object) -> object:
        """接受标量或 ``None``。"""
        return _as_str_list(value)


class ExtractedRelation(_TolerantModel):
    """模型抽出的一条实体关系；两端用**实体名**给出，落库前需解析成 id。"""

    src: str = Field(min_length=1)
    dst: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    source_chunk_markers: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)

    @field_validator("source_chunk_markers", mode="before")
    @classmethod
    def _coerce_markers(cls, value: object) -> object:
        """接受标量或 ``None``。"""
        return _as_str_list(value)


class ExtractionPayload(_TolerantModel):
    """一次抽取请求的完整返回体。"""

    cards: list[ExtractedCard] = Field(default_factory=list)
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)

    @field_validator("cards", "entities", "relations", mode="before")
    @classmethod
    def _coerce_sections(cls, value: object) -> object:
        """某个分组缺失或为 ``null`` 时按空列表处理。"""
        return value if isinstance(value, list) else []


class ExtractionStats(AgentMemModel):
    """单个文档的抽取统计。"""

    document_id: Id
    document_title: str = ""
    batches: int = 0
    batches_failed: int = 0
    cards_created: int = 0
    cards_updated: int = 0
    cards_skipped: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    relations_created: int = 0
    relations_skipped: int = 0
    skipped_reason: str | None = Field(default=None, description="未执行抽取的原因，如 no_chunks")
    embedding_error: str | None = Field(default=None, description="卡片向量化失败的原因")


class ExtractionFailure(AgentMemModel):
    """一个文档抽取失败的原因。"""

    document_id: Id
    code: str
    message: str


class ExtractionReport(AgentMemModel):
    """一次抽取任务（可能含多个文档）的汇总结果。"""

    space_id: Id
    documents: list[ExtractionStats] = Field(default_factory=list)
    failures: list[ExtractionFailure] = Field(default_factory=list)
    duration_ms: int = 0
    cards_created: int = 0
    cards_updated: int = 0
    entities_created: int = 0
    relations_created: int = 0

    def absorb(self, stats: ExtractionStats) -> None:
        """把单个文档的统计并入总量。"""
        self.cards_created += stats.cards_created
        self.cards_updated += stats.cards_updated
        self.entities_created += stats.entities_created
        self.relations_created += stats.relations_created


class VectorSyncResult(AgentMemModel):
    """卡片向量同步的结果：失败原因一并带出，由调用方决定是告警还是中断。"""

    embedded: int = 0
    skipped: bool = Field(default=False, description="未配置 embedding 角色而跳过")
    error: str | None = None


class MemoryEvent(AgentMemModel):
    """一条待推送的记忆层事件：事件名 + 载荷模型。

    core 不认识 SSE（不得依赖 Web 框架），只产出事件名与 Pydantic 载荷，
    分帧由 ``apps/api`` 负责。
    """

    name: str
    payload: BaseModel
