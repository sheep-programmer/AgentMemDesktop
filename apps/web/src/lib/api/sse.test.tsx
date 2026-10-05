import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchSSE, useSSE } from './sse';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  isMockMode: () => false,
}));
const response = (body: ReadableStream<Uint8Array>) =>
  new Response(body, { headers: { 'content-type': 'text/event-stream' } });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('stream transport', () => {
  it('requires an explicit result for task streams, while business errors are terminal', async () => {
    const makeStream = (text: string) =>
      response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(text));
            controller.close();
          },
        }),
      );
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(makeStream('event: stage\ndata: {}\n\n'))
        .mockResolvedValueOnce(
          makeStream('event: error\ndata: {"message":"失败"}\n\n'),
        ),
    );
    await expect(
      fetchSSE({ url: '/task', terminalEvent: 'done' }),
    ).rejects.toMatchObject({ code: 'INCOMPLETE_STREAM' });
    const error = vi.fn();
    await fetchSSE({
      url: '/task',
      terminalEvent: 'done',
      handlers: { error },
    });
    expect(error).toHaveBeenCalledWith({ message: '失败' });
  });
  it('decodes split UTF-8 characters and CRLF and unlocks its reader', async () => {
    const bytes = new TextEncoder().encode(
      'event: delta\r\ndata: {"text":"中文"}\r\n\r\n',
    );
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      },
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(stream)));
    const message = vi.fn();
    await fetchSSE({ url: '/test', onMessage: message });
    expect(message).toHaveBeenCalledWith('delta', { text: '中文' });
    expect(stream.locked).toBe(false);
  });

  it('cancels blocked reads on abort and reports closure once', async () => {
    const cancelled = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ cancel: cancelled });
    const controller = new AbortController();
    const open = vi.fn();
    const close = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(stream)));
    const request = fetchSSE({
      url: '/test',
      signal: controller.signal,
      onOpen: open,
      onClose: close,
    });
    await waitFor(() => expect(open).toHaveBeenCalledOnce());
    controller.abort();
    await request;
    expect(cancelled).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
    expect(stream.locked).toBe(false);
  });

  it('rejects non-stream responses and keeps diagnostic request IDs', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(Response.json({ wrong: true }))
        .mockResolvedValueOnce(
          Response.json(
            { error: { code: 'UNAVAILABLE', message: '离线' } },
            { status: 503, headers: { 'x-request-id': '42' } },
          ),
        ),
    );
    await expect(fetchSSE({ url: '/test' })).rejects.toMatchObject({
      code: 'INVALID_STREAM',
    });
    await expect(fetchSSE({ url: '/test' })).rejects.toMatchObject({
      requestId: '42',
      status: 503,
    });
  });
});

describe('subscription lifecycle', () => {
  beforeEach(() => vi.useFakeTimers());

  it('reconnects GET after a disconnect and stops retries on manual abort', async () => {
    const fetch = vi.fn().mockResolvedValue(
      response(
        new ReadableStream({
          start(controller) {
            controller.close();
          },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetch);
    const { result } = renderHook(() => useSSE({ url: '/events' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    act(() => result.current.abort());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('never repeats POST writes or permanent client errors', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        Response.json({ error: { message: 'Forbidden' } }, { status: 403 }),
      );
    vi.stubGlobal('fetch', fetch);
    const { unmount } = renderHook(() => useSSE({ url: '/events' }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    unmount();
    fetch.mockResolvedValue(
      response(
        new ReadableStream({
          start(controller) {
            controller.close();
          },
        }),
      ),
    );
    renderHook(() =>
      useSSE({ url: '/write', method: 'POST', body: { value: 1 } }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('does not reopen connections when only callbacks or header identities change', async () => {
    const fetch = vi.fn().mockResolvedValue(response(new ReadableStream()));
    vi.stubGlobal('fetch', fetch);
    const { rerender, unmount } = renderHook(
      ({ callback }: { callback: () => void }) =>
        useSSE({
          url: '/events',
          headers: { 'x-test': 'same' },
          onClose: callback,
        }),
      { initialProps: { callback: vi.fn() } },
    );
    await act(async () => {});
    rerender({ callback: vi.fn() });
    expect(fetch).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => {});
    expect(vi.getTimerCount()).toBe(0);
  });
});
