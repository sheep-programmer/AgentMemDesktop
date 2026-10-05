/** Unified JSON transport. Read requests are bounded; model operations can set their own timeout. */
export interface ApiErrorDetail {
  code: string;
  message: string;
  detail?: Record<string, unknown>;
}

export class ApiError extends Error {
  code: string;
  status: number;
  detail?: Record<string, unknown>;
  requestId?: string;

  constructor(status: number, error: ApiErrorDetail, requestId?: string) {
    super(error.message || `请求失败（${status}）`);
    this.name = 'ApiError';
    this.code = error.code || 'UNKNOWN_ERROR';
    this.status = status;
    this.detail = error.detail;
    this.requestId = requestId;
  }
}

export interface ApiRequestOptions extends RequestInit {
  /** 0 disables the timeout. Writes have no default timeout because model calls can take minutes. */
  timeoutMs?: number;
}

function parseError(value: unknown, fallback: ApiErrorDetail): ApiErrorDetail {
  if (!value || typeof value !== 'object' || !('error' in value))
    return fallback;
  const error = value.error;
  if (!error || typeof error !== 'object') return fallback;
  return {
    code:
      'code' in error && typeof error.code === 'string'
        ? error.code
        : fallback.code,
    message:
      'message' in error && typeof error.message === 'string'
        ? error.message
        : fallback.message,
    detail:
      'detail' in error &&
      error.detail &&
      typeof error.detail === 'object' &&
      !Array.isArray(error.detail)
        ? (error.detail as Record<string, unknown>)
        : undefined,
  };
}

export async function apiClient<T>(
  endpoint: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { timeoutMs, signal, ...init } = options;
  const url = endpoint.startsWith('http') ? endpoint : `/api/v1${endpoint}`;
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (
    init.body != null &&
    !(init.body instanceof FormData) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json');
  }

  const controller = new AbortController();
  const forwardAbort = () => controller.abort(signal?.reason);
  if (signal?.aborted) forwardAbort();
  else signal?.addEventListener('abort', forwardAbort, { once: true });
  const timeout =
    timeoutMs ??
    (['GET', 'HEAD'].includes((init.method || 'GET').toUpperCase())
      ? 30_000
      : 0);
  let timedOut = false;
  const timer =
    timeout > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeout)
      : undefined;
  let requestId: string | undefined;

  try {
    const response = await fetch(url, {
      ...init,
      headers,
      signal: controller.signal,
    });
    requestId = response.headers.get('X-Request-ID') || undefined;
    if (!response.ok) {
      const fallback = {
        code: `HTTP_${response.status}`,
        message: response.statusText || `请求失败（${response.status}）`,
      };
      let error = fallback;
      try {
        error = parseError(await response.json(), fallback);
      } catch {
        /* HTML gateway errors retain the HTTP status. */
      }
      if (timedOut)
        throw new ApiError(
          408,
          {
            code: 'REQUEST_TIMEOUT',
            message: '请求超时，请检查服务状态后重试。',
          },
          requestId,
        );
      throw new ApiError(response.status, error, requestId);
    }
    if (response.status === 204) return {} as T;
    return (await response.json()) as T;
  } catch (error) {
    if (signal?.aborted) throw signal.reason ?? error;
    if (error instanceof ApiError) throw error;
    if (timedOut)
      throw new ApiError(
        408,
        {
          code: 'REQUEST_TIMEOUT',
          message: '请求超时，请检查服务状态后重试。',
        },
        requestId,
      );
    if (error instanceof SyntaxError)
      throw new ApiError(
        502,
        {
          code: 'INVALID_RESPONSE',
          message: '服务返回的数据无法读取，请刷新后重试。',
        },
        requestId,
      );
    throw new ApiError(
      0,
      {
        code: 'NETWORK_ERROR',
        message: '无法连接服务，请确认本地服务正在运行。',
      },
      requestId,
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
  }
}

export function isMockMode(): boolean {
  return (
    import.meta.env.VITE_USE_MOCK === '1' ||
    import.meta.env.VITE_USE_MOCK === 'true'
  );
}
