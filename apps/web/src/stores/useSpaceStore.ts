import { create } from 'zustand';
import type { Space, SpaceCreate } from '../lib/api/types.temp';
import { spaceService } from '../lib/api/services/spaces';

const SPACE_KEY = 'agentmem.currentSpace';
function savedSpaceId(): string {
  try {
    return localStorage.getItem(SPACE_KEY) || '';
  } catch {
    return '';
  }
}
function rememberSpace(id: string) {
  try {
    localStorage.setItem(SPACE_KEY, id);
  } catch {
    /* 当前会话仍可使用。 */
  }
}
let pendingLoad: Promise<void> | null = null;

interface SpaceState {
  currentSpaceId: string;
  spaces: Space[];
  isLoading: boolean;
  hasLoaded: boolean;
  /** 上次拉取 Space 列表是否失败。
   *  失败与「一个 Space 都没有」在界面上长得一模一样——空列表会让人以为
   *  知识库真的空了并去新建一个，而实际只是没连上。必须能区分。 */
  loadError: boolean;
  loadSpaces: () => Promise<void>;
  setCurrentSpaceId: (id: string) => void;
  setSpaces: (spaces: Space[]) => void;
  createSpace: (data: SpaceCreate) => Promise<Space>;
  getCurrentSpace: () => Space | undefined;
}

export const useSpaceStore = create<SpaceState>((set, get) => ({
  currentSpaceId: savedSpaceId(),
  spaces: [],
  isLoading: false,
  hasLoaded: false,
  loadError: false,

  loadSpaces: () => {
    if (pendingLoad) return pendingLoad;
    set({ isLoading: true, loadError: false });
    pendingLoad = (async () => {
      try {
        const spaces = await spaceService.getSpaces();
        const current = get().currentSpaceId;
        const currentSpaceId = spaces.some((space) => space.id === current)
          ? current
          : spaces[0]?.id || '';
        rememberSpace(currentSpaceId);
        set({
          spaces,
          currentSpaceId,
          isLoading: false,
          loadError: false,
          hasLoaded: true,
        });
      } catch (error: unknown) {
        console.error('Failed to fetch spaces:', error);
        set({ isLoading: false, loadError: true, hasLoaded: true });
      } finally {
        pendingLoad = null;
      }
    })();
    return pendingLoad;
  },

  setCurrentSpaceId: (id: string) => {
    rememberSpace(id);
    set({ currentSpaceId: id });
  },
  setSpaces: (spaces: Space[]) => set({ spaces, hasLoaded: true }),

  createSpace: async (data: SpaceCreate) => {
    const created = await spaceService.createSpace(data);
    const existing = get().spaces;
    rememberSpace(created.id);
    set({
      spaces: [created, ...existing],
      currentSpaceId: created.id,
    });
    return created;
  },

  getCurrentSpace: () => {
    const { spaces, currentSpaceId } = get();
    if (spaces.length === 0) return undefined;
    return spaces.find((s) => s.id === currentSpaceId) || spaces[0];
  },
}));
