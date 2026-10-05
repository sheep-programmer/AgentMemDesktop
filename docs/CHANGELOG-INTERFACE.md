# AgentMem · 接口变更与对齐记录 (CHANGELOG-INTERFACE)

本文档用于前端与后端并行研发过程中的接口对齐、变更记录与待裁决事项。

## Phase 1B 完成记录 (2026-09-16)

- 前端地基状态: 全部完成 (Phase 1B Completed)
- 代码交付目录: `apps/web/`
- 验证命令与结果:
  - `pnpm --dir apps/web run typecheck`: 0 errors
  - `pnpm --dir apps/web run lint`: 0 errors, 0 warnings
  - `pnpm --dir apps/web run build`: production build succeeded

### 1. 临时 API 类型与 Mock 策略
- 后端服务在 Phase 1 尚未完全启动前，前端通过 `src/lib/api/types.temp.ts` 严格镜像 `docs/02-DATA-MODEL.md` 和 `docs/03-API-SPEC.md` 定义数据模型与请求响应类型，并注明待 `openapi-typescript` 自动生成后无缝替换。
- 前端开发环境默认开启 `VITE_USE_MOCK=1` 提供全保真静态数据与 SSE 流式事件模拟（涵盖 `thought`、`retrieval`、`citation`、`delta`、`summary`、`done`），待后端接口就绪后只需设置 `VITE_USE_MOCK=0` 即可直连后端 `http://localhost:8765`。

### 2. 字段增强与对齐建议
1. `Space` 模型:
   - 前端在空间卡片与侧边栏渲染时扩展了 `doc_count?: number`、`insight_count?: number`、`color?: string`（或首字母色彩），建议在 `GET /api/v1/spaces` 列表中携带这两个聚合计数字段，避免前端需要额外发起 N 次查询。
2. `DocumentItem` 字段:
   - `docs/02-DATA-MODEL.md` 中为 `source_uri`，`docs/03-API-SPEC.md` 部分示例写为 `source_url`。前端当前对两种字段均做了兼容容错。
3. `ConflictGroup` 字段:
   - 建议在 `ConflictGroup` 中携带 `reason?: string` 解释两组 L3 经验存在冲突的具体原因（如：“对加固壳的 Hook 时机策略相互排斥”），便于冲突消解横幅直观展示。
4. `ProviderItem` 字段:
   - 增加了 `status: 'online' | 'offline' | 'degraded'` 与 `latency_ms?: number`，以支持设置页面中的服务商存活探测点与延迟状态显示。

---

## 2026-09-16 · Phase 1 收口

### ⚠️ 破坏性变更：provider 接口不再返回明文密钥

背景：`GET /providers` 原先直接返回 `ProviderConfig`，其中 `api_key` 是插值后的明文。
同时 `save_models_config` 会把插值后的明文密钥写回 `config/models.yaml`，
用户只要在界面上改一次 provider，`${DEEPSEEK_API_KEY}` 就会变成真实密钥落盘——
这类文件极易被误提交进 git。两处都已修复。

接口变化（前端需在 Phase 2 接真实 API 时对齐）：

| 影响的接口 | 变化 |
|---|---|
| `GET /providers` | 响应模型 `ProviderConfig[]` → `ProviderPublic[]` |
| `POST /providers` | 响应模型 → `ProviderPublic` |
| `PATCH /providers/{id}` | 响应模型 → `ProviderPublic` |
| `GET /providers/{id}` | 新增（原先只有 PATCH/DELETE，取不存在的 id 会返回 405 而非 404） |

`ProviderPublic` 相对 `ProviderConfig` 的差异：
- 移除 `api_key`（明文）
- 新增 `has_api_key: boolean` —— 是否已配置
- 新增 `api_key_hint: string | null` —— 掩码，形如 `••••9f2a`，仅供界面辨识
- 新增 `api_key_from_env: string | null` —— 形如 `${DEEPSEEK_API_KEY}`

前端需要做的：
1. Provider 列表/表单的密钥字段改为读 `has_api_key` + `api_key_hint` 展示，
   不要再期望拿到明文回显（本来也不该回显）。
2. 密钥来自环境变量时（`api_key_from_env` 非空），输入框应展示为只读并提示
   「由环境变量 `XXX` 提供」，避免用户在界面上覆盖掉占位符。
3. 写入方向不变，`POST`/`PATCH` 仍然提交明文 `api_key`。

详见 `docs/03-API-SPEC.md` §2.1。

### 路由顺序约束

`GET /providers/{provider_id}` 必须声明在 `/roles`、`/usage`、`/discover` 之后。
FastAPI 按声明顺序匹配，路径参数路由排在前面会把这些字面路径一并吃掉。

### SSE 测试的正确姿势（供后续写流式测试参考）

不要用 `httpx.ASGITransport` 测 SSE —— 它会 `await` 整个 ASGI 应用执行完毕才返回响应对象，
对永不结束的流就是死锁。starlette `TestClient` 虽支持流式，但在流未关闭时
从同一线程再发请求会与它的 portal 互锁。正确做法是直接消费路由返回的
`StreamingResponse.body_iterator`（见 `tests/test_api.py::test_ingest_sse_stream`）。

---

## 2026-09-16 · Phase 1 交叉审计修复

接口与安全验证发现 2 个高危 + 1 个中危，均已修复并用原始攻击手法复验。
新增 `packages/core/agentmem/security.py` 统一收口，回归测试见 `tests/test_security.py`。

| 漏洞 | 攻击路径 | 修复 |
|---|---|---|
| 🔴 任意文件读取 | Space 从 zip 导入 → 不可信 `meta.db` 里 `raw_path=/etc/passwd` → `GET /documents/{id}/raw` | `ensure_within()` 把目标锁死在该 Space 的 `raw/` 内；越界一律按 404 对外，不透露路径是否存在 |
| 🔴 SSRF | `POST /documents/url` 抓取 `http://169.254.169.254/` 等内网/元数据地址 | `resolve_public_url()` 校验协议 + 解析全部 DNS 记录；关掉自动重定向改为逐跳校验（只验首个 URL 会被 302 绕过） |
| 🟡 密钥回显 | `POST /providers` 格式错误 → 422 的 `errors[].input` 原样吐出明文 `api_key` | `redact_sensitive()` 按字段名递归打码，非敏感字段保持原样 |
| 🟡 过滤表达式注入 | LanceDB `where` 无参数绑定，三处裸插值 | 新增 `sql_literal()` 转义 + `delete_by_field()` 白名单接口，路由不再自己拼过滤串 |

