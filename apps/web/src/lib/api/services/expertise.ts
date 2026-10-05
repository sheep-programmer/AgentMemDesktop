import { taskDelay } from '@/lib/taskDelay';
import { apiClient } from '../client';
import { fetchSSE } from '../sse';
import type {
  ExpertiseDimensions,
  ExpertiseSnapshot,
  ConsistencyProbe,
  KnowledgeGapNode,
  EvalItem,
  EvalItemRequest,
  EvalRun,
  EvalRunRequest,
  EvalGenerateRequest,
  ExpertiseGapsResponse,
  OutlineResponse,
  ExpertiseHistoryResponse,
  EvalRunListResponse,
  RetrievalMetrics,
  PageResponse,
  EvalCompareRequest,
  EvalArmDelta,
  InsightAttribution,
} from '../types';
import {
  mockCurrentExpertise,
  mockExpertiseHistory,
  mockKnowledgeGapsTree,
  mockEvalItems,
  mockEvalRuns,
} from '../mock/data';
import { isMockMode } from './spaces';

export type { PageResponse };

export interface EvalListOptions {
  limit?: number;
  cursor?: string | null;
  tag?: string;
}

export interface EvalItemResultData {
  eval_id?: string;
  item_id?: string;
  passed: boolean;
  score: number;
  question?: string;
  reason?: string | null;
  metrics?: RetrievalMetrics | null;
}

export interface EvalRunDoneData {
  variant: string;
  score: number;
  run_id: string;
  metrics?: RetrievalMetrics | null;
}

export interface EvalRunHandlers {
  onItemResult?: (data: EvalItemResultData) => void;
  onProgress?: (data: { done: number; total: number }) => void;
  onError?: (err: { code?: string; message: string }) => void;
  onDone?: (data: EvalRunDoneData) => void;
}

export interface EvalGenerateHandlers {
  onItem?: (data: EvalItem) => void;
  onProgress?: (data: { done: number; total: number }) => void;
  onError?: (err: { code?: string; message: string }) => void;
  onDone?: (data: { count: number }) => void;
}

export interface EvalCompareStageData {
  stage: string;
  status: string;
  label: string;
}

export interface EvalCompareArmData {
  label: string;
  run_id: string;
  score: number;
  metrics?: RetrievalMetrics | null;
}

export interface EvalCompareDoneData {
  space_id: string;
  items: number;
  baseline: string;
  deltas: EvalArmDelta[];
}

export interface EvalCompareErrorData {
  code?: string;
  message?: string;
  detail?: unknown;
}

export interface EvalCompareHandlers {
  onStage?: (data: EvalCompareStageData) => void;
  onArm?: (data: EvalCompareArmData) => void;
  onDone?: (data: EvalCompareDoneData) => void;
  onError?: (err: EvalCompareErrorData) => void;
}

