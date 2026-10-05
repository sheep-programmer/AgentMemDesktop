import { create } from 'zustand';

export interface ReaderTarget {
  documentId: string;
  documentTitle?: string;
  chunkId?: string | null;
  spaceId?: string;
  /** 引用依据的原句在全文里的区间，阅读器据此精确到句高亮。 */
  quote?: { start: number; end: number } | null;
}

const SIDEBAR_KEY = 'agentmem.sidebarExpanded';
const SCORES_KEY = 'agentmem.showRetrievalScores';

function readFlag(key: string, fallback = false): boolean {
  try {
    const saved = localStorage.getItem(key);
    return saved === null ? fallback : saved === '1';
  } catch {
    return fallback;
  }
}

function writeFlag(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? '1' : '0');
  } catch {
    // 隐私模式等写不进去时只影响下次打开的默认状态
  }
}

interface UiState {
  /** 侧边栏是否展开。点按钮或 ⌘B 切换，记在本地，刷新后保持。 */
  isSidebarExpanded: boolean;
  toggleSidebar: () => void;
  /** 证据卡片上是否显示各路检索分数（调参用，默认收起，记在本地） */
  showRetrievalScores: boolean;
  toggleRetrievalScores: () => void;
  isEvidenceOpen: boolean;
  toggleEvidence: () => void;
  setEvidenceOpen: (open: boolean) => void;
  activeEvidenceTab: 'chunks' | 'insights' | 'trace';
  setActiveEvidenceTab: (tab: 'chunks' | 'insights' | 'trace') => void;
  highlightedChunkId: string | null;
  setHighlightedChunkId: (id: string | null) => void;
  /** 第几次请求定位。同一条证据被连点两次时 id 不变，靠这个计数让证据栏重新滚过去。 */
  highlightRequestSeq: number;
  isCommandOpen: boolean;
  setCommandOpen: (open: boolean) => void;
  isSpaceSwitcherOpen: boolean;
  setSpaceSwitcherOpen: (open: boolean) => void;
  activeReaderTarget: ReaderTarget | null;
  openReader: (target: ReaderTarget) => void;
  closeReader: () => void;
  /** 对话页当前打开的会话；侧栏的最近对话列表据此高亮。 */
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
  /** 会话列表变了（新建、改名、删除、有新回答）就加一，侧栏据此重拉。 */
  conversationsVersion: number;
  bumpConversations: () => void;
  /** 侧栏里哪些分组展开着，记在本地。 */
  expandedNavGroups: string[];
  toggleNavGroup: (group: string) => void;
}

const NAV_GROUPS_KEY = 'agentmem.expandedNavGroups';

function readGroups(): string[] {
  try {
    const saved = localStorage.getItem(NAV_GROUPS_KEY);
    if (saved === null) return ['chat'];
    const parsed: unknown = JSON.parse(saved);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : ['chat'];
  } catch {
    return ['chat'];
  }
}

export const useUiStore = create<UiState>((set) => ({
  activeConversationId: null,
  setActiveConversationId: (id) => set({ activeConversationId: id }),
  conversationsVersion: 0,
  bumpConversations: () => set((state) => ({ conversationsVersion: state.conversationsVersion + 1 })),
  expandedNavGroups: readGroups(),
  toggleNavGroup: (group) =>
    set((state) => {
      const next = state.expandedNavGroups.includes(group)
        ? state.expandedNavGroups.filter((item) => item !== group)
        : [...state.expandedNavGroups, group];
      try {
        localStorage.setItem(NAV_GROUPS_KEY, JSON.stringify(next));
      } catch {
        // 写不进去只影响下次打开时的展开状态
      }
      return { expandedNavGroups: next };
    }),
  isSidebarExpanded: readFlag(SIDEBAR_KEY, true),
  toggleSidebar: () =>
    set((state) => {
      const next = !state.isSidebarExpanded;
      writeFlag(SIDEBAR_KEY, next);
      return { isSidebarExpanded: next };
    }),
  showRetrievalScores: readFlag(SCORES_KEY),
  toggleRetrievalScores: () =>
    set((state) => {
      const next = !state.showRetrievalScores;
      writeFlag(SCORES_KEY, next);
      return { showRetrievalScores: next };
    }),
  isEvidenceOpen: typeof window !== 'undefined' && window.innerWidth >= 1440,
  toggleEvidence: () => set((state) => ({ isEvidenceOpen: !state.isEvidenceOpen })),
  setEvidenceOpen: (open: boolean) => set({ isEvidenceOpen: open }),
  activeEvidenceTab: 'chunks',
  setActiveEvidenceTab: (tab) => set({ activeEvidenceTab: tab }),
  highlightedChunkId: null,
  highlightRequestSeq: 0,
  setHighlightedChunkId: (id) =>
    set((state) => ({ highlightedChunkId: id, highlightRequestSeq: state.highlightRequestSeq + 1 })),
  isCommandOpen: false,
  setCommandOpen: (open: boolean) => set({ isCommandOpen: open }),
  isSpaceSwitcherOpen: false,
  setSpaceSwitcherOpen: (open: boolean) => set({ isSpaceSwitcherOpen: open }),
  activeReaderTarget: null,
  openReader: (target) => set({ activeReaderTarget: target }),
  closeReader: () => set({ activeReaderTarget: null }),
}));
