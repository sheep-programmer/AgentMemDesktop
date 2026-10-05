# AgentMem · 系统架构规范

> 本文记录系统分层、模块职责、接口边界与维护约定。

---

## 1. 总体分层

```
┌─────────────────────────────────────────────────────────────┐
│  apps/web          React 19 + Vite + TS + Tailwind          │
│                    仅通过 HTTP/SSE 与后端通信，无业务逻辑      │
└──────────────────────────┬──────────────────────────────────┘
                           │  REST + SSE (JSON)
┌──────────────────────────┴──────────────────────────────────┐
│  apps/api          FastAPI 薄适配层                          │
│                    路由 / 校验 / 序列化 / SSE 推流            │
│                    ⚠️ 不允许写业务逻辑，只调 core             │
└──────────────────────────┬──────────────────────────────────┘
                           │  Python 直接调用
┌──────────────────────────┴──────────────────────────────────┐
│  packages/core     agentmem —— 纯 Python 库，可独立使用       │
│                                                             │
│   providers/   模型可插拔层（LLM / Embedding / Rerank）       │
│   ingest/      文档解析 → 切分 → 归一化                       │
│   store/       存储抽象（向量库 / 关系库 / 对象存储）           │
│   retrieve/    混合检索 + 重排 + 上下文装配                    │
│   memory/      L2 知识卡片 / L3 经验 / L4 人格                │
│   evolve/      进化闭环（Trace→Critique→Distill→Promote）     │
│   expert/      专家画像、EvalSet、专家度指数                   │
│   space/       多知识库隔离与生命周期                          │
└─────────────────────────────────────────────────────────────┘
```

铁律
- `core` 不得 import fastapi / 任何 Web 相关包。它必须能被 CLI、脚本、Jupyter 直接使用。
- `api` 不得出现 `if/else` 业务分支、SQL、Prompt 字符串。它只做「解包请求 → 调 core → 包装响应」。
- `web` 不得有任何"如果模型是 ollama 就……"这类判断。所有能力由后端 `/capabilities` 声明。

---

## 2. 目录结构（最终形态）

```
AgentMemDesktop/
├── docs/                          # 规范文档（本目录）
├── apps/
│   ├── api/
│   │   ├── main.py                # FastAPI 入口
│   │   ├── deps.py                # 依赖注入（获取 core 单例）
│   │   ├── sse.py                 # SSE 工具
│   │   └── routers/
│   │       ├── spaces.py
│   │       ├── documents.py
│   │       ├── chat.py
│   │       ├── memory.py
│   │       ├── evolve.py
│   │       ├── expert.py
│   │       └── providers.py
│   └── web/
│       ├── src/
│       │   ├── app/               # 路由与页面
│       │   ├── components/        # 复用组件
│       │   │   └── ui/            # shadcn/ui 原子组件
│       │   ├── features/          # 按业务域组织（chat/library/memory/...）
│       │   ├── lib/               # api client / utils
│       │   ├── stores/            # zustand
│       │   └── styles/
│       ├── index.html
│       └── vite.config.ts
├── packages/
│   └── core/
│       └── agentmem/
│           ├── __init__.py
│           ├── config.py          # 配置加载（YAML + env）
│           ├── types.py           # 全局 Pydantic 模型
│           ├── errors.py
│           ├── providers/
│           ├── ingest/
│           ├── store/
│           ├── retrieve/
│           ├── memory/
│           ├── evolve/
│           ├── expert/
│           └── space/
├── data/                          # 运行时数据（gitignore）
│   └── spaces/<space_id>/
│       ├── meta.db                # SQLite：元数据、轨迹、经验
│       ├── vectors/               # LanceDB 向量表
│       ├── raw/                   # 原始文件
│       └── space.yaml             # 该 Space 的配置与 Persona
├── config/
│   └── models.yaml                # 模型 Provider 配置
├── scripts/
├── tests/
├── pyproject.toml
└── README.md
```

---

## 3. 技术选型（v1 冻结）