### 前端修复
路由改 `React.lazy` 懒加载：主包 2,370 kB → 595 kB（gzip 192 kB），各页独立分包。

### 遗留（不阻断 Phase 2）
`delete_document` 仍在 API 层逐层调用存储实现，违反「api 只做薄壳」铁律。
应下沉为 core 的聚合方法 `runtime.delete_document(...)`，在 Phase 2 重构时一并处理。

### 测试写法备忘（踩过的坑）
涉及文档 `meta` 的测试，必须先等摄取流水线跑到 `ready`/`failed` 再改数据——
后台任务结束时会重写 `meta`，提前改会被静默覆盖，导致安全测试假通过。

---

## 2026-09-16 · Phase 2 后端完成（检索管线与流式对话）

### 交付
- `packages/core/agentmem/retrieve/`：`pipeline`（并行召回 → RRF → 重排 → L3/L2 召回）、
  `fusion`（RRF）、`citations`（`[^cN]` 流式解析与注册）、`context`（core 模型 → Prompt 形状映射）、
  `rewriting`（指代补全 / 多查询扩展 / HyDE）、`chat`（事件流、消息与轨迹落库）、`session`（生成任务登记与中断）。
- `apps/api/routers/chat.py`：§5 全部路由落地（原先返回 501）。
  `/spaces/{id}/search` 也留在本文件——`03-API-SPEC.md` §5 把检索与对话归为一组，
  路由文件本就该按 §1–§8 分组，拆到 `spaces.py` 反而要把同一节的接口劈成两半。
- 重构：`delete_document` 级联下沉到 `runtime.delete_document()`，路由只调一次。

### ⚠️ 前端需要对齐的行为

1. `POST /spaces/{id}/search`（检索调试面板）
- `mode: hybrid | vector | fts`；`top_k` 覆盖默认返回条数（默认取 `space.yaml` 的 `top_n_rerank`）。
- 每条命中同时带回 `vec_score` / `bm25_score` / `rrf` / `rerank_score`，四者都可能为 `null`：
  - 没配 rerank provider 时 `rerank_score` 恒为 `null`（跳过重排，不报错）；
  - 只被单路召回的命中，另一路分数为 `null`；
  - `vector` / `fts` 模式下 `rrf` 为 `null`，`score` 就是该路原始分；
    `hybrid` 模式下 `score` 是重排分（有则用）或 RRF 分。
- `reranked: bool` 不在响应体里（属 core 内部产物），前端按 `rerank_score` 是否有值判断。

2. 会话 CRUD
- `POST /spaces/{id}/conversations` 的请求体仍需带 `space_id`，但必须与路径一致，
  不一致返回 `422 VALIDATION_ERROR`（避免会话静默建到另一个 Space）。
- `GET/PATCH/DELETE /conversations/{cid}` 不带 `space_id`：后端逐个 Space 定位会话，
  不存在返回 `404 NOT_FOUND`。

3. `POST /conversations/{cid}/chat`（SSE）
- 事件顺序：`trace_start → rewrite(可选) → retrieval → insights → delta(多条) → citation(穿插) → done`。
- `retrieval` 与 `insights` 恒定发送（内容可能为空列表）。这样前端状态机不必为
  「这次没检索」「这次没经验」单独分支；`use_retrieval=false` 时两帧都在但 `chunks` 为空。
- `rewrite` 只在真的发生改写时发送（单轮提问、或模型认为无需改写时不发）。
- `citation` 事件可能同一 `marker` 只发一次；正文里的 `[^c1]` 会被后端剥离，
  前端不要自己解析正文里的标记，直接用 `citation` 事件渲染芯片。
- 失败时以 `error` 事件结束（`{code, message}`），且失败前已经发过的事件仍然有效；
  半截回答与轨迹照常落库（Phase 3 蒸馏要用）。
- `POST /conversations/{cid}/stop` 返回 `{stopped: bool}`；没有在跑的生成时返回 `false`。
  SSE 连接断开时后端同样会取消底层任务并落库半截回答。

### 待确认与修复的事项

1. 流式增量的空白被裁剪（建议修 `providers/base.py`）：
   `AgentMemModel` / `ProviderModel` 的 `str_strip_whitespace=True` 会把首尾空白裁掉。
   - `types.DeltaEvent(text=" ")` → `""`：retrieve 已改用不裁剪的
     `agentmem.retrieve.models.DeltaChunk` 兜住（形状相同，纯 SSE 载荷，不影响 OpenAPI）。
   - `providers.base.ChatChunk(delta=" ")` → `""`：这一层在本次改动范围之外，
     后果是上游把空格作为独立分片下发时，正文会丢空格
     （`tests/test_chat.py` 里能看到实际表现）。建议对 `ChatChunk.delta` /
     `DeltaEvent` / `Message.content` 关闭该裁剪。
2. L3 的 `applied_count` 未自增：本次只如实记录 `traces.used_insights`，
   效用统计（applied/success）留给 Phase 3 的 Promote/Demote 环节回填。
3. 多查询扩展（expand）已实现但无开关：`RetrievalSettings` 只有 `hyde`，
   `ChatRequest` 也没有对应字段，因此暂时只在 core 内部可用。如需对外提供，应增加相应配置项。
4. 会话定位无全局索引：`/conversations/{cid}` 逐个 Space 查 `meta.db`。
   Space 数量到几十个时建议在全局库加一张 `conversation_id → space_id` 索引表。
5. `mypy` 全量检查（含 `tests/`）会在 numpy stub 上报语法错：
   `pyproject.toml` 的 `python_version = "3.11"` 与 numpy 的 `type` 语句不兼容，
   属既有问题（未改动的 `tests/test_store.py` 同样触发）。
   指定范围内的 `.venv/bin/mypy --strict packages/core/agentmem apps/api` 通过。
6. `FtsIndex` 并发不安全（本阶段已在检索管线里绕开）：
   `SQLiteDatabase` 的读写方法都会加 `_lock`，但 `store/fts.py` 是直接拿底层
   `connection` 执行 SQL 的，绕过了那把锁；而连接是以 `check_same_thread=False`
   共享的单个连接。两路全文召回同时跑时实测出现 `bm25()` 返回 `NULL`
   （`float(None)` 抛错、整路召回丢失），复现概率约 50%。
   检索管线已用一把管线级锁把所有 SQLite 访问串起来（向量召回仍与 SQL 并行），
   但根因在存储层，建议改 `FtsIndex` 走 `SQLiteDatabase` 的加锁方法，
   否则并发摄取多篇文档时也会踩到同一个坑。
