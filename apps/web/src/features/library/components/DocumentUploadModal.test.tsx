import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DocumentUploadModal } from './DocumentUploadModal';
import { documentService } from '@/lib/api';
import { mockDocuments } from '@/lib/api/mock/data';

vi.mock('@/lib/api', () => ({
  documentService: {
    pasteText: vi.fn(),
    ingestUrl: vi.fn(),
    uploadFiles: vi.fn(),
  },
}));
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
const onClose = vi.fn();
const onUploaded = vi.fn();
const props = { spaceId: 'S', isOpen: true, onClose, onUploaded };
beforeEach(() => {
  vi.mocked(documentService.pasteText).mockResolvedValue(mockDocuments[0]);
  vi.mocked(documentService.ingestUrl).mockResolvedValue(mockDocuments[0]);
  vi.mocked(documentService.uploadFiles).mockResolvedValue({
    count: 1,
    documents: [mockDocuments[0]],
    rejected: [],
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('rejects a non-web link beside the field before making a request', async () => {
  render(<DocumentUploadModal {...props} />);
  fireEvent.click(screen.getByRole('tab', { name: '网页链接' }));
  const field = await screen.findByRole('textbox', { name: '网页地址' });
  fireEvent.change(field, { target: { value: 'ftp://example.com/a' } });
  fireEvent.click(screen.getByRole('button', { name: '导入网页' }));
  expect((await screen.findByRole('alert')).textContent).toContain('https://');
  expect(field.getAttribute('aria-invalid')).toBe('true');
  expect(documentService.ingestUrl).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

it('keeps a failed text submission editable and allows retry', async () => {
  vi.mocked(documentService.pasteText)
    .mockRejectedValueOnce(new Error('暂时离线'))
    .mockResolvedValue(mockDocuments[0]);
  render(<DocumentUploadModal {...props} />);
  fireEvent.click(screen.getByRole('tab', { name: '粘贴文本' }));
  const field = await screen.findByRole('textbox', { name: '文本正文' });
  fireEvent.change(field, { target: { value: '第一段。\n第二段。' } });
  fireEvent.click(screen.getByRole('button', { name: '导入文本' }));
  expect((await screen.findByRole('alert')).textContent).toContain('暂时离线');
  expect((field as HTMLTextAreaElement).value).toBe('第一段。\n第二段。');
  fireEvent.click(screen.getByRole('button', { name: '导入文本' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  expect(documentService.pasteText).toHaveBeenCalledTimes(2);
});

it('guards repeated submissions and ignores completion after the dialog is hidden', async () => {
  let finish!: () => void;
  vi.mocked(documentService.pasteText).mockReturnValue(
    new Promise((resolve) => {
      finish = () => resolve(mockDocuments[0]);
    }),
  );
  const { rerender } = render(<DocumentUploadModal {...props} />);
  fireEvent.click(screen.getByRole('tab', { name: '粘贴文本' }));
  fireEvent.change(await screen.findByRole('textbox', { name: '文本正文' }), {
    target: { value: '待提交文本。' },
  });
  const button = screen.getByRole('button', { name: '导入文本' });
  act(() => {
    fireEvent.click(button);
    fireEvent.click(button);
  });
  expect(documentService.pasteText).toHaveBeenCalledOnce();
  rerender(<DocumentUploadModal {...props} isOpen={false} />);
  await act(async () => finish());
  expect(onUploaded).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

it('keeps partial file rejections on the page without hiding accepted uploads', async () => {
  vi.mocked(documentService.uploadFiles).mockResolvedValue({
    count: 1,
    documents: [mockDocuments[0]],
    rejected: [
      {
        filename: 'duplicate.md',
        code: 'DUPLICATE_DOCUMENT',
        message: '资料已经存在',
      },
    ],
  });
  render(<DocumentUploadModal {...props} />);
  fireEvent.change(screen.getByLabelText('选择资料文件'), {
    target: {
      files: [new File(['a'], 'accepted.md'), new File(['b'], 'duplicate.md')],
    },
  });
  expect((await screen.findByRole('alert')).textContent).toContain(
    'duplicate.md',
  );
  expect(onUploaded).toHaveBeenCalledOnce();
  expect(onClose).not.toHaveBeenCalled();
});

it('prevents oversized files from being uploaded', async () => {
  render(<DocumentUploadModal {...props} />);
  const file = new File(['x'], 'large.pdf', { type: 'application/pdf' });
  Object.defineProperty(file, 'size', { value: 100 * 1024 * 1024 + 1 });
  fireEvent.change(screen.getByLabelText('选择资料文件'), {
    target: { files: [file] },
  });
  expect((await screen.findByRole('alert')).textContent).toContain('100 MB');
  expect(documentService.uploadFiles).not.toHaveBeenCalled();
});

it('submits a valid address and optional title through the labelled form', async () => {
  render(<DocumentUploadModal {...props} />);
  fireEvent.click(screen.getByRole('tab', { name: '网页链接' }));
  fireEvent.change(await screen.findByRole('textbox', { name: '网页地址' }), {
    target: { value: ' https://example.com/article ' },
  });
  fireEvent.change(screen.getByRole('textbox', { name: /资料标题/ }), {
    target: { value: '工程资料' },
  });
  fireEvent.click(screen.getByRole('button', { name: '导入网页' }));
  await waitFor(() =>
    expect(documentService.ingestUrl).toHaveBeenCalledWith(
      'S',
      'https://example.com/article',
      '工程资料',
    ),
  );
  expect(onUploaded).toHaveBeenCalledOnce();
  expect(onClose).toHaveBeenCalledOnce();
});
