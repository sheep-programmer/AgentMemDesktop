# AgentMem · 知识处理层差距分析（对标 LlamaIndex / Anthropic / Graphiti / mem0 / Letta / LightRAG / RAGFlow / RAGAS）

> 调研日期：2026-09-17。差距表里每一条的「我们现在怎么做」都在当前 commit 的代码上逐行核对过，
> 给出文件与函数名；「对标项目怎么做」给出可核查来源（论文 / 官方文档 / 仓库路径），
> License 与仓库状态经 GitHub API 实查，见附录 B。
>
> 调研范围：L0→L1 摄取与切分、L1 检索与上下文装配、L2 卡片与实体图、L3 经验召回与晋升、评测与度量。
> 不在范围内：前端、多 Space 隔离、Provider 抽象层。

---

## 0. 先摆三个自己的问题

这三条与对标无关，是读代码时实测出来的，而且比表里绝大多数行更要紧，所以放在最前面。
它们同时也各占差距表的一行（G1 / G2 / G8）。

### 0.1 切片器的三个连锁缺陷（已复现）

`ingest/chunk.py` 的断句正则 `_SENTENCE_SPLIT = r"(?<=[。！？!?；;])|(?<=\.)\s+"`
在匹配英文句末时把句号后的空格吃掉了（`\s+` 是消耗性的，不在 lookbehind 里），
于是 `"".join(_split_sentences(t)) != t`。实测：

```python
src = "Fig. 3 shows the result. The IC50 is 12 nM. 结论如下。  下一段开始。"
"".join(_split_sentences(src))
# 'Fig.3 shows the result.The IC50 is 12 nM.结论如下。  下一段开始。'   ← 空格被吞、与原文不等
len(_split_sentences(src))  # 4
len(_split_sentences("".join(_split_sentences(src))))  # 1  ← 不幂等
```

连锁后果有三个，都在 `split_markdown` 的真实输出上验证过：

1. 正文被破坏。`_split_oversize` 把句子 `"".join` 回 buffer 时，`in vitro. Selectivity`
   已经变成 `in vitro.Selectivity`。这段文字最终会作为 `chunks.content` 落库、送进模型。
2. `char_start` / `char_end` 不再框住 `content`。`_split_oversize` 用重建串的长度推进
   `cursor`，与原文偏移脱钩。实测一个 1081 token 的英文长段切成 2 条，
   两条的 `md[char_start:char_end] == content` 全为 False，第二条的
   `char_end` 甚至超出了文档长度。引用高亮依赖这两个字段，跳转会整体错位。
3. `_tail_overlap` 失效，相邻切片变成 2 倍目标大小。重叠段取自
   `_tail_overlap(previous, 80)`，它内部再调一次 `_split_sentences`；
   而被吞掉空格的重建串里已经没有任何 `.\s+` 边界，于是"80 token 的重叠"
   把整块 501 token 当成重叠返回。实测同一份文本：
   `ord=0 tok=501（目标 512，正常）` → `ord=1 tok=962`（1.9 倍目标，超过 `_OVERSIZE_FACTOR` 的上界）。

触发条件（诚实说明，不要过度主张）：某个空行分隔的块超过 `512 × 1.5 = 768` token，
并且块内有英文句末标点。中文句末标点（`。！？；`）不消耗后续空白，所以纯中文正文不受影响——
CJK 路径下偏移只差几个字符（重叠段与新内容之间插入的 `"\n\n"` 与 `strip()` 吃掉的首尾空白），
引用高亮偏 1–2 个字符，可接受。受影响的是：英文/中英混排的长段落、
OCR 丢掉了段落分隔的 PDF、无空行的纯文本粘贴（`ingest/parse.py::parse_text` 直接吃用户粘贴的整段）。

### 0.2 超长无空行块完全不切分（比目标大 5 倍）

`_split_blocks` 按空行切块；Markdown 表格内部没有空行，于是整张表是一个块。
块超过 768 token 时交给 `_split_oversize`，而它的断句函数 `_SENTENCE_SPLIT` 不含换行符，
整张表被当成一个"句子"，切不出任何东西。实测一张 200 行 Markdown 表（2621 token，5 倍目标）：

```
切片数: 1
  ordinal=0 tokens=2621 chars=(0,4995)
```

这条切片送进 `prompts/answer.py::render_evidence` 后，`budget.trim_to_budget` 会走
"首句就放不下"的字符截断分支（表行是 `\n` 分隔的句子，头 2/3 + 尾 1/3），
表头只在留下的前半段里，后半段的数值失去列名。

### 0.3 经验反馈闭环里有两条规则从未接线

`docs/02-DATA-MODEL.md` §4 明确规定 `applied_count` = "被注入上下文的次数"、
`success_count` = "注入后得到正反馈的次数"，以及"应用后得正反馈 +0.05 / 负反馈 -0.15"。

实测全仓库 grep 结果：

| 名字 | 定义处 | 写入处 | 调用处 |
|---|---|---|---|
| `applied_count` / `success_count` | `types.py:381-382`、`store/repos/insights.py:31` | 无 | 仅 `insights.py:99` 用于排序 |
| `ConfidenceEvent.POSITIVE_FEEDBACK` / `NEGATIVE_FEEDBACK` | `evolve/confidence.py:39-40` | — | 无 |

真正会改变置信度的只有两条路径：`evolve/cycle.py::_apply_verdict`（A/B 总分判决，整批施加）
与 `evolve/insights.py::promote`（用户手动确认）。于是：
一次回答注入了 6 条经验、用户点了 👎，`evolve/distill.py` 会把这次差评变成一条新的候选经验，
而那 6 条里真正害人的那条置信度一分未降、状态仍是 `active`，下次照样注入。

---

## 1. 差距表