7. 解析缓存的写入与读取存在竞态（既有 flake，非本阶段引入）：
   `IngestPipeline._save_parsed` 用 `aiofiles` 直接覆写 `<doc>.parse.json`，
   而 `GET /documents/{id}/content` 会在摄取进行中读同一个文件，
   偶尔读到空文件并抛
   `ValidationError: Invalid JSON: EOF while parsing a value`（HTTP 500）。
   表现为 `tests/test_api.py::test_document_paste_and_ingest` 约 1/8 概率失败。
   建议：写缓存改成「临时文件 + `os.replace`」原子替换，或让读取端把空文件
   视为「尚未解析完」返回可重试的错误而不是 500。

### 本阶段新增的测试
`tests/test_retrieve.py`（RRF 正确性、跨分片引用解析、幻觉编号、无 rerank 降级、
空 L3/L2、HyDE 与扩展、检索调试字段）、`tests/test_chat.py`（会话 CRUD、SSE 事件序列与顺序、
引用落位、轨迹字段完整落库、`use_insights` A/B、断开与停止、provider 失败路径）、
`tests/test_api.py::test_delete_document_cascades`。

测试夹具的补充：`tests/conftest.py` 的 Mock 模型服务现在支持配置回复正文
（`mock_reply` 夹具）、按请求关键词分流回复、逐字符流式下发（引用标记必然跨分片）、
逐片延时（模拟慢速生成），并新增 `/rerank` 端点。


---

## 2026-09-17 · Phase 2 检索与对话修复

混合检索管线与对话路由完成后，联调中发现三处问题。逐一复现并修复后，补充了相应回归测试（`tests/test_regressions.py`）。

| 问题 | 复现结论 | 修复 |
|---|---|---|
| 🔴 FTS 并发不安全 | `FtsIndex` 直接用裸 `sqlite3.Connection`，绕过数据库锁。实测：串行 6/6 正常，并发第一轮即抛 `InterfaceError`（比报告的「bm25 返回 NULL」更严重） | `SQLiteDatabase` 新增 `exclusive()` 独占上下文；`FtsIndex` 五处裸连接全部收进锁内。并发 30 轮稳定 |
| 🟡 流式空格被吞 | 全局 `str_strip_whitespace=True` 作用到了 `ChatChunk.delta`。实测 `"Frida is a dynamic instrumentation toolkit"` → `"Fridaisatoolkit"` | 新增 `ContentModel` / `ProviderContentModel` 基类（不裁剪），正文类模型全部改继承。名称类字段保持裁剪不变 |
| 🟡 解析缓存读写竞态 | `_save_parsed` 用 `"w"` 打开会立即清空文件，并发读到空内容报 `Invalid JSON: EOF`。实测 1/12 | 改为「同目录临时文件 + `os.replace`」原子写。20 次连跑 0 失败 |

### 顺带修正的语义问题
原子写之后缓存文件在写完前不存在，`GET /documents/{id}/content` 会 404。
对 `paste` / `url` 而言原文本身就是 Markdown，现在直接回退读 `raw/`——
用户刚粘进去的内容应当立刻可见，没有理由等向量化跑完。

### 后续改进事项
1. `expand`（多查询扩展）已实现但无开关：`RetrievalSettings` 缺 `expand` 字段，`ChatRequest` 也没有对应参数，目前只能从 core 调用。
2. `GET /conversations/{cid}` 在缺 `space_id` 时靠逐 Space 查定位，数据量大时应加全局索引表。
3. L3 `applied_count` 未自增（效用统计属 Phase 3，trace 已如实记录可回填）。

### 新增的通用约束
- 任何绕过 `execute` / `fetchall` 直接拿 `connection` 的代码，必须在 `SQLiteDatabase.exclusive()` 内进行。该锁不可重入。
- 新增模型时：装 content / body / markdown / snippet 的继承 `ContentModel`；装 name / title / domain 的继承 `AgentMemModel`。

---

## 2026-09-17 · Phase 3A：L2 知识抽取 + 知识图谱 + Memory L2 路由

### 交付

- `packages/core/agentmem/memory/`（新建）
  - `extract.py` · `KnowledgeExtractor`：按 `ordinal` 顺序把切片切成相邻批次（每批 ≤4 片且 ≤12000 字符），
    调 `registry.llm("fast")`（temperature 0.1）；模型输出用 `extract_json` 容错解析，解析不出来重问一次。
    marker（`c1`…）每批重新编号，批内翻译成 `chunk_id` 后即丢弃；卡片按 `title + kind` 跨批合并、
    实体按 `(name, type)` 查重并 `mention_count += 1`、关系两端解析成 `entity_id`（解析不到就跳过并记日志）。
    单条坏数据（类型不认识、正文为空、置信度越界）只丢那一条并计数。
  - `cards.py` · `CardService`：卡片落库与去重合并、人工校订规则、`cards_vec` 向量同步（标题 + 正文一起编码）。
  - `graph.py` · `build_graph`：N 度邻居展开（BFS）+ 按 `mention_count` 截断 + 中心节点保底。
  - `models.py`：抽取流程的模型；`MemoryEvent`（事件名 + Pydantic 载荷）让 core 不依赖任何 Web 框架。
- `ingest/pipeline.py`：`_stage_extracting` 从占位改为真实调用。抽取失败不改变文档状态，
  原因写进 `document.meta.extraction_error`（日志同时留痕），状态照常走到 `ready`。
- `apps/api/routers/memory.py`：§6 的 L2 部分落地（卡片 CRUD、`cards/extract` SSE、`/graph`）。
  L3 insights 全部路由保持 501 骨架不变，待 `core/evolve` 落地后替换。
- `space/runtime.py`：新增 `knowledge_extractor()` / `card_service()` / `knowledge_graph()` 三个聚合入口。
- 测试：`tests/test_memory.py`（24 例）、`tests/test_memory_api.py`（9 例）。

### 前端需要对齐的行为