| 层 | 选型 | 理由 |
|---|---|---|
| 后端框架 | FastAPI + Uvicorn | 异步、自带 OpenAPI、SSE 友好 |
| 语言版本 | Python 3.11+ | `asyncio.TaskGroup`、性能 |
| 包管理 | uv | 快、lockfile 可靠 |
| 关系/元数据库 | SQLite (WAL) | 零依赖、本地优先、单文件易备份 |
| 向量库 | LanceDB（嵌入式） | 零服务进程、列式、支持全文+向量混检、Apache-2.0 |
| 全文检索 | SQLite FTS5（BM25）+ jieba 预分词 | 与 chunk 元数据同库，混合检索与调试简单；不依赖 LanceDB FTS API 变动 |
| 文档解析 | docling（主，MIT）→ markitdown（兜底，MIT） | ⚠️ PyMuPDF4LLM 已排除：AGPL-3.0 会传染。MinerU 有营收附加条款，仅作用户自担许可的可选增强 |
| 模型抽象 | 自研 Provider 接口；`openai_compatible` 为默认适配器，`litellm` 为长尾适配器 | 见 §4 |
| 前端框架 | React 19 + Vite + TypeScript | 纯 SPA，Tauri 打包无摩擦 |
| 样式/组件 | Tailwind CSS + shadcn/ui | 完全可定制，不受组件库审美绑架 |
| 数据请求 | TanStack Query | 缓存、失效、乐观更新 |
| 客户端状态 | Zustand | 轻量 |
| 动画 | Motion (framer-motion) | 交互质感 |
| 图谱可视化 | react-force-graph-2d | Obsidian 式力导向观感；节点过万时切 Cytoscape.js |
| 本地 Embedding | BAAI/bge-m3（MIT，默认档） | 低配档 Qwen3-Embedding-0.6B；⚠️ Jina 系列为 CC-BY-NC，禁用 |
| 本地 Reranker | BAAI/bge-reranker-v2-m3（Apache-2.0） | 经 `sentence-transformers` CrossEncoder 直连 |
| 桌面化 | Tauri v2（Phase 5，非 v1） | 先纯 Web 本地跑，后期套壳 |

> 上述选型于 2026-09-16 确认。调整时应记录原因与验证结果。

### 3.1 为什么是「自研核心 + 借鉴开源」而不是 fork 某个项目

调研实测了 14 个开源 RAG 平台。结论：

| 候选 | 排除原因 |
|---|---|
| Khoj / Cherry Studio | AGPL-3.0，传染，商业闭源禁用 |
| Open WebUI | BSD-3 + 禁止移除品牌条款（终端用户 >50 人即触发） |
| Dify / FastGPT | Apache-2.0 + 附加条款：不得移除 LOGO、SaaS 需商业授权；且 Dify 模型层已迁至插件守护进程，自研 provider 反成负担 |
| RAGFlow | License 干净、前端最漂亮，但默认强绑 Elasticsearch、Go+Python 双栈，与「本地单机」重心错位 → 仅作前端设计与解析方案的参考对象 |
| AnythingLLM | MIT 干净、provider 抽象最完整，但是 Node/JS 单体，要用 docling / 本地 embedding 等 Python 生态必须起侧车进程，桌面打包时是灾难 → 仅借鉴其四层抽象结构 |

我们复用的是「库」而不是「壳」：LiteLLM（模型）、LanceDB（向量）、docling（解析）、bge（模型权重）都是成熟组件，直接站在肩膀上；而 AgentMem 真正的差异点——L3 经验层与可验证的进化闭环——在上述任何项目中都不存在，fork 任何一个都得重写这部分。

> 可选增强（Phase 4 评估）：`Cognee` 的 session distillation、`Graphiti` 的双时态事实失效机制，二者均 Apache-2.0 且可作为 Python 库嵌入，与本架构 L3 设计高度契合。

---

## 4. 模型可插拔层（最关键的设计）

### 4.1 三类独立能力

绝不把 LLM / Embedding / Rerank 混为一谈。三者独立配置、独立切换。

```python
# packages/core/agentmem/providers/base.py


class LLMProvider(Protocol):
    name: str

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ChatResult: ...

    async def stream(self, messages: list[Message], **kw) -> AsyncIterator[ChatChunk]: ...


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    async def embed(
        self, texts: list[str], *, kind: Literal["doc", "query"] = "doc"
    ) -> list[list[float]]: ...


class RerankProvider(Protocol):
    name: str

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]: ...
```

### 4.2 配置驱动，零代码接入

`config/models.yaml`：

