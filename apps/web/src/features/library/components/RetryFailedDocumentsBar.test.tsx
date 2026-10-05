import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { documentService } from '@/lib/api';
import { mockDocuments } from '@/lib/api/mock/data';
import { toast } from 'sonner';
import { RetryFailedDocumentsBar } from './RetryFailedDocumentsBar';

vi.mock('@/lib/api', () => ({
  documentService: { retryFailedDocumentsStream: vi.fn() },
}));
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('retry outcome reporting', () => {
  it('counts repeated failure events once and reports partial success as a warning', async () => {
    const document = mockDocuments[0];
    vi.mocked(documentService.retryFailedDocumentsStream).mockImplementation(
      async (_id, handlers) => {
        const failure = { document_id: document.id, message: '解析失败' };
        handlers.onError?.(failure);
        handlers.onError?.(failure);
        handlers.onDone?.({ total: 2, retried: 1 });
      },
    );
    render(
      <RetryFailedDocumentsBar
        spaceId="S"
        failedDocuments={[document]}
        onRetryComplete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '全部重试' }));
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(
        '重试完成：成功 1 篇，仍失败 1 篇',
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('reports an all-failed retry as an error', async () => {
    const document = mockDocuments[0];
    vi.mocked(documentService.retryFailedDocumentsStream).mockImplementation(
      async (_id, handlers) => {
        handlers.onError?.({ document_id: document.id, message: '解析失败' });
        handlers.onDone?.({ total: 1, retried: 0 });
      },
    );
    render(
      <RetryFailedDocumentsBar
        spaceId="S"
        failedDocuments={[document]}
        onRetryComplete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '全部重试' }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        '重试完成：成功 0 篇，仍失败 1 篇',
      ),
    );
    expect(toast.success).not.toHaveBeenCalled();
  });
});