1. `POST /spaces/{id}/cards/extract`（SSE）
   - 事件序列：`progress`（逐批，可多条）→ `error`（单篇失败，可多条）→ `done`（汇总报告）。
   - `progress` 的载荷仍是既有的 `ProgressEvent`：`stage` 恒为 `"extracting"`，`done` / `total` 是该文档的批次进度，
     多文档时按 `document_id` 分组累计。每批结束就发一帧（不论该批是否抽出了内容）；
     若连角色都没绑定（`registry.llm()` 直接抛 `PROVIDER_NOT_CONFIGURED`），则该文档一帧 progress 都没有，只有 `error`。
   - `done` 载荷是新增的 `ExtractionReport`：`{space_id, documents[], failures[], duration_ms, cards_created, cards_updated, entities_created, relations_created}`；
     `documents[]` 是每篇的 `ExtractionStats`（含 `batches` / `batches_failed` / `cards_skipped` / `relations_skipped` / `embedding_error` /
     `skipped_reason`（值为 `"no_chunks"` 表示这篇还没切片））。
   - 流总是以 `done` 收尾；只有 Space 不存在或 `document_ids` 里有不存在的文档时，才在进入流之前返回 404。
   - `document_ids: []` 表示抽取该 Space 全部 `ready` 文档（上限 200 篇）。重复抽取是幂等的：卡片按 `title + kind` 合并。
2. `GET /spaces/{id}/graph`
   - `center` 接受实体 id 或名称（忽略大小写）；找不到返回 `404 NOT_FOUND`（不是空图）。
   - `depth` 1–4（默认 2，`depth=1` 是直接邻居），`limit`（默认 300）按 `mention_count` 从高到低截断；
     指定了 `center` 时中心节点一定保留，边只保留两端都在结果里的。
   - `nodes[].weight` = 实体的 `mention_count`，`edges[].weight` = `relations.weight`。
3. 卡片 CRUD
   - `PATCH` 未显式给 `verified_by` / `confidence` 时置为 `"user"` / `1.0`（人工校订语义）；显式传的值以请求为准。
   - `DELETE` 会连带删除 `cards_vec` 里的向量。
   - `GET /cards` 的 `next_cursor` 在按 `confidence DESC` 排序时恒为 `null`（沿用既有游标只对 id 排序生效的约定）。
4. 文档 `meta` 新增两个键（`DocumentMeta` 是 `extra="allow"`，旧前端可忽略）：
   - `meta.extraction`：本篇最近一次抽取的 `ExtractionStats`；
   - `meta.extraction_error`：抽取失败原因（文档状态仍是 `ready`，前端不要因此把它显示成失败）。
5. `/capabilities` 的 `graph_enabled` 一直是 `true`，现在才真的有数据可渲染。
6. 用量统计多了两个 purpose：LLM 抽取记为 `purpose="extract"`（原先只有 `chat` / `distill` / `judge`），
   卡片向量化记为 `purpose="ingest"`（与切片的向量化同一档）。
   `GET /providers/usage?group_by=purpose` 的分组里因此会多出 `extract` 这一行，前端不要再把它当作未知值丢掉。

### 设计取舍

- marker 每批重编而不是全局编号：模型只会在本批上下文里引用 marker，维护一张跨批全局表既无必要也更容易错。
- 没有可解析来源的卡片直接丢弃（`cards_skipped`）：「没有来源的卡片不可追溯，等于不可信」，宁可不入库。
- 关系跨批/跨轮去重（`(src, dst, predicate)` 已存在则跳过）：图谱是渲染给眼睛看的，重复边只是噪音。
- 正文覆盖规则：新结果置信度更高才替换正文；`verified_by='user'` 的卡片正文一律不动，只并集来源与别名。
- 抽取失败一律降级为警告：走到 `extracting` 时 L1 检索已经可用，把文档判成 `failed` 等于把可用资料藏起来。
  单篇失败进 `failures`，整批失败（`batches_failed == batches`）额外发一条 `error` 事件。
- 向量化失败不中断抽取：卡片与图谱先落库（检索管线在向量索引为空时会按置信度兜底），
  失败原因进 `ExtractionStats.embedding_error` 与日志。维度守卫仍在 store 层强制（`ensure_table` 抛
  `EMBEDDING_DIM_MISMATCH`，绝不写入维度对不上的表），这里只是把它降级成"可见的错误"而不是"中止整篇抽取"。

### 跨模块改进事项

1. `types.py` 不在本阶段改动范围，所以抽取流程的模型（`ExtractionReport` / `ExtractionStats` / `ExtractedCard` 等）
   暂放在 `packages/core/agentmem/memory/models.py`。按 §9「所有模型放 types.py」的约定应该迁过去，
   建议与 Phase 3B 的模型一并处理（直接 import 也可以，没有循环依赖）。
2. `mention_count` 会在重复抽取时累加：`POST /cards/extract`、重跑 `extracting` 阶段都会 +1，
   图谱权重因此随抽取次数漂移。精确统计需要 `(entity_id, document_id)` 关联表，属 store 层改动，未自行实施。
3. Store 缺两个查询，本次用拼装绕过：
   - `RelationRepo` 没有分页/限量接口，`build_graph` 会一次性把所有关系读进内存；
   - `EntityRepo.list_by_space` 只有按 id 排序的分页，取"提及最多的 N 个实体"只能全量翻页（上限 5000）后再排。
   数据量上来后建议在 store 侧提供 `top_by_mention` 与关系分页。
4. 实体没有来源追溯：`entities` 表没有 `source_chunks` 列，模型给出的实体 marker 被丢弃（卡片与关系都有存）。
   如果要让"这个实体是从哪段原文抽出来的"可点，需要给实体加来源列。
5. `EntityRepo.create` 对已存在的 `(space, name, type)` 静默返回既有行、不累加 `mention_count`，
   本次在调用侧用 `find` + `update` 兜住（多一次查询）。store 侧若能提供 upsert-and-increment 更省事。
6. 检索侧卡片召回上限写死在 core（`MAX_CARDS = 4`）：`space.yaml` 只有 `max_insights`，没有对应的卡片上限开关。
   如需对外提供，应增加相应配置项。

### 验证（本阶段实际输出）

```
PYTHONPATH=packages/core .venv/bin/python -m pytest tests/ -q
220 passed, 1 warning in 11.70s          # 基线 187 → 220（新增 33 例）
```

```
.venv/bin/ruff check packages/core/agentmem/memory packages/core/agentmem/ingest/pipeline.py \
  packages/core/agentmem/space/runtime.py apps/api/routers/memory.py \
  tests/test_memory.py tests/test_memory_api.py tests/test_ingest.py tests/test_api.py
All checks passed!

.venv/bin/mypy --strict packages/core/agentmem/memory packages/core/agentmem/ingest \
  packages/core/agentmem/space apps/api
Success: no issues found in 28 source files
```

⚠️ 全仓库检查当前不是全绿的，但失败全部落在 Phase 3B 正在写的 `evolve/` 与 `expert/` 里（本阶段未触碰，
且这两个目录在写这份记录时仍在变化，行号与数量会随之变）：

