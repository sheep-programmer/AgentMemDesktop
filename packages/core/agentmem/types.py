"""全局 Pydantic v2 数据模型。

按 ``02-DATA-MODEL.md`` 的 §1~§8 顺序分节定义，每个实体三件套：
``XxxCreate`` / ``Xxx`` / ``XxxUpdate``。

约定：
- 主键统一 ULID 字符串，时间统一 Unix 毫秒时间戳（UTC）。
- 数据库中以 JSON 字符串存储的字段（``source_chunks`` / ``aliases`` / ``retrieved`` 等）
  在此处一律为强类型；JSON 编解码由 ``agentmem.store`` 负责，不向调用方暴露字符串。
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field

Id: TypeAlias = str  # 主键：ULID，字典序即时间序
Timestamp: TypeAlias = int  # Unix 毫秒时间戳（UTC）

# ---------------------------------------------------------------------------
# 枚举（用 Literal 表达，便于 OpenAPI 生成枚举）
# ---------------------------------------------------------------------------

DocumentSourceType = Literal["file", "url", "paste", "conversation"]
DocumentStatus = Literal[
    "pending", "parsing", "chunking", "embedding", "extracting", "ready", "failed"
]
CardKind = Literal["concept", "fact", "procedure", "pitfall", "tool"]
ChunkKind = Literal["body", "summary"]  # 正文切片 / 文档概要切片
VerifiedBy = Literal["user", "eval"]
InsightKind = Literal["correction", "preference", "heuristic", "constraint", "terminology"]
InsightScope = Literal["space", "global"]
InsightStatus = Literal["candidate", "active", "conflicted", "archived"]
ConsistencySource = Literal["probe", "proxy"]  # 实测 / 代理指标
InsightOrigin = Literal[
    "user_correction", "negative_feedback", "positive_feedback", "judge", "manual"
]
MessageRole = Literal["user", "assistant", "system", "tool"]
FeedbackKind = Literal["up", "down", "correction", "edit"]
EvalVariant = Literal["baseline", "with_insights", "custom"]
EvalSource = Literal["manual", "auto_from_doc", "from_correction"]
UsageKind = Literal["llm", "embedding", "rerank"]
SearchMode = Literal["hybrid", "vector", "fts"]
ProviderKind = Literal["llm", "embedding", "rerank"]
RoleName = Literal["chat", "fast", "distill", "judge", "embedding", "rerank"]


class AgentMemModel(BaseModel):
    """全局模型基类：禁止未声明字段，字符串值自动去除首尾空白。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ContentModel(BaseModel):
    """承载**正文**的模型：禁止未声明字段，但**绝不裁剪空白**。

    裁剪空白对 name / title / domain 这类字段是好事，对正文则会造成实际损坏：

    - 切片正文被裁剪后，与 ``char_start`` / ``char_end`` 偏移对不上，引用定位会错位；
    - 代码块、Markdown 缩进的前导空格被吃掉；
    - 消息正文首尾的换行丢失。

    凡是字段里装 content / body / markdown / snippet 的模型，都继承本类。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# ===========================================================================
# §1 Space（知识库空间）
# ===========================================================================


class SpaceCreate(AgentMemModel):
    """新建 Space 的入参。"""

    name: str = Field(min_length=1, description="Space 名称")
    domain: str = Field(min_length=1, description="领域描述，如 'Android 逆向工程'")
    icon: str | None = Field(default=None, description="emoji 或图标 key")
    color: str | None = Field(default=None, description="主题色 hex")
    description: str | None = Field(default=None, description="一句话简介")


class SpaceUpdate(AgentMemModel):
    """Space 部分更新，全部字段可选。"""

    name: str | None = Field(default=None, min_length=1)
    domain: str | None = Field(default=None, min_length=1)
    icon: str | None = None
    color: str | None = None
    description: str | None = None


class Space(AgentMemModel):
    """Space 完整实体（对应 spaces 表）。"""

    id: Id
    name: str
    domain: str
    icon: str | None = None
    color: str | None = None
    description: str | None = None
    created_at: Timestamp
    updated_at: Timestamp


# ===========================================================================
# §2 L0 / L1：文档与切片
# ===========================================================================


class DocumentMeta(AgentMemModel):
    """documents.meta 的结构化内容；未知键原样保留。"""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    authors: list[str] = Field(default_factory=list, description="作者")
    page_count: int | None = Field(default=None, description="页数")
    tags: list[str] = Field(default_factory=list, description="标签")
    language: str | None = Field(default=None, description="语言代码")
    source_url: str | None = Field(default=None, description="抓取来源 URL")
    raw_path: str | None = Field(default=None, description="原始文件在磁盘上的路径")
    context_summary: str | None = Field(
        default=None,
        description="文档级上下文：只拼在索引与嵌入的文本前，不进切片正文",
    )
    chunker_version: int | None = Field(
        default=None,
        description="产出这些切片时的切片算法版本；与当前版本不一致即需要重建索引",
    )


class DocumentCreate(AgentMemModel):
    """登记文档（落盘前的元数据）。"""

    space_id: Id
    title: str = Field(min_length=1)
    source_type: DocumentSourceType
    source_uri: str | None = None
    mime: str | None = None
    sha256: str = Field(min_length=1, description="去重依据")
    size_bytes: int | None = None
    status: DocumentStatus = "pending"
    error: str | None = None
    meta: DocumentMeta = Field(default_factory=DocumentMeta)
    token_count: int = 0


class DocumentUpdate(AgentMemModel):
    """文档部分更新。"""

    title: str | None = Field(default=None, min_length=1)
    source_type: DocumentSourceType | None = None
    source_uri: str | None = None
    mime: str | None = None
    status: DocumentStatus | None = None
    error: str | None = None
    meta: DocumentMeta | None = None
    token_count: int | None = None


class Document(AgentMemModel):
    """文档完整实体（对应 documents 表）。"""

    id: Id
    space_id: Id
    title: str
    source_type: DocumentSourceType
    source_uri: str | None = None
    mime: str | None = None
    sha256: str
    size_bytes: int | None = None
    status: DocumentStatus
    error: str | None = None
    meta: DocumentMeta = Field(default_factory=DocumentMeta)
    token_count: int = 0
    created_at: Timestamp
    updated_at: Timestamp


class ChunkCreate(ContentModel):
    """写入切片。"""

    space_id: Id
    document_id: Id
    ordinal: int = Field(ge=0, description="文档内序号")
    content: str = Field(min_length=1)
    heading_path: str | None = Field(default=None, description="如 '第3章 > 3.2 脱壳'")
    page: int | None = Field(default=None, description="PDF 页码")
    char_start: int | None = None
    char_end: int | None = None
    token_count: int | None = None
    kind: ChunkKind = Field(
        default="body",
        description="body=正文切片；summary=文档概要切片（没有可定位的原文区间）",
    )


class ChunkUpdate(ContentModel):
    """切片部分更新。"""

    ordinal: int | None = Field(default=None, ge=0)
    content: str | None = Field(default=None, min_length=1)
    heading_path: str | None = None
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    token_count: int | None = None


class Chunk(ContentModel):
    """切片完整实体（对应 chunks 表）。"""

    id: Id
    space_id: Id
    document_id: Id
    ordinal: int
    content: str
    heading_path: str | None = None
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    token_count: int | None = None
    kind: ChunkKind = Field(
        default="body", description="body=正文切片；summary=文档概要（char_start = char_end = 0）"
    )
    created_at: Timestamp


# ===========================================================================
# §3 L2：知识卡片与实体关系
# ===========================================================================


class KnowledgeCardCreate(ContentModel):
    """新建知识卡片。"""

    space_id: Id
    kind: CardKind
    title: str = Field(min_length=1)
    body: str = Field(min_length=1, description="Markdown 正文")
    aliases: list[str] = Field(default_factory=list, description="术语同义词")
    source_chunks: list[str] = Field(default_factory=list, description="来源 chunk_id")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified_by: VerifiedBy | None = None


class KnowledgeCardUpdate(ContentModel):
    """知识卡片部分更新。"""

    kind: CardKind | None = None
    title: str | None = Field(default=None, min_length=1)
    body: str | None = Field(default=None, min_length=1)
    aliases: list[str] | None = None
    source_chunks: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    verified_by: VerifiedBy | None = None


class KnowledgeCard(ContentModel):
    """知识卡片完整实体（对应 knowledge_cards 表）。"""

    id: Id
    space_id: Id
    kind: CardKind
    title: str
    body: str
    aliases: list[str] = Field(default_factory=list)
    source_chunks: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    verified_by: VerifiedBy | None = None
    created_at: Timestamp
    updated_at: Timestamp


class CardVersion(AgentMemModel):
    """知识卡片的一个**历史版本**（被新版本取代后的快照）。

    L2 卡片是「事实」，而事实会变：新版临床指南把推荐剂量从 400mg 改到 200mg，
    旧值不该被静默覆盖——「什么时候改的、原来是多少」恰是医药与合规场景里最该
    看得到的信息。当前版本仍存在 ``knowledge_cards`` 里，这张表只放被取代的旧版本。
    """

    id: Id
    card_id: Id
    space_id: Id
    version: int = Field(ge=1, description="第几版，从 1 开始数")
    kind: CardKind
    title: str
    body: str
    aliases: list[str] = Field(default_factory=list)
    source_chunks: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    verified_by: VerifiedBy | None = None
    valid_from: Timestamp = Field(description="这一版从什么时候开始生效")
    valid_to: Timestamp = Field(description="这一版到什么时候被取代")
    created_at: Timestamp


class CardHistoryResponse(AgentMemModel):
    """``GET /spaces/{id}/cards/{cid}/versions`` 响应。"""

    card_id: Id
    card: KnowledgeCard = Field(description="当前版本")
    versions: list[CardVersion] = Field(default_factory=list, description="历史版本，新的在前")


class InsightEventCreate(AgentMemModel):
    """写入一条经验置信度变更流水。"""

    insight_id: Id
    space_id: Id
    event: str = Field(description="触发原因：user_confirm / positive_feedback / eval_improved …")
    confidence_before: float | None = Field(default=None, description="变更前；新建时为 None")
    confidence_after: float = Field(ge=0.0, le=1.0)
    status_before: InsightStatus | None = None
    status_after: InsightStatus
    share: float | None = Field(default=None, description="反馈类事件的均摊份额")
    reason: str | None = Field(default=None, description="可读说明，例如「A/B +2.5」")


class InsightEvent(InsightEventCreate):
    """一条经验置信度变更流水（对应 insight_events 表）。"""

    id: Id
    created_at: Timestamp


class InsightHistoryResponse(AgentMemModel):
    """``GET /spaces/{id}/insights/{iid}/history`` 响应。"""

    insight_id: Id
    insight: Insight
    events: list[InsightEvent] = Field(
        default_factory=list, description="变更流水，旧的在前（按时间顺序读就是它的经历）"
    )


class ConsistencyQuestionScore(AgentMemModel):
    """单个问题的稳定性：重复回答之间的平均语义相似度。"""

    question: str
    similarity: float = Field(ge=0.0, le=1.0)
    answers: list[str] = Field(default_factory=list, description="重复生成的答案，供人工核对")


class ConsistencyProbeCreate(AgentMemModel):
    """写入一次一致性探测。"""

    space_id: Id
    questions: int = Field(ge=1)
    repeats: int = Field(ge=2)
    similarity: float = Field(ge=0.0, le=1.0)
    detail: list[ConsistencyQuestionScore] = Field(default_factory=list)


class ConsistencyProbe(AgentMemModel):
    """一次一致性探测的完整记录（对应 consistency_probes 表）。"""

    id: Id
    space_id: Id
    questions: int = Field(ge=1)
    repeats: int = Field(ge=2)
    similarity: float = Field(ge=0.0, le=1.0)
    detail: list[ConsistencyQuestionScore] = Field(default_factory=list)
    created_at: Timestamp


class EntityCreate(AgentMemModel):
    """新建实体。"""

    space_id: Id
    name: str = Field(min_length=1)
    type: str = Field(min_length=1, description="领域自定义：工具/协议/漏洞/人物…")
    summary: str | None = None
    card_id: Id | None = None
    mention_count: int = 0


class EntityUpdate(AgentMemModel):
    """实体部分更新。"""

    name: str | None = Field(default=None, min_length=1)
    type: str | None = Field(default=None, min_length=1)
    summary: str | None = None
    card_id: Id | None = None
    mention_count: int | None = None


class Entity(AgentMemModel):
    """实体完整实体（对应 entities 表）。"""

    id: Id
    space_id: Id
    name: str
    type: str
    summary: str | None = None
    card_id: Id | None = None
    mention_count: int = Field(
        default=0, description="被多少篇文档提到过；由 entity_mentions 明细重算，不做增量累加"
    )
    created_at: Timestamp


class RelationCreate(AgentMemModel):
    """新建实体关系。"""

    space_id: Id
    src_id: Id
    dst_id: Id
    predicate: str = Field(min_length=1, description="如 '用于' / '依赖' / '对抗'")
    weight: float = 1.0
    source_chunks: list[str] = Field(default_factory=list)


class RelationUpdate(AgentMemModel):
    """实体关系部分更新。"""

    predicate: str | None = Field(default=None, min_length=1)
    weight: float | None = None
    source_chunks: list[str] | None = None


class Relation(AgentMemModel):
    """实体关系完整实体（对应 relations 表）。"""

    id: Id
    space_id: Id
    src_id: Id
    dst_id: Id
    predicate: str
    weight: float = 1.0
    source_chunks: list[str] = Field(default_factory=list)
    created_at: Timestamp


# ===========================================================================
# §4 L3：经验条目
# ===========================================================================


class InsightCreate(AgentMemModel):
    """新建经验条目。"""

    space_id: Id
    trigger: str = Field(min_length=1, description="什么情况下适用")
    guidance: str = Field(min_length=1, description="应该怎么做")
    rationale: str | None = Field(default=None, description="为什么")
    kind: InsightKind
    scope: InsightScope = "space"
    confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    status: InsightStatus = "candidate"
    origin: InsightOrigin
    applied_count: int = 0
    success_count: int = 0
    eval_delta: float | None = None
    source_trace_ids: list[str] = Field(default_factory=list)
    supersedes: Id | None = None


class InsightUpdate(AgentMemModel):
    """经验条目部分更新。"""

    trigger: str | None = Field(default=None, min_length=1)
    guidance: str | None = Field(default=None, min_length=1)
    rationale: str | None = None
    kind: InsightKind | None = None
    scope: InsightScope | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: InsightStatus | None = None
    origin: InsightOrigin | None = None
    applied_count: int | None = None
    success_count: int | None = None
    eval_delta: float | None = None
    source_trace_ids: list[str] | None = None
    supersedes: Id | None = None


class Insight(AgentMemModel):
    """经验条目完整实体（对应 insights 表）。"""

    id: Id
    space_id: Id
    trigger: str
    guidance: str
    rationale: str | None = None
    kind: InsightKind
    scope: InsightScope = "space"
    confidence: float = 0.3
    status: InsightStatus = "candidate"
    origin: InsightOrigin
    applied_count: int = 0
    success_count: int = 0
    eval_delta: float | None = None
    source_trace_ids: list[str] = Field(default_factory=list)
    supersedes: Id | None = None
    created_at: Timestamp
    updated_at: Timestamp


# ===========================================================================
# §5 对话、轨迹与反馈
# ===========================================================================


class Citation(ContentModel):
    """回答中的引用标记与原文定位。"""

    marker: str = Field(description="正文中的标记，如 'c3'")
    chunk_id: Id
    document_id: Id
    document_title: str | None = Field(
        default=None, description="来源文档标题；界面用它说明这条引用出自哪篇资料"
    )
    page: int | None = None
    heading_path: str | None = Field(
        default=None, description="切片所在的章节路径（如「一、概况 > 历史沿革」），界面据此标出处"
    )
    ordinal: int | None = Field(default=None, description="切片在文档内的序号（第几段）")
    char_start: int | None = Field(default=None, description="切片在原文里的起始字符位置")
    char_end: int | None = Field(default=None, description="切片在原文里的结束字符位置")
    kind: ChunkKind = Field(
        default="body", description="body=正文切片；summary=文档概要（没有原文区间）"
    )
    snippet: str | None = None
    char_offset: int | None = Field(
        default=None,
        description="该标记在回答正文里的字符位置；正文存的是剥离标记后的文本，"
        "这个位置就是剥离后的坐标。用于算「带引用的句子占比」",
    )
    quote: str | None = Field(
        default=None,
        description="切片里真正支撑这句回答的原句；切片常跨多个小节，界面优先展示并高亮这一句",
    )
    quote_start: int | None = Field(default=None, description="原句在全文里的起始字符位置")
    quote_end: int | None = Field(default=None, description="原句在全文里的结束字符位置")


class ConversationCreate(AgentMemModel):
    """新建会话。"""

    space_id: Id
    title: str = Field(default="新对话", min_length=1)
    pinned: bool = False


class ConversationUpdate(AgentMemModel):
    """会话部分更新。"""

    title: str | None = Field(default=None, min_length=1)
    pinned: bool | None = None


class Conversation(AgentMemModel):
    """会话完整实体（对应 conversations 表）。"""

    id: Id
    space_id: Id
    title: str
    pinned: bool = False
    created_at: Timestamp
    updated_at: Timestamp


class MessageCreate(ContentModel):
    """写入消息。"""

    conversation_id: Id
    role: MessageRole
    content: str
    citations: list[Citation] = Field(default_factory=list)


class MessageUpdate(ContentModel):
    """消息部分更新。"""

    content: str | None = None
    citations: list[Citation] | None = None


class Message(ContentModel):
    """消息完整实体（对应 messages 表）。"""

    id: Id
    conversation_id: Id
    role: MessageRole
    content: str
    citations: list[Citation] = Field(default_factory=list)
    trace_id: Id | None = Field(
        default=None,
        description="这条回答对应的轨迹 id。由 traces 表按 message_id 关联读出，"
        "不落 messages 表——同一条消息只会有一条轨迹",
    )
    feedback: list[FeedbackKind] = Field(
        default_factory=list,
        description="这条回答收到过的反馈种类（按时间顺序，可重复）。"
        "同样读时关联，不落 messages 表；此前前端只记在 sessionStorage，刷新就忘了点过赞",
    )
    created_at: Timestamp


class TraceRetrievedItem(AgentMemModel):
    """轨迹中单条检索命中的各路分数。

    落库时只存 id 与分数；``document_title`` / ``snippet`` / ``kind`` 是读接口按
    ``chunk_id`` 回表补齐的——证据面板要显示「这条证据出自哪篇、内容是什么」，
    只给一个 id 的话界面上全是「未命名文档」。
    """

    chunk_id: Id
    document_id: Id | None = None
    document_title: str | None = None
    snippet: str | None = None
    kind: ChunkKind = "body"
    page: int | None = None
    heading_path: str | None = Field(default=None, description="切片所在章节路径，读接口回表补齐")
    ordinal: int | None = Field(default=None, description="切片在文档内的序号，读接口回表补齐")
    quote_start: int | None = Field(
        default=None, description="切片里与问题最贴近的那句在全文里的起始位置，读接口现算"
    )
    quote_end: int | None = Field(default=None, description="那句的结束位置，读接口现算")
    merged_from: list[Id] = Field(
        default_factory=list,
        description="这条证据由哪几条切片合并而来（同一节里连号的兄弟切片）；为空即原始切片",
    )
    vec_score: float | None = None
    bm25_score: float | None = None
    rrf: float | None = None
    rerank_score: float | None = None
    legs: list[str] = Field(
        default_factory=list, description="召回这条路命中的召回路径（向量 / 全文 / 图谱）"
    )


class TraceCreate(AgentMemModel):
    """记录一次回答轨迹。

    ``id`` 可以由调用方预先指定：对话在生成开始前就要把 trace_id 发给前端
    （SSE 的 ``trace_start``），后续的反馈与「为什么这么答」面板都拿它回查，
    所以落库时必须沿用同一个 id，不能另生成一个。
    """

    id: Id | None = None
    space_id: Id
    conversation_id: Id
    message_id: Id
    query: str
    rewritten_query: str | None = None
    retrieved: list[TraceRetrievedItem] = Field(default_factory=list)
    used_insights: list[str] = Field(default_factory=list)
    used_cards: list[str] = Field(default_factory=list)
    llm_role: str | None = None
    provider_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


class TraceUpdate(AgentMemModel):
    """轨迹部分更新。"""

    rewritten_query: str | None = None
    retrieved: list[TraceRetrievedItem] | None = None
    used_insights: list[str] | None = None
    used_cards: list[str] | None = None
    llm_role: str | None = None
    provider_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


class Trace(AgentMemModel):
    """轨迹完整实体（对应 traces 表）。"""

    id: Id
    space_id: Id
    conversation_id: Id
    message_id: Id
    query: str
    rewritten_query: str | None = None
    retrieved: list[TraceRetrievedItem] = Field(default_factory=list)
    used_insights: list[str] = Field(default_factory=list)
    used_cards: list[str] = Field(default_factory=list)
    llm_role: str | None = None
    provider_id: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    created_at: Timestamp


class TraceView(Trace):
    """``GET /traces/{id}`` 响应：轨迹 + 按 id 回表补全的经验。

    轨迹只存经验 id。此前前端拿到 id 就当条件文案显示，对策兜底成一句套话，
    作用域一律标「全局」——同一条经验流式时是真内容，刷新后变成假文案。
    查不到的（经验已被删）不在这里，前端按 ``used_insights`` 与本列表的差集如实标出。
    """

    insight_details: list[Insight] = Field(default_factory=list)


class FeedbackCreate(AgentMemModel):
    """写入反馈。"""

    trace_id: Id
    kind: FeedbackKind
    comment: str | None = None
    judge_score: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_reason: str | None = None
    distilled: bool = False


class FeedbackUpdate(AgentMemModel):
    """反馈部分更新。"""

    kind: FeedbackKind | None = None
    comment: str | None = None
    judge_score: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_reason: str | None = None
    distilled: bool | None = None


class Feedback(AgentMemModel):
    """反馈完整实体（对应 feedback 表）。"""

    id: Id
    trace_id: Id
    kind: FeedbackKind
    comment: str | None = None
    judge_score: float | None = None
    judge_reason: str | None = None
    distilled: bool = False
    created_at: Timestamp


# ===========================================================================
# §6 L4：专家画像（space.yaml，不入库）
# ===========================================================================


class OutputStyle(AgentMemModel):
    """输出风格约束。"""

    language: str = "zh-CN"
    tone: str = "简洁、技术化、不废话"
    must_cite: bool = True


class Persona(AgentMemModel):
    """L4 人格画像。"""

    name: str = "领域专家"
    domain: str = ""
    role_description: str = ""
    principles: list[str] = Field(default_factory=list)
    output_style: OutputStyle = Field(default_factory=OutputStyle)
    quality_bar: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict, description="术语表")


class RetrievalSettings(AgentMemModel):
    """Space 级检索参数。"""

    top_k_vector: int = Field(default=50, ge=1)
    top_k_fts: int = Field(default=50, ge=1)
    top_n_rerank: int = Field(default=8, ge=1)
    rerank_pool: int = Field(
        default=50,
        ge=1,
        le=200,
        description="参与重排的候选数上限。重排耗时与候选数近似成正比（本机实测 50 条约 "
        "23 秒、30 条约 13 秒），但调小会真的漏证据：同一套黄金评测集上把 50 降到 30，"
        "上下文召回从 1.0 掉到 0.875，总分从 81.6 掉到 80.1。除非检索慢到不可用，"
        "不建议调小",
    )
    relevance_floor: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="问答时，重排分低于本轮最高分这个比例的证据不送给回答模型（0 关闭）。"
        "只在重排给出 0~1 的相关概率、且本轮最高分不低于 0.5 时生效（宽泛的概览题不裁）；"
        "本机 39 题实测挡掉约两成证据（多为 0.000~0.06 的无关表格行），评测集里可核对的"
        "要点一条未丢。检索调试接口不受影响",
    )
    max_insights: int = Field(default=6, ge=0)
    hyde: bool = False
    diversity: bool = Field(default=True, description="检索结果去重 + MMR 多样性重排总开关")
    mmr_lambda: float = Field(default=0.7, ge=0.0, le=1.0, description="MMR 相关性权重")
    dedup_threshold: float = Field(default=0.85, ge=0.0, le=1.0, description="判为近重复的重合度")
    auto_merge: bool = Field(
        default=True,
        description="同一节里连号命中的切片并回父节点：省掉重复的标题抬头与引用编号，"
        "也不再把一句话从中间劈开。只合并真正相邻的，中间缺一条就不合",
    )
    graph_expansion: bool = Field(
        default=False,
        description="把知识图谱上相邻实体所在的切片并进候选池。默认关：它会改变检索"
        "结果，应当在 EvalSet 的多臂对比里证明有效之后再打开",
    )
    contextualize_when_needed: bool = Field(
        default=True,
        description="只在问题依赖上文（含指代、省略）时才用历史改写。改写是一次完整"
        "模型调用，自足的问题改写只是白等",
    )
    skip_insights_for_verbatim: bool = Field(
        default=True,
        description="用户在索取原文（「把第三章原文贴出来」）时不注入 L3 经验——"
        "那类问题没有「场景」，经验只会占预算并带偏措辞",
    )


class SpaceYaml(AgentMemModel):
    """``space.yaml`` 的完整结构。"""

    persona: Persona = Field(default_factory=Persona)
    models: dict[str, str] = Field(default_factory=dict, description="覆盖全局 roles，可选")
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)


# ===========================================================================
# §7 专家度与评测
# ===========================================================================


class EvalItemCreate(AgentMemModel):
    """新建测验题。"""

    space_id: Id
    question: str = Field(min_length=1)
    reference: str | None = Field(default=None, description="参考答案，可空")
    must_include: list[str] = Field(default_factory=list, description="必须出现的关键点")
    tags: list[str] = Field(default_factory=list)
    source: EvalSource = "manual"


class EvalItemUpdate(AgentMemModel):
    """测验题部分更新。"""

    question: str | None = Field(default=None, min_length=1)
    reference: str | None = None
    must_include: list[str] | None = None
    tags: list[str] | None = None
    source: EvalSource | None = None


class EvalItem(AgentMemModel):
    """测验题完整实体（对应 eval_items 表）。"""

    id: Id
    space_id: Id
    question: str
    reference: str | None = None
    must_include: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source: EvalSource = "manual"
    created_at: Timestamp


class RetrievalMetrics(AgentMemModel):
    """检索侧指标（0~1 的比例，不是 0~100 的分数）。

    用来区分「证据没捞到」与「模型没用上证据」：总分掉了但 context recall 没掉，
    说明检索没问题、是提示词或模型的问题；recall 掉了才是检索的锅。

    ``None`` 表示该项没测到（题目没有 must_include、检索为空、judge 没给出审计），
    与「测出来是 0」严格区分——A/B 对比时把两者混起来会得出相反的结论。
    """

    context_recall: float | None = Field(
        default=None, ge=0.0, le=1.0, description="参考答案要点中被证据支撑的比例"
    )
    context_precision: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Context Precision@K：证据排得越靠前越高"
    )
    faithfulness: float | None = Field(
        default=None, ge=0.0, le=1.0, description="答案论断中有证据支撑的比例"
    )
    claims: int = Field(default=0, ge=0, description="参与 faithfulness 的论断条数")
    evidence: int = Field(default=0, ge=0, description="检索到的证据条数；整轮汇总时是逐题求和")
    audited: bool = Field(default=False, description="judge 是否给出了可用的审计结果")


class RetrievalMetricsDelta(AgentMemModel):
    """检索侧指标的差值。

    刻意不复用 :class:`RetrievalMetrics`：那边是比例，取值被夹在 0~1，
    而差值可以是负数（指标变差就是负的），复用会在构造时直接抛校验错误。
    """

    context_recall: float | None = None
    context_precision: float | None = None
    faithfulness: float | None = None
    claims: int = 0
    evidence: int = 0
    audited: bool = False


class EvalItemScore(AgentMemModel):
    """单题得分明细。"""

    item_id: Id
    score: float = Field(ge=0.0, le=100.0)
    passed: bool = False
    reason: str | None = None
    metrics: RetrievalMetrics | None = Field(default=None, description="该题的检索侧指标")
    measured: bool = Field(
        default=True,
        description=(
            "这道题是否真的测出了分数。裁判输出解析不出来时为 False——"
            "那是「没测到」，不是「答得差」，算总分时必须排除而不能当 0 分计入"
        ),
    )


class EvalRunDetail(AgentMemModel):
    """eval_runs.detail 的结构化内容。"""

    items: list[EvalItemScore] = Field(default_factory=list)
    metrics: RetrievalMetrics | None = Field(
        default=None, description="整轮检索侧指标（只统计审计成功的题目）"
    )
    retrieval: dict[str, object] | None = Field(
        default=None, description="本轮使用的检索配置覆盖项；为空表示用 Space 默认配置"
    )


class EvalArmSpec(AgentMemModel):
    """一次多臂对比里的一臂：只换检索配置（和可选的注入经验集）。"""

    label: str = Field(min_length=1, description="这一臂的名字，展示用，例如「默认」「关掉 MMR」")
    retrieval: dict[str, object] | None = Field(
        default=None,
        description="只写要改的检索旋钮，其余沿用 Space 配置；未知键会被拒绝",
    )
    insight_set: list[Id] = Field(default_factory=list, description="这一臂注入的经验 id")


class EvalCompareRequest(AgentMemModel):
    """多臂对比入参。"""

    arms: list[EvalArmSpec] = Field(min_length=2, description="至少两臂，第一臂作为基准")
    persist: bool = Field(default=True, description="是否把每一臂都落库为一条评测记录")


class EvalItemDelta(AgentMemModel):
    """单题的配对差值（同一道题、同一批经验，只有检索配置不同）。"""

    item_id: Id
    score_delta: float = Field(description="相对基准臂的分数差，正=更好")


class EvalArmOutcome(AgentMemModel):
    """一臂的结果。"""

    label: str
    run_id: Id
    score: float = Field(ge=0.0, le=100.0)
    metrics: RetrievalMetrics | None = None
    item_scores: list[EvalItemScore] = Field(default_factory=list)


class EvalArmDelta(AgentMemModel):
    """一臂相对基准臂的差值。"""

    label: str
    score_delta: float = Field(description="总分差，正=更好")
    metrics_delta: RetrievalMetricsDelta | None = Field(
        default=None, description="检索侧指标的差值；某项有一侧没测到就留空"
    )
    item_deltas: list[EvalItemDelta] = Field(default_factory=list)
    paired_items: int = Field(default=0, description="两臂都测出分数、参与配对的题数")
    std_error: float | None = Field(default=None, description="配对均差的标准误")
    t_stat: float | None = Field(default=None, description="配对 t 值")
    significant: bool = Field(
        default=False,
        description=(
            "总分差是否显著（双侧约 95%）。界面只该给显著的差值上涨跌色："
            "此前所有非零差值一律红绿着色，t≈0.6 的 +1.3 也被画成「提升」"
        ),
    )
    metrics_significant: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "各检索指标的差值是否显著（逐题配对检验）。A/A 对照里 faithfulness "
            "曾波动 +0.111，比所有真实参数臂的变化都大，不检验就没法读"
        ),
    )


class InsightContribution(AgentMemModel):
    """留一法算出来的单条经验贡献。"""

    insight_id: Id
    trigger: str = Field(default="", description="经验的场景描述，展示用")
    score_without: float = Field(ge=0.0, le=100.0, description="把这条经验拿掉之后的总分")
    contribution: float = Field(description="基准分 − 去掉它之后的分。正=它在帮忙，负=它在拖后腿")
    items_helped: int = Field(default=0, description="去掉它之后变差的题数（即它帮到的题）")
    items_hurt: int = Field(default=0, description="去掉它之后反而变好的题数")


class InsightAttribution(AgentMemModel):
    """一次留一法归因的结果。

    此前只能整批均摊：一次 👎 按注入条数平摊到每条经验头上，那是相关性不是因果。
    留一法是真做了对照实验——把某一条拿掉重跑同一批题，分数掉了多少就是它的贡献。
    代价是评测要跑 N+1 轮，所以调用方必须显式选定要归因的经验。
    """

    space_id: Id
    items: int = Field(ge=0, description="参与的题目数")
    baseline_score: float = Field(ge=0.0, le=100.0, description="全部经验都在时的分数")
    noise_floor: float | None = Field(
        default=None,
        description="A/A 噪声底：同一配置跑两遍的分差绝对值。贡献小于它的不该当结论",
    )
    contributions: list[InsightContribution] = Field(default_factory=list)


class InsightAttributionRequest(AgentMemModel):
    """留一法归因入参。"""

    insight_ids: list[Id] = Field(
        default_factory=list,
        description="要归因的经验；留空表示取当前生效（active）的那些",
    )
    max_insights: int = Field(
        default=5, ge=1, le=20, description="最多归因几条——每多一条就多跑一整轮评测"
    )
    include_noise_floor: bool = Field(
        default=True, description="是否多跑一轮 A/A 求噪声底（多一轮的代价，换一条判据）"
    )
    persist: bool = Field(default=False, description="是否把每一轮都落库为评测记录")


class EvalComparison(AgentMemModel):
    """多臂对比结果。

    同一批题目、同一批经验，只改检索配置——配对比较，题目难度与采样噪声因此
    大部分被消掉。把同一个配置跑两臂（A/A 对照）得到的就是这一轮的噪声底：
    差值落在这个范围内的改动不值得当结论。
    """

    space_id: Id
    items: int = Field(ge=0, description="参与对比的题目数")
    baseline: str = Field(description="基准臂的名字（第一臂）")
    arms: list[EvalArmOutcome] = Field(default_factory=list)
    deltas: list[EvalArmDelta] = Field(default_factory=list, description="各臂相对基准臂的差值")


class EvalRunCreate(AgentMemModel):
    """记录一次评测。"""

    space_id: Id
    variant: EvalVariant
    insight_set: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=100.0)
    detail: EvalRunDetail = Field(default_factory=EvalRunDetail)
    duration_ms: int | None = None


class EvalRunUpdate(AgentMemModel):
    """评测记录部分更新。"""

    variant: EvalVariant | None = None
    insight_set: list[str] | None = None
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    detail: EvalRunDetail | None = None
    duration_ms: int | None = None


class EvalRun(AgentMemModel):
    """评测记录完整实体（对应 eval_runs 表）。"""

    id: Id
    space_id: Id
    variant: EvalVariant
    insight_set: list[str] = Field(default_factory=list)
    score: float
    detail: EvalRunDetail = Field(default_factory=EvalRunDetail)
    duration_ms: int | None = None
    created_at: Timestamp


class ExpertiseScore(AgentMemModel):
    """专家度五维分数与加权总分。"""

    coverage: float = Field(ge=0.0, le=100.0)
    accuracy: float = Field(ge=0.0, le=100.0)
    consistency: float = Field(
        ge=0.0, le=100.0, description="同问多答的稳定性；来源见 consistency_source"
    )
    groundedness: float = Field(ge=0.0, le=100.0)
    groundedness_samples: int = Field(
        default=0, ge=0, description="依据度统计了多少条回答；样本很少时分数说明不了什么"
    )
    insight_density: float = Field(ge=0.0, le=100.0)
    overall: float = Field(ge=0.0, le=100.0)
    consistency_source: ConsistencySource = Field(
        default="proxy",
        description="probe=真的重复提问测过；proxy=用「active 经验占比」近似，界面要如实标注",
    )
    consistency_measured_at: Timestamp | None = Field(
        default=None, description="实测时间；proxy 时为空"
    )


class ExpertiseSnapshotCreate(ExpertiseScore):
    """写入专家度快照。"""

    space_id: Id


class ExpertiseSnapshotUpdate(AgentMemModel):
    """专家度快照部分更新。"""

    coverage: float | None = None
    accuracy: float | None = None
    consistency: float | None = None
    groundedness: float | None = None
    insight_density: float | None = None
    overall: float | None = None


class ExpertiseSnapshot(ExpertiseScore):
    """专家度快照完整实体（对应 expertise_snapshots 表）。"""

    id: Id
    space_id: Id
    created_at: Timestamp


# ===========================================================================
# §8 用量统计
# ===========================================================================


class UsageRecordCreate(AgentMemModel):
    """写入一条用量记录。"""

    space_id: Id | None = None
    provider_id: str
    model: str
    kind: UsageKind
    purpose: str | None = Field(default=None, description="chat | distill | judge | ingest")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = Field(
        default=0, description="命中前缀缓存的 token，已含在 prompt_tokens 内"
    )
    cache_write_tokens: int = Field(default=0, description="写入缓存的 token（仅 Anthropic）")
    latency_ms: int | None = None
    ok: bool = True


class UsageRecordUpdate(AgentMemModel):
    """用量记录部分更新。"""

    space_id: Id | None = None
    purpose: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    ok: bool | None = None


class UsageRecord(AgentMemModel):
    """用量记录完整实体（对应 usage_records 表）。"""

    id: Id
    space_id: Id | None = None
    provider_id: str
    model: str
    kind: UsageKind
    purpose: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int | None = None
    ok: bool = True
    created_at: Timestamp


# ===========================================================================
# 存储层辅助模型（LanceDB 向量表 / FTS5 索引）
# ===========================================================================

VectorTableName = Literal["chunks_vec", "cards_vec", "insights_vec"]


class VectorRecord(AgentMemModel):
    """写入 LanceDB 的一条向量记录。

    ``id`` 对应各表的主键：``chunks_vec`` 为 chunk_id，``cards_vec`` 为 card_id，
    ``insights_vec`` 为 insight_id。
    """

    id: Id
    space_id: Id
    vector: list[float]
    embedding_model: str
    document_id: Id | None = Field(default=None, description="仅 chunks_vec 使用")


class VectorHit(AgentMemModel):
    """向量检索命中。"""

    id: Id
    space_id: Id
    document_id: Id | None = None
    score: float = Field(description="距离换算后的相似度，越大越相关")
    embedding_model: str | None = None


class VectorTableMeta(AgentMemModel):
    """向量表的维度台账，用于维度守卫。"""

    table_name: VectorTableName
    embedding_model: str
    dim: int = Field(ge=1)
    updated_at: Timestamp


class FtsHit(ContentModel):
    """FTS5 全文检索命中。"""

    chunk_id: Id
    space_id: Id
    document_id: Id
    score: float = Field(description="BM25 分数取正，越大越相关")
    content: str | None = Field(default=None, description="命中的原文片段")


# ===========================================================================
# API 传输模型
# ===========================================================================

T = TypeVar("T")


class Page(AgentMemModel, Generic[T]):
    """统一分页响应体。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None


