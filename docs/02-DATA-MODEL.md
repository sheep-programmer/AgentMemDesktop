# AgentMem · 数据模型规范

> SQLite 表结构 + Pydantic 模型。实现必须与此完全一致；变更需先改本文件。

命名约定：表名复数 snake_case；主键统一 `id TEXT`（ULID，字典序即时间序）；时间统一 `INTEGER` Unix 毫秒时间戳（UTC）。

---

## 1. Space（知识库空间）

一个 Space = 一个独立领域专家。物理隔离在 `data/spaces/<space_id>/`。

```sql
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
```

Space 的 Persona 与模型角色覆盖存在 `space.yaml`（见 §6），不入库——方便用户直接编辑与版本管理。

---

## 2. L0 / L1：文档与切片

```sql
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
```

向量存储（LanceDB，不在 SQLite）
表 `chunks_vec`，schema：
```
chunk_id: str  |  space_id: str  |  document_id: str
vector: fixed_size_list<float32>[dim]
embedding_model: str      # 记录产出该向量的模型，维度迁移时用
```

---

## 3. L2：知识卡片与实体关系

```sql
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
    mention_count INTEGER DEFAULT 0,      -- 被多少篇文档提到过；由 entity_mentions 重算
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
```

知识卡片同样写入 LanceDB 表 `cards_vec`，使卡片可被独立检索。

---

## 4. L3：经验条目（进化的核心产物）

```sql
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
```

经验置信度流水（`insight_events`）

产品讲「经验可证伪」，那就得答得出「这条经验为什么变成现在这样」。每次加减分与状态
流转都留一条流水（谁触发的、从多少到多少、反馈类事件的均摊份额）：

```sql
CREATE TABLE insight_events (
    id                TEXT PRIMARY KEY,
    insight_id        TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
    space_id          TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    event             TEXT NOT NULL,   -- positive_feedback / eval_improved / user_confirm …
    confidence_before REAL,
    confidence_after  REAL NOT NULL,
    status_before     TEXT,
    status_after      TEXT NOT NULL,
    share             REAL,            -- 反馈类事件按注入条数均摊的份额
    reason            TEXT,            -- 可读说明，如「均摊 1/6」「A/B +2.5」
    created_at        INTEGER NOT NULL
);
```

一致性实测（`consistency_probes`）

雷达上的「逻辑一致性」原本是代理指标（active 经验占非归档经验的比例）——它量的是
规则沉淀多不多，与「同样的问题问两遍会不会得到两个说法」不是一回事。真口径是
重复提问、比较答案语义：

```sql
CREATE TABLE consistency_probes (
    id          TEXT PRIMARY KEY,
    space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    questions   INTEGER NOT NULL,     -- 测了几个问题
    repeats     INTEGER NOT NULL,     -- 每题重复回答几遍（至少 2）
    similarity  REAL NOT NULL,        -- 平均两两余弦相似度，0~1
    detail      TEXT NOT NULL,        -- 逐题相似度与各次答案
    created_at  INTEGER NOT NULL
);
```

一次探测是 `questions × repeats` 次生成调用，所以按需触发
（`POST /spaces/{id}/expertise/consistency`），不随页面加载自动跑。
专家度计算优先读最近一次探测（超过 30 天视为过期），分数上带
`consistency_source`（`probe` / `proxy`）与 `consistency_measured_at`，
界面必须如实标注来源——不能让用户以为那是测出来的。

实体提及明细（`entity_mentions`）

`mention_count` 不做增量累加——每抽取一次就 +1 的话，重新解析过的文档会把节点权重
越推越高。提及按 `(entity_id, document_id, chunk_id)` 去重记在明细表里，计数再从明细
重算：

```sql
CREATE TABLE entity_mentions (
    id          TEXT PRIMARY KEY,
    entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    space_id    TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id    TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE (entity_id, document_id, chunk_id)
);
```

语义也随之明确为「被多少篇文档提到过」：同一篇文档抽十遍仍然只算一次，删掉文档时
它带来的提及随外键消失、计数跟着重算。

切片分「正文」与「文档概要」（`chunks.kind`）

正文切片各自只有几百 token，谁也不代表整篇文档：「这份报告讲了哪些限制」这类跨全文
汇总问题，按相似度召回的是零散段落，答案只能拼。摄取时会额外生成一条概要切片
（`kind='summary'`，把文档级上下文作为可检索证据），参与向量与全文两条召回。

它没有可定位的原文区间（`char_start = char_end = 0`），所以必须与正文区分开：
前端拿偏移去原文里画高亮时，遇到 summary 要显示「文档概要」而不是试图高亮。

知识卡片的版本留档（`card_versions`）