```
.venv/bin/ruff check .                        # 8 errors，分布：evolve/{confidence,consolidate}.py、expert/{__init__,evalgen,evaluator}.py
.venv/bin/ruff format --check .               # 6 files would be reformatted，全部是 evolve/ 与 expert/
.venv/bin/mypy --strict packages/core/agentmem apps/api
                                              # 19 errors，仅 evolve/{__init__,consolidate,critique,distill}.py 与 expert/{evalgen,evaluator}.py
```

另有两处既有测试因本阶段语义变化做了调整（同属预期内）：
- `tests/test_api.py::test_skeleton_routes_return_501`：`/cards`、`/graph` 已实现，从 501 名单里移除（insights / expertise / evals / evolve 仍在）。
- `tests/test_ingest.py::test_pipeline_state_machine`：该夹具只绑定了 embedding 角色，抽取拿不到 LLM 因而不产生逐批 progress 帧；
  断言改为「前三阶段有 progress 帧 + `meta.extraction_error` 已记录 + 状态机照常走完」。

---

## 2026-09-17 · Phase 3 记忆与进化 + Phase 2 验证补充

### Phase 2 验证补充
回归检查发现此前的空格处理不完整：真正的流式路径经过
`CitationFeed`（重新裁剪空格），仅在 ChatChunk 层验证不够。补齐 3 处 ContentModel 漏改：
- `CitationFeed`（citations.py）→ ContentModel：流式正文 "Frida is a tool" 不再塌成 "Fridaisatool"
- `ScoredChunk`（retrieve/models.py）→ ContentModel：检索命中的代码缩进不再被吞
- `ParseResult`（ingest/parse.py）→ ContentModel：markdown 不再被裁剪导致 char 偏移错位
- 附带：`DeltaEvent`（types.py）、`RankedDoc`（providers/base.py）一并改为不裁剪基类
全部复现 + 回归测试见 `tests/test_regressions.py`。

### Phase 3B 进化闭环
新增 `core/evolve/`（confidence / critique / distill / consolidate / cycle / insights）
与 `core/expert/`（evaluator / expertise / evalgen）。§7 §8 全部路由 + §6 L3 经验路由落地。

关键设计：
1. 置信度规则是纯函数（`evolve/confidence.py`），严格实现 `02-DATA-MODEL.md` §4 规则表，
   18 项穷举测试。这是「经验可证伪」的唯一决策点。
2. L3 经验必须向量化才可召回：检索管线读 `insights_vec`，此前无人写入——是个功能缺口。
   `InsightService` 在「让经验生效」（新建/确认/合并转正）时写 `insights_vec`，
   「让经验失效」（归档/删除）时移除，保证「置信度状态」与「是否可被召回」一致。
   向量化的是 `trigger`（适用场景），因为召回时拿问题匹配「什么场景下适用」。
3. 无测验集不自动转正：A/B 评测（`/evolve/cycle`）在没有 EvalSet 时跳过评测，
   候选留在 candidate 等人工确认——绝不在没有裁判的情况下把经验转正。
4. A/B 分差有噪声阈值（`MIN_EVAL_DELTA=1.0`）：分差在噪声范围内不动置信度。

### 待办 / 已知近似（Phase 4）
- consistency 维度是代理指标：用「active 经验占比」近似，真实一致性需重复提问测语义相似度。
- coverage 无大纲时用卡片数对数缩放：领域大纲未持久化（仅按需生成），gaps 每次实时生成大纲。
- 进化历史用 eval_runs 近似：完整的「每次新增/淘汰经验数」需要独立的进化日志表。
- 冲突分组粗糙：consolidate 不持久化「哪几条属同一组」，list_conflicts 把全部 conflicted 作一组返回。
- 已记录的存储层问题（EntityRepo.top_by_mention、RelationRepo 分页、mention_count 精确记账）仍待补。

### 契约：SSE 事件形状
`/evolve/cycle` 的 stage 事件把 detail 拍平进顶层（`{stage, status, ...detail}`），贴合 §7 规范。
`/evals/run`、`/evals/generate`、`/evolve/distill`、`/evolve/consolidate` 均为 SSE，事件名见各路由。

---

## 2026-09-17 · Phase 3C：前端从 Mock 切换到真实后端 API

前端六大核心视图已全部完成从静态 Mock 到真实 FastAPI 后端 API 的端到端对接，并完整保留 `VITE_USE_MOCK=1`（或 URL `?mock=1`）的平滑降级开关。

### 1. 对接范围与契约对齐清单

| 页面 | 对应路由与后端契约 | 交互与流式对接说明 |
|---|---|---|
| Settings | `GET/POST/PATCH/DELETE /api/v1/providers`<br>`POST /api/v1/providers/{id}/health`<br>`POST /api/v1/providers/discover`<br>`GET/PUT /api/v1/providers/roles` | 支持 Provider 增删改查、密钥掩码回显 (`ProviderPublic.has_api_key`)、环境注入标记、本地 Ollama/vLLM 探活、角色四路绑定及 Embedding 更换时的 Reindex 警告横幅。 |
| Library | `GET/POST /api/v1/spaces/{id}/documents`<br>`/documents/upload` (FormData)<br>`/documents/paste` & `/documents/url`<br>`GET /api/v1/documents/{id}/content`<br>`DELETE /api/v1/documents/{id}`<br>`SSE GET /api/v1/spaces/{id}/ingest/stream` | 支持多格式文档上传、文本快速粘贴、URL 抓取，接入真实 SSE 摄取进度流实时驱动顶部处理队列与文档卡片阶段演进（extracting -> embedding -> complete）。支持原文字段查看与级联删除确认。 |
| Chat | `GET/POST /api/v1/spaces/{id}/conversations`<br>`PATCH/DELETE /api/v1/conversations/{id}`<br>`GET /api/v1/conversations/{id}/messages`<br>`SSE POST /api/v1/conversations/{id}/chat`<br>`POST /api/v1/conversations/{id}/stop`<br>`POST /api/v1/traces/{id}/feedback` | 重写 `useChatStream`，完整消费 `trace_start -> rewrite -> retrieval -> insights -> delta -> citation -> done` 事件序列；过程链实时高亮；正文自然拼接打字流；引用标号跨分片解析与证据抽屉联动；支持点赞、点踩（原因采集）与人工纠偏并直传真实 feedback 接口。 |
| Memory | `GET/POST /api/v1/spaces/{id}/cards`<br>`PATCH/DELETE /api/v1/cards/{id}`<br>`GET /api/v1/spaces/{id}/graph`<br>`GET/POST /api/v1/spaces/{id}/insights`<br>`PATCH /api/v1/insights/{id}`<br>`GET /api/v1/spaces/{id}/insights/conflicts`<br>`POST /api/v1/spaces/{id}/insights/conflicts/{gid}/resolve` | 呈现 L2 知识卡片（支持分类筛选与侧边抽屉编辑保存）、实体共现知识图谱、L3 经验条目管理；支持冲突横幅裁决（keep_a / keep_b / merge）与经验启用/归档状态切换。 |
| Evolve | `GET /api/v1/spaces/{id}/evolve/pending`<br>`GET /api/v1/spaces/{id}/evolve/history`<br>`SSE POST /api/v1/spaces/{id}/evolve/cycle` | 实时加载待学习反馈与纠偏统计；发起进化闭环，消费 SSE 四阶段事件（蒸馏 -> 整合 -> 评测 -> 晋升），展现流水线实时动效与结果总结看板；支持历史进化时间线渲染。 |
| Expertise | `GET /api/v1/spaces/{id}/expertise`<br>`GET /api/v1/spaces/{id}/expertise/history`<br>`GET /api/v1/spaces/{id}/expertise/gaps`<br>`GET/POST /api/v1/spaces/{id}/evals`<br>`SSE POST /api/v1/spaces/{id}/evals/generate`<br>`SSE POST /api/v1/spaces/{id}/evals/run` | 加载五维雷达图与综合得分看板；渲染自进化成长面积图；展示知识盲区与依归度树，一键跳转资料库补充；支持 AI 基于文档提炼黄金考题，以及运行双盲评测集 SSE 流式逐题打分。 |