已具备的能力不列在此表，见附录 A——那里逐条写明「已具备」并给出实现位置，避免把已有能力包装成差距。
优先级列：`P0` 立刻做 / `P1` 本阶段 / `P2` 下一阶段 / `P3` 有余力再做 / `P4` 依赖前置。

| # | 能力点 | 对标项目怎么做 | 我们现在怎么做 | 差距的实际影响（具体场景） | 改动成本 | 风险 | 优先级 |
|---|---|---|---|---|---|---|---|
| G1 | 切片与原文的可逆映射（英文超长块 / 字符偏移） | LlamaIndex 的 `TextNode` 携带 `start_char_idx` / `end_char_idx`，node 与原文之间保证可逆定位（[api_reference/schema](https://docs.llamaindex.ai/en/stable/api_reference/schema/)，实查两字段存在）；RAGFlow DeepDoc 先做版面分析再切分，从结构上避免"句子被重建" | `ingest/chunk.py::_split_sentences`（正则 `(?<=\.)\s+` 吃掉句后空格，导致 `"".join(...) != 原文` 且不幂等）、`_split_oversize`（用重建串长度推进 `cursor`）、`split_markdown`（`char_start/char_end` 由 `content_start + len(content)` 反推） | 投喂一篇英文论文，某个 1000+ token 长段被切开后：模型读到的是 `in vitro.Selectivity` 这种被粘住的句子；前端拿 `char_start` 去全文里定位，`md[0:2358] != content`，点引用跳转高亮到错误位置；紧接着第二条切片涨到 962 token（目标 512），挤占证据预算 | 低（3 个纯函数） | 低（会改变既有文档的切片，须重建索引；`tests/test_ingest.py` 可加不变量断言） | P0 |
| G2 | 超长无空行块（典型是 Markdown 表格）的切分 | RAGFlow DeepDoc 用 TSR 先识别表格结构再切（[deepdoc/README](https://github.com/infiniflow/ragflow/blob/main/deepdoc/README.md)，`t_recognizer.py --mode tsr`，10 类版面组件含 Table / Figure caption）；LlamaIndex 的 `SentenceSplitter` / `SemanticSplitterNodeParser` 至少保证不超目标 | `ingest/chunk.py::_split_blocks`（按空行切块）+ `_split_oversize`（断句正则 `_SENTENCE_SPLIT` 不含 `\n`）。实测 200 行表 → 1 条 2621 token 的切片 | 用户从财报/申报材料里投喂一张 200 行的参数表，问「表里 IC50 最小的是哪个」。这条切片进上下文后被 `prompts/budget.py::trim_to_budget` 头 2/3 + 尾 1/3 斩断，后半段的数字没有列名（表头只在前半段），模型只能猜哪一列是 IC50 | 低 | 低 | P0 |
| G3 | 摄取期文档级上下文预置（Contextual Retrieval） | Anthropic：给每个 chunk 预置 50–100 token 的文档级上下文，再嵌入与建 BM25 索引。Contextual Embeddings 把 top-20 召回失败率从 5.7% 降到 3.7%（-35%），叠加 Contextual BM25 降到 2.9%（-49%），再加 rerank -67%；一次性成本 $1.02 / 百万文档 token（[anthropic.com/news/contextual-retrieval](https://www.anthropic.com/news/contextual-retrieval)，数字已从原文页核对） | `ingest/pipeline.py::IngestPipeline._stage_embedding` 直接把 `chunk.content` 送去 embedding；`store/fts.py::to_index_text` 索引的也是裸正文；`heading_path` 只作为元数据存下，最终出现在 `prompts/answer.py::_evidence_location` 拼出的 `source=` 属性里，从不进入被检索的文本 | 医药文档里「该化合物可致 QT 间期延长」这一段，化合物名只在前一节出现过。用户问「XX 化合物的心脏毒性」时，这一段的向量不带化合物名、BM25 靠 jieba 词元也匹配不到（词表里没有"XX"），于是这条已知副作用整条漏检。同理，「第三章提到的三个限制条件」这种带定位信息的问法也命中不了 | 中（每篇文档一次 LLM 调用生成摘要 + 重算 embedding 与 FTS 索引） | 中（上下文串味：摘要不准会把错误主题灌进整篇的每个切片，必须可开关；摘要不得写进 `content`，否则引用高亮漂移） | P1 |
| G4 | 检索侧指标（context recall / context precision / faithfulness） | RAGAS 三个公式：`Faithfulness = 被检索上下文支撑的论断数 / 论断总数`；`Context Recall = 参考答案里能被检索上下文支撑的论断数 / 参考答案论断总数`；`Context Precision@K = Σ(Precision@k × v_k) / top-K 中相关条目数`（[docs.ragas.io](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)，公式取自已迁移的 [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas) 仓库源文件）。TruLens 的 RAG triad 把「检索相关性 / 有据性 / 答案相关性」拆成三把独立尺子 | `expert/evaluator.py::EvaluationService` 只有一条路径：生成答案 → 单次 LLM judge 打一个 0–100 总分（`prompts/judge.py::build_eval_judge_messages`），落进 `EvalRunDetail.items`，而 `types.EvalItemScore` 只有 `score / passed / reason` 三个字段。检索侧一条指标都没有 | 把 `EVIDENCE_TOKEN_BUDGET` 从 2000 调到 1500、或把 `top_n_rerank` 从 8 调到 6，唯一能看到的信号是答案总分动了 0.6 分——低于 `evolve/cycle.py::MIN_EVAL_DELTA = 1.0`，判决为「噪声内，不动」。我们无法区分「某条关键证据被预算丢了」和「模型没用好已经拿到的证据」，因此不知道该改检索还是改提示词 | 低（三条公式；输入全都有：`EvalItem.must_include` 当 claim 集合、检索到的 `ScoredChunk.content` 当 context；复用现有 `registry.llm("judge")` 与 `prompts/` 即可，不需要引入 RAGAS 依赖） | 低（对 `EvalRunDetail` 是纯加字段） | P0 |
| G5 | A/B 能对比「检索配置」，而不只是「注入哪些经验」 | 评测框架（RAGAS / LangSmith 一类）把待测变量当作运行参数，同一次实验内跑多臂并用同一批题目做配对比较，报告逐题差值而非单一总分 | `types.py:39` `EvalVariant = Literal["baseline","with_insights","custom"]`；`expert/evaluator.py::EvaluationService.__init__` 在构造时绑定一个 `self.pipeline`，其 `RetrievalSettings` 来自 `space.yaml`，`run()` 签名里没有任何检索覆盖参数；`evolve/cycle.py::_evaluate_and_promote` 也只跑 baseline / with_insights 两臂 | 我改了 `retrieve/diversity.py` 的 `mmr_lambda`、或加了 `retrieve/overlap.py` 的相邻裁剪，想证明它有效，只能手改两次 `space.yaml`、跑两次全量评测、眼看着两个数比较。两次之间 `ANSWER_TEMPERATURE=0.3` 与 `JUDGE_TEMPERATURE=0.2` 的采样噪声、题目顺序效应全部混在里面，也没有噪声底。「可证伪」这条主线在检索侧是断的——而这恰恰是产品唯一的差异点 | 中（`run()` 增加 `retrieval_override`，`EvalRunCreate` 记录该配置，一次 run 内跑两臂并保存 per-item 差值） | 中（评测成本翻倍；需要为 A/A 对照留出接口） | P0 |
| G6 | L2 卡片的事实失效与时效 | Graphiti / Zep：fact 带 validity window，新信息到来时旧 fact 被 invalidate 而不是删除，可以查「现在为真」也可以查「某时刻为真」（[getzep/graphiti README](https://github.com/getzep/graphiti)：「old facts are invalidated — not deleted」，Apache-2.0；论文 [arXiv:2501.13956](https://arxiv.org/abs/2501.13956)）。mem0 论文的图变体同样「mark them as invalid rather than physically removing them to enable temporal reasoning」（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413) §graph memory） | `memory/cards.py::CardService._merge`：同名同类卡片若 `data.confidence > existing.confidence` 就直接替换 `body`，旧正文没有任何留存；`types.KnowledgeCard` 没有 valid_from / valid_to / superseded_at；`memory/extract.py::_store_entities` 对已存在实体只累加 `mention_count`（这条漂移已在 ROADMAP 技术债里） | 投喂一版新的临床指南，卡片「XX 的推荐剂量」正文被覆盖成新值。用户再也看不到「上一版写 400mg、这一版改成 200mg」，而"什么时候改的、为什么改"恰是医药与合规场景里最该被看到的信息。同时 L3 蒸馏无法判断哪张卡片是新版，`evolve/consolidate.py` 的冲突检测只能靠语义相似度硬猜 | 低—中（`KnowledgeCard` 加版本字段 + `_merge` 改为追加版本 + Memory 页展示历史） | 低（加字段，但要决定旧数据如何回填） | P2 |
| G7 | 实体关系图参与检索 | LightRAG 的 local（实体级具体事实）/ global（关系链、跨文档主题）/ hybrid 四种模式（[HKUDS/LightRAG README](https://github.com/HKUDS/LightRAG) 明列四种查询模式，MIT）；HippoRAG 用 Personalized PageRank 在知识图上做单步多跳检索，比迭代式检索便宜 10–30 倍、快 6–13 倍（[arXiv:2405.14831](https://arxiv.org/abs/2405.14831)，MIT） | `memory/extract.py` 抽出的实体与关系落进 SQLite，但全仓库只有 `memory/graph.py::build_graph` 读它，唯一调用方是 `space/runtime.py:275` 的 `/graph` 端点（前端力导向图）。`retrieve/pipeline.py` 的 `_vector_leg` / `_fts_leg` / `_recall_cards` / `_recall_insights` 没有任何一处碰过 `relations` 表 | 用户问「还有哪些和 X 协同作用」。向量召回按语义相似度排序，会把"字面上很像但讲的是别的化合物"的切片排在前面；而图上明明与 X 相连的那些实体所在的切片，只要字面不像就进不了 top-8。我们有图，等于没有——投入在 L2 抽取上的算力没有产生任何检索收益 | 中（1–2 跳扩展用现有 SQLite 关系表 + `relations.source_chunks` 就能做，不需要图数据库，也不需要 PPR） | 中（图扩展会引入"跑题"证据稀释预算，必须靠 G4/G5 的尺子验证） | P3 |
| G8 | 单条经验的效用统计与反馈回流 | 我们自己的 `docs/02-DATA-MODEL.md` §4 就规定了 `applied_count` / `success_count` 与「应用后 +0.05 / -0.15」；mem0 的 ADD / UPDATE / DELETE / NOOP 是逐条 fact 判定的（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413) §update phase） | 实测 grep：`applied_count` / `success_count` 全仓库只有 `store/repos/insights.py` 建表读取与排序用到，无任何写入点；`ConfidenceEvent.POSITIVE_FEEDBACK` / `NEGATIVE_FEEDBACK` 只在 `evolve/confidence.py:39-40` 定义，无任何调用点。唯二改置信度的路径是 `evolve/cycle.py::_apply_verdict`（整批）与 `evolve/insights.py::promote`（手动） | 一次回答注入 6 条经验，用户点 👎。`evolve/distill.py` 产出的是一条新候选，那 6 条里真正害人的那条置信度一分未降、仍是 `active`，下一轮照样注入。只有当整批候选 A/B 下降时才整批 -0.2——一条坏经验可以活到把整批拖下水 | 低（`retrieve/chat.py::_persist` 已在写 `used_insights`，加一次自增；反馈入口按 `trace.used_insights` 分发事件） | 低—中（"用了这条经验"≠"这条经验起了作用"，一次好评归因到 6 条上不可靠，应限幅或只在单条注入时归因） | P2 |
| G9 | groundedness 的真实口径 | RAGAS faithfulness：论断级（`被上下文支撑的论断数 / 论断总数`），是答案-证据之间的关系度量，与"有没有检索到东西"无关 | `expert/expertise.py::ExpertiseService._groundedness`：只看最近 `GROUNDEDNESS_WINDOW=50` 条 trace 里 `bool(t.retrieved)`——只要检索返回过任意证据就算 100 分；而 `WEIGHTS["groundedness"] = 0.25`，是总分里第二大的权重 | 一个只检索到 1 条不相关切片、然后凭模型自身知识作答的回答，与一个引用 5 条原文、每句论断都有出处的回答，在专家度指数上完全一样。这一屏（雷达图 + 成长曲线）是产品最有说服力的展示，但它 25% 的权重那一维现在几乎恒等于满分，一边抬高总分一边抹掉真实差异 | 低（答案正文里已有 `[^cN]` 标记，`retrieve/citations.py::CitationStreamParser` 已能把标记映射回 chunk），「带引句子 / 总句」的原料全在库里 | 低 | P2 |
| G10 | 扫描件 / 图片型文档不要静默成功 | RAGFlow DeepDoc 的 OCR 先兜住"文档是图像"这一情形（[deepdoc/README](https://github.com/infiniflow/ragflow/blob/main/deepdoc/README.md) `t_ocr.py`），再做版面识别与 TSR | `ingest/parse.py::_parse_sync` 只有 docling / markitdown 两条文本路径，且未开启 docling 的 OCR；`ingest/pipeline.py::run` 在 chunking 产出 0 条时不作任何判定，`_stage_extracting` 把 `skipped_reason="no_chunks"` 只写进日志，最后照常 `_set_status(document_id, "ready")` | 用户投喂一份扫描版申报材料：界面显示绿色 `ready`，检索时永远搜不到它，也没有任何提示。用户以为资料进去了，实际知识库里什么都没有。本地知识库场景下这类文件占比不低 | 低（chunking 后切片数为 0 时置 `failed` 或带警告的 `ready`，把原因写进 `DocumentMeta`；OCR 留给用户自选） | 低 | P2 |
| G11 | 经验晋升的单条归因 | mem0 对每条 fact 单独分类操作（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)）；Graphiti 每条 fact 独立失效 | `evolve/cycle.py::_apply_verdict(candidates, delta)`：用一次 A/B 的总分差，对全部候选施加同一个事件（`EVAL_IMPROVED` 或 `EVAL_REGRESSED`） | 一轮蒸馏产出 8 条候选，A/B 总分 +2.5。其中 7 条有用、1 条有害（例如"回答要尽量简短"）。8 条一起转 `active`、一起 +0.2。那条有害的从此常驻注入池，而它造成的伤害被另外 7 条的收益掩盖，下一轮 A/B 也揪不出它 | 高（leave-one-out 需要 N 次额外评测） | 中（成本 ×N；且单条 delta 的信噪比完全依赖 G4/G5 的噪声底，没有噪声底时这个改动毫无意义） | P4 |
| G12 | 层次化检索 / 跨全文汇总 | LlamaIndex `HierarchicalNodeParser(chunk_sizes=[2048,512,128])` + `AutoMergingRetriever`：「多数子节点被取回时用父节点替换它们」（[node_parsers/modules](https://docs.llamaindex.ai/en/stable/module_guides/loading/node_parsers/modules/)）；RAPTOR 递归聚类 + 摘要建树，「配合 GPT-4 在 QuALITY 基准上绝对准确率提升 20%」（[arXiv:2401.18059](https://arxiv.org/abs/2401.18059)） | `ingest/chunk.py::split_markdown` 是单层 512/80 定长切分；`types.Chunk` 没有 `parent_id`；`store/repos/chunks.py::ChunkRepo` 只有 `get / get_many / list_by_document / list_by_space`，没有取父节点或取相邻块的方法；`retrieve/pipeline.py::_load_chunks` 只回表取命中自己那一行 | 用户问「这个化合物的所有已知副作用」。top-8 条 × 512 token 里，证据区总预算 `EVIDENCE_TOKEN_BUDGET=2000` 只能容下其中约一半；若副作用散落在 60 页报告的 5 个章节，top-8 通常只命中 1–2 处，且那两处还被从中间截断。答案会漏掉散落在其它章节的三条，而用户无从知道答案不完整 | 高（父子结构 = 新表 / 新字段 + 摄取改动 + 检索新分支 + 重算索引） | 中—高（RAPTOR 思路在多文档叠加的 Space 里会退化成"一堆无人问津的摘要节点"，且与 G1/G2/G3 的重算索引工作叠加） | P3 |
| G13 | L3 注入是固定 top-k，没有「这次要不要用经验」的决策 | MemGPT / Letta 的虚拟上下文管理：core memory（常驻上下文、可编辑）与 archival memory（外部、按需检索）分离，由 agent 通过 tool call 自己决定何时检索外部记忆（[arXiv:2310.08560](https://arxiv.org/abs/2310.08560)；[docs.letta.com](https://docs.letta.com/)，Apache-2.0） | `retrieve/pipeline.py::_recall_insights`：有向量就取 top-`max_insights`（默认 6），只在「索引非空但本次一条都不相关」时返回空；置信度门槛 `INSIGHT_MIN_CONFIDENCE = 0.5`；`prompts/answer.py::render_insights` 再把它们按置信度降序塞进当轮消息，并写明「适用时必须遵守」 | 用户说「把第三章的原文贴给我」。这是一次纯索取，没有"遇到 X 场景"可言，但系统照样注入最多 6 条经验（连带"适用时必须遵守"的措辞），既占掉当轮 token，又可能把回答风格往经验描述的方向拽。经验是给"场景—做法"用的，这类问题根本没有场景 | 低（按问题类型给出开关，或在明显是"取原文"的问法下把 `max_insights` 归零） | 低（这是口味问题；保守判断：多注入几条的代价主要是 token） | P3 |

---

## 2. 明确不建议做的事

约束前提（来自 `docs/00-VISION.md` §5 与 `docs/05-ROADMAP.md` Phase 5 注记）：
本地优先单机、零外部服务、License 不碰 AGPL、不引重依赖（PyTorch 系在桌面打包时是灾难）、
以及任何设计都不能绕过 L3 可证伪这条主线。

### 2.1 不把 LlamaIndex 引为运行时依赖（License 干净，但它是"框架"不是"库"）

MIT，License 没问题。问题在抽象层次：一旦引入，`NodeParser → Retriever → ResponseSynthesizer`
三层会接管我们的上下文装配，而 §5.1 的证据预算分配器（`prompts/budget.py::allocate`）、
引用协议（`citations.py` 的 `[^cN]` 流式解析）、L4→L3→L2→L1 的注入顺序、
以及前缀缓存友好的 system / 当轮消息切分，全部是自研且与框架的 response synthesis 直接冲突。
借鉴它的 node parser 概念，不引依赖。

### 2.2 不做完整版 RAPTOR（arXiv:2401.18059）

它需要 UMAP 降维 + GMM 软聚类 + 递归摘要，会拖进 `numpy + scipy + scikit-learn + umap-learn`；
Python 3.12 环境下 UMAP 的 wheel 与 numba 版本耦合是出了名的脆。
更要紧的是它的假设不成立：RAPTOR 面向单篇长文档，而我们是多文档叠加、单机 CPU、
用户在持续投喂。折中方案见 G12——只做"文档级摘要节点"：
每篇文档生成一个摘要 chunk（一次 LLM 调用），作为普通切片参与现有检索管线，
不引入任何新依赖，就能覆盖"跨全文汇总"场景里最常被问到的那些问题。

### 2.3 不引 Graphiti 作为依赖（Apache-2.0，但强绑服务型图库）

Graphiti 的核心存储是 Neo4j / FalkorDB——外部服务进程，直接违反"零外部服务"红线，
与 ROADMAP 里排除 RAGFlow 的理由（默认强绑 Elasticsearch）是同一条。
但它「失效而非删除」的数据模型值得原样抄下来（G6）：
给 `relations` / `insights` / `cards` 加 `valid_from / valid_to / invalidated_by` 三个字段，
零新依赖，纯 SQLite 就能落。

### 2.4 不做 HippoRAG 的 Personalized PageRank（arXiv:2405.14831，MIT）

PPR 需要在全图上迭代求解（`scipy` 稀疏矩阵或 `networkx`），而收益集中在"多跳问答"这一个窄场景。
现实是：我们连图上直接相邻的切片都还没用上（G7）。
先用 `relations` 表做 1–2 跳扩展（纯 SQL，`relations.source_chunks` 就直接指向切片），
拿到收益之后再评估 PPR——到那时也只需要一个几十行的幂迭代，不需要引库。

### 2.5 不采用 mem0 的 ADD / UPDATE / DELETE / NOOP 作为写入默认路径

这条反直觉，但证据明确：mem0 的原始论文（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413)）
确实让 LLM 通过 tool call 在 ADD / UPDATE / DELETE / NOOP 四者中选一个，
但它自己的 OSS 已经在 v3 改回了 ADD-only——当前 README 明写：
「Single-pass ADD-only extraction — one LLM call, no UPDATE/DELETE.
Memories accumulate; nothing is overwritten.」（[mem0ai/mem0 README](https://github.com/mem0ai/mem0)）。
让模型在写入时决定 DELETE / UPDATE，会产生不可审计的静默覆盖，
而我们整个产品的卖点恰恰是"一切可追溯、经验可证伪"。
对 L2 卡片应改成"追加 + 标记失效"（G6），而不是让模型选 DELETE。

### 2.6 不引 RAGAS / TruLens 作为依赖（License 都干净）

RAGAS 已迁至 [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas)（Apache-2.0），
TruLens MIT。但 RAGAS 会拖进 `datasets / pandas / langchain-core / nest-asyncio` 一串，
而我们要的只是三条公式（G4）。这三条公式的输入我们全都有：
`EvalItem.must_include` 当 claim 集合、检索到的 `ScoredChunk.content` 当 context、答案当 response。
用现有 `prompts/` + `registry.llm("judge")` 约一百行就能实现，还能复用
`prompts/_base.py::schema_block` / `extract_json` 的既有约定。抄公式，不引依赖。

### 2.7 不做 SentenceWindowNodeParser 式的句子级索引

LlamaIndex 的 `SentenceWindowNodeParser(window_size=3)` 把索引单元缩到单句，
检索时用 `MetadataReplacementNodePostProcessor` 换回前后三句
（[node_parsers/modules](https://docs.llamaindex.ai/en/stable/module_guides/loading/node_parsers/modules/)）。
它的方向是更小的索引粒度，而我们已经有 80-token 重叠 + `retrieve/overlap.py` 的相邻重叠零损失裁剪
+ 证据预算分配三件套，句子级索引会与这三者正面打架：索引条数涨几十倍、
FTS 与向量召回的名次被大量近乎重复的单句淹没、RRF 融合后的 top-50 里塞满同一段的碎片。
我们缺的是更大粒度的父节点（G12），不是更小的。

### 2.8 不引 RAGFlow 的 DeepDoc 视觉模型栈

Apache-2.0 干净，但它是 ONNX 视觉模型集合（OCR + layout + TSR），
要下几十到几百 MB 权重并常驻推理，与"桌面双击即用"的 Phase 5 目标直接冲突。
而 docling 已经是硬依赖（`pyproject.toml` 第 26 行）且自带版面分析与 TableFormer 表格结构识别，
重复投入不划算。只参考它的版面组件分类（10 类）与"先识别结构再切分"的思路，
把收益落在 G1/G2 的纯 Python 修复上。

### 2.9 不做 Letta 式的 agent 自编辑记忆

Letta 让 agent 在对话中通过 tool call 自由改写 core memory blocks。
我们的 L4 persona 与 L3 经验都要求可审计、可回溯，
而在对话中自由改写记忆会绕开 A/B 评测这条唯一的裁判链——与"可证伪"直接冲突。
Letta 的 core / archival 分层我们其实已有等价物（system 里的 persona ≈ core memory，
`insights_vec` ≈ archival memory），缺的是检索决策权（G13），
那个应该做成用户的开关，而不是交给模型。

---

## 3. 建议的实施顺序

排序原则：先让证据本身是对的 → 再让改动可以被证伪 → 然后才动检索。
理由很简单：在 G1/G2 没修之前，检索实验的输入是坏的；
在 G4/G5 没做之前，任何检索改动都只是"我感觉变好了"。

### P0-a · 修切片器（G1 + G2）

- 为什么排这里：这是唯一一类"证据本身是错的"问题，不依赖任何评测就能验收，
  而且它决定索引格式——改完必须重建索引，所以要和后面的重建工作合并成一次。
- 前置：无。
- 怎么验证：不需要 A/B，这是有确定性验收标准的修复。
  在 `tests/test_ingest.py` 里加三条硬不变量断言：
  1. `"".join(_split_sentences(t)) == t`（对中英混排样例）；
  2. 对任意文档，`markdown[draft.char_start:draft.char_end] == draft.content` 恒成立；
  3. 切片 token 数的 p95 ≤ `target_tokens × 1.1`（含 200 行 Markdown 表格用例）。
  第 2 条同时会把 `_reslice` 类偏移同步问题永久钉死。

### P0-b · 修尺子：检索侧指标 + 配对 A/B（G4 + G5）

- 为什么排这里：排在 P0-a 之后、其它一切之前。产品的唯一差异点是"可证伪"，
  而现在检索侧完全没有可证伪能力——这条主线是断的。
- 前置：无（现有 `eval_items` / judge 角色即可支撑）。
- 怎么验证它自己有效——这一步本身就是验证：
  1. 先在同一份检索配置上跑 A/A 对照（A 臂与 B 臂参数完全相同，各跑一次）。
     得到的 delta 分布就是噪声底 σ。⚠️ 不做 A/A，就永远不知道
     `MIN_EVAL_DELTA = 1.0` 到底是不是噪声——以 N=30 题、`JUDGE_TEMPERATURE=0.2`
     单次采样计，答案级分数的题间标准差通常有好几分。
  2. 建一个检索专测集：从 `expert/evalgen.py` 出的题里，用 tag 挑出
     "答案必须跨多条切片 / 跨文档才能答全"的题（例如"列出全部已知副作用"这类）。
     普通题单切片就能答对，检索改动在它们上面会被完全淹没。
  3. 之后所有检索侧改动的验收标准统一为：在检索专测集上，
     `context_recall` 的提升 > 2σ，且答案总分不下降。

### P1 · 摄取期上下文预置（G3）

- 为什么排这里：这是所有对标做法里证据最硬、代价最清楚的一项
  （Anthropic 的 -35% / -49% / -67% 与本项目的场景高度吻合：都是中文/英文专业语料 + 混合检索 + rerank，
  而我们这三件套已经全都有了）。它需要一次全量重算 embedding 与 FTS 索引，
  正好与 P0-a 的索引重建合并成一次。
- 前置：P0-a（切片稳定后重算才有意义）。
- 怎么验证：走 P0-b 的尺子。
  预期信号是 `context_recall` 明显上升、答案总分小幅上升——
  因为召回变好之后，仍然受 `EVIDENCE_TOKEN_BUDGET=2000` 封顶，
  多召回的证据要靠预算分配器取舍。
  如果 `context_recall` 上升而答案分不动，说明瓶颈是预算而不是召回，
  那下一步该调预算是另一件事——这正是 G4 存在的意义。

### P2 · 三项低成本高确定性修复（G6 + G8 + G9 + G10）

- 为什么排这里：都不依赖检索尺子，都是纯粹的"该有的没有"，
  且除 G6 外都只动单点代码。放在 P1 之后是因为它们不阻塞检索主线。
- 前置：无（G8 的归因策略需要一点设计决策，见差距表的"风险"列）。
- 怎么验证：
  - G6 / G9 / G10 不适合用 A/B 验证，它们改变的是"能不能看到"而不是"答得对不对"。
    应当用功能测试 + 场景走查：更新一版文档后 Memory 页能看到 v1/v2 两版；
    造一个只有 1 条不相关证据的回答，看 groundedness 是否明显低于满分；
    投一份纯扫描 PDF，看是否报错而不是绿标。
  - G8 可以部分用 A/B：构造一个"注入一条已知有害经验"的对照，
    看单条 👎 之后它的置信度是否下降、是否被移出 `insights_vec`。

### P3 · 图的 1–2 跳扩展 + 注入决策开关（G7 + G13）

- 为什么排这里：这两项都会改变进入上下文的证据集合，
  必须等 P0-b 的尺子就位才能判断是赚是赔。G13 更靠前一点，因为它更便宜。
- 前置：P0-b（强依赖）；G7 还需要一个多跳题集
  （在 `evalgen` 出题时给题目打 `tags`，现有字段够用）。
- 怎么验证：在多跳题集上跑配对 A/B。
  关键是要看负向信号：图扩展很可能在"字面相似"的题上引入跑题证据，
  所以除了整体 delta，还要看检索专测集里的非多跳题有没有退化。

### P4 · 经验的单条归因（G11）

- 为什么排最后：它需要 N 次额外评测（leave-one-out），
  而单条 delta 的信噪比完全依赖 P0-b 给出的噪声底——没有噪声底，这个改动毫无意义。
- 前置：P0-b（强依赖）。
- 怎么验证：构造一个"8 条候选里混 1 条有害经验"的对照，
  看这条是否被单独识别并降级，而其余 7 条正常晋升。
  在这之前，可以先做一个便宜的中间态：只在整批提升时才做 leave-one-out
  （下降时保持现有整批判决），把成本从 N 次压到只在高价值时刻付出。

---

## 附录 A · 已具备能力清单（明确不是差距）

以下能力在这个表里不出现在"差距"一侧，列在这里是为了防止后续 review 把它们当新需求重做。

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 混合检索（向量 ‖ BM25） | `retrieve/pipeline.py::RetrievalPipeline._search_with` + `_vector_leg` / `_fts_leg` | 多路并发，单路失败不影响其它路（`_guarded`） |
| RRF 融合 | `retrieve/fusion.py::reciprocal_rank_fusion` | k=60，权重可选 |
| Rerank | `retrieve/pipeline.py::_rerank` | 失败回退 RRF 顺序；`RERANK_INPUT_LIMIT=50` 封顶付费调用 |
| MMR 多样性重排 | `retrieve/diversity.py::mmr_rerank` | 相关性由名次推导，免疫 rerank 分数量纲差异 |
| 跨文档近重复抑制 | `retrieve/diversity.py::suppress_redundant` + `text_similarity` | 字符 4-gram 重合度（`|A∩B| / min(|A|,|B|)`），非 Jaccard |
| 相邻切片重叠的零损失裁剪 | `retrieve/overlap.py::trim_adjacent_overlap` / `common_overlap` | 只裁重复段，保留整条；同步修正 `char_start/char_end` |
| 前缀缓存友好的提示装配 | `prompts/answer.py::build_system_prompt` / `build_turn_message` / `build_answer_messages` | system 只放跨轮不变内容；守护测试 `tests/test_prompt_cache*.py` |
| 证据 token 预算分配 | `prompts/budget.py::allocate`（注水式）/ `trim_to_budget`（只用句子边界） | 总量封顶 `EVIDENCE_TOKEN_BUDGET`，权重按名次 |
| 查询改写 / HyDE | `retrieve/rewriting.py::QueryRewriter.contextualize / expand / hyde` | 任一步失败退化为原检索式 |
| 引用协议与原文下钻 | `retrieve/citations.py` + `prompts/answer.py::_citation_rules` | `[^cN]` 流式解析，幻觉编号被丢弃 |
| 五维专家度指数 | `expert/expertise.py::ExpertiseService.compute` | 见 G9：其中 groundedness 与 consistency 是代理指标，已在文档与债表记录 |
| A/B 评测闭环 | `expert/evaluator.py` + `evolve/cycle.py` + `evolve/confidence.py` | 对经验这条轴是完整可用的；对检索配置这条轴缺失（G5） |
| L2 抽取与图谱聚合 | `memory/extract.py::KnowledgeExtractor` + `memory/graph.py::build_graph` | 抽取失败不影响文档可用（`_stage_extracting` 隔离异常） |

一处小的文案不一致（不值得单列一行）：`prompts/answer.py::render_insights` 写着
「置信度低于 0.5 的条目可以参考但不要当作定论」，但 `pipeline.py::INSIGHT_MIN_CONFIDENCE = 0.5`
在召回时已经把低于 0.5 的过滤掉了，所以这句话只在 A/B 评测的 `insight_set` 注入路径下才可能生效。

---

## 附录 B · 来源与 License 核验（2026-09-17）

License 与仓库状态经 GitHub API 实查，数字类主张已从原始页面/论文抓取核对。

| 项目 | 来源 | License | 备注 |
|---|---|---|---|
| LlamaIndex | [docs.llamaindex.ai](https://docs.llamaindex.ai/en/stable/module_guides/loading/node_parsers/modules/) / [api_reference/schema](https://docs.llamaindex.ai/en/stable/api_reference/schema/) | MIT | `TextNode.start_char_idx/end_char_idx` 实查存在 |
| Anthropic Contextual Retrieval | [anthropic.com/news/contextual-retrieval](https://www.anthropic.com/news/contextual-retrieval) | 技术方案，非依赖 | 35% / 49% / 67% / $1.02 已从原文核对 |
| Graphiti / Zep | [getzep/graphiti](https://github.com/getzep/graphiti) / [arXiv:2501.13956](https://arxiv.org/abs/2501.13956) | Apache-2.0 | 主存为 Neo4j / FalkorDB（服务型），见 §2.3 |
| mem0 | [mem0ai/mem0](https://github.com/mem0ai/mem0) / [arXiv:2504.19413](https://arxiv.org/abs/2504.19413) | Apache-2.0 | 当前 OSS 已改回 ADD-only，见 §2.5 |
| Letta / MemGPT | [docs.letta.com](https://docs.letta.com/) / [arXiv:2310.08560](https://arxiv.org/abs/2310.08560) | Apache-2.0 | |
| LightRAG | [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) / [arXiv:2410.05779](https://arxiv.org/abs/2410.05779) | MIT | local / global / hybrid / mix 四种查询模式 |
| HippoRAG | [OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) / [arXiv:2405.14831](https://arxiv.org/abs/2405.14831) | MIT | |
| RAPTOR | [arXiv:2401.18059](https://arxiv.org/abs/2401.18059) | 论文 | QuALITY +20% 绝对准确率（GPT-4） |
| RAGFlow DeepDoc | [infiniflow/ragflow](https://github.com/infiniflow/ragflow) / [deepdoc/README](https://github.com/infiniflow/ragflow/blob/main/deepdoc/README.md) | Apache-2.0 | OCR + layout（10 类组件）+ TSR |
| RAGAS | [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas)（已从 explodinggradients 迁移）/ [docs.ragas.io](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/) | Apache-2.0 | |
| TruLens | [truera/trulens](https://github.com/truera/trulens) | MIT | RAG triad |

---

## 附录 C · 落地记录（2026-09-18）

本轮的实现与验证情况，供之后回看「这条差距当时是怎么补的」：

| # | 结论 | 实现位置 | 验证方式 |
|---|---|---|---|
| G1 | 已实现 | `ingest/chunk.py` 全程搬运区间 `[start, end)`，正文一律由 `markdown[start:end]` 切出；断句复用 `prompts.budget.split_sentences` | `tests/test_chunk_invariants.py`（偏移可切回原文、可见字符零丢失、极小目标下无空白切片）；随机 + CRLF/BOM/转义竖线/未闭合围栏/emoji 等约两千例对抗探查在目标 64–512 上零违例 |
| G2 | 已实现 | 表格按行切，每段重复表头（合成前缀不计入偏移，长度可由 `len(content) - (char_end - char_start)` 反推） | 同上；另在 HTTP 层断言 `/chunks` 的偏移能在 `/content` 的全文里切回正文（`tests/test_api.py`） |
| G4 | 已实现 | `expert/metrics.py` + `prompts/judge.py` 的检索审计块，分数与审计同一次调用产出 | `tests/test_eval_metrics.py`：公式穷举 + 真实评测链路端到端 |
| G5 | 已实现 | `EvaluationService.compare()` + `POST /evals/compare`；覆盖项经 `RetrievalSettings` 校验，未知旋钮当场报错 | 同上：三臂对比断言覆盖项真的改变了证据条数、差值按题配对、A/A 差值为 0 |
| G8 | 已实现 | `InsightService.attribute_feedback()` + `InsightRepo.bump_applied()` | `tests/test_feedback_loop.py`：好评/差评/纠错、多条注入的份额均摊、跌阈值归档并移出召回池、judge 不回流 |
| G9 | 已实现 | `expert/expertise.py::_groundedness` 改为「带有效引用的句子 / 全部句子」 | `tests/test_expertise.py` |
| G10 | 已实现 | `ingest/pipeline.py::_stage_chunking` 切分为空即抛 `PARSE_FAILED` | `tests/test_ingest.py`、`tests/test_api.py` |
| G3 | 已实现（文档级，非逐切片） | `prompts/contextual.py` 生成文档级上下文，存进 `documents.meta.context_summary`；全文索引与嵌入的文本加前缀，切片正文与偏移不动 | `tests/test_ingest.py`：只出现在上下文里的词能被 BM25 命中，且 `markdown[char_start:char_end] == content` 仍成立；关掉开关即回到旧行为 |

本轮另修掉三个不在差距表里的真缺陷，记录在此以免重犯：

1. SSE 的字典载荷整条流都发不出去：`SseEvent.data` 的联合类型把 `BaseModel` 写在 `dict` 前面，
   pydantic 会把传入的 dict 塞进一个空的 `BaseModel` 实例，构造期无异常，序列化时才抛
   `PydanticUserError`——而响应头已经发出，客户端只看到断流。受影响的是所有用字典发事件的
   流式路由（一键进化、蒸馏、整合、出题、评测、重建索引）。守护测试 `tests/test_sse.py`。
2. 重切留下孤儿向量：重新解析只删 SQLite 里的切片，`chunks_vec` 里旧切片的向量还在，
   检索会命中回不了表的条目，向量表随每次重切膨胀。
3. `expert` 包单独 import 会 ImportError：`expert.evaluator` 与 `evolve.cycle` 在模块层互相
   import 成环，真实入口恰好先 import evolve 才没暴露。

本轮（同一日下半场）又修掉五个不在差距表里的真缺陷，一并记下：

4. 轨迹 id 前后不一致：SSE 的 `trace_start` / `done` 给出的 `trace_id` 与落库的
   不是同一个，`GET /traces/{id}` 与反馈接口全都 404——聊天里的 👍/👎 与「为什么
   这么答」面板等于从来没通过。
5. groundedness 恒为 0：引用标记在流式解析时就被剥离出正文，按正文里的标记数
   句子永远数不到。改为记录 `Citation.char_offset`。
6. 导入 Space 会清空导入的数据：写回 space 行时用了 `INSERT OR REPLACE`，
   而 `documents` 等表是 `REFERENCES spaces(id) ON DELETE CASCADE`——刷新一行
   把整库数据删干净，接口还返回成功。
7. 导入压缩包可越界写盘：包内 `meta.db` 的 space id 直接拼进文件系统路径，
   `../../evil` 能把 Space 目录写到 data 之外；顺带补上解包体量上限挡 zip bomb。
8. SSE 的字典载荷整条流发不出去（见上文第 1 条）与删除 Space 不删磁盘数据
   （界面上看不见、几个 GB 仍躺在盘上，同 id 再也导不回来）。
