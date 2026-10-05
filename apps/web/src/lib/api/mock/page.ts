import type { PageResponse } from '../types.temp';

/** Demo lists follow the same pagination contract so browser QA exercises real list behavior. */
export function mockPage<T>(
  items: T[],
  limit: number,
  cursor: string | null,
): PageResponse<T> {
  const offset = cursor?.startsWith('mock:') ? Number(cursor.slice(5)) : 0;
  const start = Number.isSafeInteger(offset) && offset >= 0 ? offset : 0;
  const size = Math.max(1, Math.min(200, limit));
  const end = start + size;
  return {
    items: items.slice(start, end),
    total: items.length,
    next_cursor: end < items.length ? `mock:${end}` : null,
  };
}
