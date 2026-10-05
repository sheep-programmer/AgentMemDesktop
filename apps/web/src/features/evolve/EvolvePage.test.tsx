import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  evolveService,
  type EvolveCycleHandlers,
} from '@/lib/api/services/evolve';
import { EvolvePage } from './EvolvePage';

vi.mock('@/lib/api/services/evolve', () => ({
  evolveService: {
    getPending: vi.fn(),
    getHistory: vi.fn(),
    runCycle: vi.fn(),
  },
}));
vi.mock('@/lib/api/services/expertise', () => ({
  expertiseService: {
    getEvals: vi.fn().mockResolvedValue({ items: [], total: 1 }),
  },
}));
vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: () => ({ currentSpaceId: 'S' }),
}));
vi.mock('@/components/shared/ConfirmProvider', () => ({
  useConfirm: () => async () => true,
}));
vi.mock('./components/EvolvePipelineStages', () => ({
  EvolvePipelineStages: () => null,
}));
vi.mock('./components/EvolveSummaryCard', () => ({
  EvolveSummaryCard: () => <p>已完成汇总</p>,
}));
vi.mock('./components/EvolveHistoryTimeline', () => ({
  EvolveHistoryTimeline: () => null,
}));
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
}));
import { toast } from 'sonner';

let handlers: EvolveCycleHandlers;
let signal: AbortSignal;
let finish: () => void;
beforeEach(() => {
  vi.mocked(evolveService.getPending).mockResolvedValue({
    pending_count: 1,
    feedback_count: 1,
    correction_count: 0,
  });
  vi.mocked(evolveService.getHistory).mockResolvedValue([]);
  vi.mocked(evolveService.runCycle).mockImplementation(
    (_id, callbacks, abort) => {
      handlers = callbacks;
      signal = abort!;
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
  );
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('evolution task lifecycle', () => {
  it('reports a stream with no completion and restores the controls', async () => {
    render(
      <MemoryRouter>
        <EvolvePage />
      </MemoryRouter>,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: '开始一次进化' }),
    );
    await waitFor(() => expect(evolveService.runCycle).toHaveBeenCalledOnce());
    await act(async () => finish());
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByText('已完成汇总')).toBeNull();
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '开始一次进化' })).toBeTruthy();
  });

  it('aborts on navigation and ignores old success callbacks', async () => {
    const { unmount } = render(
      <MemoryRouter>
        <EvolvePage />
      </MemoryRouter>,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: '开始一次进化' }),
    );
    await waitFor(() => expect(evolveService.runCycle).toHaveBeenCalledOnce());
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      handlers.onDone?.({ produced: 1 });
      finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('does not interpret an error followed by a done event as success', async () => {
    render(
      <MemoryRouter>
        <EvolvePage />
      </MemoryRouter>,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: '开始一次进化' }),
    );
    await waitFor(() => expect(evolveService.runCycle).toHaveBeenCalledOnce());
    await act(async () => {
      handlers.onError?.({ message: '失败' });
      handlers.onDone?.({ produced: 1 });
      finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('失败');
  });

  it('allows a stopped cycle to restart without an old request clearing its progress', async () => {
    render(
      <MemoryRouter>
        <EvolvePage />
      </MemoryRouter>,
    );
    fireEvent.click(
      await screen.findByRole('button', { name: '开始一次进化' }),
    );
    await waitFor(() => expect(evolveService.runCycle).toHaveBeenCalledOnce());
    const old = { handlers, signal, finish };
    fireEvent.click(screen.getByRole('button', { name: '停止进化' }));
    expect(old.signal.aborted).toBe(true);
    expect(screen.getByRole('status').textContent).toContain('请求已停止');
    fireEvent.click(screen.getByRole('button', { name: '开始一次进化' }));
    await waitFor(() =>
      expect(evolveService.runCycle).toHaveBeenCalledTimes(2),
    );
    await act(async () => {
      old.handlers.onDone?.({ produced: 1 });
      old.finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '停止进化' })).toBeTruthy();
    expect(signal.aborted).toBe(false);
    await act(async () => {
      handlers.onDone?.({ produced: 2 });
      finish();
    });
    expect(await screen.findByText('已完成汇总')).toBeTruthy();
  });
});
