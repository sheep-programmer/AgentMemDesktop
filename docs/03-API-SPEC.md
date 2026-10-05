# AgentMem · HTTP API 规范

Base URL：`http://127.0.0.1:8765/api/v1`

通用约定：
- 请求/响应均 `application/json`，除文件上传（`multipart/form-data`）与 SSE（`text/event-stream`）
- 成功返回资源本体或 `{ "items": [...], "total": n }`
- 失败统一 `{ "error": { "code": "...", "message": "...", "detail": {...} } }`，HTTP 状态码语义化
- 所有 `space` 相关路由前缀 `/spaces/{space_id}`
- 分页参数：`?limit=50&cursor=<id>`，返回 `next_cursor`

---

## 1. 系统 / System

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{status, version, uptime_ms}` |
| GET | `/capabilities` | 后端能力声明，前端据此显隐功能。返回 `{rerank_available, local_embedding, docling_available, graph_enabled}` |
| GET | `/stats` | 全局统计：space 数、文档数、chunk 数、磁盘占用 |

---

## 2. 模型 Provider

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/providers` | 列出全部 provider（返回 `ProviderPublic`，见下方安全约定） |
| GET | `/providers/{id}` | 单个详情。⚠️ 路由必须声明在 `/roles`、`/usage`、`/discover` 之后，否则会把这些字面路径吃掉 |
| POST | `/providers` | 新增（写回 `config/models.yaml`） |
| PATCH | `/providers/{id}` | 修改 |
| DELETE | `/providers/{id}` | 删除（被 role 引用时拒绝，返回 `PROVIDER_IN_USE`） |
| POST | `/providers/{id}/health` | 实测连通性：`{ok, latency_ms, resolved_model, error}` |
| POST | `/providers/discover` | 入参 `{adapter, base_url}`，探测该端点可用模型列表。用于「一键发现本机 Ollama 模型」 |
| GET | `/providers/roles` | 当前角色绑定 `{chat, fast, distill, judge, embedding, rerank}` |
| PUT | `/providers/roles` | 更新角色绑定。若 `embedding` 维度变化，返回 `{requires_reindex: true, affected_spaces: [...]}` |
| GET | `/providers/usage` | 用量统计，支持 `?group_by=provider|model|purpose&since=` |

### 2.1 密钥安全约定（强制）

任何 HTTP 响应都不得包含明文 `api_key`。 provider 的对外模型是 `ProviderPublic`：

```jsonc
{
  "id": "deepseek-chat",
  "kind": "llm",
  "adapter": "openai_compatible",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1",
  "has_api_key": true,              // 是否已配置
  "api_key_hint": "••••9f2a",       // 仅供界面辨识，只留尾 4 位
  "api_key_from_env": "${DEEPSEEK_API_KEY}",  // 来自哪个环境变量占位符
  "dimension": null, "device": null, "enabled": true, "extra": {}
}
```

写入方向（`POST` / `PATCH`）仍接受明文 `api_key`——用户总得有办法填进来。

落盘同样不得明文：`load_models_config` 会记住每个 `api_key` 在 YAML 里的原始写法
（`ProviderConfig.api_key_ref`），`save_models_config` 写回时原样还原 `${VAR}`。
靠事后反查环境变量去猜哪个值原本是占位符是不可靠的（变量可能已变更、多个变量可能同值），
只作为用户在界面手填密钥时的兜底，且只作用于 `api_key` 字段。

---

