import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { chatService } from '@/lib/api/services/chat';
import { ChatPage } from './ChatPage';

const hooks = vi.hoisted(() => ({
  send: vi.fn(),
  reset: vi.fn(),
  history: vi.fn(),
}));
vi.mock('@/lib/api/services/chat', () => ({
  chatService: {
    createConversation: vi.fn(),
    updateConversation: vi.fn(),
    getConversations: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
}));
vi.mock('@/lib/api', () => ({
  documentService: {
    getDocuments: vi.fn().mockResolvedValue({ items: [], total: 0 }),
  },
}));
vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: () => ({
    currentSpaceId: 'S',
    getCurrentSpace: () => ({ id: 'S', name: '空间', doc_count: 0 }),
  }),
}));
vi.mock('@/hooks/useMediaQuery', () => ({ useMediaQuery: () => false }));
vi.mock('./hooks/useChatStream', () => ({
  useChatStream: () => ({
    messages: [],
    isStreaming: false,
    isHistoryLoading: false,
    processStages: [],
    activeTrace: null,
    sendMessage: hooks.send,
    resetConversation: hooks.reset,
    loadConversationHistory: hooks.history,
    abortStream: vi.fn(),
    focusTrace: vi.fn(),
  }),
}));
vi.mock('./components/ConversationList', () => ({
  ConversationList: () => null,
}));
vi.mock('./components/EvidenceSidebar', () => ({
  EvidenceSidebar: () => null,
}));
vi.mock('./components/ProcessBar', () => ({ ProcessBar: () => null }));
vi.mock('./components/ChatInputArea', () => ({
  ChatInputArea: ({ onSend }: { onSend: (text: string) => void }) => (
    <button
      onClick={() => {
        onSend('第一个问题');
        onSend('第二个问题');
      }}
    >
      连续提交
    </button>
  ),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('creates only one conversation while the first send is awaiting the server', async () => {
  const conversation = {
    id: 'created',
    space_id: 'S',
    title: '新对话',
    pinned: false,
    created_at: 1,
    updated_at: 1,
  };
  let finish!: (value: typeof conversation) => void;
  vi.mocked(chatService.createConversation).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  vi.mocked(chatService.updateConversation).mockResolvedValue(conversation);
  render(
    <MemoryRouter>
      <ChatPage />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole('button', { name: '连续提交' }));
  expect(chatService.createConversation).toHaveBeenCalledOnce();
  await act(async () => finish(conversation));
  await waitFor(() => expect(hooks.send).toHaveBeenCalledOnce());
  expect(hooks.send).toHaveBeenCalledWith('第一个问题', {
    conversationId: 'created',
    contextMode: 'standard',
  });
});
