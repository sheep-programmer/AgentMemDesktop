import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { providerService } from '@/lib/api';
import type { UsageGroupItem } from '@/lib/api/types';

export type TimeRange = 'today' | '7d' | 'all';

function computeSinceTimestamp(range: TimeRange): number | null {
  if (range === 'today') {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return today.getTime();
  }
  if (range === '7d') {
    return Date.now() - 7 * 24 * 60 * 60 * 1000;
  }
  return null;
}

export function useContextCost() {
  const [timeRange, setTimeRange] = useState<TimeRange>('today');

  const since = useMemo(() => computeSinceTimestamp(timeRange), [timeRange]);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    // 只统计 LLM：embedding / rerank 没有前缀缓存，算进分母只会稀释命中率。
    // 实测 embedding 曾占到输入 token 的四成，把真实的 25% 压成 14%。
    queryKey: [
      'providers',
      'usage',
      { group_by: 'provider', kind: 'llm', timeRange, since },
    ],
    queryFn: () =>
      providerService.getUsage({ group_by: 'provider', since, kind: 'llm' }),
    staleTime: 30_000,
  });

  const items: UsageGroupItem[] = useMemo(() => data?.items ?? [], [data]);

  const metrics = useMemo(() => {
    let calls = 0;
    let promptTokens = 0;
    let completionTokens = 0;
    let cachedTokens = 0;
    let cacheWriteTokens = 0;

    for (const item of items) {
      calls += item.calls || 0;
      promptTokens += item.prompt_tokens || 0;
      completionTokens += item.completion_tokens || 0;
      cachedTokens += item.cached_tokens || 0;
      cacheWriteTokens += item.cache_write_tokens || 0;
    }

    // cached_tokens 已包含在 prompt_tokens 中，总 token = prompt + completion
    const totalTokens = promptTokens + completionTokens;
    const overallHitRate =
      promptTokens > 0 ? Math.min(cachedTokens / promptTokens, 1.0) : 0;
    return {
      calls,
      promptTokens,
      completionTokens,
      totalTokens,
      cachedTokens,
      cacheWriteTokens,
      overallHitRate,
    };
  }, [items]);

  const isEmpty =
    !isLoading &&
    !isError &&
    (items.length === 0 || metrics.promptTokens === 0);

  return {
    timeRange,
    setTimeRange,
    items,
    metrics,
    isLoading,
    isFetching,
    isError,
    error,
    isEmpty,
    refetch,
  };
}