### 2. 架构与类型规范保障

- 零后端入侵：所有修改严格限定于 `apps/web/` 目录，未改动任何 `packages/` 或 `apps/api/` 代码。
- 环境适配与降级：API Client 默认连接真实后端 (`/api/v1`)，在 `VITE_USE_MOCK=1` 或 URL 带 `?mock=1` 时透明切回内置 mock 数据。
- 四态完备：所有核心页面均统一通过 `FourStateView` 处理 `loading`、`empty`、`error` 与 `ready` 四种状态，空 Space 呈现清晰引导与空状态插画。
- 构建质量验证：
  - `pnpm --dir apps/web typecheck`：通过（0 错误）
  - `pnpm --dir apps/web lint`：通过（0 警告，0 错误）
  - `pnpm --dir apps/web build`：生产环境打包成功（7709 模块编译完成）

---

## 2026-09-17 · 本机服务配置复用

设置页支持扫描受支持的本机配置来源，展示可复用的服务地址、参数和环境变量占位符。选择候选条目后，可将其导入 Provider 配置。

- 扫描仅读取白名单配置文件，不遍历主目录，不读取登录凭据或会话令牌。
- 候选响应不返回明文密钥；缺少必要配置时会说明原因。
- 导入前检查重复条目，并确认所需角色绑定。
- 相关实现与验证位于 `providers/local_agents.py` 与 `tests/test_local_agents.py`。

---

## 2026-09-17 · 上下文经济学：前缀缓存 + 证据预算（影响 Prompt 层、Provider 层、用量接口）

设计背景与完整推导见 `01-ARCHITECTURE.md` §5.1。这里只列跨 Agent 的接口契约变化。

### 1. Prompt 装配的结构变了（改动者必读）

`agentmem.prompts.answer` 的 `build_system_prompt` 不再接受 `chunks` / `insights` / `cards`：

```python
build_system_prompt(persona, *, must_cite=True)          # 只含跨轮不变的内容
build_turn_message(question, *, chunks, insights, cards) # 随问题变化的都在这里
```

硬规则：system 只放跨轮不变的内容。 把易变内容塞回 system，不只是 system 自己不能缓存
——system 一变，后面所有历史轮次一并失配，整段对话的前缀复用归零。

守护测试 `tests/test_prompt_cache.py`。它挂了说明你把东西放错了位置，改代码不要改测试。

连带影响：`_citation_rules` 现在是模式无关的，措辞同时覆盖「本轮有资料」与「本轮无资料」，
不能再按 `bool(chunks)` 切换严格程度。

### 2. 证据区有了全局 token 预算

`render_evidence` 总量封顶 `EVIDENCE_TOKEN_BUDGET`（含标签开销），按名次分配。

⚠️ 权重必须按名次，不能按 rerank 分：各家分数量纲不可比（Cohere 0–1；
本地 CrossEncoder 是原始 logits，可负无界；无 reranker 时的 RRF 分只差千分之几）。
实测同一排序换量纲，保留条数在 5–8 之间跳。守护测试
`tests/test_budget.py::test_allocation_is_invariant_to_score_scale`。

⚠️ 已知且刻意接受的后果：marker 在渲染之前由 `assign_markers` 分配，
所以某条证据因预算被丢弃时，引用注册表里存在 `c7` 而上下文里没有 `c7`。
模型只会引用它看得见的编号，所以正常情况下不会被用到。详见 `render_evidence` 的 docstring。

### 3. Provider 层新增缓存用量字段

`ChatResult` / `ChatChunk` 增加：

| 字段 | 含义 |
|---|---|
| `cached_tokens` | 命中前缀缓存的输入 token |
| `cache_write_tokens` | 写入缓存的 token（只有 Anthropic 区分） |

⚠️ 口径已在适配器层统一为「`prompt_tokens` 包含 `cached_tokens`」。
原始口径不一致：Anthropic 的 `input_tokens` 不含缓存读写，OpenAI 系包含。
因此上层统计 不可 把 `cached_tokens` 再加一次。

各家字段名也不同，适配器已各自吸收：Anthropic `cache_read_input_tokens` /
`cache_creation_input_tokens`；OpenAI `prompt_tokens_details.cached_tokens`；
DeepSeek `prompt_cache_hit_tokens`。取不到时返回 `None`（「不知道」）而非 `0`（「确认没命中」）。

新增 provider 配置项 `extra.prompt_cache`（默认 `true`）：部分第三方中转不认
`cache_control` 会直接 400，关掉即可，不必换供应商。

### 4. 用量接口新增字段（前端需要跟进）

`GET /providers/usage` 的 `UsageGroupItem` 增加 `cached_tokens` / `cache_write_tokens` /
`cache_hit_rate`（0–1）。

⚠️ 前端措辞注意：`cached_tokens` 已含在 `prompt_tokens` 内，命中的部分只是
更便宜（约 0.1x），不是更少的 token。不要写成「节省了 X token」，
应写「X token 以约 1/10 价格计费」。