```yaml
providers:
  - id: local-qwen
    kind: llm
    adapter: openai_compatible      # 适配器类型
    base_url: http://localhost:11434/v1
    model: qwen3:14b
    api_key: ollama

  - id: deepseek-chat
    kind: llm
    adapter: openai_compatible
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key: ${DEEPSEEK_API_KEY}    # 从环境变量注入

  - id: local-bge
    kind: embedding
    adapter: sentence_transformers
    model: BAAI/bge-m3
    dimension: 1024
    device: auto

  - id: local-reranker
    kind: rerank
    adapter: sentence_transformers_ce
    model: BAAI/bge-reranker-v2-m3

# 角色绑定：业务代码只认角色名，不认具体 provider
roles:
  chat:        deepseek-chat        # 主对话
  fast:        local-qwen           # 快速任务（标题生成、改写）
  distill:     deepseek-chat        # 经验蒸馏（需强推理）
  judge:       deepseek-chat        # 评分
  embedding:   local-bge
  rerank:      local-reranker
```

业务代码永远这样写：
```python
llm = registry.llm("distill")  # ✅ 按角色取
llm = OpenAIProvider(...)  # ❌ 禁止
```

### 4.3 适配器清单（v1）

| adapter | 覆盖 | 说明 |
|---|---|---|
| `openai_compatible` | OpenAI / DeepSeek / 智谱 / 通义 / Ollama / LM Studio / vLLM / OneAPI | 主力。三者均已完整实现 OpenAI 协议含 `/v1/embeddings`。直连 `AsyncOpenAI`，零黑盒、易排错 |
| `anthropic` | Claude | 原生 SDK，Messages 格式不同，约 100 行转换 |
| `litellm` | 其余长尾（141 个 provider） | 覆盖广度兜底，不作为默认路径 |
| `sentence_transformers` | 本地 Embedding | bge-m3 / Qwen3-Embedding |
| `sentence_transformers_ce` | 本地 Reranker | bge-reranker-v2-m3 |
| `cohere_rerank` | 云端 Rerank | Cohere / SiliconFlow / Voyage |
| `ollama_native` | Ollama 特有能力 | 模型发现、pull 进度、`num_ctx` / `keep_alive` |

⚠️ Rerank 不走 LiteLLM：其 rerank 覆盖远不如 chat 完整，且 DeepSeek / 智谱官方 API 根本不提供 `/rerank` 端点。Rerank 用独立的 `RerankProvider` 接口 + 本地 CrossEncoder / Cohere 规范云服务。

各本地运行时端点速查
| 运行时 | Base URL | api_key | 备注 |
|---|---|---|---|
| Ollama | `http://localhost:11434/v1` | 任意非空串 | embedding 需先 `ollama pull` 对应模型 |
| LM Studio | `http://localhost:1234/v1` | 可空 | 需在 Developer 页加载 embedding 模型 |
| vLLM | `http://localhost:8000/v1` | `--api-key` 设定或空 | embedding 需启动时 `--task embed` |

### 4.4 必须实现的运行时能力

- 健康检查：`GET /providers/{id}/health` → 延迟、可用性、实际返回的模型名
- 自动发现：连上 Ollama / LM Studio 后自动拉取可用模型列表
- 降级链：角色可配 `fallback: [a, b]`，主 provider 失败自动切换并告警
- 用量统计：每次调用记录 token 与耗时，前端可看成本面板
- 维度守卫：切换 Embedding 模型且维度变化时，必须提示用户重建索引，禁止静默写入维度不匹配的向量

---

## 5. 检索管线

```
Query
 ├─ 查询改写（HyDE / 多查询扩展，可开关）
 ├─ 并行召回
 │    ├─ 向量检索 (LanceDB, top 50)
 │    └─ 全文检索 (FTS5 BM25, top 50)
 ├─ RRF 融合 (Reciprocal Rank Fusion)
 ├─ Rerank (top 50 → top 8)
 ├─ 冗余抑制 / MMR 多样性重排
 ├─ 上下文装配
 │    system: L4 Persona + 固定规则      ← 跨轮不变，可被前缀缓存
 │    当轮消息: L3 Insights + L2 Cards + L1 Chunks + 问题  ← 每轮变化
 └─ 生成（流式）+ 引用标注
```

