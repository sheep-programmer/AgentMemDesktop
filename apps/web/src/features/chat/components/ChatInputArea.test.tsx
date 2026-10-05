import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatInputArea } from './ChatInputArea';

vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: (
    select: (state: { getCurrentSpace: () => { domain: string } }) => unknown,
  ) => select({ getCurrentSpace: () => ({ domain: '工程' }) }),
}));
vi.mock('@/lib/api/services/providers', () => ({
  providerService: {
    getRoleBindings: vi.fn().mockResolvedValue({ chat: 'p' }),
    getProviders: vi.fn().mockResolvedValue([{ id: 'p', kind: 'llm', model: '示例模型' }]),
  },
}));
afterEach(cleanup);

it('selects economy context and includes it when submitting a question', async () => {
  const onSend = vi.fn();
  function Input() {
    const [mode, setMode] = useState<'standard' | 'economy'>('standard');
    return (
      <ChatInputArea
        onSend={onSend}
        onAbort={() => {}}
        isStreaming={false}
        contextMode={mode}
        onContextModeChange={setMode}
      />
    );
  }
  render(
    <MemoryRouter>
      <Input />
    </MemoryRouter>,
  );
  await screen.findByText('示例模型');
  fireEvent.click(screen.getByRole('button', { name: '选择上下文策略' }));
  expect(
    (
      await screen.findByRole('menuitemradio', { name: /标准上下文/ })
    ).getAttribute('aria-checked'),
  ).toBe('true');
  fireEvent.click(screen.getByRole('menuitemradio', { name: /节省上下文/ }));
  expect(
    screen.getByRole('button', { name: '选择上下文策略' }).textContent,
  ).toContain('节省');
  expect(
    screen
      .getByRole('button', { name: '选择上下文策略' })
      .getAttribute('aria-expanded'),
  ).toBe('false');
  fireEvent.change(screen.getByRole('textbox', { name: '向知识库提问' }), {
    target: { value: '如何配置重试？' },
  });
  fireEvent.click(screen.getByRole('button', { name: '发送问题' }));
  expect(onSend).toHaveBeenCalledWith(
    '如何配置重试？',
    expect.objectContaining({ contextMode: 'economy', useRetrieval: true }),
  );
});