## 3. Space

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/spaces` | 列表，附带 `doc_count`、`insight_count`、`expertise_overall` |
| POST | `/spaces` | `{name, domain, icon?, color?, description?}`，自动生成初始 Persona |
| GET | `/spaces/{id}` | 详情（含 `space.yaml` 解析后的 persona/retrieval/models） |
| PATCH | `/spaces/{id}` | 修改基本信息 |
| DELETE | `/spaces/{id}` | 删除（`?purge=true` 同时删磁盘数据） |
| GET | `/spaces/{id}/persona` | 获取 L4 画像 |
| PUT | `/spaces/{id}/persona` | 更新 L4 画像 |
| POST | `/spaces/{id}/persona/suggest` | 让 LLM 根据已有文档自动起草 Persona（返回草稿，不直接落库） |
| GET | `/spaces/{id}/settings` | 检索参数等 |
| PUT | `/spaces/{id}/settings` | |
| POST | `/spaces/demo` | 铺设示例 Space（幂等：同名已存在则返回它），返回 `Space` |
| POST | `/spaces/{id}/reindex` | 重建切片与向量索引（重切 + 重嵌入，走解析缓存），SSE 返回逐篇进度。`?only_stale=true` 重做失败 / 中断文档以及切片算法版本过期的文档（不清空向量表，可用来接着跑完中断的重建）。重建期间该 Space 拒绝新的摄取与重新解析（409 `SPACE_LOCKED`） |
| GET | `/spaces/{id}/export` | 导出为 zip（含 raw + meta.db + yaml） |
| POST | `/spaces/import` | 导入 zip |

---

## 4. 文档 / L0-L1

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/spaces/{id}/documents` | 支持 `?status=&q=&tag=` |
| POST | `/spaces/{id}/documents/upload` | multipart，支持多文件。立即返回 `{documents: [...]}`（status=pending），处理异步 |
| POST | `/spaces/{id}/documents/url` | `{url, crawl_depth?}` 抓取网页 |
| POST | `/spaces/{id}/documents/paste` | `{title, content}` 直接粘贴文本 |
| GET | `/spaces/{id}/documents/{doc_id}` | 详情 |
| GET | `/spaces/{id}/documents/{doc_id}/content` | 解析后的 Markdown 全文 |
| GET | `/spaces/{id}/documents/{doc_id}/chunks` | 切片列表 |
| GET | `/spaces/{id}/documents/{doc_id}/raw` | 下载原始文件 |
| DELETE | `/spaces/{id}/documents/{doc_id}` | 级联删除 chunk 与向量 |
| POST | `/spaces/{id}/documents/retry-failed` | 把该 Space 里 `failed` 的文档整条链路重跑一遍（SSE：`begin` / `progress` / `error` / `done`），覆盖全部分页，单篇失败不阻断其它；`done.retried` 是确认成功数 |
| POST | `/spaces/{id}/documents/{doc_id}/reprocess` | 重新解析 |
| GET | `/spaces/{id}/ingest/stream` | SSE：全局摄取进度流 |

SSE 事件格式
```
event: progress
data: {"document_id":"01H...","stage":"embedding","done":42,"total":180,"percent":23}

event: status
data: {"document_id":"01H...","status":"ready"}

event: error
data: {"document_id":"01H...","code":"PARSE_FAILED","message":"..."}
```

---

## 5. 检索与对话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/spaces/{id}/search` | 纯检索（不生成）。入参 `{query, top_k?, mode?: "hybrid"\|"vector"\|"fts"}`，返回带各路分数的命中列表，用于「检索调试面板」 |
| GET | `/spaces/{id}/conversations` | 会话列表，支持 `q` 标题搜索（最长 200 字）与游标分页；搜索计数覆盖该空间全部会话 |
| POST | `/spaces/{id}/conversations` | 新建 |
| GET | `/conversations/{cid}` | 含消息列表 |
| PATCH | `/conversations/{cid}` | 改标题 / pin |
| DELETE | `/conversations/{cid}` | |
| POST | `/conversations/{cid}/chat` | SSE 流式问答，见下 |
| POST | `/conversations/{cid}/stop` | 中断生成 |

`POST /conversations/{cid}/chat` 入参
```jsonc
{
  "content": "这个壳怎么脱？",
  "use_retrieval": true,     // 关掉则纯聊天
  "use_insights": true,      // 关掉则不注入 L3，用于 A/B 对比
  "context_mode": "standard", // standard（默认）或 economy；上下文预算与摘取策略
  "llm_role": "chat",        // 可临时指定角色
  "attachments": ["doc_id"]  // 限定只在指定文档内检索
}
```

