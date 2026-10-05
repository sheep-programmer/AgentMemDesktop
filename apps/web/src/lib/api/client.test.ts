import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiClient } from './client';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('JSON transport', () => {
  it('reads JSON without adding a content type to GET', async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetch);
    expect(await apiClient('/health')).toEqual({ ok: true });
    const options = fetch.mock.calls[0][1] as RequestInit;
    expect((options.headers as Headers).get('Content-Type')).toBeNull();
    expect((options.headers as Headers).get('Accept')).toBe('application/json');
  });

  it('preserves structured errors and the diagnostic request ID', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          Response.json(
            {
              error: {
                code: 'NOT_FOUND',
                message: '空间不存在',
                detail: { id: 'x' },
              },
            },
            { status: 404, headers: { 'X-Request-ID': 'request-42' } },
          ),
        ),
    );
    await expect(apiClient('/spaces/x')).rejects.toMatchObject({
      code: 'NOT_FOUND',
      status: 404,
      message: '空间不存在',
      requestId: 'request-42',
      detail: { id: 'x' },
    });
  });

  it('handles malformed error payloads and HTML gateway errors', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({ error: 'invalid' }, { status: 502 }),
      )
      .mockResolvedValueOnce(
        new Response('<html>Bad Gateway</html>', { status: 502 }),
      );
    vi.stubGlobal('fetch', fetch);
    for (let i = 0; i < 2; i++)
      await expect(apiClient('/health')).rejects.toMatchObject({
        status: 502,
        code: 'HTTP_502',
      });
  });

  it('classifies network failures and never automatically repeats a write', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    vi.stubGlobal('fetch', fetch);
    await expect(
      apiClient('/spaces', { method: 'POST', body: '{}' }),
    ).rejects.toMatchObject({ code: 'NETWORK_ERROR', status: 0 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('rejects invalid successful responses with a useful error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not JSON')));
    await expect(apiClient('/health')).rejects.toMatchObject({
      code: 'INVALID_RESPONSE',
      status: 502,
    });
  });

  it('supports uploads and empty success responses', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch);
    await apiClient('/documents/upload', {
      method: 'POST',
      body: new FormData(),
    });
    expect(
      (fetch.mock.calls[0][1].headers as Headers).has('Content-Type'),
    ).toBe(false);
  });

  it('bounds read requests and distinguishes user cancellation', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, options: RequestInit) =>
          new Promise((_resolve, reject) => {
            options.signal?.addEventListener('abort', () =>
              reject(options.signal?.reason),
            );
          }),
      ),
    );
    const request = apiClient('/health');
    const assertion = expect(request).rejects.toMatchObject({
      code: 'REQUEST_TIMEOUT',
      status: 408,
    });
    await vi.advanceTimersByTimeAsync(30_000);
    await assertion;
    expect(vi.getTimerCount()).toBe(0);

    const controller = new AbortController();
    const reason = new DOMException('User cancelled', 'AbortError');
    const cancelled = apiClient('/health', { signal: controller.signal });
    controller.abort(reason);
    await expect(cancelled).rejects.toBe(reason);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('exposes typed errors', () => {
    expect(
      new ApiError(500, { code: 'INTERNAL_ERROR', message: '失败' }),
    ).toBeInstanceOf(Error);
  });
});