引用机制：生成时要求模型用 `[^c3]` 形式标注，后端把标记映射回 chunk_id，前端可点击下钻到原文高亮位置。

---

## 5.1 上下文经济学（为什么上面那样分层装配）

上下文既是成本也是能力上限，这一节的设计目标是：在固定 token 预算内让信息量最大。

### 前缀缓存：什么放 system，什么放当轮消息

各家供应商都对逐字节一致的前缀打折（命中部分约 0.1x 计费）。由此推出一条硬规则：

> system 只放跨轮不变的内容。任何随问题变化的东西一律进当轮 user 消息。

违反它的代价不只是 system 本身不能缓存——system 一变，后面所有历史轮次一并失配，整段对话的复用归零。曾经的实现把 L4→L3→L2→L1→规则拼成一个 system 字符串，命中率恒为 0，且稳定内容还排在易变内容之后。

由 `agentmem.prompts.answer` 的 `build_system_prompt` / `build_turn_message` 分别承担，守护测试见 `tests/test_prompt_cache.py`。

各家的差异必须在适配器层吸收：

| 供应商 | 缓存机制 | 适配器要做的事 |
|---|---|---|
| OpenAI | 自动前缀匹配 | 无需干预；读 `prompt_tokens_details.cached_tokens` |
| DeepSeek | 自动磁盘缓存，忽略 `cache_control` | 无需干预；读 `prompt_cache_hit_tokens` |
| Anthropic | 必须显式打 `cache_control` 断点 | 打在 system 与「历史最后一条」上 |

⚠️ Anthropic 的断点绝不能打在最后一条 user 消息上——那里是本轮独有的检索证据，下轮必然不同，写缓存按 1.25x 计费，纯属浪费。断点位置由 `tests/test_prompt_cache_anthropic.py` 守护。

⚠️ 口径不统一，已在适配器层对齐：Anthropic 的 `input_tokens` 不含缓存读写，OpenAI 系包含。对外统一为「`prompt_tokens` 含缓存部分」，因此 `cached_tokens` 在统计时不可再加一次。

### 证据预算：总量封顶 + 按名次分配

证据（L1）占当轮 token 的绝大部分，必须有全局上限，而不是「单条上限 × 条数」。

- 总量封顶 `EVIDENCE_TOKEN_BUDGET`（含标签开销），由 `allocate()` 注水式分配：短证据用不完的份额回流给长证据；
- 份额低于下限的整条丢弃——宁可少一条证据，也不要一段没有信息量的残片；
- 裁剪只在句子边界进行。按字符切会把 `IC50 = 12 nM` 从中间劈开，在专业领域是灾难性的。

权重按名次，不按分数。 这是一条踩过坑的硬规则：各家 rerank 的分根本不在一个量纲——Cohere 是 0–1 的 `relevance_score`，本地 CrossEncoder 直接返回原始 logits（可负、无界），没配 reranker 时退回的 RRF 分彼此只差千分之几。实测同一组排序只换量纲，保留的证据条数在 5–8 之间跳。名次是三种情况下唯一可比的信号。守护测试见 `tests/test_budget.py::test_allocation_is_invariant_to_score_scale`。

### 冗余抑制

top-N 里的重复有两个来源：同一篇文档相邻切片的 80 token 重叠，以及跨文档的近重复（转载、多版本）。两者都会原样占掉证据区的预算，把本该进来的第 N+1 条挤出去。这一步在 rerank 之后、截断到 top-N 之前执行，由 `agentmem.retrieve.diversity` 承担。

相似度用字符 n-gram 的重合度：`|A∩B| / min(|A|,|B|)`，纯标准库，中英混排通用，n 取 4。

为什么不是 Jaccard（分母取并集）：等长文本下两者相同，长短悬殊时才有区别，而长短悬殊正是检索结果的常态。一条 50 字的结论被一条 2000 字的正文整段包含时，Jaccard 被正文的其余内容稀释到 0.02，而重合度是 1.0——「这条毫无新信息」只有后者看得出来。

丢弃只在重合度 > `dedup_threshold`（默认 0.85）时发生，而且丢整条。 相邻切片的重叠只占各自正文的 15% 上下，丢整条等于把独有内容一起扔了。实测 8 条相邻切片（512 token 目标 / 80 token 重叠）的证据区共 1704 token，其中只有 281 token 是重叠文字，重合度 0.14–0.21，抑制器一条都不丢——这是刻意的结果，不是没生效。