SSE 事件序列（顺序有意义，前端按此渲染）
```
event: trace_start
data: {"trace_id":"01H...","message_id":"01H..."}

event: rewrite            # 查询改写结果（若开启）
data: {"rewritten":"..."}

event: retrieval          # 检索命中，前端立刻展示"参考了 8 篇资料"
data: {"chunks":[{"id":"...","document_id":"...","title":"...","page":3,"score":0.82,"snippet":"...","kind":"body",
        "vec_score":0.67,"bm25_score":17.2,"rrf":0.032,"rerank_score":0.15,"legs":["vector:1","fts:1"],"merged_from":[]}],
       "degraded":[]}   # 各路分数与召回路径与轨迹里的 retrieved 同源；degraded 见 RetrievalResult.degraded

event: insights           # 本次注入的经验，前端展示"应用了 3 条经验"
data: {"insights":[{"id":"...","trigger":"...","guidance":"...","confidence":0.85}]}

event: context            # 本轮输入文本的启发式估算，不是服务商计费 token
data: {"mode":"economy","original_estimated_tokens":3000,"estimated_tokens":1800,
       "saved_estimated_tokens":1200,"history_messages":2,"evidence_count":3,"insight_count":1,"card_count":1}

event: delta              # 正文流式增量
data: {"text":"根据"}

event: citation           # 引用标记落位
data: {"marker":"c3","chunk_id":"...","document_id":"...","page":3}

event: done
data: {"message_id":"...","trace_id":"...","usage":{"prompt_tokens":3012,"completion_tokens":428,"latency_ms":4210}}

event: error
data: {"code":"PROVIDER_TIMEOUT","message":"..."}
```

`context_mode` 控制本次回答的历史、L1、L2、L3 预算，不减少候选召回数量，也不触发额外模型摘要。标准模式保持现有 L1 预算；节省模式可摘取与问题相关的原文窗口。索取原文时采用标准策略。`context` 事件中的模式是实际采用的策略，统计本轮已注入的资料数量；上下文溢出重试前可以再次发送，客户端应使用最后一份估算。实际计费用量仍来自 `done.usage` 和服务商 usage 记录。

`insights` 及轨迹的 `used_insights` / `used_cards` 只包含实际注入的条目。预算丢弃的 L1 编号不会注册为可用引用；原始文档、切片 ID 和位置保持可回溯。输入估算通过流即时展示，当前不单独持久化为轨迹字段。

---

## 6. 记忆 L2 / L3

### L2 知识卡片
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/spaces/{id}/cards` | `?kind=&q=&min_confidence=` |
| POST | `/spaces/{id}/cards` | 手动新建 |
| PATCH | `/spaces/{id}/cards/{card_id}` | 编辑（人工校订，`verified_by='user'`、confidence 拉满） |
| DELETE | `/spaces/{id}/cards/{card_id}` | |
| POST | `/spaces/{id}/cards/extract` | 从指定文档批量抽取，SSE 进度 |
| GET | `/spaces/{id}/graph` | 实体关系图：`{nodes:[{id,name,type,weight}], edges:[{src,dst,predicate,weight}]}`，支持 `?center=&depth=2&limit=300` |

### L3 经验
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/spaces/{id}/insights` | `?status=&kind=&q=&sort=confidence\|recent\|applied` |
| POST | `/spaces/{id}/insights` | 手动新增（`origin='manual'`，confidence=0.8） |
| PATCH | `/spaces/{id}/insights/{iid}` | 编辑内容或直接调整 status |
| DELETE | `/spaces/{id}/insights/{iid}` | |
| POST | `/spaces/{id}/insights/{iid}/promote` | 人工确认 → active，confidence +0.3 |
| POST | `/spaces/{id}/insights/{iid}/archive` | 归档 |
| GET | `/spaces/{id}/insights/conflicts` | 冲突组列表 `[{group_id, insights:[...]}]` |
| POST | `/spaces/{id}/insights/conflicts/{gid}/resolve` | `{keep_id, archive_ids[], merged_text?}` |
| GET | `/spaces/{id}/insights/review` | 值得复查的经验：注入 ≥ 5 次且成功率 ≤ 40%（统计线索，不是判决），按成功率升序 |
| GET | `/spaces/{id}/insights/{iid}/history` | 置信度变更流水：每次加减分与状态流转（`event / confidence_before / confidence_after / status_* / share / reason`），旧的在前 |
| GET | `/spaces/{id}/insights/{iid}/lineage` | 溯源：由哪些 trace / feedback 产生，取代了谁 |

