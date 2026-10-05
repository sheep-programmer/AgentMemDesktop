import { useEffect, useMemo, useState } from 'react';
import {
  useInfiniteQuery,
  useQueryClient,
  type InfiniteData,
} from '@tanstack/react-query';
import type { PageResponse } from '@/lib/api/types.temp';

export interface ListOptions {
  limit: number;
  cursor: string | null;
  q?: string;
  signal: AbortSignal;
}

interface SearchOptions<T> {
  key: string;
  spaceId: string | null;
  search: string;
  fetchPage: (
    spaceId: string,
    options: ListOptions,
  ) => Promise<PageResponse<T>>;
}

export function usePaginatedSearch<T extends { id: string }>({
  key,
  spaceId,
  search,
  fetchPage,
}: SearchOptions<T>) {
  const client = useQueryClient();
  const [query, setQuery] = useState(search.trim());
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const queryKey = [key, spaceId, query];
  const result = useInfiniteQuery({
    queryKey,
    enabled: Boolean(spaceId),
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      spaceId
        ? fetchPage(spaceId, {
            limit: 50,
            cursor: pageParam,
            q: query || undefined,
            signal,
          })
        : Promise.resolve({ items: [], total: 0 }),
    getNextPageParam: (page) => page.next_cursor || undefined,
    retry: false,
    staleTime: 30_000,
  });
  const items = useMemo(() => {
    const unique = new Map<string, T>();
    for (const page of result.data?.pages || [])
      for (const item of page.items) unique.set(item.id, item);
    return [...unique.values()];
  }, [result.data]);
  const waitingForSearch = query !== search.trim();
  const status: 'empty' | 'loading' | 'error' | 'ready' = !spaceId
    ? 'empty'
    : waitingForSearch || result.isPending
      ? 'loading'
      : result.isError && !result.data
        ? 'error'
        : items.length === 0
          ? 'empty'
          : 'ready';

  function updateItem(item: T) {
    client.setQueriesData<InfiniteData<PageResponse<T>>>(
      { queryKey: [key, spaceId] },
      (data) =>
        data && {
          ...data,
          pages: data.pages.map((page) => ({
            ...page,
            items: page.items.map((current) =>
              current.id === item.id ? item : current,
            ),
          })),
        },
    );
  }

  async function removeItems(ids: string[]) {
    const removed = new Set(ids);
    await client.cancelQueries({ queryKey: [key, spaceId] });
    client.setQueriesData<InfiniteData<PageResponse<T>>>(
      { queryKey: [key, spaceId] },
      (data) => {
        if (!data) return data;
        const count = new Set(
          data.pages.flatMap((page) =>
            page.items
              .filter((item) => removed.has(item.id))
              .map((item) => item.id),
          ),
        ).size;
        return {
          ...data,
          pages: data.pages.map((page) => ({
            ...page,
            total: Math.max(0, page.total - count),
            items: page.items.filter((item) => !removed.has(item.id)),
          })),
        };
      },
    );
    await client.invalidateQueries({ queryKey: [key, spaceId] });
  }

  return {
    items,
    query,
    status,
    error: result.error,
    total: result.data?.pages[0]?.total ?? 0,
    isLoading: status === 'loading',
    isLoadingMore: result.isFetchingNextPage,
    hasMore: Boolean(result.hasNextPage),
    loadMore: () => {
      if (!waitingForSearch && !result.isFetching)
        return result.fetchNextPage();
    },
    refresh: async () => {
      const response = await result.refetch();
      return !response.isError;
    },
    updateItem,
    removeItems,
  };
}
