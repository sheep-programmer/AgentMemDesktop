import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/lib/api/client';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import { mockSpaces } from '@/lib/api/mock/data';
import { CommandDialog } from './CommandDialog';

vi.mock('@/lib/api/client', async (original) => ({
  ...(await original<typeof import('@/lib/api/client')>()),
  apiClient: vi.fn(),
  isMockMode: () => false,
}));
beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLElement.prototype.scrollIntoView = vi.fn();
  useSpaceStore.setState({
    spaces: mockSpaces,
    currentSpaceId: mockSpaces[0].id,
  });
  useUiStore.setState({ isCommandOpen: true, activeReaderTarget: null });
  vi.mocked(apiClient).mockResolvedValue({ hits: [] });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('command search workflow', () => {
  it('has a command context and opens the exact document from semantic search', async () => {
    vi.mocked(apiClient).mockResolvedValue({
      hits: [
        {
          chunk_id: 'chunk-42',
          document_id: 'doc-42',
          document_title: '另一种表达的来源',
          snippet: '相关证据',
          score: 0.9,
        },
      ],
    });
    render(
      <MemoryRouter>
        <CommandDialog />
      </MemoryRouter>,
    );
    const dialog = await screen.findByRole('dialog', {
      name: '搜索知识或命令',
    });
    expect(dialog.querySelector('[data-slot="command"]')).toBeTruthy();
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: '不在标题中的语义问题' },
    });
    const match = await screen.findByRole('option', {
      name: /另一种表达的来源/,
    });
    expect(screen.queryByText('未找到匹配的指令或知识条目')).toBeNull();
    fireEvent.click(match);
    expect(useUiStore.getState().activeReaderTarget).toMatchObject({
      documentId: 'doc-42',
      chunkId: 'chunk-42',
      spaceId: mockSpaces[0].id,
    });
    expect(useUiStore.getState().isCommandOpen).toBe(false);
  });

  it('debounces the request and cancels obsolete searches', async () => {
    let signal!: AbortSignal;
    vi.mocked(apiClient).mockImplementation((_url, options) => {
      signal = options!.signal as AbortSignal;
      return new Promise(() => {});
    });
    render(
      <MemoryRouter>
        <CommandDialog />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByRole('combobox'), {
      target: { value: '旧问题' },
    });
    expect(apiClient).not.toHaveBeenCalled();
    await waitFor(() => expect(apiClient).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: '新问题' },
    });
    expect(signal.aborted).toBe(true);
    act(() => useUiStore.getState().setCommandOpen(false));
  });

  it('starts a new conversation from the command', async () => {
    function Route() {
      return <output>{useLocation().search}</output>;
    }
    render(
      <MemoryRouter>
        <CommandDialog />
        <Route />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole('option', { name: /新建对话/ }));
    expect(screen.getByText('?new=1')).toBeTruthy();
  });

  it('displays a transport failure separately from an empty result', async () => {
    vi.mocked(apiClient).mockRejectedValue(new Error('检索服务不可用'));
    render(
      <MemoryRouter>
        <CommandDialog />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByRole('combobox'), {
      target: { value: '遥远QA问题' },
    });
    expect(await screen.findByText('检索服务不可用')).toBeTruthy();
  });
});
