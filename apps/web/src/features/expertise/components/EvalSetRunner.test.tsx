import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  expertiseService,
  type EvalRunHandlers,
  type EvalGenerateHandlers,
} from '@/lib/api/services/expertise';
import { mockEvalItems } from '@/lib/api/mock/data';
import { EvalSetRunner } from './EvalSetRunner';
import { toast } from 'sonner';

vi.mock('@/lib/api/services/expertise', () => ({
  expertiseService: {
    getAllEvals: vi.fn(),
    getEvalRuns: vi.fn(),
    runEvalsStream: vi.fn(),
    generateEvalsStream: vi.fn(),
  },
}));
vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: () => ({ currentSpaceId: 'S' }),
}));
vi.mock('@/components/shared/ConfirmProvider', () => ({
  useConfirm: () => async () => true,
}));
vi.mock('./EvalSetItemRow', () => ({ EvalSetItemRow: () => <p>测验题</p> }));
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
}));
let handlers: EvalRunHandlers;
let signal: AbortSignal;
let finish: () => void;
beforeEach(() => {
  vi.mocked(expertiseService.getAllEvals).mockResolvedValue(
    mockEvalItems.slice(0, 1),
  );
  vi.mocked(expertiseService.getEvalRuns).mockResolvedValue([]);
  vi.mocked(expertiseService.runEvalsStream).mockImplementation(
    (_id, _options, callbacks, abort) => {
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

async function start() {
  const button = await screen.findByRole('button', { name: /运行评测/ });
  await waitFor(() => expect(button.hasAttribute('disabled')).toBe(false));
  fireEvent.click(button);
  await waitFor(() =>
    expect(expertiseService.runEvalsStream).toHaveBeenCalledOnce(),
  );
}

describe('evaluation lifecycle', () => {
  it('stops on unmount and ignores late completion', async () => {
    const { unmount } = render(<EvalSetRunner />);
    await start();
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      handlers.onDone?.({ run_id: 'old', score: 80, variant: 'baseline' });
      finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('restores controls when the connection ends without a result', async () => {
    render(<EvalSetRunner />);
    await start();
    await act(async () => finish());
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining('未收到完成结果'),
    );
    expect(screen.queryByRole('button', { name: '停止评测' })).toBeNull();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('manual stop cancels the request without publishing an old success', async () => {
    render(<EvalSetRunner />);
    await start();
    fireEvent.click(screen.getByRole('button', { name: '停止评测' }));
    expect(signal.aborted).toBe(true);
    await act(async () => {
      handlers.onDone?.({ run_id: 'old', score: 80, variant: 'baseline' });
      finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('aborts question generation on unmount and ignores late items', async () => {
    let generated!: EvalGenerateHandlers;
    let abort!: AbortSignal;
    let settled!: () => void;
    vi.mocked(expertiseService.generateEvalsStream).mockImplementation(
      (_id, _options, callbacks, signal) => {
        generated = callbacks;
        abort = signal!;
        return new Promise((resolve) => {
          settled = resolve;
        });
      },
    );
    const { unmount } = render(<EvalSetRunner />);
    fireEvent.click(await screen.findByRole('button', { name: 'AI 提炼考题' }));
    await waitFor(() =>
      expect(expertiseService.generateEvalsStream).toHaveBeenCalledOnce(),
    );
    unmount();
    expect(abort.aborted).toBe(true);
    await act(async () => {
      generated.onItem?.(mockEvalItems[0]);
      generated.onDone?.({ count: 1 });
      settled();
    });
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('recovers question generation from EOF without claiming completion', async () => {
    vi.mocked(expertiseService.generateEvalsStream).mockResolvedValue();
    render(<EvalSetRunner />);
    fireEvent.click(await screen.findByRole('button', { name: 'AI 提炼考题' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      '考题生成连接中断',
    );
    expect(screen.queryByRole('button', { name: '停止出题' })).toBeNull();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('stops generation and restores evaluation controls without late success', async () => {
    let generated!: EvalGenerateHandlers;
    let abort!: AbortSignal;
    let settled!: () => void;
    vi.mocked(expertiseService.generateEvalsStream).mockImplementation(
      (_id, _options, callbacks, signal) => {
        generated = callbacks;
        abort = signal!;
        return new Promise((resolve) => {
          settled = resolve;
        });
      },
    );
    render(<EvalSetRunner />);
    const run = await screen.findByRole('button', { name: /运行评测/ });
    await waitFor(() => expect(run.hasAttribute('disabled')).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'AI 提炼考题' }));
    expect(run.hasAttribute('disabled')).toBe(true);
    fireEvent.click(await screen.findByRole('button', { name: '停止出题' }));
    expect(abort.aborted).toBe(true);
    expect(screen.getByRole('status').textContent).toContain('请求已停止');
    expect(run.hasAttribute('disabled')).toBe(false);
    await act(async () => {
      generated.onItem?.(mockEvalItems[0]);
      generated.onDone?.({ count: 1 });
      settled();
    });
    expect(toast.success).not.toHaveBeenCalled();
  });
});