### 5. 数据库迁移 v2

`usage_records` 增加 `cached_tokens` / `cache_write_tokens` 两列。

⚠️ `store/schema.sql` 是版本 1 的快照，不是「当前 schema」。改结构时
只加迁移，不要动 `schema.sql`——两边都加会让新库 `duplicate column name` 直接建不起来
（这个坑已经踩过一次）。

---

## 2026-09-17 · 检索结果的冗余抑制（影响 space.yaml、检索接口的返回条数）

设计推导见 `01-ARCHITECTURE.md` §5.1「冗余抑制」。这里只列跨 Agent 的接口契约变化。

### 1. `space.yaml` 的 `retrieval` 段新增三个字段

| 字段 | 默认 | 含义 |
|---|---|---|
| `diversity` | `true` | 去重 + MMR 多样性重排的总开关，便于 A/B 对比 |
| `mmr_lambda` | `0.7` | MMR 的相关性权重（`1.0` = 纯相关性，`0.0` = 纯多样性） |
| `dedup_threshold` | `0.85` | 重合度高于此值判为近重复，整条丢弃 |

旧文件必须能照常加载：三个字段都有默认值，旧 `space.yaml` 里没有它们也能解析，
不报 validation error。守护测试 `tests/test_diversity.py::test_space_yaml_without_diversity_keys_still_loads`
（含「读旧文件 → 写回 → 再读」的往返）。

⚠️ `AgentMemModel` 是 `extra="forbid"`：新字段出了以后，回到旧版本代码会拒绝加载。
回滚版本时要连 `space.yaml` 一起回滚，或手工删掉这三个键。

### 2. `GET /spaces/{id}/search` 与对话链路的 L1 证据

- 条数可能少于 `top_n_rerank`：候选池里确实只剩重复内容时不再凑数。
  正常情况下由 `limit × 2` 的候选池补位，条数不变（实测 8 → 8）。
- 顺序不再等于纯相关性顺序：MMR 会在重合度高的候选之间做取舍，默认 λ=0.7 偏相关性。
  调试面板若假设「第 1 条相关性最高」，措辞要改成「按相关性与多样性综合排序」。
- 各路分数不变：`vec_score` / `bm25_score` / `rrf` / `rerank_score` 原样返回，
  重排只改顺序与取舍，不改分数。因此 `score` 的展示逻辑无需调整。
- 前端的 `RetrievalSettings` 类型（`apps/web/src/lib/api/types.gen.ts`）已同步补上三个字段。

---

## 2026-09-18 · 切片偏移、反馈回流与专家度口径（影响前端展示与旧数据）

### 1. 切片器的偏移与表格（后端已改，前端无需改代码，但旧数据要重建）

- 切片正文与 `char_start` / `char_end` 现在严格可逆：`markdown[char_start:char_end]`
  就是该切片的正文。唯一例外是 Markdown 表格的续段——它的 `content` 开头多一段
  重复的表头（列名），前缀长度 `len(content) - (char_end - char_start)`，偏移只指向
  该段自己的行区间。前端按 `char_start`/`char_end` 在全文里高亮时，不要假设
  `content` 一定等于 `markdown[char_start:char_end]`。
- 200 行以内的表格现在会切成多条，每条都自带表头。
- ⚠️ 本次改动之前入库的文档，切片偏移是错的（引用高亮会跳到错误位置）。
  修复方式：对每篇文档调用 `POST /documents/{id}/reprocess`（解析 → 重切 → 重嵌入，
  已确认会清掉旧向量）。没有 Space 级批量重建入口，需要逐个文档触发。

### 2. 反馈开始真的改变经验置信度（前端需配合防重）

`POST /traces/{trace_id}/feedback` 的响应体不变，但副作用变了：

- `up` → 该次回答注入过的经验（`trace.used_insights`）合计 +0.05；
  `down` / `correction` / `edit` → 合计 -0.15。增量按注入条数均摊，
  只有一条被注入时才等于 +0.05 / -0.15。
- 置信度跌破 0.15 的经验转 `archived` 并移出召回池。
- 每条反馈记录都会回流一次，所以同一条回答重复提交会重复计票。前端需要让
  👍/👎 提交后进入已提交态、同一 trace 同方向不可重复提交。
- `judge`（LLM 自动评分）不回流置信度，它只走 EvalSet 的 A/B 路径。
- 新增计数语义：`applied_count` 在对话落库、写入 `trace.used_insights` 时 +1
  （`use_insights=false` 的对照轮不计）；`success_count` 只在收到 `up` 时 +1。
  Memory 页展示「被应用 N 次 / 成功 M 次」时以后者为准。

### 3. 专家度 groundedness 换成真实口径（数值会下降，是变准了）

- 旧口径：最近 50 条 trace 里「检索到过证据」的占比——只要检索返回过任意一条切片
  就是 100 分，与答案有没有引用无关。
- 新口径：最近 50 条回答里，带有效引用标记的句子 / 全部句子（Markdown 标题行
  不计入分母；正文里写了标记但没有解析出对应切片的幻觉编号不算有据）。
- 影响：Expertise 页的 groundedness 与总分都会比之前低，这是纠正虚高，
  不是回归。雷达图与成长曲线的历史快照里，改动之前的点仍是旧口径。

### 4. 摄取修掉了重切留下的孤儿向量

重新解析（`POST /documents/{id}/reprocess`）此前只删 SQLite 里的切片，`chunks_vec`
里旧切片的向量留着，检索会命中回不了表的幽灵条目，向量表也随每次重切膨胀。
现在重切会按 `document_id` 清掉该文档的向量。

---

## 2026-09-18（下半场）· 检索侧指标、多臂对比、重建索引

### 1. 评测多了三个检索侧指标（前端可直接展示）

`POST /spaces/{id}/evals/run` 的 SSE 事件：
```
event: item   data: {"item_id":"...","score":72.4,"passed":true,"metrics":{...}}
event: done   data: {"variant":"baseline","score":72.4,"run_id":"...","metrics":{...}}
```
`metrics` 结构：`{context_recall, context_precision, faithfulness, claims, evidence, audited}`。
三项指标取值 `0~1`（不是 0–100），`null` 表示这一项没测到——题目没有 `must_include`、
检索为空、或 judge 没给出审计。不要用 0 兜底，也不要把「没测到」画成 0%。

`GET /spaces/{id}/evals/runs` 的每条 run：`detail.metrics`（整轮）、`detail.items[].metrics`（逐题）、
`detail.retrieval`（该轮用的检索覆盖项与 `label`）。