class DeleteResponse(AgentMemModel):
    """删除类操作的统一响应。"""

    deleted: bool = True
    id: str


# --------------------------------------------------------------------------
# §1 系统
# --------------------------------------------------------------------------


class HealthResponse(AgentMemModel):
    """GET /health 响应。"""

    status: str
    version: str
    uptime_ms: int


class ProviderAlert(AgentMemModel):
    """某个 provider 最近一段时间的失败情况。

    数据来自 ``usage_records``：每次调用（无论成败）本来就会记一条，所以这里不需要
    另立一套状态，进程重启也不会丢。
    """

    provider_id: str
    kind: str = Field(description="能力类型：llm | embedding | rerank")
    failures: int = Field(description="窗口内的失败次数")
    calls: int = Field(description="窗口内的总调用次数")
    last_failed_at: int = Field(description="最近一次失败的时间（Unix 毫秒）")
    recovered: bool = Field(
        description="最近一次调用是否已经成功——失败之后又通了，就只是提示而不是故障"
    )


class ProviderAlertsResponse(AgentMemModel):
    """GET /system/alerts 响应：provider 失联与降级的实况。

    界面据此给出「某个模型连不上」的提示。没有这条，provider 挂掉时用户看到的
    只是「回答失败」，既不知道是哪一个挂了，也不知道要不要去改配置。
    """

    window_ms: int = Field(description="统计窗口长度")
    alerts: list[ProviderAlert] = Field(default_factory=list)
    degraded: bool = Field(
        default=False, description="是否存在尚未恢复的 provider（界面据此决定要不要报警）"
    )


