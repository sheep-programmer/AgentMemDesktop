import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Space } from '@/lib/api/types.temp';

vi.mock('../lib/api/services/spaces', () => ({
  spaceService: { getSpaces: vi.fn() },
}));

const space = (id: string): Space => ({
  id,
  name: id,
  domain: '测试',
  doc_count: 0,
  insight_count: 0,
  created_at: 1,
  updated_at: 1,
});

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  vi.stubGlobal('localStorage', {
    getItem: vi.fn().mockReturnValue('space-b'),
    setItem: vi.fn(),
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('space loading', () => {
  it('shares startup requests and restores the last valid space', async () => {
    const { spaceService } = await import('../lib/api/services/spaces');
    const { useSpaceStore } = await import('./useSpaceStore');
    vi.mocked(spaceService.getSpaces).mockResolvedValue([
      space('space-a'),
      space('space-b'),
    ]);
    const first = useSpaceStore.getState().loadSpaces();
    const second = useSpaceStore.getState().loadSpaces();
    expect(first).toBe(second);
    await first;
    expect(spaceService.getSpaces).toHaveBeenCalledTimes(1);
    expect(useSpaceStore.getState()).toMatchObject({
      currentSpaceId: 'space-b',
      hasLoaded: true,
      loadError: false,
    });
  });

  it('replaces deleted spaces and clears stale selection for an empty list', async () => {
    const { spaceService } = await import('../lib/api/services/spaces');
    const { useSpaceStore } = await import('./useSpaceStore');
    vi.mocked(spaceService.getSpaces)
      .mockResolvedValueOnce([space('space-a')])
      .mockResolvedValueOnce([]);
    await useSpaceStore.getState().loadSpaces();
    expect(useSpaceStore.getState().currentSpaceId).toBe('space-a');
    await useSpaceStore.getState().loadSpaces();
    expect(useSpaceStore.getState().currentSpaceId).toBe('');
  });

  it('preserves existing data on a failed refresh, then allows retry', async () => {
    const { spaceService } = await import('../lib/api/services/spaces');
    const { useSpaceStore } = await import('./useSpaceStore');
    vi.spyOn(console, 'error').mockImplementation(() => {});
    useSpaceStore.setState({ spaces: [space('space-b')] });
    vi.mocked(spaceService.getSpaces)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce([space('space-b')]);
    await useSpaceStore.getState().loadSpaces();
    expect(useSpaceStore.getState()).toMatchObject({
      loadError: true,
      isLoading: false,
      spaces: [space('space-b')],
    });
    await useSpaceStore.getState().loadSpaces();
    expect(useSpaceStore.getState().loadError).toBe(false);
  });

  it('works when browser storage is unavailable', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    });
    const { spaceService } = await import('../lib/api/services/spaces');
    const { useSpaceStore } = await import('./useSpaceStore');
    vi.mocked(spaceService.getSpaces).mockResolvedValue([space('space-a')]);
    await useSpaceStore.getState().loadSpaces();
    expect(useSpaceStore.getState().currentSpaceId).toBe('space-a');
    expect(() =>
      useSpaceStore.getState().setCurrentSpaceId('space-a'),
    ).not.toThrow();
  });
});
