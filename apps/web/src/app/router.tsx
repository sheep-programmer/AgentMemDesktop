import React, { Suspense, lazy } from 'react';
import { createBrowserRouter, Navigate, useParams } from 'react-router';
import { RootLayout } from './layouts/RootLayout';
import { SpaceEntry } from './SpaceEntry';
import { RouteRecovery } from '@/components/shared/RouteRecovery';
import { FourStateView } from '@/components/shared/FourStateView';
import { useSpaceStore } from '@/stores/useSpaceStore';

const ChatPage = lazy(() =>
  import('@/features/chat/ChatPage').then((m) => ({ default: m.ChatPage })),
);
const LibraryPage = lazy(() =>
  import('@/features/library/LibraryPage').then((m) => ({
    default: m.LibraryPage,
  })),
);
const MemoryPage = lazy(() =>
  import('@/features/memory/MemoryPage').then((m) => ({
    default: m.MemoryPage,
  })),
);
const EvolvePage = lazy(() =>
  import('@/features/evolve/EvolvePage').then((m) => ({
    default: m.EvolvePage,
  })),
);
const ExpertisePage = lazy(() =>
  import('@/features/expertise/ExpertisePage').then((m) => ({
    default: m.ExpertisePage,
  })),
);
const KnowledgeMapPage = lazy(() =>
  import('@/features/graph/KnowledgeMapPage').then((m) => ({
    default: m.KnowledgeMapPage,
  })),
);
const SettingsPage = lazy(() =>
  import('@/features/settings/SettingsPage').then((m) => ({
    default: m.SettingsPage,
  })),
);

function PageSuspense({ children }: { children: React.ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="flex h-full w-full items-center justify-center p-6 text-sm text-muted-foreground animate-pulse">
          加载中...
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

/**
 * 空间内的页面按空间重新挂载。
 *
 * `/s/A/chat → /s/B/chat` 只是路由参数变了，React 会复用同一个页面实例：选中的会话、
 * 消息、证据栏轨迹全都留着 A 的。实测切到 B 之后提问，请求发往 A 的会话，问题被
 * 写进了 A。以空间 id 作 key，换空间就是一个全新的页面，旧状态无处可留。
 */
function SpaceScoped({ children }: { children: React.ReactNode }) {
  const { spaceId } = useParams();
  const { spaces, currentSpaceId, hasLoaded, loadError, loadSpaces } =
    useSpaceStore();
  if (
    !hasLoaded ||
    (spaces.some((space) => space.id === spaceId) && currentSpaceId !== spaceId)
  ) {
    return (
      <div
        role="status"
        className="flex h-full items-center justify-center text-sm text-muted-foreground"
      >
        正在准备知识空间…
      </div>
    );
  }
  if (!spaces.some((space) => space.id === spaceId)) {
    if (loadError)
      return (
        <div className="workspace-content">
          <FourStateView
            status="error"
            error="暂时无法获取知识空间，请检查服务状态后重试。"
            onRetry={() => void loadSpaces()}
          >
            {null}
          </FourStateView>
        </div>
      );
    return <Navigate to="/" replace />;
  }
  return <PageSuspense key={spaceId}>{children}</PageSuspense>;
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <RootLayout />,
    errorElement: <RouteRecovery />,
    children: [
      {
        index: true,
        element: <SpaceEntry />,
      },
      {
        path: 's/:spaceId/chat',
        element: (
          <SpaceScoped>
            <ChatPage />
          </SpaceScoped>
        ),
      },
      {
        path: 's/:spaceId/library',
        element: (
          <SpaceScoped>
            <LibraryPage />
          </SpaceScoped>
        ),
      },
      {
        path: 's/:spaceId/memory',
        element: (
          <SpaceScoped>
            <MemoryPage />
          </SpaceScoped>
        ),
      },
      {
        path: 's/:spaceId/graph',
        element: (
          <SpaceScoped>
            <KnowledgeMapPage />
          </SpaceScoped>
        ),
      },
      {
        path: 's/:spaceId/evolve',
        element: (
          <SpaceScoped>
            <EvolvePage />
          </SpaceScoped>
        ),
      },
      {
        path: 's/:spaceId/expertise',
        element: (
          <SpaceScoped>
            <ExpertisePage />
          </SpaceScoped>
        ),
      },
      {
        path: 'settings',
        element: (
          <PageSuspense>
            <SettingsPage />
          </PageSuspense>
        ),
      },
      {
        path: '*',
        element: <SpaceEntry />,
      },
    ],
  },
]);
