import { apiClient } from '../client';
import { fetchSSE } from '../sse';
import type {
  Conversation,
  ConversationCreate,
  ConversationUpdate,
  Message,
  CitationMarker,
  FeedbackRequest,
  Feedback,
  TraceDetail,
  PageResponse,
  Insight,
  ContextUsage,
} from '../types';
import type { Schemas } from '../types.temp';
import { mockPage } from '../mock/page';
import { mockConversations, mockMessages, mockTraceDetail } from '../mock/data';
import { isMockMode } from './spaces';

export type { PageResponse };

export interface ConversationListOptions {
  limit?: number;
  cursor?: string | null;
  q?: string;
  signal?: AbortSignal;
}

export interface DonePayload {
  message_id: string;
  trace_id: string;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    latency_ms: number;
  };
}

export interface ChatStreamHandlers {
  onContext?: (data: ContextUsage) => void;
  /** 与后端 TraceStartEvent 一致：只有 trace_id 与 message_id，问题原文由调用方自己带着 */
  onTraceStart?: (data: { trace_id: string; message_id?: string }) => void;
  /** 与后端 RewriteEvent 一致：字段叫 rewritten（此前读 rewritten_query，永远是 undefined） */
  onRewrite?: (data: { rewritten: string }) => void;
  onRetrieval?: (data: {
    chunks: Array<{
      id?: string;
      chunk_id?: string;
      document_id?: string;
      title?: string;
      document_title?: string;
      page?: number;
      snippet?: string;
      score?: number;
      vec_score?: number;
      bm25_score?: number;
      rrf?: number;
      rerank_score?: number;
      legs?: string[];
      kind?: 'body' | 'summary';
      heading_path?: string | null;
      ordinal?: number | null;
      merged_from?: string[];
      quote_start?: number | null;
      quote_end?: number | null;
    }>;
    /** 配置了却没用上的检索环节：`vector`（退化成纯全文）、`rerank`（退回 RRF 顺序） */
    degraded?: string[];
  }) => void;
  onInsights?: (data: {
    insights: Array<{
      id: string;
      trigger: string;
      guidance: string;
      confidence: number;
      scope?: 'space' | 'global';
    }>;
  }) => void;
  onDelta?: (data: { text: string }) => void;
  onCitation?: (data: CitationMarker) => void;
  onDone?: (data: DonePayload) => void;
  onError?: (err: { code?: string; message: string }) => void;
}

