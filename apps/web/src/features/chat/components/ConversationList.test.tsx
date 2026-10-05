import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ConversationList } from './ConversationList';
import { chatService } from '@/lib/api/services/chat';

vi.mock('@/lib/api/services/chat', () => ({
  chatService: {
    getConversations: vi.fn(),
    createConversation: vi.fn(),
    updateConversation: vi.fn(),
    deleteConversation: vi.fn(),
  },
}));
vi.mock('@/components/shared/ConfirmProvider', () => ({
  useConfirm: () => vi.fn().mockResolvedValue(true),
}));
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));
const conversation = {
  id: 'c',
  space_id: 'S',
  title: '工程讨论',
  pinned: false,
  created_at: 1,
  updated_at: 1,
};
let client: QueryClient;
const onSelect = vi.fn();
function openPage() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConversationList spaceId="S" currentId="c" onSelect={onSelect} />
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.mocked(chatService.getConversations).mockResolvedValue({
    items: [conversation],
    total: 1,
  });
  vi.mocked(chatService.updateConversation).mockResolvedValue({
    ...conversation,
    pinned: true,
  });
});
afterEach(() => {
  cleanup();
  client?.clear();
  vi.clearAllMocks();
});

it('opens named actions without selecting a different conversation', async () => {
  openPage();
  fireEvent.click(
    await screen.findByRole('button', { name: '对话操作：工程讨论' }),
  );
  fireEvent.click(await screen.findByRole('menuitem', { name: '置顶对话' }));
  await waitFor(() =>
    expect(chatService.updateConversation).toHaveBeenCalledWith('c', {
      pinned: true,
    }),
  );
  expect(onSelect).not.toHaveBeenCalled();
});

it('renames through the menu and prevents Enter plus blur from saving twice', async () => {
  vi.mocked(chatService.updateConversation).mockResolvedValue({
    ...conversation,
    title: '新的讨论',
  });
  openPage();
  fireEvent.click(
    await screen.findByRole('button', { name: '对话操作：工程讨论' }),
  );
  fireEvent.click(await screen.findByRole('menuitem', { name: '重命名' }));
  const field = await screen.findByRole('textbox', { name: '会话标题' });
  fireEvent.change(field, { target: { value: '新的讨论' } });
  fireEvent.keyDown(field, { key: 'Enter' });
  fireEvent.blur(field);
  await waitFor(() =>
    expect(chatService.updateConversation).toHaveBeenCalledOnce(),
  );
  expect(chatService.updateConversation).toHaveBeenCalledWith('c', {
    title: '新的讨论',
  });
});

it('creates only one conversation while the first request is pending', async () => {
  let finish!: () => void;
  vi.mocked(chatService.createConversation).mockReturnValue(
    new Promise((resolve) => {
      finish = () => resolve({ ...conversation, id: 'new' });
    }),
  );
  openPage();
  await screen.findByRole('button', { name: '打开对话：工程讨论' });
  const button = screen.getByRole('button', { name: /新建对话/ });
  act(() => {
    fireEvent.click(button);
    fireEvent.click(button);
  });
  expect(chatService.createConversation).toHaveBeenCalledOnce();
  await act(async () => finish());
  expect(onSelect).toHaveBeenCalledWith('new');
});

it('hands focus to the rename field and preserves the title when editing is cancelled', async () => {
  openPage();
  fireEvent.click(
    await screen.findByRole('button', { name: '对话操作：工程讨论' }),
  );
  fireEvent.click(await screen.findByRole('menuitem', { name: '重命名' }));
  const field = await screen.findByRole('textbox', { name: '会话标题' });
  await waitFor(() => expect(document.activeElement).toBe(field));
  fireEvent.change(field, { target: { value: '未保存草稿' } });
  fireEvent.keyDown(field, { key: 'Escape' });
  fireEvent.blur(field);
  expect(
    await screen.findByRole('button', { name: '打开对话：工程讨论' }),
  ).toBeTruthy();
  expect(chatService.updateConversation).not.toHaveBeenCalled();
});
