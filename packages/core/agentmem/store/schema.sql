-- AgentMem SQLite 表结构（与 docs/02-DATA-MODEL.md §1~§8 一致）
-- 命名约定：表名复数 snake_case；主键统一 id TEXT（ULID）；时间统一 Unix 毫秒时间戳（UTC）。

-- ---------------------------------------------------------------------------
-- §1 Space（知识库空间）
-- ---------------------------------------------------------------------------
CREATE TABLE spaces (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    domain       TEXT NOT NULL,          -- 领域描述，如 "Android 逆向工程"
    icon         TEXT,                   -- emoji 或图标 key
    color        TEXT,                   -- 主题色 hex
    description  TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- §2 L0 / L1：文档与切片
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL,          -- file | url | paste | conversation
    source_uri   TEXT,                   -- 原始路径或 URL
    mime         TEXT,
    sha256       TEXT NOT NULL,          -- 去重依据
    size_bytes   INTEGER,
    status       TEXT NOT NULL,          -- pending|parsing|chunking|embedding|extracting|ready|failed
    error        TEXT,
    meta         TEXT,                   -- JSON：作者/页数/标签等
    token_count  INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE(space_id, sha256)
);
CREATE INDEX idx_documents_space_status ON documents(space_id, status);

CREATE TABLE chunks (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,       -- 文档内序号
    content      TEXT NOT NULL,
    heading_path TEXT,                   -- "第3章 > 3.2 脱壳"，用于上下文补全
    page         INTEGER,                -- PDF 页码，供前端跳转高亮
    char_start   INTEGER,
    char_end     INTEGER,
    token_count  INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX idx_chunks_doc ON chunks(document_id, ordinal);

-- 全文索引（BM25）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content,
    content='chunks', content_rowid='rowid',
    tokenize='unicode61'   -- 中文场景由 ingest 层预分词后写入
);