export const chatService = {
  async getConversations(
    spaceId: string,
    options?: ConversationListOptions | number,
    cursorParam?: string | null,
  ): Promise<PageResponse<Conversation>> {
    let limit = 50;
    let cursor: string | null = null;
    let q: string | undefined;
    let signal: AbortSignal | undefined;

    if (typeof options === 'number') {
      limit = typeof cursorParam === 'number' ? cursorParam : options || 50;
      cursor = typeof cursorParam === 'string' ? cursorParam : null;
    } else if (options) {
      if (options.limit !== undefined) limit = options.limit;
      if (options.cursor !== undefined) cursor = options.cursor;
      q = options.q;
      signal = options.signal;
    }

    if (isMockMode()) {
      const filtered = mockConversations.filter(
        (c) =>
          (c.space_id === spaceId || !spaceId) &&
          (!q || c.title.toLowerCase().includes(q.toLowerCase())),
      );
      return mockPage(
        [...filtered].sort(
          (a, b) =>
            Number(b.pinned) - Number(a.pinned) ||
            b.updated_at - a.updated_at ||
            b.id.localeCompare(a.id),
        ),
        limit,
        cursor,
      );
    }

    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    if (q) params.set('q', q);
    const qs = params.toString();

    const res = await apiClient<PageResponse<Conversation>>(
      `/spaces/${spaceId}/conversations${qs ? `?${qs}` : ''}`,
      { signal },
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  async createConversation(
    spaceId: string,
    data?: Partial<ConversationCreate>,
  ): Promise<Conversation> {
    if (isMockMode()) {
      const newConv: Conversation = {
        id: `conv-${Date.now()}`,
        space_id: spaceId,
        title: data?.title || '新对话',
        pinned: data?.pinned ?? false,
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockConversations.unshift(newConv);
      return newConv;
    }
    return apiClient<Conversation>(`/spaces/${spaceId}/conversations`, {
      method: 'POST',
      body: JSON.stringify({
        space_id: spaceId,
        title: data?.title || '新对话',
        pinned: data?.pinned ?? false,
      }),
    });
  },

  async getConversation(
    conversationId: string,
  ): Promise<{ conversation: Conversation; messages: Message[] }> {
    if (isMockMode()) {
      const conv =
        mockConversations.find((c) => c.id === conversationId) ||
        mockConversations[0];
      return { conversation: conv, messages: mockMessages };
    }
    const res = await apiClient<{
      conversation?: Conversation;
      messages?: Message[];
      id?: string;
      title?: string;
      space_id?: string;
      pinned?: boolean;
      created_at?: number;
      updated_at?: number;
    }>(`/conversations/${conversationId}`);
    if (res.conversation && res.messages) {
      return { conversation: res.conversation, messages: res.messages };
    }
    const conv: Conversation = {
      id: res.id || conversationId,
      space_id: res.space_id || '',
      title: res.title || '',
      pinned: Boolean(res.pinned),
      created_at: res.created_at || Date.now(),
      updated_at: res.updated_at || Date.now(),
    };
    return { conversation: conv, messages: res.messages || [] };
  },

  async updateConversation(
    conversationId: string,
    data: ConversationUpdate,
  ): Promise<Conversation> {
    if (isMockMode()) {
      const found = mockConversations.find((c) => c.id === conversationId);
      if (found) {
        if (data.title !== undefined && data.title !== null)
          found.title = data.title;
        if (data.pinned !== undefined && data.pinned !== null)
          found.pinned = data.pinned;
        found.updated_at = Date.now();
        return found;
      }
      return mockConversations[0];
    }
    return apiClient<Conversation>(`/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async deleteConversation(
    conversationId: string,
  ): Promise<{ deleted: boolean }> {
    if (isMockMode()) {
      const idx = mockConversations.findIndex((c) => c.id === conversationId);
      if (idx >= 0) mockConversations.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(`/conversations/${conversationId}`, {
      method: 'DELETE',
    });
  },

  async streamChat(
    conversationId: string,
    query: string,
    handlers: ChatStreamHandlers,
    signal?: AbortSignal,
    options?: {
      useRetrieval?: boolean;
      useInsights?: boolean;
      llmRole?: string;
      searchMode?: 'hybrid' | 'vector';
      contextMode?: 'standard' | 'economy';
    },
  ): Promise<void> {
    if (isMockMode()) {
      // simulate SSE in mock mode
      handlers.onTraceStart?.({ trace_id: 'tr-mock' });
      await new Promise((r) => setTimeout(r, 200));
      handlers.onRewrite?.({ rewritten: query + ' (mock expanded)' });
      await new Promise((r) => setTimeout(r, 250));
      handlers.onRetrieval?.({
        chunks: [
          {
            chunk_id: 'chk-01',
            document_title:
              '第三代小分子 EGFR-TKI 抑制剂设计与构效关系研究.pdf',
            snippet:
              '奥希替尼等第三代化合物通过丙烯酰胺基团与 EGFR ATP 结合口袋边缘保留的 Cys797 形成共价键结合。',
            rerank_score: 0.94,
            vec_score: 0.92,
            bm25_score: 16.5,
            rrf: 0.031,
            legs: ['vector:1', 'fts:1'],
          },
        ],
      });
      await new Promise((r) => setTimeout(r, 200));
      handlers.onInsights?.({
        insights: [
          {
            id: 'ins-01',
            trigger: '先导化合物优化评估',
            guidance:
              '优先查验 ADMET 五项性质与微粒体稳定性，规避 hERG 心脏毒性',
            confidence: 0.96,
          },
        ],
      });
      await new Promise((r) => setTimeout(r, 150));
      const sampleText =
        '针对 EGFR T790M 耐药突变，建议引入共价丙烯酰胺弹头靶向 Cys797，并严格评估野生型选择性窗口与 ADMET 心脏毒性。';
      for (const char of sampleText) {
        if (signal?.aborted) return;
        handlers.onDelta?.({ text: char });
        await new Promise((r) => setTimeout(r, 20));
      }
      handlers.onCitation?.({
        marker: 'c1',
        chunk_id: 'chk-01',
        document_id: 'doc-01',
        snippet:
          '奥希替尼等第三代化合物通过丙烯酰胺基团与 EGFR ATP 结合口袋边缘保留的 Cys797 形成共价键结合。',
        document_title: '第三代小分子 EGFR-TKI 抑制剂设计与构效关系研究.pdf',
      });
      handlers.onDone?.({
        message_id: `msg-${Date.now()}`,
        trace_id: 'tr-mock',
        usage: { prompt_tokens: 300, completion_tokens: 80, latency_ms: 1200 },
      });
      return;
    }

    await fetchSSE({
      url: `/conversations/${conversationId}/chat`,
      method: 'POST',
      // 字段名与后端 ChatRequest 对齐：此前发的是 query，后端要 content，
      // 真实环境下每次提问都是 422，界面还停在「思考中…」
      body: {
        content: query,
        use_retrieval: options?.useRetrieval ?? true,
        // 此前「仅向量」只在前端改了个状态，请求里没有它，实际仍是混合检索
        search_mode: options?.searchMode ?? 'hybrid',
        use_insights: options?.useInsights ?? true,
        context_mode: options?.contextMode ?? 'standard',
        ...(options?.llmRole ? { llm_role: options.llmRole } : {}),
      },
      signal,
      handlers: {
        context: (data) => handlers.onContext?.(data as ContextUsage),
        trace_start: (data) =>
          handlers.onTraceStart?.(
            data as { trace_id: string; message_id?: string },
          ),
        rewrite: (data) => handlers.onRewrite?.(data as { rewritten: string }),
        retrieval: (data) =>
          handlers.onRetrieval?.(
            data as {
              chunks: Array<{
                id?: string;
                chunk_id?: string;
                document_id?: string;
                title?: string;
                document_title?: string;
                page?: number;
                snippet?: string;
                score?: number;
                vec_score?: number;
                bm25_score?: number;
                rrf?: number;
                rerank_score?: number;
                legs?: string[];
                kind?: 'body' | 'summary';
                heading_path?: string | null;
                ordinal?: number | null;
                merged_from?: string[];
              }>;
              degraded?: string[];
            },
          ),
        insights: (data) =>
          handlers.onInsights?.(
            data as {
              insights: Array<{
                id: string;
                trigger: string;
                guidance: string;
                confidence: number;
              }>;
            },
          ),
        delta: (data) => handlers.onDelta?.(data as { text: string }),
        citation: (data) => handlers.onCitation?.(data as CitationMarker),
        done: (data) => handlers.onDone?.(data as DonePayload),
        error: (data) =>
          handlers.onError?.(data as { code?: string; message: string }),
      },
    });
  },

  async stopChat(conversationId: string): Promise<{ stopped: boolean }> {
    if (isMockMode()) return { stopped: true };
    return apiClient<{ stopped: boolean }>(
      `/conversations/${conversationId}/stop`,
      {
        method: 'POST',
      },
    );
  },

  async sendFeedback(
    traceId: string,
    feedback: FeedbackRequest,
  ): Promise<Feedback> {
    if (isMockMode()) {
      return {
        id: `fb-${Date.now()}`,
        trace_id: traceId,
        kind: feedback.kind,
        comment: feedback.comment || null,
        created_at: Date.now(),
        distilled: false,
      };
    }
    return apiClient<Feedback>(`/traces/${traceId}/feedback`, {
      method: 'POST',
      body: JSON.stringify(feedback),
    });
  },

  async getTrace(traceId: string): Promise<TraceDetail> {
    if (isMockMode()) return mockTraceDetail;
    const res = await apiClient<Schemas['TraceView']>(`/traces/${traceId}`);
    // 轨迹只存经验 id，后端按 id 回表补全放在 insight_details。按原顺序换成完整对象；
    // 回表查不到的（经验已被删）保留 id，由证据栏如实写「已删除」，不编文案
    const details = new Map((res.insight_details ?? []).map((i) => [i.id, i]));
    const trace: Omit<typeof res, 'insight_details'> & {
      insight_details?: unknown;
    } = { ...res };
    delete trace.insight_details;
    return {
      ...trace,
      used_insights: (res.used_insights ?? []).map(
        (id) => (details.get(id) as Insight | undefined) ?? id,
      ),
    } as TraceDetail;
  },
};
