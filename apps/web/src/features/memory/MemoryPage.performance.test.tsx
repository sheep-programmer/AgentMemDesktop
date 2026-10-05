import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { memoryService } from '@/lib/api/services/memory';
import { MemoryPage } from './MemoryPage';

vi.mock('@/lib/api/services/memory', () => ({
  memoryService: {
    getCards: vi.fn(),
    getInsights: vi.fn(),
    getConflicts: vi.fn(),
    getInsightsForReview: vi.fn(),
    getGraph: vi.fn(),
  },
}));
vi.mock('@/lib/api/services/spaces', () => ({
  spaceService: {
    getSystemCapabilities: vi
      .fn()
      .mockResolvedValue({ distill_available: true }),
  },
}));
vi.mock('@/stores/useSpaceStore', () => ({
  useSpaceStore: () => ({ currentSpaceId: 'space-test' }),
}));
vi.mock('./components/MemoryCardsTab', () => ({
  MemoryCardsTab: () => <p>卡片列表</p>,
}));
vi.mock('./components/MemoryInsightsTab', () => ({
  MemoryInsightsTab: () => <p>经验列表</p>,
}));
vi.mock('./components/KnowledgeCardDrawer', () => ({
  KnowledgeCardDrawer: () => null,
}));
vi.mock('./components/InsightDetailDrawer', () => ({
  InsightDetailDrawer: () => null,
}));

beforeEach(() => {
  vi.mocked(memoryService.getCards).mockResolvedValue({ items: [], total: 0 });
  vi.mocked(memoryService.getInsights).mockResolvedValue({
    items: [],
    total: 0,
  });
  vi.mocked(memoryService.getConflicts).mockResolvedValue([]);
  vi.mocked(memoryService.getInsightsForReview).mockResolvedValue({
    items: [],
    min_applied: 3,
    max_success_rate: 0.5,
  });
  vi.mocked(memoryService.getGraph).mockResolvedValue({ nodes: [], edges: [] });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('loads graph data only when the graph tab is opened and reuses fresh data', async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MemoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(memoryService.getCards).toHaveBeenCalled());
  expect(memoryService.getGraph).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('tab', { name: '知识图谱' }));
  expect(await screen.findByText('知识图谱尚未构建')).toBeTruthy();
  expect(memoryService.getGraph).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('tab', { name: /卡片/ }));
  fireEvent.click(screen.getByRole('tab', { name: '知识图谱' }));
  await screen.findByText('知识图谱尚未构建');
  expect(memoryService.getGraph).toHaveBeenCalledOnce();
  client.clear();
});