-- ---------------------------------------------------------------------------
-- §3 L2：知识卡片与实体关系
-- ---------------------------------------------------------------------------
CREATE TABLE knowledge_cards (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,          -- concept | fact | procedure | pitfall | tool
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,          -- Markdown
    aliases      TEXT,                   -- JSON string[]，术语同义词
    source_chunks TEXT,                  -- JSON string[]，来源 chunk_id
    confidence   REAL NOT NULL DEFAULT 0.5,
    verified_by  TEXT,                   -- null | 'user' | 'eval'
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE entities (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,          -- 领域自定义：工具/协议/漏洞/人物...
    summary      TEXT,
    card_id      TEXT REFERENCES knowledge_cards(id) ON DELETE SET NULL,
    mention_count INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE(space_id, name, type)
);

CREATE TABLE relations (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    src_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate    TEXT NOT NULL,          -- "用于" / "依赖" / "对抗" ...
    weight       REAL DEFAULT 1.0,
    source_chunks TEXT,                  -- JSON string[]
    created_at   INTEGER NOT NULL
);
CREATE INDEX idx_relations_src ON relations(space_id, src_id);

-- ---------------------------------------------------------------------------
-- §4 L3：经验条目（进化的核心产物）
-- ---------------------------------------------------------------------------
CREATE TABLE insights (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,

    -- 经验三段式：场景 → 做法 → 依据
    trigger      TEXT NOT NULL,          -- 什么情况下适用（会被向量化用于匹配）
    guidance     TEXT NOT NULL,          -- 应该怎么做
    rationale    TEXT,                   -- 为什么（来自哪次纠错）

    kind         TEXT NOT NULL,          -- correction | preference | heuristic | constraint | terminology
    scope        TEXT NOT NULL DEFAULT 'space',   -- space | global

    confidence   REAL NOT NULL DEFAULT 0.3,
    status       TEXT NOT NULL DEFAULT 'candidate',
                 -- candidate | active | conflicted | archived
    origin       TEXT NOT NULL,          -- user_correction | negative_feedback | positive_feedback | judge | manual

    -- 效用统计，Promote/Demote 的依据
    applied_count    INTEGER DEFAULT 0,  -- 被注入上下文的次数
    success_count    INTEGER DEFAULT 0,  -- 注入后得到正反馈的次数
    eval_delta       REAL,               -- A/B 评测分数增量，正=有效

    source_trace_ids TEXT,               -- JSON string[]
    supersedes       TEXT,               -- 被本条取代的 insight_id
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX idx_insights_active ON insights(space_id, status, confidence DESC);

-- ---------------------------------------------------------------------------
-- §5 对话、轨迹与反馈
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    title        TEXT NOT NULL,
    pinned       INTEGER DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,       -- user | assistant | system | tool
    content         TEXT NOT NULL,
    citations       TEXT,                -- JSON: [{marker:"c3", chunk_id, document_id, page, snippet}]
    created_at      INTEGER NOT NULL
);

-- 一次完整回答的可追溯轨迹
CREATE TABLE traces (
    id              TEXT PRIMARY KEY,
    space_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,       -- 对应的 assistant 消息
    query           TEXT NOT NULL,
    rewritten_query TEXT,
    retrieved       TEXT NOT NULL,       -- JSON: [{chunk_id, vec_score, bm25_score, rrf, rerank_score}]
    used_insights   TEXT,                -- JSON string[]，本次注入的 insight_id
    used_cards      TEXT,                -- JSON string[]
    llm_role        TEXT,                -- chat | fast ...
    provider_id     TEXT,
    model           TEXT,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    latency_ms      INTEGER,
    created_at      INTEGER NOT NULL
);
CREATE INDEX idx_traces_msg ON traces(message_id);

CREATE TABLE feedback (
    id           TEXT PRIMARY KEY,
    trace_id     TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,          -- up | down | correction | edit
    comment      TEXT,                   -- 用户的纠错内容
    judge_score  REAL,                   -- LLM-as-Judge 0~1
    judge_reason TEXT,
    distilled    INTEGER DEFAULT 0,      -- 是否已被蒸馏处理
    created_at   INTEGER NOT NULL
);
CREATE INDEX idx_feedback_pending ON feedback(distilled, created_at);

-- ---------------------------------------------------------------------------
-- §7 专家度与评测
-- ---------------------------------------------------------------------------
CREATE TABLE eval_items (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    question     TEXT NOT NULL,
    reference    TEXT,                   -- 参考答案（可空，则用 judge 按 quality_bar 打分）
    must_include TEXT,                   -- JSON string[]，必须出现的关键点
    tags         TEXT,                   -- JSON string[]
    source       TEXT,                   -- manual | auto_from_doc | from_correction
    created_at   INTEGER NOT NULL
);

CREATE TABLE eval_runs (
    id           TEXT PRIMARY KEY,
    space_id     TEXT NOT NULL,
    variant      TEXT NOT NULL,          -- baseline | with_insights | custom
    insight_set  TEXT,                   -- JSON string[]，本次注入的经验
    score        REAL NOT NULL,          -- 0~100
    detail       TEXT NOT NULL,          -- JSON: 每题得分
    duration_ms  INTEGER,
    created_at   INTEGER NOT NULL
);

CREATE TABLE expertise_snapshots (
    id                TEXT PRIMARY KEY,
    space_id          TEXT NOT NULL,
    coverage          REAL NOT NULL,
    accuracy          REAL NOT NULL,
    consistency       REAL NOT NULL,
    groundedness      REAL NOT NULL,
    insight_density   REAL NOT NULL,
    overall           REAL NOT NULL,     -- 加权总分
    created_at        INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- §8 用量统计
-- ---------------------------------------------------------------------------
CREATE TABLE usage_records (
    id                TEXT PRIMARY KEY,
    space_id          TEXT,
    provider_id       TEXT NOT NULL,
    model             TEXT NOT NULL,
    kind              TEXT NOT NULL,     -- llm | embedding | rerank
    purpose           TEXT,              -- chat | distill | judge | ingest
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms        INTEGER,
    ok                INTEGER DEFAULT 1,
    created_at        INTEGER NOT NULL
    -- 注：cached_tokens / cache_write_tokens 由迁移 v2 追加，见 store/sqlite.py。
);