class CapabilitiesResponse(AgentMemModel):
    """GET /capabilities 响应：前端据此显隐功能，也用来解释空态。"""

    rerank_available: bool
    local_embedding: bool
    docling_available: bool
    graph_enabled: bool
    chat_available: bool = Field(
        default=False, description="主对话角色是否绑定了可用模型；没绑定时问答不可用"
    )
    distill_available: bool = Field(
        default=False,
        description="蒸馏角色是否绑定可用模型；没绑定时卡片抽取、经验蒸馏、出题都跑不了",
    )


class StatsResponse(AgentMemModel):
    """GET /stats 响应。"""

    space_count: int
    document_count: int
    chunk_count: int
    insight_count: int
    disk_usage_bytes: int


# --------------------------------------------------------------------------
# §2 模型 Provider
# --------------------------------------------------------------------------


class ProviderConfig(AgentMemModel):
    """``config/models.yaml`` 中的单个 provider 条目。"""

    id: str = Field(min_length=1)
    kind: ProviderKind
    adapter: str = Field(min_length=1)
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(
        default=None, description="插值后的明文密钥。⚠️ 绝不可出现在任何 HTTP 响应中"
    )
    api_key_ref: str | None = Field(
        default=None,
        description="api_key 在 models.yaml 中的原始写法（如 ${DEEPSEEK_API_KEY}），"
        "供写回时原样还原，避免明文落盘。由 load_models_config 填充。",
    )
    dimension: int | None = Field(default=None, ge=1, description="embedding 维度")
    device: str | None = Field(default=None, description="auto | cpu | cuda | mps")
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict, description="适配器专属参数")

    def to_public(self) -> ProviderPublic:
        """转成可安全返回给前端的视图（密钥只留掩码）。"""
        return ProviderPublic(
            id=self.id,
            kind=self.kind,
            adapter=self.adapter,
            model=self.model,
            base_url=self.base_url,
            has_api_key=bool(self.api_key),
            api_key_hint=_mask_secret(self.api_key),
            api_key_from_env=self.api_key_ref,
            dimension=self.dimension,
            device=self.device,
            enabled=self.enabled,
            extra=self.extra,
        )