### 2. 新端点 `POST /spaces/{id}/evals/compare`（SSE）

入参 `{arms:[{label, retrieval?, insight_set?}], persist?}`，至少两臂，第一臂是基准。
`retrieval` 只写要改的旋钮（`top_n_rerank` / `diversity` / `mmr_lambda` / `dedup_threshold` /
`top_k_vector` / `top_k_fts` / `max_insights` / `hyde`），拼错的键会当场报错，不会被忽略。

事件：每臂开始一条 `stage`，逐臂结束时一条 `arm`（带 `label` / `run_id` / `score` / `metrics`），
最后 `done` 带 `{space_id, items, baseline, deltas:[{label, score_delta, metrics_delta, item_deltas}]}`。
`metrics_delta` 的字段可为负——它是差值，与 `metrics` 不是同一个类型。
同一配置跑两臂就是 A/A 对照，那个差值就是本轮噪声底。

### 3. `POST /spaces/{id}/reindex` 现在会重切

以前只跑 embedding，切片规则改了也不会重切，`char_start` / `char_end` 仍旧对不上原文。
现在跑 `chunking + embedding`（走解析缓存，不重新解析）。切片 id 会全部换新，
前端若有按 chunk id 缓存的选中状态需要重新取数据。

### 4. 空文档不再静默成功

解析出来没有正文（空白、扫描件、纯图片 PDF）时，文档状态判 `failed`，
`error` 里说明原因并提示改用带文本层的文件或先 OCR。此前会一路走到 `ready`，
用户以为资料进库了，实际检索不到任何内容。

### 5. SSE 流内错误改为事件下发

响应头在第一个事件发出后就已经落地，之后任何异常（包括参数校验失败）都会以
`event: error` + `{"code","message"}` 结束这条流，而不是把连接直接掐断。
前端应当监听 `error` 事件并把它展示出来，不要只依赖 HTTP 状态码。

### 6. 摄取期的文档级上下文（Contextual Retrieval）

- 新增设置 `contextual_retrieval`（默认开）。开启时，解析完成后会给整篇文档生成一段
  上下文，只拼进全文索引与嵌入的文本，切片正文与 `char_start` / `char_end` 不变，
  引用高亮不受影响。
- 该上下文存在 `GET /documents/{id}` 的 `meta.context_summary` 里，前端可以展示
  「这篇文档被理解成了什么」，也可以不展示。
- 生成失败（未配 `fast` 角色、模型报错）只记日志，不阻断摄取——退化为没有上下文。
- ⚠️ 对本轮之前入库的文档不生效，需要跑一次 `POST /spaces/{id}/reindex`（会重切重嵌）。

### 7. 轨迹 id、引用位置、切片分页（前端需要跟进两处）

- 轨迹 id 现在前后一致：此前 SSE 的 `trace_start` / `done` 给出的 `trace_id`
  与落库的 id 不是同一个，`GET /traces/{id}` 与 `POST /traces/{id}/feedback` 全都 404。
  已修，前端无需改动，但值得知道：反馈链路此前是断的。
- `Citation` 新增 `char_offset`（回答正文里的字符位置，正文是剥离标记后的文本）。
  前端不需要用它，但 `types.gen.ts` 已同步；它是「带引用的句子占比」的唯一依据。
- 切片列表支持游标分页：`GET /spaces/{id}/documents/{id}/chunks?limit=&cursor=`，
  `cursor` 取上一页返回的 `next_cursor`（值是 `ordinal`）。
  ⚠️ 前端阅读器目前只取 `limit=500` 一页，超过 500 条切片的文档仍会缺页 ——
  请改成翻页拉全。
- `POST /spaces/{id}/evals/generate` 不传 `document_ids` 时，改为从最近入库的
  若干篇 ready 文档出题（此前静默产出 0 道，按钮点了没反应）。

### 8. 分页契约统一（已完成，前端已对齐）

后端一律是 `?limit=&cursor=` + `Page{items,total,next_cursor}`。此前前端多处在传
`page` / `page_size`，FastAPI 会静默忽略——列表照常返回，只是永远停在默认条数。
已全部改为 `limit` + `next_cursor` 翻页：会话列表、文档列表、卡片、经验、切片。

- 切片列表：`next_cursor` 的值是 `ordinal`；阅读器改为循环拉全，超大文档显示已取条数。
- `POST /spaces/{id}/search` 的入参是 `top_k`（不是 `limit`）；⌘K 检索已修正。
- `POST /spaces/{id}/evals/generate` 不传 `document_ids` 时从最近入库的 ready 文档出题。

给后续实现的提醒：凡是「新加一个带查询参数的接口」，都要同时改三处——
后端路由、`docs/03-API-SPEC.md` §1 的分页约定、前端的调用点。这类参数名写错
不会报错，只会静默少拿数据，已经出现过五次（文档内容路径、切片参数、
测验集参数、经验/卡片参数、检索 top_k）。

### 9. 一致性改成实测口径（前端需要标注来源）

- `GET /spaces/{id}/expertise` 与快照多了两个字段：
  - `consistency_source`: `"probe"`（真的重复提问测过）| `"proxy"`（用 active 经验占比近似）
  - `consistency_measured_at`: 实测时间（毫秒），`proxy` 时为 `null`
- 新端点 `POST /spaces/{id}/expertise/consistency?questions=3&repeats=3`：同步返回一次
  探测结果（`{id, questions, repeats, similarity, detail:[{question, similarity, answers}]}`），
  落库后专家度优先采用。`repeats` 至少 2；一个问题都没问过时返回 422。
- ⚠️ 界面上「逻辑一致性」必须显示来源：`proxy` 时标注为「近似」并给出实测入口
  （一个按钮触发上面的端点），否则用户会以为这个数字是测出来的。

### 10. 证据的召回路径与更完整的进化历史（前端可用来做可视化）

- `/spaces/{id}/search` 的每条 hit、以及轨迹 `GET /traces/{id}` 的每条 `retrieved`，
  新增 `legs: string[]`：这条证据被哪几路召回（`vector:1` / `fts:1` / `graph`）。
  证据面板可以据此标出「向量 / 全文 / 图谱」来源——调试时最常问的是「它为什么会出现」。
- `GET /spaces/{id}/evolve/history` 的每条现在带 `expertise_before` / `expertise_after` /
  `duration_ms`（此前只有 `run_at` 与 `eval_delta`，且 `eval_delta` 是拿评测分数顶替的）。
