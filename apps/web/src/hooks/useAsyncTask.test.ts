import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useAsyncTask } from './useAsyncTask';
import { taskDelay } from '@/lib/taskDelay';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe('async task ownership', () => {
  it('prevents duplicate starts and ignores an old task after cancellation', () => {
    const { result } = renderHook(() => useAsyncTask('A'));
    const old = result.current.start()!;
    expect(result.current.start()).toBeNull();
    act(() => result.current.cancel());
    const next = result.current.start()!;
    expect(old.signal.aborted).toBe(true);
    expect(old.finish()).toBe(false);
    expect(next.current()).toBe(true);
    expect(next.finish()).toBe(true);
    expect(result.current.running()).toBe(false);
  });

  it('aborts on scope changes and unmount', () => {
    const { result, rerender, unmount } = renderHook(
      ({ scope }) => useAsyncTask(scope),
      { initialProps: { scope: 'A' } },
    );
    const first = result.current.start()!;
    rerender({ scope: 'B' });
    expect(first.current()).toBe(false);
    expect(first.signal.aborted).toBe(true);
    const next = result.current.start()!;
    unmount();
    expect(next.signal.aborted).toBe(true);
    expect(next.current()).toBe(false);
  });

  it('cancels demo delays without leaving timers', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const waiting = taskDelay(10_000, controller.signal);
    const assertion = expect(waiting).rejects.toMatchObject({
      name: 'AbortError',
    });
    controller.abort();
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });
});
