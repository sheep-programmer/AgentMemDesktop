import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { memoryService } from '@/lib/api/services/memory';
import { GapFillPanel } from './GapFillPanel';

vi.mock('@/lib/api/services/memory', () => ({ memoryService: { createCard: vi.fn() } }));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const topic = { topic: '知识库管理', importance: 'core', subtopics: ['知识结构化'], covered: false, card_count: 0, mastery: 0 };

function mount(onFilled = vi.fn()) {
  return render(<MemoryRouter><GapFillPanel spaceId="s" topic={topic} count={0} mastery={0} suggestion="知识结构化" defaultOpen onFilled={onFilled} /></MemoryRouter>);
}

it('保存时附上所属主题，提交期间避免重复创建，成功后更新图谱', async () => {
  let finish!: () => void;
  vi.mocked(memoryService.createCard).mockImplementation(() => new Promise((resolve) => {
    finish = () => resolve({} as Awaited<ReturnType<typeof memoryService.createCard>>);
  }));
  const onFilled = vi.fn();
  mount(onFilled);
  fireEvent.change(screen.getByLabelText('卡片正文'), { target: { value: '将知识按主题与来源组织，保留溯源关系。' } });
  fireEvent.click(screen.getByRole('button', { name: '补上' }));
  fireEvent.keyDown(screen.getByLabelText('卡片正文'), { key: 'Enter', metaKey: true });
  expect(memoryService.createCard).toHaveBeenCalledTimes(1);
  expect(memoryService.createCard).toHaveBeenCalledWith('s', expect.objectContaining({
    title: '知识结构化', aliases: ['知识库管理'], kind: 'concept', confidence: 1, verified_by: 'user',
  }));
  expect(onFilled).not.toHaveBeenCalled();
  await act(async () => finish());
  expect(onFilled).toHaveBeenCalledWith('知识结构化');
});

it('保存失败保留输入内容，可以再次提交', async () => {
  vi.mocked(memoryService.createCard).mockRejectedValue(new Error('连接失败'));
  const onFilled = vi.fn();
  mount(onFilled);
  fireEvent.change(screen.getByLabelText('卡片正文'), { target: { value: '需要保留的草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '补上' }));
  await waitFor(() => expect((screen.getByRole('button', { name: '补上' }) as HTMLButtonElement).disabled).toBe(false));
  expect((screen.getByLabelText('卡片正文') as HTMLTextAreaElement).value).toBe('需要保留的草稿');
  expect(onFilled).not.toHaveBeenCalled();
});