def _mask_secret(secret: str | None) -> str | None:
    """只保留尾部 4 位，其余打码；过短的密钥整体打码，避免反推。"""
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"••••{secret[-4:]}"


class ProviderPublic(AgentMemModel):
    """provider 的对外视图。

    与 :class:`ProviderConfig` 的唯一区别：**不含明文 api_key**。
    所有 HTTP 响应必须用这个模型——密钥一旦进了 JSON，就可能被浏览器缓存、
    被日志采集、被截图带走。
    """

    id: str
    kind: ProviderKind
    adapter: str
    model: str | None = None
    base_url: str | None = None
    has_api_key: bool = Field(default=False, description="是否已配置密钥")
    api_key_hint: str | None = Field(default=None, description="掩码后的密钥，仅供界面辨识")
    api_key_from_env: str | None = Field(
        default=None, description="密钥来自哪个环境变量占位符，如 ${DEEPSEEK_API_KEY}"
    )
    dimension: int | None = None
    device: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderCreate(AgentMemModel):
    """POST /providers 入参。"""

    id: str = Field(min_length=1)
    kind: ProviderKind
    adapter: str = Field(min_length=1)
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    dimension: int | None = Field(default=None, ge=1)
    device: str | None = None
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(AgentMemModel):
    """PATCH /providers/{id} 入参。"""

    kind: ProviderKind | None = None
    adapter: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    dimension: int | None = Field(default=None, ge=1)
    device: str | None = None
    enabled: bool | None = None
    extra: dict[str, Any] | None = None


