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
  type EvalCompareHandlers,
} from '@/lib/api/services/expertise';
import { toast } from 'sonner';
import { EvalCompareDialog } from './EvalCompareDialog';

vi.mock('@/lib/api/services/expertise', () => ({
  expertiseService: { compareEvalsStream: vi.fn() },
}));
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}));

let handlers: EvalCompareHandlers;
let signal: AbortSignal;
let finish: () => void;
const result = { space_id: 'S', items: 1, baseline: '基准', deltas: [] };
const onClose = vi.fn();
const onComparisonComplete = vi.fn();
const props = {
  isOpen: true,
  spaceId: 'S',
  evalItemCount: 1,
  onClose,
  onComparisonComplete,
};

beforeEach(() => {
  vi.mocked(expertiseService.compareEvalsStream).mockImplementation(
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

async function start(count = 1) {
  fireEvent.click(await screen.findByRole('button', { name: /开始多臂对比/ }));
  await waitFor(() =>
    expect(expertiseService.compareEvalsStream).toHaveBeenCalledTimes(count),
  );
}

describe('comparison task lifecycle', () => {
  it('aborts on unmount and suppresses late completion', async () => {
    const { unmount } = render(<EvalCompareDialog {...props} />);
    await start();
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      handlers.onDone?.(result);
      finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(onComparisonComplete).not.toHaveBeenCalled();
  });

  it('recovers controls and reports EOF without a result', async () => {
    render(<EvalCompareDialog {...props} />);
    await start();
    await act(async () => finish());
    expect(screen.getByRole('alert').textContent).toContain('未收到完成结果');
    expect(screen.queryByText('对比结果')).toBeNull();
    expect(
      screen
        .getByRole('button', { name: /开始多臂对比/ })
        .hasAttribute('disabled'),
    ).toBe(false);
    expect(onComparisonComplete).not.toHaveBeenCalled();
  });

  it('ignores done after a terminal error', async () => {
    render(<EvalCompareDialog {...props} />);
    await start();
    await act(async () => {
      handlers.onError?.({ message: '服务中断' });
      handlers.onDone?.(result);
      finish();
    });
    expect(screen.getByRole('alert').textContent).toContain('服务中断');
    expect(toast.success).not.toHaveBeenCalled();
    expect(onComparisonComplete).not.toHaveBeenCalled();
  });

  it('can stop and restart while the old request is still settling', async () => {
    render(<EvalCompareDialog {...props} />);
    await start();
    const old = { handlers, signal, finish };
    fireEvent.click(screen.getByRole('button', { name: '停止对比' }));
    expect(old.signal.aborted).toBe(true);
    expect(screen.getByRole('status').textContent).toContain('请求已停止');
    await start(2);
    await act(async () => {
      old.handlers.onDone?.(result);
      old.finish();
    });
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '停止对比' })).toBeTruthy();
    expect(signal.aborted).toBe(false);
    await act(async () => {
      handlers.onDone?.(result);
      finish();
    });
    expect(await screen.findByText('对比结果')).toBeTruthy();
    expect(onComparisonComplete).toHaveBeenCalledOnce();
  });

  it('allows closing an active dialog and cancels its request', async () => {
    render(<EvalCompareDialog {...props} />);
    await start();
    fireEvent.click(screen.getByRole('button', { name: '关闭对话框' }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(signal.aborted).toBe(true);
    await act(async () => {
      handlers.onDone?.(result);
      finish();
    });
    expect(onComparisonComplete).not.toHaveBeenCalled();
  });

  it('invalidates a request when the dialog is hidden without unmounting', async () => {
    const { rerender } = render(<EvalCompareDialog {...props} />);
    await start();
    const old = { handlers, signal, finish };
    rerender(<EvalCompareDialog {...props} isOpen={false} />);
    expect(old.signal.aborted).toBe(true);
    rerender(<EvalCompareDialog {...props} />);
    await act(async () => {
      old.handlers.onDone?.(result);
      old.finish();
    });
    expect(onComparisonComplete).not.toHaveBeenCalled();
    expect(
      screen
        .getByRole('button', { name: /开始多臂对比/ })
        .hasAttribute('disabled'),
    ).toBe(false);
  });
});