MMR 的相关性必须由名次推导，理由与证据预算那一条完全相同：各家 rerank 的分不在一个量纲上。`1/(rank+2)` 再 min-max 归一化到 0–1，λ 才在候选条数变化时保持同一含义；默认 λ=0.7。守护测试 `tests/test_diversity.py::test_mmr_is_invariant_to_score_scale`。

⚠️ 候选池必须先放大到 `limit × 2` 再截断。 只拿 `limit` 条的话，被丢掉的重复条目腾出的位置没人补，去重只会让结果变短。`keep_min` 保持在 1：池子里确实只剩重复内容时，结果短于 `limit` 才是诚实的，凑满条数等于把同样的文字送两遍。rerank 成功与回退 RRF 两条分支都要走这一步——RRF 只看名次，没有任何语义去重能力。

⚠️ `chunk_overlap` 的单位是 token 不是字符，80 token 对 512 token 的切片只占约 15%。因此切片重叠的 token 浪费有限，而整条丢弃会亏掉 85% 的独有内容。真要把它也省下来，正确做法是只裁掉与已保留条目重叠的那一段（保留整条、切掉冗余前缀/后缀），当前未实现，记为后续优化。

实测（同一批新药研发中文正文，`render_evidence` 的真实输出）：

| 场景 | 条数 | 证据区 token | 其中重复 token | 独有 token |
|---|---|---|---|---|
| 8 条相邻重叠切片，抑制前后 | 8 → 8 | 1704 → 1704 | 281 → 280 | 1423 → 1424 |
| 跨文档近重复（3 个版本 + 8 条其他，池 11） | 8 → 8 | 1741 → 1724 | 763 → 161 | 978 → 1563 |
| 长候选 + 2 条转载（池 7，总需求超预算） | 7 → 5 | 1580 → 1857 | 522 → 19 | 1058 → 1838 |

即：token 总数几乎不变（预算封顶），换来的是同样预算里多出五六成的独有内容。残余的 161 / 19 token 是别的相邻切片重叠，属于应有之义。

---

## 6. 数据流：文档入库

```
上传文件
  → 落盘 data/spaces/<id>/raw/  + 计算 sha256 去重
  → 解析 (docling 主 / markitdown 兜底) → Markdown + 结构信息
  → 切分（标题感知 + 语义边界，默认 512 token / 80 overlap）
  → 批量 Embedding（带进度 SSE 推送）
  → 写入 LanceDB + FTS5
  → 【异步】LLM 抽取 L2 知识卡片与实体关系
  → 状态机：pending → parsing → chunking → embedding → extracting → ready | failed
```

每个阶段可单独重试，失败不影响已完成阶段。

---

## 7. 错误与可观测

- 统一错误体：`{ "error": { "code": "PROVIDER_TIMEOUT", "message": "...", "detail": {...} } }`
- 所有 core 异常继承 `AgentMemError`，带 `code`
- 结构化日志（structlog），每条带 `space_id` / `trace_id`
- 长任务统一走 SSE，事件格式：`{"type": "progress"|"log"|"done"|"error", ...}`

---

## 8. 编码规范

Python
- 强制类型注解，`mypy --strict` 通过
- 所有对外数据结构用 Pydantic v2 模型，禁止裸 dict 跨模块传递
- 格式化 `ruff format`，检查 `ruff check`
- I/O 一律 `async`；CPU 密集（embedding、解析）走 `asyncio.to_thread`
- Prompt 全部集中在 `*/prompts.py`，禁止散落在逻辑代码中

TypeScript
- `strict: true`，禁止 `any`（必要时 `unknown` + 类型收窄）
- API 类型由后端 OpenAPI 自动生成到 `src/lib/api/types.gen.ts`，禁止手写
- 组件文件 ≤ 200 行，超了就拆
- 业务逻辑放 hooks，组件只负责渲染

通用
- 命名：Python `snake_case`，TS `camelCase`，组件/类 `PascalCase`
- 提交信息：`feat|fix|docs|refactor|test(scope): 描述`

---

下一步：数据模型见 [`02-DATA-MODEL.md`](./02-DATA-MODEL.md)，API 见 [`03-API-SPEC.md`](./03-API-SPEC.md)