class ProviderHealth(AgentMemModel):
    """provider 连通性实测结果。"""

    ok: bool
    latency_ms: int | None = None
    resolved_model: str | None = None
    error: str | None = None


class DiscoverRequest(AgentMemModel):
    """POST /providers/discover 入参。"""

    adapter: str = Field(min_length=1)
    base_url: str | None = None
    api_key: str | None = None


class DiscoveredModel(AgentMemModel):
    """发现到的单个模型。"""

    id: str
    owned_by: str | None = None
    size_bytes: int | None = None
    family: str | None = None


class DiscoverResponse(AgentMemModel):
    """POST /providers/discover 响应。"""

    adapter: str
    base_url: str | None = None
    models: list[DiscoveredModel] = Field(default_factory=list)
    error: str | None = None


# --------------------------------------------------------------------------
# 本机 Agent 配置复用（Claude Code / Codex / Continue / 环境变量）
# --------------------------------------------------------------------------

#: 支持复用的本机 Agent 来源
LocalAgentSource = Literal["claude_code", "codex", "continue", "environment", "ollama", "lm_studio"]


class LocalAgentCandidate(AgentMemModel):
    """从本机 Agent 配置里发现的一个可复用 provider 候选。

    ⚠️ **绝不携带明文密钥**。密钥只以两种形式出现：
    - ``api_key_env``：密钥来自某个环境变量，导入时写成 ``${VAR}`` 占位符（首选，不落盘明文）；
    - ``api_key_hint``：配置文件里是字面量密钥时的掩码，仅供用户辨认是哪一个。
    """

    source: LocalAgentSource
    source_label: str = Field(description="人类可读的来源名，如 'Claude Code'")
    source_path: str | None = Field(default=None, description="读取到的配置文件路径，供用户核对")

    suggested_id: str = Field(description="建议的 provider id")
    kind: ProviderKind
    adapter: str
    model: str | None = None
    base_url: str | None = None

    has_api_key: bool = False
    api_key_env: str | None = Field(
        default=None, description="密钥所在的环境变量名；导入时转成 ${VAR} 占位符"
    )
    api_key_hint: str | None = Field(default=None, description="字面量密钥的掩码，仅供辨认")

    already_imported: bool = Field(default=False, description="同名 provider 是否已存在")
    note: str | None = Field(default=None, description="给用户的提示，如需要手动补密钥")


