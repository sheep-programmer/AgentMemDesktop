/**
 * AgentMem · 前端领域扩展类型定义 (types.temp.ts)
 *
 * 【为什么还留着这个文件】
 * types.gen.ts 由后端 OpenAPI 生成，是契约的唯一事实来源。这里只放**后端契约里没有、
 * 也不该有**的前端形态；后端补上字段之后，对应的扩展就会被删掉（已删过一批：
 * source_uri / kind / trace_id / document_title / insight_count / 盲区大纲字段 …）。
 *
 * 目前仍在扩展的，只有这几类：
 *
 * 1. 纯前端的 UI 状态：``ProviderItem.status`` / ``latency_ms``（实时连通性探测结果）、
 *    ``ChunkItem.score``（这一次检索给的分，不属于切片本身）、``DocumentItem.source_url``。
 * 2. 可视化结构：``EntityNode`` / ``EntityEdge``（力导向图要的度数与连线 id）、
 *    ``KnowledgeGapNode``（盲区树的递归结构）。
 * 3. 联合类型的兼容层：``TraceDetail.used_insights`` / ``used_cards``（后端存 id、
 *    接口回填对象，历史数据两种都可能）、``EvalRun.detail``（老记录是数组，新记录是对象）。
 * 4. 流式过程指示器 ``ProcessStage``：它是前端状态机，后端只推事件。
 * 5. 通用分页契约 ``PageResponse``。
 */

import type { components } from './types.gen';

/** SSE context event: input estimates, separate from provider billing usage. */
export interface ContextUsage {
  mode: 'standard' | 'economy';
  original_estimated_tokens: number;
  estimated_tokens: number;
  saved_estimated_tokens: number;
  history_messages: number;
  evidence_count: number;
  insight_count: number;
  card_count: number;
}

export type Schemas = components['schemas'];

// --- 通用分页响应契约 (对齐后端 Page[T] 数据结构: items, total, next_cursor) ---
export interface PageResponse<T> {
  items: T[];
  total: number;
  next_cursor?: string | null;
}

// --- Space 领域空间 ---
export type Space = Schemas['SpaceSummary'] & {
  embedding_dim?: number;
};
export type SpaceCreate = Schemas['SpaceCreate'];
export type SpaceUpdate = Schemas['SpaceUpdate'];
export type SpaceSummary = Schemas['SpaceSummary'];

// --- Document 与切片 ---
export type DocumentStatus = Schemas['Document']['status'];
export type SourceType = Schemas['Document']['source_type'];
export type DocumentItem = Schemas['Document'] & {
  /** 网页来源的原始链接。后端只存 source_uri（本地文件是路径），展示用的外链留在前端。 */
  source_url?: string;
};
export type DocumentContent = Schemas['DocumentContent'];
export type DocumentDetail = Schemas['Document'] & {
  chunk_count?: number;
  card_count?: number;
  chunks?: ChunkItem[];
  source_url?: string;
};
export type ChunkItem = Schemas['Chunk'] & {
  /** 检索阶段的相关度：切片本身不带分，分数来自这一次检索。 */
  score?: number;
};
export type Chunk = ChunkItem;

// --- Document 重试 SSE 事件契约 ---
export interface DocumentRetryBeginEvent {
  total: number;
}

export interface DocumentRetryProgressEvent {
  document_id: string;
  stage: string;
  done: number;
  total: number;
  percent: number;
}

export interface DocumentRetryErrorEvent {
  document_id: string;
  message: string;
}

export interface DocumentRetryDoneEvent {
  total: number;
  retried: number;
}

// --- 结构化记忆 L2 (Knowledge Cards & Graph) ---
export type KnowledgeCardKind = Schemas['KnowledgeCard']['kind'];
export type KnowledgeCard = Schemas['KnowledgeCard'];
export type KnowledgeCardRequest = Schemas['KnowledgeCardRequest'];
export type KnowledgeCardUpdate = Schemas['KnowledgeCardUpdate'];
export type CardVersion = Schemas['CardVersion'];
export type CardHistoryResponse = Schemas['CardHistoryResponse'];
export type EntityNode = Schemas['GraphNode'] & {
  mention_count?: number;
};
export type EntityEdge = Schemas['GraphEdge'] & {
  id?: string;
};
export type KnowledgeGraphData = {
  nodes: EntityNode[];
  edges: EntityEdge[];
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
};
export type CardExtractRequest = Schemas['CardExtractRequest'];