---

## 7. 进化闭环

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/traces/{trace_id}` | 完整轨迹（检索明细、注入的经验、prompt token 分布），供「为什么这么答」面板 |
| POST | `/traces/{trace_id}/feedback` | `{kind: "up"\|"down"\|"correction"\|"edit", comment?}`。落库后立即回流置信度：对该次回答注入过的经验（`trace.used_insights`）施加一次事件——`up` +0.05、`down`/`correction`/`edit` -0.15，都按注入条数均摊；跌破 0.15 归档并移出召回池。响应体不变 |
| POST | `/traces/{trace_id}/judge` | 触发 LLM-as-Judge 打分（分数只进 A/B 评测路径，不回流置信度） |
| GET | `/spaces/{id}/evolve/pending` | 待蒸馏的反馈数量与预览 |
| POST | `/spaces/{id}/evolve/distill` | 触发蒸馏，SSE。产出 InsightCandidate 列表 |
| POST | `/spaces/{id}/evolve/consolidate` | 去重/合并/冲突检测，SSE |
| POST | `/spaces/{id}/evolve/cycle` | 一键完整进化：distill → consolidate → evaluate → promote，SSE 全程推送每阶段结果。这是产品的「高光按钮」 |
| GET | `/spaces/{id}/evolve/history` | 历次进化（读进化日志）：每次 `produced / merged / conflicts / promoted / demoted / eval_delta / expertise_before / expertise_after / duration_ms` |

`/evolve/cycle` SSE 事件

实际只会发 `stage` 与 `done` 两类（外加流内异常时的 `error`）：每一阶段的产出直接摊平
进 `stage` 事件的 `detail` 里，前端因此只需要一个处理器。

```
event: stage  data: {"stage":"distill","status":"running"}
event: stage  data: {"stage":"distill","status":"done","produced":7,"skipped":0}
event: stage  data: {"stage":"consolidate","status":"done","merged":2,"duplicates":0,"conflicts":1}
event: stage  data: {"stage":"evaluate","status":"running","variant":"baseline"}
event: stage  data: {"stage":"evaluate","status":"done","baseline":62.4,"with_insights":71.8,"delta":9.4}
event: stage  data: {"stage":"promote","status":"done","promoted":5,"demoted":2}
event: done   data: {"delta":9.4,"expertise_before":58.2,"expertise_after":64.7,
                     "produced":7,"promoted":5,"demoted":2,"eval_delta":9.4}
```

> 早先这里写过 `candidate` 与 `eval` 事件，但实现从未发出过——文档与代码不一致，
> 前端照着文档写就会一直等一个不来的事件。对齐以本页为准：需要逐条候选经验时走
> `POST /evolve/distill`（它确实会发 `candidate`）。阶段名是 `evaluate`，
> 不是 `eval`。

---

## 8. 专家度与评测

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/spaces/{id}/expertise` | 当前五维分数 + 总分 |
| GET | `/spaces/{id}/expertise/history` | 快照序列，画成长曲线。`?since=&bucket=day\|week` |
| POST | `/spaces/{id}/expertise/consistency` | `?questions=3&repeats=3` 测一次一致性：取最近问过的问题各回答若干遍，比较答案语义相似度并落库。`repeats ≥ 2`，没问过问题时报 422 |
| POST | `/spaces/{id}/expertise/outline` | 重新生成领域大纲并落库，返回 `{outline, covered, total}`。大纲决定覆盖率的分子分母与盲区基准，但生成要调模型，所以只在显式调用时跑 |
| GET | `/spaces/{id}/expertise/gaps` | 知识盲区：领域大纲中尚无资料覆盖的节点 → 引导用户补充投喂 |
| GET | `/spaces/{id}/evals` | 测验集列表 |
| POST | `/spaces/{id}/evals` | 手动新增题目 |
| POST | `/spaces/{id}/evals/generate` | 从文档自动生成题目，SSE |
| PATCH/DELETE | `/spaces/{id}/evals/{eid}` | |
| POST | `/spaces/{id}/evals/run` | `{variant, insight_set?}` 执行评测，SSE 逐题推送 |
| POST | `/spaces/{id}/evals/compare` | `{arms:[{label, retrieval?, insight_set?}], persist?}` 多臂对比检索配置，SSE。第一臂为基准，其余各臂给逐题配对差值；同一配置跑两臂即 A/A 对照（噪声底） |
| GET | `/spaces/{id}/evals/runs` | 历次评测结果。`detail.metrics` 是整轮检索指标，`detail.items[].metrics` 是逐题指标，`detail.retrieval` 记录该轮用的检索覆盖项 |