class LocalAgentScanResponse(AgentMemModel):
    """GET /providers/local-agents 响应。"""

    candidates: list[LocalAgentCandidate] = Field(default_factory=list)
    scanned_paths: list[str] = Field(
        default_factory=list, description="实际读取过的文件路径，透明告知用户"
    )
    errors: list[str] = Field(default_factory=list, description="读取/解析失败的说明")


class LocalAgentImportRequest(AgentMemModel):
    """POST /providers/import-local 入参：导入选中的候选。"""

    suggested_ids: list[str] = Field(
        default_factory=list, description="要导入的候选 suggested_id 列表"
    )


class RoleBindings(AgentMemModel):
    """角色 → provider id 的绑定表。"""

    chat: str | None = None
    fast: str | None = None
    distill: str | None = None
    judge: str | None = None
    embedding: str | None = None
    rerank: str | None = None


class RoleFallbacks(AgentMemModel):
    """角色 → 降级链。"""

    chat: list[str] = Field(default_factory=list)
    fast: list[str] = Field(default_factory=list)
    distill: list[str] = Field(default_factory=list)
    judge: list[str] = Field(default_factory=list)
    embedding: list[str] = Field(default_factory=list)
    rerank: list[str] = Field(default_factory=list)


class RolesUpdateRequest(AgentMemModel):
    """PUT /providers/roles 入参。"""

    roles: RoleBindings
    fallbacks: RoleFallbacks | None = None


class RolesUpdateResponse(AgentMemModel):
    """PUT /providers/roles 响应；embedding 维度变化时提示重建索引。"""

    roles: RoleBindings
    requires_reindex: bool = False
    affected_spaces: list[str] = Field(default_factory=list)


class UsageGroupItem(AgentMemModel):
    """用量聚合的一行。"""

    key: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cache_hit_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, description="cached_tokens / prompt_tokens"
    )
    avg_latency_ms: float | None = None


class UsageResponse(AgentMemModel):
    """GET /providers/usage 响应。"""

    group_by: str
    items: list[UsageGroupItem] = Field(default_factory=list)
    total_tokens: int = 0


# --------------------------------------------------------------------------
# §3 Space
# --------------------------------------------------------------------------


class SpaceSummary(Space):
    """列表项：Space 基本信息 + 聚合计数。"""

    doc_count: int = 0
    insight_count: int = 0
    expertise_overall: float | None = None


class SpaceDetail(Space):
    """详情：含 space.yaml 解析结果。"""

    config: SpaceYaml = Field(default_factory=SpaceYaml)
    doc_count: int = 0
    insight_count: int = 0
    expertise_overall: float | None = None


class PersonaSuggestRequest(AgentMemModel):
    """POST /spaces/{id}/persona/suggest 入参。"""

    instructions: str | None = Field(default=None, description="用户的额外要求")


class PersonaSuggestion(AgentMemModel):
    """Persona 草稿（不落库）。"""

    persona: Persona
    rationale: str | None = None


class SettingsUpdate(AgentMemModel):
    """PUT /spaces/{id}/settings 入参。"""

    retrieval: RetrievalSettings | None = None
    models: dict[str, str] | None = None


class ImportResponse(AgentMemModel):
    """POST /spaces/import 响应。"""

    space_id: str
    name: str
    document_count: int = 0


# --------------------------------------------------------------------------
# §4 文档
# --------------------------------------------------------------------------


class PasteDocumentRequest(ContentModel):
    """POST /spaces/{id}/documents/paste 入参。"""

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)


class UrlDocumentRequest(AgentMemModel):
    """POST /spaces/{id}/documents/url 入参。"""

    url: str = Field(min_length=1)
    crawl_depth: int = Field(default=0, ge=0, le=3)