// --- 结构化经验 L3 (Insights) ---
export type InsightKind = Schemas['Insight']['kind'];
export type InsightScope = Schemas['Insight']['scope'];
export type InsightStatus = Schemas['Insight']['status'];
export type InsightOrigin = Schemas['Insight']['origin'];
export type Insight = Schemas['Insight'];
export type InsightRequest = Schemas['InsightRequest'];
export type InsightUpdate = Schemas['InsightUpdate'];
export type ConflictGroup = Schemas['InsightConflictGroup'];
export type ConflictResolveRequest = Schemas['ConflictResolveRequest'];
export type InsightEvent = Schemas['InsightEvent'];
export type InsightHistoryResponse = Schemas['InsightHistoryResponse'];
export type InsightReviewItem = Schemas['InsightReviewItem'];
export type InsightReviewResponse = Schemas['InsightReviewResponse'];

// --- 对话与推理执行 (Chat & Trace) ---
export type Conversation = Schemas['Conversation'];
export type ConversationCreate = Schemas['ConversationCreate'];
export type ConversationUpdate = Schemas['ConversationUpdate'];
export type CitationMarker = Omit<Schemas['Citation'], 'kind'> & {
  /** 概要切片没有原文区间，引用芯片据此换一种说法；老记录里没有这个字段，缺省按正文切片。 */
  kind?: 'body' | 'summary' | (string & {});
};
export type Message = Omit<Schemas['Message'], 'citations'> & {
  /** 只为了多带一个 kind，见 CitationMarker。 */
  citations?: CitationMarker[];
};
export type ChatRequest = Schemas['ChatRequest'];
export type FeedbackRequest = Schemas['FeedbackRequest'];
export type Feedback = Schemas['Feedback'];

/** 过程链条的四个阶段。收成联合类型是因为 ProcessBar 要按 id 匹配文案：
 *  写成 string 时曾经漏过一次（产出方用 `rewrite`/`retrieval`，消费方匹配
 *  `rewriting`/`retrieving`），摘要行因此长期少了前两环且无人报错。 */
export type ProcessStageId =
  'rewrite' | 'retrieval' | 'insights' | 'generating';

export interface ProcessStage {
  id: ProcessStageId;
  label: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'skipped';
  detail?: string;
  /** 阶段完成了但打了折扣（如检索退化成纯全文），界面要明示而不是照常显示「完成」 */
  warning?: string;
}

export type TraceDetail = Omit<
  Schemas['Trace'],
  'used_insights' | 'used_cards'
> & {
  /**
   * 后端存的是 id 列表，而证据栏要展示完整对象；历史轨迹两种形态都可能出现，
   * 所以这里是联合类型。
   */
  used_insights: (Insight | string)[];
  used_cards: (KnowledgeCard | string)[];
};

// --- 自进化闭环 (Evolution Loop) ---
export type EvolvePendingSummary = Partial<Schemas['EvolvePendingResponse']> & {
  pending_count?: number;
  feedback_count?: number;
  correction_count?: number;
  last_evolve_at?: number;
  preview?: Feedback[];
};

export type EvolveHistoryItem = Partial<Schemas['EvolveHistoryItem']> & {
  id?: string;
  date?: string;
  promoted_count?: number;
  demoted_count?: number;
  score_before?: number;
  score_after?: number;
  summary?: string;
  created_at?: number;
  run_at?: number;
  produced?: number;
  merged?: number;
  conflicts?: number;
  promoted?: number;
  demoted?: number;
  eval_delta?: number | null;
};
export type EvolveHistoryResponse = Schemas['EvolveHistoryResponse'];

// --- 领域大纲与知识盲区 (Outline & Expertise Gaps) ---
export interface OutlineNode {
  topic: string;
  subtopics: string[];
  importance: 'core' | 'peripheral' | (string & {});
}

export interface DomainOutline {
  id: string;
  space_id: string;
  domain: string;
  nodes: OutlineNode[];
  created_at: number;
}

export interface OutlineResponse {
  outline: DomainOutline;
  covered: number;
  total: number;
  /** 模型把领域理解成了什么：领域名是简称时，用户靠它判断大纲有没有跑偏 */
  interpretation?: string | null;
}