L2 卡片是「事实」，事实会变：新版指南把推荐剂量从 400mg 改到 200mg，旧值不该被静默覆盖。
`knowledge_cards` 只存当前版本，被取代的旧版本存进 `card_versions`：

```sql
CREATE TABLE card_versions (
    id            TEXT PRIMARY KEY,
    card_id       TEXT NOT NULL REFERENCES knowledge_cards(id) ON DELETE CASCADE,
    space_id      TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,       -- 从 1 开始
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    aliases       TEXT NOT NULL DEFAULT '[]',
    source_chunks TEXT NOT NULL DEFAULT '[]',
    confidence    REAL NOT NULL DEFAULT 0.5,
    verified_by   TEXT,
    valid_from    INTEGER NOT NULL,       -- 这一版开始生效的时间
    valid_to      INTEGER NOT NULL,       -- 被取代的时间
    created_at    INTEGER NOT NULL
);
```

留档时机：人工编辑改了标题或正文、自动抽取用更高置信度的正文覆盖旧值时。
只改别名不算「事实变了」，不产生版本。`verified_by='user'` 的卡片不会被抽取覆盖，
因此也永远不产生自动版本。

置信度更新规则（必须实现）
| 事件 | 变化 |
|---|---|
| 初次蒸馏产出 | `confidence = 0.3`，`status = candidate` |
| 用户显式确认 | `+0.3`，`status = active`，`verified` |
| 应用后得正反馈 | `+0.05` |
| 应用后得负反馈 | `-0.15` |
| EvalSet A/B 提升 | `+0.2`，`status = active` |
| EvalSet A/B 下降 | `-0.2` |
| `confidence < 0.15` | `status = archived`（软删除，可恢复） |
| 与现有条目语义冲突 | 双方 `status = conflicted`，待用户裁决 |

两个「应用后」增量的口径：它们是一次反馈的总量，不是每条经验的增量。一次回答可能同时注入多条经验，而用户点的是整条回答，系统无从知道哪一条起了作用，所以这次反馈的证据按当时注入的条数均摊（`share = 1 / 注入条数`）；只有一条被注入时 `share = 1`，增量与上表逐字一致。均摊防止一次误点的 👎 把六条经验一起推向归档，而 `success_count / applied_count` 会逐条累积，单条归因日后据此做统计。

`applied_count` 在对话落库、写入 `trace.used_insights` 时自增（关掉经验的 A/B 对照轮不计）；`success_count` 只在收到 `up` 时自增。纠错（`correction` / `edit`）与 `down` 同类：它们是「这个回答错了」的最强表态。judge 的自动评分不回流置信度，它只在 EvalSet 的 A/B 路径上生效。

`trigger` 字段写入 LanceDB 表 `insights_vec`，回答时按当前问题语义召回 Top-K 高置信度经验。经验因反馈跌到阈值以下被归档时要同步移出该表。

---

## 5. 对话、轨迹与反馈

```sql
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
```

---

## 6. L4：专家画像（`space.yaml`，不入库）

```yaml
persona:
  name: "逆向工程专家"
  domain: "Android 应用逆向与协议分析"
  role_description: |
    你是一名有十年经验的 Android 逆向工程师……
  principles:                 # 方法论，恒定注入
    - 先静态后动态，优先定位关键字符串
    - 任何结论必须给出可复现的验证步骤
  output_style:
    language: zh-CN
    tone: "简洁、技术化、不废话"
    must_cite: true           # 强制引用原文
  quality_bar:                # LLM-as-Judge 的打分标准
    - 是否给出了具体的类名/方法名而非泛泛而谈
    - 是否标注了 Android 版本适用范围
  glossary:                   # 术语表，进 Prompt 防止用词漂移
    脱壳: "从加固 APK 中还原原始 dex"

models:                       # 覆盖全局 roles，可选
  chat: local-qwen

retrieval:
  top_k_vector: 50
  top_k_fts: 50
  top_n_rerank: 8
  max_insights: 6
  hyde: false
```

---

## 7. 专家度与评测

```sql
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
```

总分权重（v1）：`accuracy 0.35 + groundedness 0.25 + coverage 0.20 + consistency 0.10 + insight_density 0.10`

快照在每次全量评测后生成，前端画成长曲线。

---

## 8. 用量统计

```sql
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
);
```

---

## 9. Pydantic 模型约定

- 每张表对应三个模型：`XxxCreate`（入参）/ `Xxx`（完整实体）/ `XxxUpdate`（部分更新，全字段 Optional）
- 数据库里的 JSON 字段，在 Pydantic 侧必须是强类型（`list[str]` / 嵌套模型），序列化在 store 层完成，不得把 JSON 字符串暴露给上层
- 所有模型放 `packages/core/agentmem/types.py`，按 §1–§8 顺序分节，禁止分散定义