class DocumentContent(ContentModel):
    """解析后的 Markdown 全文。"""

    document_id: Id
    title: str
    markdown: str


class UploadRejection(AgentMemModel):
    """一次多文件上传里没收下的一个文件。"""

    filename: str
    code: str
    message: str


class UploadResponse(AgentMemModel):
    """上传接口的即时响应，处理在后台异步进行。"""

    documents: list[Document] = Field(default_factory=list)
    rejected: list[UploadRejection] = Field(
        default_factory=list,
        description="没收下的文件及原因。此前一个文件出错整批报失败，前面已收下的却照常在后台处理",
    )


# --------------------------------------------------------------------------
# §5 检索与对话
# --------------------------------------------------------------------------


class SearchRequest(AgentMemModel):
    """POST /spaces/{id}/search 入参。"""

    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1)
    mode: SearchMode = "hybrid"


class SearchHit(ContentModel):
    """单条检索命中，保留各路分数供检索调试面板展示。"""

    chunk_id: Id
    document_id: Id
    document_title: str | None = None
    heading_path: str | None = None
    page: int | None = None
    snippet: str
    score: float
    vec_score: float | None = None
    bm25_score: float | None = None
    rrf: float | None = None
    rerank_score: float | None = None
    legs: list[str] = Field(default_factory=list, description="召回这条路命中的召回路径")
    quote_start: int | None = Field(
        default=None, description="命中里与问题最贴近的那句在全文里的起始位置"
    )
    quote_end: int | None = Field(default=None, description="那句的结束位置")


class SearchResponse(AgentMemModel):
    """POST /spaces/{id}/search 响应。"""

    query: str
    mode: SearchMode
    hits: list[SearchHit] = Field(default_factory=list)
    latency_ms: int | None = None


class ConversationDetail(Conversation):
    """会话详情：含消息列表。"""

    messages: list[Message] = Field(default_factory=list)


class ChatRequest(ContentModel):
    """POST /conversations/{cid}/chat 入参。"""

    content: str = Field(min_length=1)
    use_retrieval: bool = True
    search_mode: SearchMode = Field(
        default="hybrid", description="检索方式：hybrid=向量+全文；vector=只用向量；fts=只用全文"
    )
    use_insights: bool = True
    context_mode: Literal["standard", "economy"] = Field(
        default="standard",
        description="上下文策略：standard=标准预算；economy=更小预算与问题相关的原文窗口。"
        "不减少检索召回，不新增摘要模型调用",
    )
    llm_role: RoleName | None = None
    attachments: list[str] = Field(default_factory=list, description="限定检索的 document_id")
    deep_thinking: bool | None = Field(
        default=None,
        description="回答前是否让推理模型先思考。null=按模型服务的默认（含 provider 配置的 "
        "reasoning_effort）；false=要求不思考，首字更快、回答更简短；true=不压制思考，"
        "即使 provider 配置了 reasoning_effort。服务端不认推理参数时自动忽略",
    )


class StopResponse(AgentMemModel):
    """POST /conversations/{cid}/stop 响应。"""

    stopped: bool


class ContextUsageEvent(AgentMemModel):
    """当轮上下文的启发式输入估算，不是提供方的账单 token。"""

    mode: Literal["standard", "economy"]
    original_estimated_tokens: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    saved_estimated_tokens: int = Field(ge=0)
    history_messages: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    insight_count: int = Field(ge=0)
    card_count: int = Field(ge=0)


# --------------------------------------------------------------------------
# §6 记忆 L2 / L3
# --------------------------------------------------------------------------


class GraphNode(AgentMemModel):
    """知识图谱节点。"""

    id: Id
    name: str
    type: str
    weight: float = 0.0


class GraphEdge(AgentMemModel):
    """知识图谱边。"""

    src: Id
    dst: Id
    predicate: str
    weight: float = 1.0


class GraphResponse(AgentMemModel):
    """GET /spaces/{id}/graph 响应。

    图按节点数上限裁剪，``total_nodes`` 是裁剪前的实体总数——界面据此如实说明
    「显示了 300 / 800 个实体」，而不是让用户以为看到的就是全部。
    """

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    total_nodes: int = Field(default=0, description="裁剪前的实体总数")
    total_edges: int = Field(default=0, description="裁剪前的关系总数")
    truncated: bool = Field(default=False, description="是否因为节点上限被裁剪")


class KnowledgeCardRequest(ContentModel):
    """POST /spaces/{id}/cards 入参（space_id 来自路径）。"""

    kind: CardKind
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    source_chunks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified_by: VerifiedBy | None = None


class InsightRequest(AgentMemModel):
    """POST /spaces/{id}/insights 入参（space_id 来自路径，origin 固定为 manual）。"""

    trigger: str = Field(min_length=1)
    guidance: str = Field(min_length=1)
    rationale: str | None = None
    kind: InsightKind
    scope: InsightScope = "space"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class EvalItemRequest(AgentMemModel):
    """POST /spaces/{id}/evals 入参（space_id 来自路径）。"""

    question: str = Field(min_length=1)
    reference: str | None = None
    must_include: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source: EvalSource = "manual"


class CardExtractRequest(AgentMemModel):
    """POST /spaces/{id}/cards/extract 入参。"""

    document_ids: list[str] = Field(default_factory=list)


class InsightConflictGroup(AgentMemModel):
    """一组相互冲突的经验。"""

    group_id: str
    insights: list[Insight] = Field(default_factory=list)


class ConflictResolveRequest(AgentMemModel):
    """POST /spaces/{id}/insights/conflicts/{gid}/resolve 入参。"""

    keep_id: Id
    archive_ids: list[str] = Field(default_factory=list)
    merged_text: str | None = None


class InsightReviewItem(AgentMemModel):
    """一条「值得复查」的经验。

    不是「已证明有害」——单次好评/差评会被均摊到当时注入的所有经验上，归因本身是
    粗的。这里给出的是**统计线索**：被应用够多次、却很少收到好评，那就值得人看一眼。
    """

    insight: Insight
    applied_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0, description="success_count / applied_count")
    reason: str


class InsightReviewResponse(AgentMemModel):
    """``GET /spaces/{id}/insights/review`` 响应。"""

    items: list[InsightReviewItem] = Field(default_factory=list)
    min_applied: int = Field(description="入选的最小注入次数")
    max_success_rate: float = Field(description="入选的最大成功率")


class InsightLineage(AgentMemModel):
    """经验溯源。"""

    insight_id: Id
    source_traces: list[Trace] = Field(default_factory=list)
    source_feedback: list[Feedback] = Field(default_factory=list)
    supersedes: Id | None = None
    superseded_by: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# §7 进化闭环
# --------------------------------------------------------------------------


class FeedbackRequest(AgentMemModel):
    """POST /traces/{trace_id}/feedback 入参。"""

    kind: FeedbackKind
    comment: str | None = None


class JudgeResponse(AgentMemModel):
    """POST /traces/{trace_id}/judge 响应。"""

    trace_id: Id
    score: float = Field(ge=0.0, le=1.0)
    reason: str | None = None
    feedback_id: Id | None = None


class EvolvePendingResponse(AgentMemModel):
    """GET /spaces/{id}/evolve/pending 响应。"""

    pending_count: int = 0
    by_kind: dict[str, int] = Field(
        default_factory=dict,
        description="待蒸馏反馈按种类计数。前端此前从 preview（只有前 20 条）里数，反馈一多就少算",
    )
    preview: list[Feedback] = Field(default_factory=list)


class EvolutionRunCreate(AgentMemModel):
    """写入一次进化日志。"""

    space_id: Id
    produced: int = Field(default=0, ge=0, description="蒸馏产出的候选经验数")
    merged: int = Field(default=0, ge=0, description="合并掉的重复经验数")
    duplicates: int = Field(default=0, ge=0)
    conflicts: int = Field(default=0, ge=0, description="标记为冲突、待裁决的条数")
    promoted: int = Field(default=0, ge=0, description="本轮晋升为 active 的条数")
    demoted: int = Field(default=0, ge=0, description="本轮降到 archived 的条数")
    eval_delta: float | None = Field(default=None, description="A/B 总分差；没跑评测时为空")
    expertise_before: float = 0.0
    expertise_after: float = 0.0
    duration_ms: int | None = None


class EvolutionRun(EvolutionRunCreate):
    """一次进化的完整记录（对应 evolution_runs 表）。"""

    id: Id
    created_at: Timestamp


