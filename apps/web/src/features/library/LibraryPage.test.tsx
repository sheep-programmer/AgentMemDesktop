import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { documentService } from '@/lib/api';
import { mockDocuments } from '@/lib/api/mock/data';
import { toast } from 'sonner';
import { LibraryPage } from './LibraryPage';

vi.mock('@/lib/api', () => ({
  documentService: { getDocuments: vi.fn(), deleteDocument: vi.fn() },
}));
vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: () => ({ currentSpaceId: 'test-space' }),
}));
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
vi.mock('./components/DocumentReaderModal', () => ({
  DocumentReaderModal: () => null,
}));
vi.mock('./components/DocumentUploadModal', () => ({
  DocumentUploadModal: () => null,
}));
vi.mock('./components/IngestionQueueBar', () => ({
  IngestionQueueBar: () => null,
}));
vi.mock('./components/RetryFailedDocumentsBar', () => ({
  RetryFailedDocumentsBar: () => null,
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('document batch deletion', () => {
  it('prevents duplicate submission and retains documents that could not be deleted', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    let documents = mockDocuments
      .slice(0, 2)
      .map((document) => ({ ...document, space_id: 'test-space' }));
    const first = documents[0];
    const second = documents[1];
    vi.mocked(documentService.getDocuments).mockImplementation(async () => ({
      items: documents,
      total: documents.length,
    }));
    let finishDelete!: () => void;
    const pending = new Promise<void>((resolve) => {
      finishDelete = resolve;
    });
    vi.mocked(documentService.deleteDocument).mockImplementation(async (id) => {
      if (id === second.id) throw new Error('offline');
      await pending;
      documents = documents.filter((document) => document.id !== id);
      return { deleted: true };
    });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <LibraryPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByRole('checkbox', { name: `选择资料：${first.title}` });
    fireEvent.click(
      screen.getByRole('checkbox', { name: `选择资料：${first.title}` }),
    );
    fireEvent.click(
      screen.getByRole('checkbox', { name: `选择资料：${second.title}` }),
    );
    const button = screen.getByRole('button', { name: '删除' });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    await waitFor(() =>
      expect(documentService.deleteDocument).toHaveBeenCalledTimes(1),
    );
    expect(
      screen.getByRole('button', { name: '删除中…' }).getAttribute('disabled'),
    ).not.toBeNull();
    await act(async () => finishDelete());
    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith(
        expect.stringContaining('1 篇失败'),
      ),
    );
    expect(documentService.deleteDocument).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByRole('checkbox', { name: `选择资料：${first.title}` }),
    ).toBeNull();
    expect(
      screen.getByRole('checkbox', { name: `选择资料：${second.title}` }),
    ).toBeTruthy();
    expect(screen.getByText('已选 1 项')).toBeTruthy();
    expect(toast.success).not.toHaveBeenCalled();
    client.clear();
  });
});
