import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatMessageItem } from './ChatMessageItem';
import { chatService } from '@/lib/api/services/chat';
import { toast } from 'sonner';

vi.mock('@/components/shared/MarkdownView', () => ({
  MarkdownView: ({ children }: { children: string }) => <p>{children}</p>,
}));
vi.mock('@/lib/api/services/chat', () => ({
  chatService: { sendFeedback: vi.fn().mockResolvedValue({}) },
}));
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
const message = {
  id: 'm',
  conversation_id: 'c',
  role: 'assistant' as const,
  content: '回答正文',
  trace_id: 't',
  created_at: 1,
};
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

it('keeps actions named and available without hovering', () => {
  const regenerate = vi.fn();
  render(<ChatMessageItem message={message} onRegenerate={regenerate} />);
  expect(screen.getByRole('group', { name: '回答操作' })).toBeTruthy();
  for (const name of ['复制', '再答一次', '有帮助', '不满意', '纠错']) {
    expect(screen.getByRole('button', { name })).toBeTruthy();
  }
  fireEvent.click(screen.getByRole('button', { name: '再答一次' }));
  expect(regenerate).toHaveBeenCalledWith('m');
});

it('reports clipboard failure instead of claiming that content was copied', async () => {
  vi.stubGlobal('navigator', {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
  });
  render(<ChatMessageItem message={message} />);
  fireEvent.click(screen.getByRole('button', { name: '复制' }));
  await waitFor(() =>
    expect(toast.error).toHaveBeenCalledWith(
      expect.stringContaining('手动复制'),
    ),
  );
  expect(toast.success).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '已复制' })).toBeNull();
});

it('records a correction as learning material without promising immediate activation', async () => {
  render(<ChatMessageItem message={message} />);
  fireEvent.click(screen.getByRole('button', { name: '纠错' }));
  fireEvent.change(
    await screen.findByRole('textbox', { name: '正确的事实或做法' }),
    { target: { value: '正确的做法需要先核验来源。' } },
  );
  const submit = screen.getByRole('button', { name: '保存纠错' });
  fireEvent.click(submit);
  await waitFor(() =>
    expect(chatService.sendFeedback).toHaveBeenCalledWith('t', {
      kind: 'correction',
      comment: '正确的做法需要先核验来源。',
    }),
  );
  expect(toast.success).toHaveBeenCalledWith(
    expect.stringContaining('待学习素材'),
  );
});