评测的 SSE 事件
```
event: stage  data: {"stage":"evaluate","status":"running","variant":"baseline"}
event: item   data: {"item_id":"...","score":72.4,"passed":true,"metrics":{...}}
event: done   data: {"variant":"baseline","score":72.4,"run_id":"...","metrics":{...}}
```

摄取的并发闸门：同时运行的摄取任务数由 `AGENTMEM_INGEST_CONCURRENCY`（默认 2）决定，超出的在总线里排队，SSE 仍会按文档推送各自的进度与状态事件；一篇失败只发一条 `error`，不影响其它文档。

检索命中的召回路径：`/spaces/{id}/search` 的每条 hit 与轨迹里的每条 `retrieved` 都带 `legs: ["vector:1","fts:1","graph"]`——这条证据是被哪几路召回的。调试「它为什么会出现」比看它排第几更有用。

`metrics` 的字段（`0~1` 的比例，不是 0–100 的分数；`null` 表示没测到，与「测出来是 0」不同）：
`context_recall` 参考答案要点中被证据支撑的比例（低=检索没捞到）、
`context_precision` 被用到的证据排得靠前不靠前（低=排序问题）、
`faithfulness` 答案论断中有证据支撑的比例（低=模型没用上证据）、
`claims` / `evidence` / `audited`。

流内错误：SSE 响应头在第一个事件发出后就已经落地，之后任何异常都以
`event: error` + `{"code","message"}` 收场，不会把连接直接掐断。

---

## 9. 状态码与错误码

| code | HTTP | 含义 |
|---|---|---|
| `NOT_FOUND` | 404 | 资源不存在 |
| `VALIDATION_ERROR` | 422 | 参数错误 |
| `SPACE_LOCKED` | 409 | 该 Space 正在重建索引，此时不接受投喂 / 重新解析 |
| `PROVIDER_NOT_CONFIGURED` | 400 | 角色未绑定 provider |
| `PROVIDER_UNAVAILABLE` | 503 | 连不上模型服务 |
| `PROVIDER_TIMEOUT` | 504 | |
| `PROVIDER_IN_USE` | 409 | 删除被引用的 provider |
| `EMBEDDING_DIM_MISMATCH` | 409 | 向量维度与索引不符，需 reindex |
| `PARSE_FAILED` | 422 | 文档解析失败 |
| `DUPLICATE_DOCUMENT` | 409 | sha256 已存在 |
| `EVAL_SET_EMPTY` | 400 | 无测验题无法评测 |
| `INTERNAL_ERROR` | 500 | |

---

## 10. 实现要求

- 路由文件按 §1–§8 分组，与 `apps/api/routers/` 一一对应
- 每个路由必须声明 `response_model`，保证 OpenAPI schema 完整（前端类型由此生成）
- SSE 统一走 `apps/api/sse.py` 的 `sse_response(generator)` 包装，禁止各路由自己拼字符串
- 所有长任务必须可中断：SSE 断开时取消底层 `asyncio.Task`
- 开发模式允许 CORS `http://localhost:5173`；生产模式 FastAPI 直接 `StaticFiles` 托管前端 `dist/`，同源无需 CORS


### 摄取并发约定（2026-10-02）

同一文档已在处理或排队时，重复重新解析返回 `SPACE_LOCKED`。摄取、聊天或文档抽取未退出时，不启动互斥索引维护；冲突在普通请求中返回 409，在已开始的 SSE 中通过 `error` 帧返回该错误码。重复上传 / 登记的内容返回 `DUPLICATE_DOCUMENT`，成功请求的原文不会由重复请求清理。

`only_stale=true` 除算法版本过期之外，也选择失败 / 中断状态的文档，用于补齐切片已更新但向量化未完成的重建。取消处理的文档会转入失败状态，可以重新解析。