class EvolveHistoryItem(AgentMemModel):
    """进化历史里的一条（由进化日志投影而来）。"""

    run_at: Timestamp
    produced: int = 0
    merged: int = 0
    conflicts: int = 0
    promoted: int = 0
    demoted: int = 0
    eval_delta: float | None = None
    expertise_before: float = 0.0
    expertise_after: float = 0.0
    duration_ms: int | None = None


class EvolveHistoryResponse(AgentMemModel):
    """GET /spaces/{id}/evolve/history 响应。"""

    items: list[EvolveHistoryItem] = Field(default_factory=list)


# --------------------------------------------------------------------------
# §8 专家度与评测
# --------------------------------------------------------------------------


class ExpertiseGap(AgentMemModel):
    """知识盲区节点。"""

    topic: str
    reason: str | None = None
    suggested_queries: list[str] = Field(default_factory=list)


class OutlineNode(AgentMemModel):
    """领域大纲的一个节点。"""

    topic: str = Field(min_length=1)
    subtopics: list[str] = Field(default_factory=list)
    importance: str = "common"


class DomainOutline(AgentMemModel):
    """一份落库的领域大纲（对应 domain_outlines 表）。"""

    id: Id
    space_id: Id
    domain: str = ""
    nodes: list[OutlineNode] = Field(default_factory=list)
    interpretation: str | None = Field(default=None, description="生成时模型把领域理解成了什么")
    created_at: Timestamp


class DomainOutlineCreate(AgentMemModel):
    """写入一份领域大纲。"""

    space_id: Id
    domain: str = ""
    nodes: list[OutlineNode] = Field(default_factory=list)
    interpretation: str | None = None


class OutlineResponse(AgentMemModel):
    """``POST /spaces/{id}/expertise/outline`` 响应。"""

    outline: DomainOutline
    covered: int = Field(default=0, description="已被知识卡片覆盖的节点数")
    total: int = Field(default=0, description="节点总数")
    interpretation: str | None = Field(
        default=None,
        description="模型把领域理解成了什么；领域名是简称时据此判断大纲有没有跑偏",
    )


class ExpertiseGapsResponse(AgentMemModel):
    """GET /spaces/{id}/expertise/gaps 响应。"""

    gaps: list[ExpertiseGap] = Field(default_factory=list)
    outline_size: int = Field(default=0, description="大纲节点总数；0 表示还没有大纲")
    outline_generated_at: Timestamp | None = Field(
        default=None, description="大纲生成时间；为空说明还没生成过（需要模型）"
    )
    outline_interpretation: str | None = Field(
        default=None, description="生成大纲时模型对领域的理解；理解偏了盲区就全是错的"
    )
    outline_error: str | None = Field(
        default=None,
        description="还没有大纲、且最近一次自动生成失败时的原因；冷却期内不再自动重试",
    )


class ExpertiseHistoryResponse(AgentMemModel):
    """GET /spaces/{id}/expertise/history 响应。"""

    snapshots: list[ExpertiseSnapshot] = Field(default_factory=list)


class EvalGenerateRequest(AgentMemModel):
    """POST /spaces/{id}/evals/generate 入参。"""

    document_ids: list[str] = Field(default_factory=list)
    count: int = Field(default=10, ge=1, le=200)


class EvalRunRequest(AgentMemModel):
    """POST /spaces/{id}/evals/run 入参。"""

    variant: EvalVariant = "baseline"
    insight_set: list[str] = Field(default_factory=list)


class EvalRunListResponse(AgentMemModel):
    """GET /spaces/{id}/evals/runs 响应。"""

    runs: list[EvalRun] = Field(default_factory=list)


# --------------------------------------------------------------------------
# SSE 事件负载（`03-API-SPEC.md` §4 / §5 / §7）
# --------------------------------------------------------------------------


class ProgressEvent(AgentMemModel):
    """摄取进度事件（event: progress）。"""

    document_id: Id
    stage: str
    done: int
    total: int
    percent: float


class StatusEvent(AgentMemModel):
    """摄取状态变更事件（event: status）。"""

    document_id: Id
    status: DocumentStatus


class QueueEvent(AgentMemModel):
    """摄取队列深度事件（event: queue）。

    投喂的文档多于并发上限时，多出来的在排队。前端据此把「排队中」与「正在处理」
    分开显示——否则用户看到一堆 pending 文档，不知道是在跑还是卡住了。
    """

    running: int = Field(ge=0, description="正在跑的摄取任务数")
    waiting: int = Field(ge=0, description="在等名额的任务数")


class SseErrorEvent(AgentMemModel):
    """SSE 流内错误事件（event: error）。"""

    code: str
    message: str
    document_id: Id | None = None


class TraceStartEvent(AgentMemModel):
    """问答开始（event: trace_start）。"""

    trace_id: Id
    message_id: Id


class RewriteEvent(AgentMemModel):
    """查询改写结果（event: rewrite）。"""

    rewritten: str


class RetrievalEventItem(AgentMemModel):
    """检索事件中的单条命中。"""

    id: Id
    document_id: Id
    title: str | None = None
    page: int | None = None
    score: float
    snippet: str
    kind: ChunkKind = Field(
        default="body", description="body=正文切片；summary=文档概要（界面据此加徽章）"
    )
    # 以下与轨迹里的 TraceRetrievedItem 同源：只在轨迹里有的话，证据栏在实时对话里
    # 拿不到召回路径和分数，每条都被标成「未记录（旧版数据）」，刷新后才正常
    vec_score: float | None = None
    bm25_score: float | None = None
    rrf: float | None = None
    rerank_score: float | None = None
    legs: list[str] = Field(default_factory=list, description="召回这条的检索路径")
    merged_from: list[Id] = Field(default_factory=list, description="合并进这条证据的切片")
    heading_path: str | None = Field(default=None, description="切片所在章节路径")
    ordinal: int | None = Field(default=None, description="切片在文档内的序号")
    quote_start: int | None = Field(
        default=None, description="命中里与问题最贴近的那句在全文里的起始位置"
    )
    quote_end: int | None = Field(default=None, description="那句的结束位置")


class RetrievalEvent(AgentMemModel):
    """检索命中（event: retrieval）。"""

    chunks: list[RetrievalEventItem] = Field(default_factory=list)
    degraded: list[str] = Field(
        default_factory=list,
        description="配置了却没用上的检索环节（`vector` / `rerank`），见 RetrievalResult.degraded",
    )


class InsightEventItem(AgentMemModel):
    """注入经验事件中的单条经验。"""

    id: Id
    trigger: str
    guidance: str
    confidence: float
    scope: InsightScope = "space"


class InsightsEvent(AgentMemModel):
    """本次注入的经验（event: insights）。"""

    insights: list[InsightEventItem] = Field(default_factory=list)


class DeltaEvent(ContentModel):
    """正文流式增量（event: delta）。"""

    text: str


class CitationEvent(AgentMemModel):
    """引用标记落位（event: citation）。

    ``char_offset`` 是该标记在**剥离标记后的正文**里的字符位置：前端据此把芯片插回
    原位。正文里的 ``[^c1]`` 已被剥离（那样才好按句子统计依归度），只留位置。
    """

    marker: str
    chunk_id: Id
    document_id: Id
    document_title: str | None = None
    snippet: str | None = None
    char_offset: int | None = None
    page: int | None = None
    heading_path: str | None = None
    ordinal: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    kind: ChunkKind = "body"
    quote: str | None = None
    quote_start: int | None = None
    quote_end: int | None = None


class UsageSummary(AgentMemModel):
    """一次生成消耗的 token 与耗时。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class DoneEvent(AgentMemModel):
    """生成结束（event: done）。"""

    message_id: Id
    trace_id: Id
    usage: UsageSummary = Field(default_factory=UsageSummary)


class StageEvent(AgentMemModel):
    """进化阶段状态（event: stage）。"""

    stage: str
    status: Literal["running", "done", "failed"]
    produced: int | None = None
    merged: int | None = None
    conflicts: int | None = None
    promoted: int | None = None
    demoted: int | None = None
    variant: str | None = None


class CandidateEvent(AgentMemModel):
    """蒸馏候选（event: candidate）。"""

    id: Id
    trigger: str
    guidance: str
    origin: InsightOrigin


class EvalEvent(AgentMemModel):
    """评测分数（event: eval）。"""

    variant: str
    score: float


class CycleDoneEvent(AgentMemModel):
    """一键进化结束（event: done）。"""

    delta: float
    expertise_before: float
    expertise_after: float
