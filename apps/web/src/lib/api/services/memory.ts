import { apiClient } from '../client';
import { fetchSSE } from '../sse';
import type {
  KnowledgeCard,
  KnowledgeCardRequest,
  KnowledgeCardUpdate,
  CardVersion,
  CardHistoryResponse,
  KnowledgeGraphData,
  Insight,
  InsightRequest,
  InsightUpdate,
  ConflictGroup,
  ConflictResolveRequest,
  InsightHistoryResponse,
  InsightReviewResponse,
  InsightReviewItem,
  CardExtractRequest,
  PageResponse,
} from '../types';
import {
  mockKnowledgeCards,
  mockGraphData,
  mockInsights,
  mockConflictGroups,
  mockInsightHistoryStore,
} from '../mock/data';
import { isMockMode } from './spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';

export type { PageResponse };

export interface CardListOptions {
  limit?: number;
  cursor?: string | null;
  kind?: string;
  q?: string;
  min_confidence?: number;
}

export interface InsightListOptions {
  limit?: number;
  cursor?: string | null;
  status?: string;
  kind?: string;
  q?: string;
  sort?: string;
}

export interface CardExtractionReport {
  cards_created: number;
  cards_updated: number;
  entities_created: number;
  relations_created: number;
  duration_ms?: number;
}

export interface CardExtractHandlers {
  onProgress?: (data: {
    stage: string;
    done: number;
    total: number;
    document_id?: string;
  }) => void;
  onError?: (err: {
    code?: string;
    message: string;
    document_id?: string;
  }) => void;
  onDone?: (report: CardExtractionReport) => void;
}

const mockCardVersionsStore: Record<string, CardVersion[]> = {
  'card-01': [
    {
      id: 'card-01-v1',
      card_id: 'card-01',
      space_id: 'space-drug-discovery',
      version: 1,
      kind: 'procedure',
      title: '先导化合物优化（Lead Optimization）初版流程',
      body: `1. **构效关系 (SAR) 轮次迭代**：基于结合构象进行片段跃迁与侧链修饰，提升对突变靶点的纳摩尔级结合活性。
2. **体外酶学与激酶谱筛选**：通过 Lance/KinomeScan 确认激酶亚型选择性窗口（> 50 倍）。
3. **早期 ADMET 与脱靶反筛**：同步测定 Caco-2 膜通透性及 hERG 膜片钳抑制（> 10 μM）。`,
      aliases: ['先导物优化流程'],
      source_chunks: [],
      confidence: 0.88,
      verified_by: null,
      valid_from: 1773125000000,
      valid_to: 1773480000000,
      created_at: 1773125000000,
    },
  ],
};

