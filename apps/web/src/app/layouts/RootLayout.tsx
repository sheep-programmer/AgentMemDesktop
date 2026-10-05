import React, { useEffect, lazy, Suspense } from 'react';
import { Outlet, useNavigate, useParams } from 'react-router';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { Toaster } from 'sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import { useUiStore } from '@/stores/useUiStore';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useThemeStore } from '@/stores/useThemeStore';
import { ConfirmProvider } from '@/components/shared/ConfirmProvider';

const CommandDialog = lazy(() =>
  import('@/components/shared/CommandDialog').then((module) => ({
    default: module.CommandDialog,
  })),
);
const SpaceSwitcherDialog = lazy(() =>
  import('@/components/shared/SpaceSwitcherDialog').then((module) => ({
    default: module.SpaceSwitcherDialog,
  })),
);
const DocumentReaderModal = lazy(() =>
  import('@/features/library/components/DocumentReaderModal').then(
    (module) => ({ default: module.DocumentReaderModal }),
  ),
);

export function RootLayout() {
  const navigate = useNavigate();
  const { spaceId } = useParams<{ spaceId: string }>();
  const { currentSpaceId, setCurrentSpaceId, loadSpaces, spaces } =
    useSpaceStore();
  const {
    setCommandOpen,
    toggleEvidence,
    toggleSidebar,
    activeReaderTarget,
    closeReader,
    isCommandOpen,
    isSpaceSwitcherOpen,
  } = useUiStore();
  const { resolvedTheme } = useThemeStore();

  useEffect(() => {
    loadSpaces();
  }, [loadSpaces]);

  useEffect(() => {
    if (!spaceId || spaceId === currentSpaceId) return;
    // 只认后端真的返回过的 Space：URL 里的 id 可能来自旧书签、mock 数据或手输，
    // 照单全收会让内容区去查一个不存在的 Space，页面全部停在加载失败
    if (spaces.length > 0 && !spaces.some((space) => space.id === spaceId)) {
      navigate(`/s/${currentSpaceId || spaces[0].id}/chat`, { replace: true });
      return;
    }
    setCurrentSpaceId(spaceId);
  }, [spaceId, currentSpaceId, spaces, setCurrentSpaceId, navigate]);

  // 全局快捷键监听: ⌘K, ⌘N, ⌘U, ⌘B, ⌘\, ⌘1~5
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
      const mod = isMac ? e.metaKey : e.ctrlKey;

      if (!mod) {
        if (e.key === 'Escape') {
          // close modals or focused element
        }
        return;
      }

      switch (e.key.toLowerCase()) {
        case 'k':
          e.preventDefault();
          setCommandOpen(true);
          break;
        case 'n':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/chat?new=1`);
          break;
        case 'u':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/library?import=1`);
          break;
        case 'b':
          e.preventDefault();
          toggleSidebar();
          break;
        case '\\':
          e.preventDefault();
          toggleEvidence();
          break;
        case '1':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/chat`);
          break;
        case '2':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/library`);
          break;
        case '3':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/memory`);
          break;
        case '4':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/evolve`);
          break;
        case '5':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/expertise`);
          break;
        case '6':
          e.preventDefault();
          if (currentSpaceId) navigate(`/s/${currentSpaceId}/graph`);
          break;
        default:
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [currentSpaceId, navigate, setCommandOpen, toggleEvidence, toggleSidebar]);

  return (
    <TooltipProvider>
      <ConfirmProvider>
      <div
        className={`app-shell flex h-dvh w-full overflow-hidden bg-background text-foreground antialiased ${resolvedTheme}`}
      >
        <Sidebar />
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <TopBar />
          <a href="#main-content" className="skip-link">
            跳转到主要内容
          </a>
          <main
            id="main-content"
            tabIndex={-1}
            className="relative min-h-0 min-w-0 flex-1 overflow-hidden outline-none"
          >
            <Outlet />
          </main>
        </div>

        <Suspense
          fallback={
            <span role="status" className="sr-only">
              正在打开面板…
            </span>
          }
        >
          {isCommandOpen && <CommandDialog />}
          {isSpaceSwitcherOpen && <SpaceSwitcherDialog />}
        </Suspense>
        <Toaster
          position="top-right"
          theme={resolvedTheme}
          richColors
          closeButton
        />

        <Suspense
          fallback={
            <span role="status" className="sr-only">
              正在打开文档…
            </span>
          }
        >
          {activeReaderTarget && (
            <DocumentReaderModal
              document={{
                id: activeReaderTarget.documentId,
                title: activeReaderTarget.documentTitle || '文档详情',
                space_id: activeReaderTarget.spaceId || currentSpaceId,
              }}
              isOpen={Boolean(activeReaderTarget)}
              onClose={closeReader}
              initialChunkId={activeReaderTarget.chunkId || undefined}
              initialQuote={activeReaderTarget.quote ?? null}
            />
          )}
        </Suspense>
      </div>
      </ConfirmProvider>
    </TooltipProvider>
  );
}