export const expertiseService = {
  async getExpertise(spaceId: string): Promise<ExpertiseDimensions> {
    if (isMockMode()) return mockCurrentExpertise;
    return apiClient<ExpertiseDimensions>(`/spaces/${spaceId}/expertise`);
  },

  async measureConsistency(
    spaceId: string,
    options?: { questions?: number; repeats?: number },
  ): Promise<ConsistencyProbe> {
    const questions = options?.questions ?? 3;
    const repeats = options?.repeats ?? 3;

    if (isMockMode()) {
      await new Promise((r) => setTimeout(r, 1200));
      return {
        id: `probe-${Date.now()}`,
        space_id: spaceId,
        questions,
        repeats,
        similarity: 0.87,
        detail: [
          {
            question: '成药性要先看什么？',
            similarity: 0.91,
            answers: [
              '成药性首先应评估目标分子的类药五原则（Lipinski Rule of 5）、代谢稳定性及水溶性，确认其具备体内成药潜能。',
              '评估成药性首要关注物理化学性质（如溶解度、LogP）与早期代谢动力学（微粒体清除率等），避免后续毒性风险。',
              '在早期发现阶段，成药性优先考量结构稳定性、体外溶解度及细胞毒性特征，综合判断可优化空间。',
            ],
          },
          {
            question:
              '如何评价化合物与血浆蛋白结合率（PPB）对有效血药浓度的影响？',
            similarity: 0.85,
            answers: [
              '根据游离药物假说，仅游离型分子能穿透生物膜并与靶点结合，因此高 PPB 会压低即时游离血药浓度。',
              '需结合体内清除速率与表观分布容积综合评价 PPB，单纯结合率高并不意味着药效不足，需考察维持游离浓度。',
              '血浆蛋白结合可充当缓释库，评价时需同时监测游离稳态浓度与组织靶点占有率。',
            ],
          },
          {
            question: '针对 EGFR T790M 耐药突变的首选化合物骨架是什么？',
            similarity: 0.88,
            answers: [
              '首选具有丙烯酰胺迈克尔受体的嘧啶并二胺骨架，能特异性与 Cys797 形成不可逆共价键。',
              '通常采用含有共价弹头的第三代 EGFR 抑制剂骨架（如奥希替尼母核），有效规避门控突变造成的空间位阻。',
              '以单苯或双苯氨基嘧啶为母核，配合侧链亲电弹头精准识别 Cys797，实现对 T790M 突变的高选择性抑制。',
            ],
          },
        ].slice(0, questions),
        created_at: Date.now(),
      };
    }

    const params = new URLSearchParams();
    if (questions) params.set('questions', String(questions));
    if (repeats) params.set('repeats', String(repeats));
    const qs = params.toString();

    return apiClient<ConsistencyProbe>(
      `/spaces/${spaceId}/expertise/consistency${qs ? `?${qs}` : ''}`,
      {
        method: 'POST',
      },
    );
  },

  async getHistory(spaceId: string): Promise<ExpertiseSnapshot[]> {
    if (isMockMode()) return mockExpertiseHistory;
    const res = await apiClient<ExpertiseHistoryResponse>(
      `/spaces/${spaceId}/expertise/history`,
    );
    return res.snapshots || [];
  },

  async getGaps(spaceId: string): Promise<{
    gaps: KnowledgeGapNode[];
    outline_size?: number;
    outline_generated_at?: number | null;
    outline_interpretation?: string | null;
    outline_error?: string | null;
  }> {
    if (isMockMode()) {
      return {
        gaps: mockKnowledgeGapsTree,
        outline_size: 11,
        outline_generated_at: 1773520000000,
      };
    }
    const res = await apiClient<ExpertiseGapsResponse>(
      `/spaces/${spaceId}/expertise/gaps`,
    );
    const backendGaps = res.gaps || [];
    const gaps = backendGaps.map((g, idx) => ({
      ...g,
      id: `gap-${idx}`,
      title: g.topic,
      covered: false,
    }));
    return {
      gaps,
      outline_size: res.outline_size,
      outline_generated_at: res.outline_generated_at,
      outline_interpretation: res.outline_interpretation ?? null,
      outline_error: res.outline_error ?? null,
    };
  },

  async generateOutline(spaceId: string): Promise<OutlineResponse> {
    if (isMockMode()) {
      await new Promise((r) => setTimeout(r, 1500));
      return {
        outline: {
          id: `outline-${Date.now()}`,
          space_id: spaceId,
          domain: '先导化合物优化与抗肿瘤药物研发',
          nodes: [
            {
              topic: '靶点突变与构效关系',
              subtopics: ['EGFR T790M', 'Cys797 反应性'],
              importance: 'core',
            },
            {
              topic: 'ADMET 成药性早期评价',
              subtopics: ['hERG 心脏毒性', '微粒体清除率'],
              importance: 'core',
            },
            {
              topic: '分子模拟与自由能微扰',
              subtopics: ['FEP+', '别构位点探测'],
              importance: 'peripheral',
            },
            {
              topic: 'PROTAC 靶向降解技术',
              subtopics: ['E3 连接酶选择', '三元复合物稳定性'],
              importance: 'peripheral',
            },
          ],
          created_at: Date.now(),
        },
        covered: 8,
        total: 11,
      };
    }
    return apiClient<OutlineResponse>(`/spaces/${spaceId}/expertise/outline`, {
      method: 'POST',
    });
  },

  /**
   * 取**全部**评测题，翻页直到取完。
   *
   * 评测分决定经验的晋升与淘汰，只评一部分题会在用户不知情的情况下改变结论。
   * `getEvals` 默认只取一页（后端 limit 默认 100），题目超过一页时
   * `EvalSetRunner` 会静默只跑前 100 题，界面上的「运行双盲评测 (N)」
   * 也只反映已加载的条数。这里一次性翻完，调用方拿到的就是完整题集。
   */
  async getAllEvals(spaceId: string, tag?: string): Promise<EvalItem[]> {
    const all: EvalItem[] = [];
    let cursor: string | null = null;
    // 兜个上限，避免后端游标异常时无限翻页
    for (let page = 0; page < 100; page += 1) {
      const res: PageResponse<EvalItem> = await this.getEvals(spaceId, {
        limit: 200,
        cursor,
        tag,
      });
      all.push(...res.items);
      cursor = res.next_cursor ?? null;
      if (!cursor) break;
    }
    return all;
  },

  async getEvals(
    spaceId: string,
    options?: EvalListOptions | number,
    cursorParam?: string | null,
    tagParam?: string,
  ): Promise<PageResponse<EvalItem>> {
    let limit = 50;
    let cursor: string | null = null;
    let tag: string | undefined;

    if (typeof options === 'number') {
      limit = typeof cursorParam === 'number' ? cursorParam : options || 50;
      cursor = typeof cursorParam === 'string' ? cursorParam : null;
      tag = tagParam;
    } else if (options) {
      if (options.limit !== undefined) limit = options.limit;
      if (options.cursor !== undefined) cursor = options.cursor;
      tag = options.tag;
    }

    if (isMockMode()) {
      let filtered = [...mockEvalItems];
      if (tag) filtered = filtered.filter((i) => i.tags?.includes(tag));
      return { items: filtered, total: filtered.length, next_cursor: null };
    }

    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    if (tag) params.set('tag', tag);
    const qs = params.toString();

    const res = await apiClient<PageResponse<EvalItem>>(
      `/spaces/${spaceId}/evals${qs ? `?${qs}` : ''}`,
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  async createEvalItem(
    spaceId: string,
    data: EvalItemRequest,
  ): Promise<EvalItem> {
    return apiClient<EvalItem>(`/spaces/${spaceId}/evals`, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async deleteEvalItem(
    spaceId: string,
    evalId: string,
  ): Promise<{ deleted: boolean; id?: string }> {
    if (isMockMode()) {
      const idx = mockEvalItems.findIndex((i) => i.id === evalId);
      if (idx >= 0) mockEvalItems.splice(idx, 1);
      return { deleted: true, id: evalId };
    }
    return apiClient<{ deleted: boolean; id: string }>(
      `/spaces/${spaceId}/evals/${evalId}`,
      {
        method: 'DELETE',
      },
    );
  },

  async generateEvalsStream(
    spaceId: string,
    data: EvalGenerateRequest,
    handlers: EvalGenerateHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      handlers.onProgress?.({ done: 1, total: 2 });
      await taskDelay(500, signal);
      handlers.onItem?.({
        id: `mock-eval-${Date.now()}-1`,
        space_id: spaceId,
        question:
          '合成致死与同源重组缺陷（HRD）在 PARP 抑制剂响应中的相关机制为何？',
        reference:
          'PARP 抑制剂通过捕获 PARP-DNA 复合物阻断单链修复，使双链断裂依赖 HRR 修复路径...',
        tags: ['合成致死', 'HRD', 'PARP'],
        source: 'auto_from_doc',
        created_at: Date.now(),
      });
      handlers.onProgress?.({ done: 2, total: 2 });
      await taskDelay(500, signal);
      handlers.onItem?.({
        id: `mock-eval-${Date.now()}-2`,
        space_id: spaceId,
        question:
          '分析共价抑制剂结合动力学中 kinact/KI 评价指标相比常规 IC50 的优势与应用局限。',
        reference:
          'kinact/KI 表征不可逆结合的两步反应速率与亲和力，更能真实反映共价阻断效率...',
        tags: ['共价抑制剂', '动力学', '构效关系'],
        source: 'auto_from_doc',
        created_at: Date.now(),
      });
      handlers.onDone?.({ count: 2 });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/evals/generate`,
      method: 'POST',
      terminalEvent: 'done',
      body: data,
      signal,
      handlers: {
        item: (d) => handlers.onItem?.(d as EvalItem),
        progress: (d) =>
          handlers.onProgress?.(d as { done: number; total: number }),
        error: (d) =>
          handlers.onError?.(d as { code?: string; message: string }),
        done: (d) => handlers.onDone?.(d as { count: number }),
      },
    });
  },

  async runEvalsStream(
    spaceId: string,
    data: EvalRunRequest,
    handlers: EvalRunHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      const items = mockEvalItems.filter((item) => item.space_id === spaceId);
      let totalScore = 0;
      for (const [index, item] of items.entries()) {
        handlers.onProgress?.({ done: index + 1, total: items.length });
        await taskDelay(600, signal);
        const passed = index !== 2;
        const score = passed ? 85 : 40;
        totalScore += score;
        handlers.onItemResult?.({
          item_id: item.id,
          passed,
          score,
          question: item.question,
          reason: '示例评测结果',
          metrics: {
            context_recall: passed ? 0.8 : 0.25,
            context_precision: 1,
            faithfulness: passed ? 0.75 : null,
            claims: 3,
            evidence: 4,
            audited: true,
          },
        });
      }
      handlers.onDone?.({
        variant: data.variant || 'baseline',
        score: items.length ? totalScore / items.length : 0,
        run_id: `mock-run-${Date.now()}`,
        metrics: {
          context_recall: 0.65,
          context_precision: 1,
          faithfulness: 0.75,
          claims: 6,
          evidence: 8,
          audited: true,
        },
      });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/evals/run`,
      method: 'POST',
      terminalEvent: 'done',
      body: data,
      signal,
      handlers: {
        item: (d) => handlers.onItemResult?.(d as EvalItemResultData),
        item_result: (d) => handlers.onItemResult?.(d as EvalItemResultData),
        progress: (d) =>
          handlers.onProgress?.(d as { done: number; total: number }),
        error: (d) =>
          handlers.onError?.(d as { code?: string; message: string }),
        done: (d) => handlers.onDone?.(d as EvalRunDoneData),
      },
    });
  },

  async compareEvalsStream(
    spaceId: string,
    data: EvalCompareRequest,
    handlers: EvalCompareHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      const arms = data.arms || [];
      for (const arm of arms) {
        handlers.onStage?.({
          stage: 'compare',
          status: 'running',
          label: arm.label,
        });
        await taskDelay(400, signal);
      }
      const outcomes: EvalCompareArmData[] = arms.map((arm, idx) => ({
        label: arm.label,
        run_id: `mock-cmp-run-${idx + 1}`,
        score: idx === 0 ? 72.4 : idx === 1 ? 71.2 : 75.8,
        metrics: {
          context_recall: idx === 0 ? 0.85 : idx === 1 ? 0.68 : 0.92,
          context_precision: idx === 0 ? 0.9 : idx === 1 ? 0.95 : 0.88,
          faithfulness: idx === 0 ? 0.8 : idx === 1 ? 0.8 : 0.85,
          claims: 4,
          evidence: 6,
          audited: true,
        },
      }));
      for (const outcome of outcomes) {
        handlers.onArm?.(outcome);
      }
      const baseline = arms[0]?.label || '基准';
      const deltas: EvalArmDelta[] = arms.slice(1).map((arm, idx) => ({
        label: arm.label,
        score_delta: idx === 0 ? -1.2 : 3.4,
        metrics_delta: {
          context_recall: idx === 0 ? -0.17 : 0.07,
          context_precision: idx === 0 ? 0.05 : -0.02,
          faithfulness: idx === 0 ? 0.0 : 0.05,
          claims: 0,
          evidence: 0,
          audited: true,
        },
        item_deltas: [
          { item_id: 'eval-1', score_delta: idx === 0 ? 0.0 : 5.0 },
          { item_id: 'eval-2', score_delta: idx === 0 ? -2.4 : 1.8 },
        ],
      }));
      handlers.onDone?.({
        space_id: spaceId,
        items: 2,
        baseline,
        deltas,
      });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/evals/compare`,
      method: 'POST',
      terminalEvent: 'done',
      body: data,
      signal,
      handlers: {
        stage: (d) => handlers.onStage?.(d as EvalCompareStageData),
        arm: (d) => handlers.onArm?.(d as EvalCompareArmData),
        done: (d) => handlers.onDone?.(d as EvalCompareDoneData),
        error: (d) => handlers.onError?.(d as EvalCompareErrorData),
      },
      onError: (err) => {
        handlers.onError?.({
          code: 'STREAM_ERROR',
          message: (err as Error)?.message || '网络连接或流式响应异常',
        });
      },
    });
  },

  /**
   * 留一法归因：把每条经验单独拿掉重跑同一批题。
   *
   * 很贵（N+1 轮评测），调用方必须挑明要归因哪几条。
   */
  async attributeInsights(
    spaceId: string,
    payload: { insight_ids: string[]; include_noise_floor?: boolean },
  ): Promise<InsightAttribution> {
    return apiClient<InsightAttribution>(
      `/spaces/${spaceId}/insights/attribute`,
      {
        method: 'POST',
        body: JSON.stringify({ include_noise_floor: true, ...payload }),
      },
    );
  },

  async getEvalRuns(spaceId: string): Promise<EvalRun[]> {
    if (isMockMode()) return mockEvalRuns;
    const res = await apiClient<EvalRunListResponse>(
      `/spaces/${spaceId}/evals/runs`,
    );
    return res.runs || [];
  },
};