// --- 专家度评估体系 (Expertise Dimensions) ---
export type ExpertiseDimensions = Schemas['ExpertiseScore'];
export type ExpertiseSnapshot = Schemas['ExpertiseSnapshot'];
export type ConsistencyProbe = Schemas['ConsistencyProbe'];
export type ConsistencyQuestionScore = Schemas['ConsistencyQuestionScore'];
export type KnowledgeGapNode = Partial<Schemas['ExpertiseGap']> & {
  id?: string;
  title?: string;
  covered?: boolean;
  doc_count?: number;
  children?: KnowledgeGapNode[];
};
export type ExpertiseGapsResponse = Schemas['ExpertiseGapsResponse'];
export type ExpertiseHistoryResponse = Schemas['ExpertiseHistoryResponse'];
export type EvalItem = Schemas['EvalItem'];
export type EvalItemRequest = Schemas['EvalItemRequest'];
export type EvalItemUpdate = Schemas['EvalItemUpdate'];
export type RetrievalMetrics = Schemas['RetrievalMetrics'];
export type EvalItemScore = Schemas['EvalItemScore'];
export type EvalRunDetail = Schemas['EvalRunDetail'];
export type EvalRun = Omit<Schemas['EvalRun'], 'duration_ms' | 'detail'> & {
  duration_ms?: number | null;
  score?: number;
  detail?:
    | Schemas['EvalRunDetail']
    | Array<{
        eval_id?: string;
        item_id?: string;
        passed: boolean;
        score: number;
        question?: string;
        reason?: string | null;
        metrics?: RetrievalMetrics | null;
      }>;
};
export type EvalRunRequest = Schemas['EvalRunRequest'];
export type EvalRunListResponse = Schemas['EvalRunListResponse'];
export type EvalGenerateRequest = Schemas['EvalGenerateRequest'];

// --- 评测对比与检索指标差值 (Multi-arm Comparison) ---
export interface RetrievalMetricsDelta {
  context_recall?: number | null;
  context_precision?: number | null;
  faithfulness?: number | null;
  claims?: number;
  evidence?: number;
  audited?: boolean;
}

export interface EvalArmOutcome {
  label: string;
  run_id: string;
  score: number;
  metrics?: RetrievalMetrics | null;
  item_scores?: EvalItemScore[];
}

export interface EvalItemDelta {
  item_id: string;
  score_delta: number;
}

export interface EvalArmDelta {
  label: string;
  score_delta: number;
  metrics_delta?: RetrievalMetricsDelta | null;
  item_deltas?: EvalItemDelta[];
  /** 两臂都测出分数、参与配对的题数 */
  paired_items?: number;
  std_error?: number | null;
  t_stat?: number | null;
  /** 总分差是否显著。界面只给显著的差值上涨跌色 */
  significant?: boolean;
  /** 各检索指标差值是否显著（逐题配对检验） */
  metrics_significant?: Record<string, boolean>;
}

export interface EvalComparison {
  space_id: string;
  items: number;
  baseline: string;
  arms: EvalArmOutcome[];
  deltas: EvalArmDelta[];
}

export interface RetrievalKnobOverrides {
  top_k_vector?: number;
  top_k_fts?: number;
  top_n_rerank?: number;
  max_insights?: number;
  hyde?: boolean;
  diversity?: boolean;
  mmr_lambda?: number;
  dedup_threshold?: number;
  [key: string]: unknown;
}

export interface EvalArmSpec {
  label: string;
  retrieval?: RetrievalKnobOverrides | Record<string, unknown> | null;
  insight_set?: string[];
}

export interface EvalCompareRequest {
  arms: EvalArmSpec[];
  persist?: boolean;
}

// --- Provider 与系统设置 ---
export type ProviderKind = Schemas['ProviderPublic']['kind'];
export type ProviderAdapter = string;
export type ProviderPublic = Schemas['ProviderPublic'];
export type ProviderCreate = Schemas['ProviderCreate'];
export type ProviderUpdate = Schemas['ProviderUpdate'];
export type ProviderHealth = Schemas['ProviderHealth'];
export type DiscoveredModel = Schemas['DiscoveredModel'];
export type DiscoverResponse = Schemas['DiscoverResponse'];
export type DiscoverRequest = Schemas['DiscoverRequest'];

export type ProviderItem = Schemas['ProviderPublic'] & {
  name?: string;
  latency_ms?: number;
  status?: 'online' | 'offline' | 'degraded';
};

export type RoleBindings = Schemas['RoleBindings'];
export type RolesUpdateRequest = Schemas['RolesUpdateRequest'];
export type RolesUpdateResponse = Schemas['RolesUpdateResponse'];
export type ModelRole = keyof RoleBindings;

export type SystemCapabilities = Schemas['CapabilitiesResponse'];
export type ProviderAlerts = Schemas['ProviderAlertsResponse'];
export type ProviderAlert = Schemas['ProviderAlert'];
export type InsightAttribution = Schemas['InsightAttribution'];
export type InsightContribution = Schemas['InsightContribution'];
export type SystemStats = Schemas['StatsResponse'];

// --- 本机 Agent 配置复用 (Local Agent Config Reuse) ---
export type LocalAgentSource = Schemas['LocalAgentCandidate']['source'];
export type LocalAgentCandidate = Schemas['LocalAgentCandidate'];
export type LocalAgentScanResponse = Schemas['LocalAgentScanResponse'];
export type LocalAgentImportRequest = Schemas['LocalAgentImportRequest'];