export const memoryService = {
  // --- L2 Cards ---
  async getCards(
    spaceId: string,
    options?: CardListOptions | number,
    cursorParam?: string | null,
    kindParam?: string,
  ): Promise<PageResponse<KnowledgeCard>> {
    let limit = 50;
    let cursor: string | null = null;
    let kind: string | undefined;
    let q: string | undefined;
    let minConfidence: number | undefined;

    if (typeof options === 'number') {
      limit = typeof cursorParam === 'number' ? cursorParam : options || 50;
      cursor = typeof cursorParam === 'string' ? cursorParam : null;
      kind = kindParam;
    } else if (options) {
      if (options.limit !== undefined) limit = options.limit;
      if (options.cursor !== undefined) cursor = options.cursor;
      kind = options.kind;
      q = options.q;
      minConfidence = options.min_confidence;
    }

    if (isMockMode()) {
      let filtered = mockKnowledgeCards.filter(
        (c) => c.space_id === spaceId || !spaceId,
      );
      if (kind && kind !== 'all')
        filtered = filtered.filter((c) => c.kind === kind);
      if (q)
        filtered = filtered.filter(
          (c) =>
            c.title.toLowerCase().includes(q.toLowerCase()) ||
            c.body.toLowerCase().includes(q.toLowerCase()),
        );
      return { items: filtered, total: filtered.length, next_cursor: null };
    }

    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    if (kind && kind !== 'all') params.set('kind', kind);
    if (q) params.set('q', q);
    if (minConfidence !== undefined)
      params.set('min_confidence', String(minConfidence));
    const qs = params.toString();

    const res = await apiClient<PageResponse<KnowledgeCard>>(
      `/spaces/${spaceId}/cards${qs ? `?${qs}` : ''}`,
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  async getCardVersions(
    spaceId: string,
    cardId: string,
  ): Promise<CardHistoryResponse> {
    if (isMockMode()) {
      const found = mockKnowledgeCards.find((c) => c.id === cardId) || {
        id: cardId,
        space_id: spaceId,
        kind: 'concept',
        title: '未知卡片',
        body: '',
        aliases: [],
        source_chunks: [],
        confidence: 0.5,
        verified_by: null,
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      return {
        card_id: cardId,
        card: found,
        versions: mockCardVersionsStore[cardId] || [],
      };
    }
    return apiClient<CardHistoryResponse>(
      `/spaces/${spaceId}/cards/${cardId}/versions`,
    );
  },

  async createCard(
    spaceId: string,
    data: KnowledgeCardRequest,
  ): Promise<KnowledgeCard> {
    if (isMockMode()) {
      const card: KnowledgeCard = {
        id: `card-${Date.now()}`,
        space_id: spaceId,
        kind: data.kind,
        title: data.title,
        body: data.body,
        aliases: data.aliases || [],
        confidence: data.confidence ?? 1.0,
        verified_by: data.verified_by || 'user',
        source_chunks: data.source_chunks || [],
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockKnowledgeCards.unshift(card);
      return card;
    }
    return apiClient<KnowledgeCard>(`/spaces/${spaceId}/cards`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async updateCard(
    spaceId: string,
    cardId: string,
    data: KnowledgeCardUpdate,
  ): Promise<KnowledgeCard> {
    if (isMockMode()) {
      const found = mockKnowledgeCards.find((c) => c.id === cardId);
      if (found) {
        // 留档规则：只有正文或标题被改动才产生历史版本
        const titleChanged =
          data.title !== undefined && data.title !== found.title;
        const bodyChanged = data.body !== undefined && data.body !== found.body;
        if (titleChanged || bodyChanged) {
          const prevList = mockCardVersionsStore[cardId] || [];
          const nextVer = prevList.length > 0 ? prevList[0].version + 1 : 1;
          const archived: CardVersion = {
            id: `${cardId}-v${nextVer}-${Date.now()}`,
            card_id: cardId,
            space_id: spaceId,
            version: nextVer,
            kind: found.kind,
            title: found.title,
            body: found.body,
            aliases: found.aliases,
            source_chunks: found.source_chunks,
            confidence: found.confidence,
            verified_by: found.verified_by,
            valid_from: found.updated_at,
            valid_to: Date.now(),
            created_at: Date.now(),
          };
          mockCardVersionsStore[cardId] = [archived, ...prevList];
        }
        Object.assign(found, data, { updated_at: Date.now() });
        return found;
      }
      return mockKnowledgeCards[0];
    }
    return apiClient<KnowledgeCard>(`/spaces/${spaceId}/cards/${cardId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async deleteCard(
    spaceId: string,
    cardId: string,
  ): Promise<{ deleted: boolean }> {
    if (isMockMode()) {
      const idx = mockKnowledgeCards.findIndex((c) => c.id === cardId);
      if (idx >= 0) mockKnowledgeCards.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(
      `/spaces/${spaceId}/cards/${cardId}`,
      {
        method: 'DELETE',
      },
    );
  },

  async extractCardsStream(
    spaceId: string,
    data: CardExtractRequest,
    handlers: CardExtractHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      handlers.onProgress?.({ stage: 'extracting', done: 1, total: 3 });
      await new Promise((r) => setTimeout(r, 400));
      handlers.onProgress?.({ stage: 'extracting', done: 2, total: 3 });
      await new Promise((r) => setTimeout(r, 400));
      handlers.onProgress?.({ stage: 'extracting', done: 3, total: 3 });
      handlers.onDone?.({
        cards_created: 4,
        cards_updated: 1,
        entities_created: 6,
        relations_created: 8,
      });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/cards/extract`,
      method: 'POST',
      body: data,
      signal,
      handlers: {
        progress: (d) =>
          handlers.onProgress?.(
            d as {
              stage: string;
              done: number;
              total: number;
              document_id?: string;
            },
          ),
        error: (d) =>
          handlers.onError?.(
            d as { code?: string; message: string; document_id?: string },
          ),
        done: (d) => handlers.onDone?.(d as unknown as CardExtractionReport),
      },
    });
  },

  // --- Knowledge Graph ---
  async getGraph(
    spaceId: string,
    center?: string,
    depth = 2,
    limit = 300,
    signal?: AbortSignal,
  ): Promise<KnowledgeGraphData> {
    if (isMockMode()) return mockGraphData;
    const query = new URLSearchParams({
      depth: String(depth),
      limit: String(limit),
    });
    if (center) query.set('center', center);
    return apiClient<KnowledgeGraphData>(
      `/spaces/${spaceId}/graph?${query.toString()}`,
      { signal },
    );
  },

  // --- L3 Insights ---
  async getInsights(
    spaceId: string,
    options?: InsightListOptions | number,
    cursorParam?: string | null,
    statusParam?: string,
  ): Promise<PageResponse<Insight>> {
    let limit = 50;
    let cursor: string | null = null;
    let status: string | undefined;
    let kind: string | undefined;
    let q: string | undefined;
    let sort: string | undefined;

    if (typeof options === 'number') {
      limit = typeof cursorParam === 'number' ? cursorParam : options || 50;
      cursor = typeof cursorParam === 'string' ? cursorParam : null;
      status = statusParam;
    } else if (options) {
      if (options.limit !== undefined) limit = options.limit;
      if (options.cursor !== undefined) cursor = options.cursor;
      status = options.status;
      kind = options.kind;
      q = options.q;
      sort = options.sort;
    }

    if (isMockMode()) {
      let filtered = mockInsights.filter(
        (i) => i.space_id === spaceId || !spaceId,
      );
      if (status && status !== 'all')
        filtered = filtered.filter((i) => i.status === status);
      if (kind && kind !== 'all')
        filtered = filtered.filter((i) => i.kind === kind);
      if (q)
        filtered = filtered.filter(
          (i) =>
            i.trigger.toLowerCase().includes(q.toLowerCase()) ||
            i.guidance.toLowerCase().includes(q.toLowerCase()),
        );
      return { items: filtered, total: filtered.length, next_cursor: null };
    }

    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    if (status && status !== 'all') params.set('status', status);
    if (kind && kind !== 'all') params.set('kind', kind);
    if (q) params.set('q', q);
    if (sort) params.set('sort', sort);
    const qs = params.toString();

    const res = await apiClient<PageResponse<Insight>>(
      `/spaces/${spaceId}/insights${qs ? `?${qs}` : ''}`,
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  async getInsightsForReview(spaceId: string): Promise<InsightReviewResponse> {
    if (isMockMode()) {
      const items: InsightReviewItem[] = mockInsights
        .filter(
          (i) =>
            i.status !== 'archived' &&
            (i.applied_count ?? 0) >= 5 &&
            (i.success_count ?? 0) / (i.applied_count || 1) <= 0.4,
        )
        .map((i) => {
          const applied = i.applied_count ?? 0;
          const success = i.success_count ?? 0;
          const rate = applied > 0 ? success / applied : 0;
          return {
            insight: i,
            applied_count: applied,
            success_count: success,
            success_rate: rate,
            reason: `被注入 ${applied} 次，只有 ${success} 次收到好评`,
          };
        });
      return {
        items,
        min_applied: 5,
        max_success_rate: 0.4,
      };
    }
    return apiClient<InsightReviewResponse>(
      `/spaces/${spaceId}/insights/review`,
    );
  },

  async createInsight(spaceId: string, data: InsightRequest): Promise<Insight> {
    if (isMockMode()) {
      const insight: Insight = {
        id: `ins-${Date.now()}`,
        space_id: spaceId,
        trigger: data.trigger,
        guidance: data.guidance,
        rationale: data.rationale || null,
        kind: data.kind || 'heuristic',
        scope: data.scope || 'space',
        confidence: data.confidence ?? 0.8,
        // 与后端 InsightService.create_manual 一致：人工写的经验直接生效
        status: 'active',
        origin: 'manual',
        applied_count: 0,
        success_count: 0,
        eval_delta: null,
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockInsights.unshift(insight);
      return insight;
    }
    return apiClient<Insight>(`/spaces/${spaceId}/insights`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async updateInsight(
    arg1: string,
    arg2: string | InsightUpdate,
    arg3?: InsightUpdate | string,
  ): Promise<Insight> {
    let spaceId: string;
    let insightId: string;
    let data: InsightUpdate;

    if (typeof arg2 === 'string') {
      spaceId = arg1;
      insightId = arg2;
      data = (arg3 as InsightUpdate) || {};
    } else {
      spaceId =
        (typeof arg3 === 'string' ? arg3 : '') ||
        useSpaceStore.getState().currentSpaceId;
      insightId = arg1;
      data = arg2;
    }

    if (isMockMode()) {
      const found = mockInsights.find((i) => i.id === insightId);
      if (found) {
        Object.assign(found, data, { updated_at: Date.now() });
        return found;
      }
      return mockInsights[0];
    }
    return apiClient<Insight>(`/spaces/${spaceId}/insights/${insightId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async deleteInsight(
    arg1: string,
    arg2?: string,
  ): Promise<{ deleted: boolean }> {
    let spaceId: string;
    let insightId: string;

    if (arg2) {
      spaceId = arg1;
      insightId = arg2;
    } else {
      spaceId = useSpaceStore.getState().currentSpaceId;
      insightId = arg1;
    }

    if (isMockMode()) {
      const idx = mockInsights.findIndex((i) => i.id === insightId);
      if (idx >= 0) mockInsights.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(
      `/spaces/${spaceId}/insights/${insightId}`,
      {
        method: 'DELETE',
      },
    );
  },

  async getInsightHistory(
    spaceId: string,
    insightId: string,
  ): Promise<InsightHistoryResponse> {
    if (isMockMode()) {
      if (mockInsightHistoryStore[insightId]) {
        return mockInsightHistoryStore[insightId];
      }
      const found =
        mockInsights.find((i) => i.id === insightId) || mockInsights[0];
      return {
        insight_id: insightId,
        insight: found,
        events: [
          {
            id: `evt-${insightId}-init`,
            insight_id: insightId,
            space_id: spaceId,
            event: 'distilled',
            confidence_before: null,
            confidence_after: found.confidence,
            status_before: null,
            status_after: found.status,
            share: null,
            reason: '初始提炼',
            created_at: found.created_at,
          },
        ],
      };
    }
    return apiClient<InsightHistoryResponse>(
      `/spaces/${spaceId}/insights/${insightId}/history`,
    );
  },

  async getConflicts(spaceId: string): Promise<ConflictGroup[]> {
    if (isMockMode()) return mockConflictGroups;
    return apiClient<ConflictGroup[]>(`/spaces/${spaceId}/insights/conflicts`);
  },

  async resolveConflict(
    spaceId: string,
    groupId: string,
    data: ConflictResolveRequest,
  ): Promise<{ resolved: boolean }> {
    if (isMockMode()) {
      const idx = mockConflictGroups.findIndex((g) => g.group_id === groupId);
      if (idx >= 0) mockConflictGroups.splice(idx, 1);
      return { resolved: true };
    }
    return apiClient<{ resolved: boolean }>(
      `/spaces/${spaceId}/insights/conflicts/${groupId}/resolve`,
      {
        method: 'POST',
        body: JSON.stringify(data),
      },
    );
  },
};
