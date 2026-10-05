import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { DataStorageSection } from './DataStorageSection';
import { spaceService } from '@/lib/api/services/spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';

vi.mock('@/lib/api/services/spaces', () => ({
  spaceService: {
    getSystemStats: vi.fn(),
    importSpace: vi.fn(),
    getSpaces: vi.fn(),
  },
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
const restored = {
  id: 'restored',
  name: '恢复的研发库',
  domain: '工程',
  created_at: 1,
  updated_at: 1,
  doc_count: 2,
  insight_count: 0,
};
const stats = {
  space_count: 1,
  document_count: 2,
  chunk_count: 3,
  insight_count: 4,
  disk_usage_bytes: 1024,
};
function Location() {
  return <output>{useLocation().pathname}</output>;
}
function openPage() {
  render(
    <MemoryRouter>
      <DataStorageSection />
      <Location />
    </MemoryRouter>,
  );
}
beforeEach(() => {
  useSpaceStore.setState({
    spaces: [],
    currentSpaceId: '',
    hasLoaded: true,
    loadError: false,
  });
  vi.mocked(spaceService.getSystemStats).mockResolvedValue(stats);
  vi.mocked(spaceService.getSpaces).mockResolvedValue([restored]);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('lets a failed storage statistic be refreshed successfully', async () => {
  vi.mocked(spaceService.getSystemStats)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue(stats);
  openPage();
  expect((await screen.findByRole('alert')).textContent).toContain('刷新');
  fireEvent.click(screen.getByRole('button', { name: '刷新存储统计' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(await screen.findByText('1.0 KB')).toBeTruthy();
});

it('refreshes the space list after restoring and offers a working next step', async () => {
  vi.mocked(spaceService.importSpace).mockResolvedValue({
    space_id: 'restored',
  });
  openPage();
  const file = new File(['zip'], 'backup.zip', { type: 'application/zip' });
  fireEvent.change(screen.getByLabelText('选择 AgentMem 备份文件'), {
    target: { files: [file] },
  });
  const enter = await screen.findByRole('button', { name: '进入知识空间' });
  expect(spaceService.getSpaces).toHaveBeenCalledOnce();
  expect(
    screen.getByRole('status', { name: '备份恢复结果' }).textContent,
  ).toContain('恢复的研发库');
  fireEvent.click(enter);
  expect(screen.getByText('/s/restored/library')).toBeTruthy();
  expect(useSpaceStore.getState().currentSpaceId).toBe('restored');
});

it('keeps a rejected restore visible and allows another attempt', async () => {
  vi.mocked(spaceService.importSpace)
    .mockRejectedValueOnce(new Error('知识空间已存在'))
    .mockResolvedValue({ space_id: 'restored' });
  openPage();
  const input = screen.getByLabelText('选择 AgentMem 备份文件');
  const file = new File(['zip'], 'backup.zip');
  fireEvent.change(input, { target: { files: [file] } });
  expect((await screen.findByRole('alert')).textContent).toContain('已存在');
  fireEvent.change(input, { target: { files: [file] } });
  expect(
    await screen.findByRole('button', { name: '进入知识空间' }),
  ).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});
