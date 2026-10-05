/**
 * AgentMem · SSE 订阅 Hook 与请求工具
 * 支持 GET / POST 流式请求、主动中断 (AbortController)、事件分发与错误处理
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { ApiError, isMockMode, type ApiErrorDetail } from './client';
import { SSEParser } from './sseParser';

export interface SSEMessage<T = unknown> {
  event: string;
  data: T;
}

export type SSEEventHandler<T = unknown> = (data: T) => void;

export interface FetchSSEOptions {
  /** A task stream must end with its result or an explicit business error. */
  terminalEvent?: string;
  url: string;
  method?: 'GET' | 'POST';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  onOpen?: () => void;
  onMessage?: (event: string, data: unknown) => void;
  onError?: (err: unknown) => void;
  onClose?: () => void;
  handlers?: Record<string, SSEEventHandler<unknown>>;
}

/**
 * 命令式执行 SSE 请求 (支持 GET 与 POST)
 */
export async function fetchSSE(options: FetchSSEOptions): Promise<void> {
  const {
    url,
    method = 'GET',
    body,
    headers: customHeaders = {},
    signal,
    onOpen,
    onMessage,
    onError,
    onClose,
    handlers = {},
    terminalEvent,
  } = options;

  // mock 模式必须是自洽的：不连后端也能把六个页面跑起来，这是它存在的全部意义。
  // 这里此前直接 fetch，没有任何 mock 判断——于是演示模式下每个 SSE 驱动的界面
  // 都在偷偷打真网络。实测后果：`pnpm check:contrast`（跑在 mock 模式、且接在 CI 里）
  // 用 mock 的 Space id 向真后端发起了 8 次 `/spaces/space-drug-discovery/ingest/stream`。
  // 没有后端时这些请求会失败并污染演示；有后端时则污染真实数据源。
  if (isMockMode()) {
    onOpen?.();
    onClose?.();
    return;
  }

  const targetUrl = url.startsWith('http')
    ? url
    : url.startsWith('/api')
      ? url
      : `/api/v1${url}`;

  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
    ...customHeaders,
  };

  let requestBody: BodyInit | undefined;
  if (body !== undefined && body !== null) {
    if (typeof body === 'string' || body instanceof FormData) {
      requestBody = body;
    } else {
      headers['Content-Type'] = 'application/json';
      requestBody = JSON.stringify(body);
    }
  }

  try {
    const response = await fetch(targetUrl, {
      method,
      headers,
      body: requestBody,
      signal,
    });

    if (!response.ok) {
      let errorDetail: ApiErrorDetail = {
        code: `HTTP_${response.status}`,
        message:
          response.statusText ||
          `Request failed with status ${response.status}`,
      };
      try {
        const errJson = await response.json();
        if (errJson && errJson.error) {
          errorDetail = {
            code: errJson.error.code || errorDetail.code,
            message: errJson.error.message || errorDetail.message,
            detail: errJson.error.detail,
          };
        } else if (errJson && errJson.detail) {
          errorDetail.message =
            typeof errJson.detail === 'string'
              ? errJson.detail
              : JSON.stringify(errJson.detail);
        } else if (errJson && errJson.message) {
          errorDetail.message = errJson.message;
        }
      } catch {
        // ignore non-json error body
      }
      throw new ApiError(
        response.status,
        errorDetail,
        response.headers.get('X-Request-ID') || undefined,
      );
    }

    onOpen?.();

    if (!response.headers.get('content-type')?.includes('text/event-stream')) {
      await response.body?.cancel();
      throw new ApiError(502, {
        code: 'INVALID_STREAM',
        message: '服务未返回流式响应，请检查服务状态。',
      });
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error('无法读取流式响应');
    const abort = () => {
      void reader.cancel().catch(() => {});
    };
    signal?.addEventListener('abort', abort, { once: true });
    let terminalSeen = false;
    const parser = new SSEParser((event, value) => {
      if (signal?.aborted) return;
      if (event === terminalEvent || event === 'error') terminalSeen = true;
      onMessage?.(event, value);
      handlers[event]?.(value);
    });
    const decoder = new TextDecoder();
    try {
      if (signal?.aborted) return;
      while (!signal?.aborted) {
        const { done, value } = await reader.read();
        if (done) {
          parser.feed(decoder.decode());
          break;
        }
        parser.feed(decoder.decode(value, { stream: true }));
      }
      if (terminalEvent && !terminalSeen && !signal?.aborted) {
        throw new ApiError(502, {
          code: 'INCOMPLETE_STREAM',
          message: '连接已结束，未收到任务完成结果。请刷新记录后确认。',
        });
      }
    } finally {
      signal?.removeEventListener('abort', abort);
      // Handler errors, navigation and cancellations must release the stream reader.
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  } catch (err: unknown) {
    if (!signal?.aborted && (err as Error)?.name !== 'AbortError') {
      onError?.(err);
      throw err;
    }
  } finally {
    onClose?.();
  }
}

export interface UseSSEOptions {
  url: string | null;
  method?: 'GET' | 'POST';
  body?: unknown;
  headers?: Record<string, string>;
  onOpen?: () => void;
  onError?: (err: unknown) => void;
  onClose?: () => void;
  enabled?: boolean;
}

/**
 * 响应式 SSE 订阅 Hook (常驻连接，如任务进度流)
 */
export function useSSE(options: UseSSEOptions) {
  const { url, method = 'GET', body, headers, enabled = true } = options;
  const [isConnected, setIsConnected] = useState(false);
  const listenersRef = useRef<Map<string, Set<SSEEventHandler<unknown>>>>(
    new Map(),
  );
  const abortControllerRef = useRef<AbortController | null>(null);
  const callbacksRef = useRef(options);
  callbacksRef.current = options;
  // Fresh callback/header objects during progress renders should not reopen the transport.
  const bodyKey = JSON.stringify(body ?? null);
  const headersKey = JSON.stringify(
    Object.entries(headers || {}).sort(([a], [b]) => a.localeCompare(b)),
  );
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);

  const subscribe = useCallback(
    <T>(event: string, handler: SSEEventHandler<T>) => {
      const handlers =
        listenersRef.current.get(event) || new Set<SSEEventHandler<unknown>>();
      listenersRef.current.set(event, handlers);
      handlers.add(handler as SSEEventHandler<unknown>);
      return () => {
        handlers.delete(handler as SSEEventHandler<unknown>);
        if (!handlers.size) listenersRef.current.delete(event);
      };
    },
    [],
  );

  const abort = useCallback(() => {
    stoppedRef.current = true;
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
    abortControllerRef.current?.abort();
    setIsConnected(false);
  }, []);

  useEffect(() => {
    if (!url || !enabled) {
      setIsConnected(false);
      return;
    }
    stoppedRef.current = false;
    let disposed = false;
    let retries = 0;
    let attempt = 0;
    const connect = () => {
      const controller = new AbortController();
      abortControllerRef.current = controller;
      const currentAttempt = ++attempt;
      let status: number | undefined;
      const current = () => !disposed && currentAttempt === attempt;
      void fetchSSE({
        url,
        method,
        body: JSON.parse(bodyKey),
        headers: Object.fromEntries(
          JSON.parse(headersKey) as [string, string][],
        ),
        signal: controller.signal,
        onOpen: () => {
          if (current()) {
            setIsConnected(true);
            callbacksRef.current.onOpen?.();
          }
        },
        onMessage: (event, data) => {
          if (!current() || controller.signal.aborted) return;
          retries = 0;
          for (const handler of listenersRef.current.get(event) || [])
            handler(data);
        },
        onError: (error) => {
          if (!current()) return;
          status = error instanceof ApiError ? error.status : undefined;
          setIsConnected(false);
          callbacksRef.current.onError?.(error);
        },
        onClose: () => {
          if (current()) {
            setIsConnected(false);
            callbacksRef.current.onClose?.();
          }
        },
      })
        .catch(() => {
          /* onError has already reported the failure. */
        })
        .finally(() => {
          if (
            !current() ||
            stoppedRef.current ||
            controller.signal.aborted ||
            isMockMode()
          )
            return;
          // Subscribe-only GET streams reconnect; model/other POST operations are never replayed.
          if (
            method !== 'GET' ||
            (status !== undefined &&
              status >= 400 &&
              status < 500 &&
              status !== 429)
          )
            return;
          const delay = Math.min(30_000, 1000 * 2 ** Math.min(retries++, 5));
          reconnectTimerRef.current = setTimeout(connect, delay);
        });
    };
    connect();
    return () => {
      disposed = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      abortControllerRef.current?.abort();
    };
  }, [url, enabled, method, bodyKey, headersKey]);

  return { isConnected, subscribe, abort };
}
