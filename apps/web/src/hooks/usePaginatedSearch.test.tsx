import type { ReactNode } from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usePaginatedSearch, type ListOptions } from './usePaginatedSearch';
import type { PageResponse } from '@/lib/api/types.temp';

type Item = { id: string; title: string };
const clients: QueryClient[] = [];
function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  clients.push(client);
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}
afterEach(() => {
  cleanup();
  for (const client of clients.splice(0)) client.clear();
});

describe('remote paginated search', () => {
  it('finds a match beyond the first 50 records', async () => {
    const all = Array.from({ length: 80 }, (_, index) => ({
      id: String(index),
      title: index === 79 ? '旧资料里的目标' : `资料 ${index}`,
    }));
    const fetchPage = vi.fn(
      async (
        _id: string,
        options: ListOptions,
      ): Promise<PageResponse<Item>> => {
        const matching = all.filter(
          (item) => !options.q || item.title.includes(options.q),
        );
        return {
          items: matching.slice(0, options.limit),
          total: matching.length,
          next_cursor: options.q ? null : '50',
        };
      },
    );
    const { result, rerender } = renderHook(
      ({ search }) =>
        usePaginatedSearch({
          key: 'documents',
          spaceId: 'A',
          search,
          fetchPage,
        }),
      { initialProps: { search: '' }, wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.items).toHaveLength(50));
    rerender({ search: '目标' });
    await waitFor(() => expect(result.current.items).toEqual([all[79]]));
    expect(result.current.total).toBe(1);
    expect(fetchPage.mock.calls.at(-1)?.[1]).toMatchObject({
      q: '目标',
      cursor: null,
    });
  });

  it('keeps the search term on later pages and deduplicates boundary records', async () => {
    const fetchPage = vi.fn(
      async (_id: string, options: ListOptions): Promise<PageResponse<Item>> =>
        options.cursor
          ? {
              items: [
                { id: '1', title: '目标一' },
                { id: '2', title: '目标二' },
              ],
              total: 2,
            }
          : {
              items: [{ id: '1', title: '目标一' }],
              total: 2,
              next_cursor: 'next',
            },
    );
    const { result } = renderHook(
      () =>
        usePaginatedSearch({
          key: 'documents',
          spaceId: 'A',
          search: '目标',
          fetchPage,
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(async () => {
      await result.current.loadMore();
    });
    await waitFor(() =>
      expect(result.current.items.map((item) => item.id)).toEqual(['1', '2']),
    );
    expect(fetchPage.mock.calls.at(-1)?.[1]).toMatchObject({
      q: '目标',
      cursor: 'next',
    });
  });

  it('cancels obsolete requests and ignores their late response', async () => {
    let resolveOld!: (page: PageResponse<Item>) => void;
    let oldSignal!: AbortSignal;
    const fetchPage = vi.fn(
      (_id: string, options: ListOptions): Promise<PageResponse<Item>> => {
        if (options.q === '新')
          return Promise.resolve({
            items: [{ id: 'new', title: '新' }],
            total: 1,
          });
        oldSignal = options.signal;
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      },
    );
    const { result, rerender } = renderHook(
      ({ search }) =>
        usePaginatedSearch({
          key: 'documents',
          spaceId: 'A',
          search,
          fetchPage,
        }),
      { initialProps: { search: '' }, wrapper: wrapper() },
    );
    await waitFor(() => expect(fetchPage).toHaveBeenCalledOnce());
    rerender({ search: '新' });
    await waitFor(() => expect(result.current.items[0]?.id).toBe('new'));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => {
      resolveOld({ items: [{ id: 'old', title: '旧' }], total: 1 });
    });
    expect(result.current.items[0].id).toBe('new');
  });

  it('distinguishes a failed request from an empty successful result', async () => {
    const fetchPage = vi
      .fn<(_id: string, options: ListOptions) => Promise<PageResponse<Item>>>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ items: [], total: 0 });
    const { result } = renderHook(
      () =>
        usePaginatedSearch({
          key: 'documents',
          spaceId: 'A',
          search: '',
          fetchPage,
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.status).toBe('error'));
    await act(async () => {
      expect(await result.current.refresh()).toBe(true);
    });
    await waitFor(() => expect(result.current.status).toBe('empty'));
  });

  it('retains the last successful data when refresh fails', async () => {
    const fetchPage = vi
      .fn<(_id: string, options: ListOptions) => Promise<PageResponse<Item>>>()
      .mockResolvedValueOnce({ items: [{ id: '1', title: '资料' }], total: 1 })
      .mockRejectedValueOnce(new Error('offline'));
    const { result } = renderHook(
      () =>
        usePaginatedSearch({
          key: 'documents',
          spaceId: 'A',
          search: '',
          fetchPage,
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      expect(await result.current.refresh()).toBe(false);
    });
    expect(result.current.items).toHaveLength(1);
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.status).toBe('ready');
  });
});
