import { lazy, Suspense } from 'react';
import { useQuery } from '@tanstack/react-query';
import { memoryService } from '@/lib/api/services/memory';
import { FourStateView } from '@/components/shared/FourStateView';

const KnowledgeGraphView = lazy(() =>
  import('./KnowledgeGraphView').then((module) => ({
    default: module.KnowledgeGraphView,
  })),
);

export function MemoryGraphTab({ spaceId }: { spaceId: string }) {
  const graph = useQuery({
    queryKey: ['knowledge-graph', spaceId],
    queryFn: ({ signal }) =>
      memoryService.getGraph(spaceId, undefined, 2, 300, signal),
    staleTime: 30_000,
    retry: false,
    enabled: Boolean(spaceId),
  });
  const status = graph.isPending
    ? 'loading'
    : graph.isError && !graph.data
      ? 'error'
      : !graph.data?.nodes.length
        ? 'empty'
        : 'ready';
  return (
    <>
      {graph.isError && graph.data && (
        <p role="alert" className="mb-3 text-sm text-destructive">
          图谱刷新失败，当前显示上次成功加载的结果。
        </p>
      )}
      <FourStateView
        status={status}
        emptyTitle="知识图谱尚未构建"
        emptyDescription="资料中的实体与关联会在这里连接起来。"
        error="无法加载知识图谱，请检查服务状态。"
        onRetry={() => void graph.refetch()}
      >
        <Suspense
          fallback={
            <div
              role="status"
              className="flex h-80 items-center justify-center text-sm text-muted-foreground"
            >
              正在加载知识图谱…
            </div>
          }
        >
          {graph.data && <KnowledgeGraphView data={graph.data} />}
        </Suspense>
      </FourStateView>
    </>
  );
}
