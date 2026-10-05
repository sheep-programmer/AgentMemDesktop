import { taskDelay } from '@/lib/taskDelay';
import { apiClient } from '../client';
import { fetchSSE } from '../sse';
import type {
  EvolvePendingSummary,
  EvolveHistoryItem,
  EvolveHistoryResponse,
} from '../types';
import { mockEvolvePending, mockEvolveHistory } from '../mock/data';
import { isMockMode } from './spaces';

export interface EvolveCycleHandlers {
  onStage?: (data: {
    stage: string;
    status: string;
    [key: string]: unknown;
  }) => void;
  onResult?: (data: {
    promoted_count?: number;
    demoted_count?: number;
    [key: string]: unknown;
  }) => void;
  onError?: (err: { code?: string; message: string }) => void;
  onDone?: (data: unknown) => void;
}

export const evolveService = {
  async getPending(
    spaceId: string,
    limit?: number,
  ): Promise<EvolvePendingSummary> {
    if (isMockMode()) return mockEvolvePending;
    const qs = limit ? `?limit=${limit}` : '';
    const res = await apiClient<EvolvePendingSummary>(
      `/spaces/${spaceId}/evolve/pending${qs}`,
    );
    // 计数以后端按种类的统计为准；此前从 preview（只有前 20 条）里数，反馈一多就少算
    const byKind = res.by_kind ?? {};
    const corrections = (byKind.correction ?? 0) + (byKind.edit ?? 0);
    return {
      ...res,
      feedback_count: res.pending_count ?? 0,
      correction_count: corrections,
    };
  },

  async getHistory(spaceId: string): Promise<EvolveHistoryItem[]> {
    if (isMockMode()) return mockEvolveHistory;
    const res = await apiClient<EvolveHistoryResponse>(
      `/spaces/${spaceId}/evolve/history`,
    );
    return res.items || [];
  },

  async runCycle(
    spaceId: string,
    handlers: EvolveCycleHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      handlers.onStage?.({ stage: 'distill', status: 'running' });
      await taskDelay(600, signal);
      handlers.onStage?.({ stage: 'distill', status: 'done', produced: 2 });
      handlers.onStage?.({ stage: 'consolidate', status: 'running' });
      await taskDelay(600, signal);
      handlers.onStage?.({
        stage: 'consolidate',
        status: 'done',
        merged: 0,
        duplicates: 0,
        conflicts: 0,
      });
      handlers.onStage?.({ stage: 'evaluate', status: 'running' });
      await taskDelay(700, signal);
      handlers.onStage?.({
        stage: 'evaluate',
        status: 'done',
        baseline: 70,
        with_insights: 76.5,
        delta: 6.5,
      });
      handlers.onStage?.({ stage: 'promote', status: 'running' });
      await taskDelay(300, signal);
      handlers.onStage?.({
        stage: 'promote',
        status: 'done',
        promoted: 2,
        demoted: 0,
      });
      const now = Date.now();
      Object.assign(mockEvolvePending, {
        pending_count: 0,
        feedback_count: 0,
        correction_count: 0,
        last_evolve_at: now,
      });
      mockEvolveHistory.unshift({
        ...mockEvolveHistory[0],
        id: `evo-${now}`,
        date: new Date(now).toISOString().slice(0, 10),
        run_at: now,
        created_at: now,
        produced: 2,
        merged: 0,
        conflicts: 0,
        promoted: 2,
        demoted: 0,
        eval_delta: 6.5,
        expertise_before: 58.2,
        expertise_after: 64.7,
        score_before: 58.2,
        score_after: 64.7,
        promoted_count: 2,
        demoted_count: 0,
      });
      handlers.onDone?.({
        produced: 2,
        merged: 0,
        promoted: 2,
        demoted: 0,
        expertise_before: 58.2,
        expertise_after: 64.7,
        delta: 6.5,
        eval_delta: 6.5,
        pending_candidates: 0,
      });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/evolve/cycle`,
      method: 'POST',
      terminalEvent: 'done',
      signal,
      handlers: {
        stage: (d) =>
          handlers.onStage?.(
            d as { stage: string; status: string; [key: string]: unknown },
          ),
        result: (d) =>
          handlers.onResult?.(
            d as {
              promoted_count?: number;
              demoted_count?: number;
              [key: string]: unknown;
            },
          ),
        error: (d) =>
          handlers.onError?.(d as { code?: string; message: string }),
        done: (d) => handlers.onDone?.(d),
      },
    });
  },
};
