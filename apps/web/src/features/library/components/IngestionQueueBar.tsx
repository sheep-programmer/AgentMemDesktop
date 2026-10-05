import React, { useEffect, useState } from 'react';
import type { DocumentItem } from '@/lib/api/types.temp';
import { useSSE } from '@/lib/api';
import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

interface IngestionQueueBarProps {
  spaceId: string;
  documents: DocumentItem[];
  onDocumentUpdated?: (docId: string, status: DocumentItem['status']) => void;
  onProgress?: (
    docId: string,
    progress: { stage: string; percent: number },
  ) => void;
  className?: string;
}

export function IngestionQueueBar({
  spaceId,
  documents,
  onDocumentUpdated,
  onProgress,
  className,
}: IngestionQueueBarProps) {
  const activeDocs = documents.filter(
    (d) => d.status !== 'ready' && d.status !== 'failed',
  );
  const hasActiveDocs = Boolean(spaceId && activeDocs.length > 0);
  // 排队深度由后端推送：文档多于并发上限时，一部分在等名额，光看 pending 分不出来
  const [queue, setQueue] = useState<{
    running: number;
    waiting: number;
  } | null>(null);

  const { subscribe } = useSSE({
    url: hasActiveDocs ? `/spaces/${spaceId}/ingest/stream` : null,
    enabled: hasActiveDocs,
  });

  useEffect(() => {
    if (!hasActiveDocs) return;

    const unsubQueue = subscribe<{ running: number; waiting: number }>(
      'queue',
      (data) => {
        if (
          typeof data?.running === 'number' &&
          typeof data?.waiting === 'number'
        ) {
          setQueue({ running: data.running, waiting: data.waiting });
        }
      },
    );

    // 事件里带着真实的阶段与百分比，早先只取了 status，卡片上那条进度环是写死的
    const unsubProgress = subscribe<{
      document_id: string;
      status?: DocumentItem['status'];
      stage?: string;
      percent?: number;
    }>('progress', (data) => {
      if (!data.document_id) return;
      if (data.status) {
        onDocumentUpdated?.(data.document_id, data.status);
      }
      if (typeof data.percent === 'number') {
        onProgress?.(data.document_id, {
          stage: data.stage || data.status || '',
          percent: data.percent,
        });
      }
    });

    // 状态迁移是独立事件：不订阅它的话，卡片上的状态徽章在整个摄取过程中都不会变
    const unsubStatus = subscribe<{
      document_id: string;
      status: DocumentItem['status'];
    }>('status', (data) => {
      if (data.document_id && data.status) {
        onDocumentUpdated?.(data.document_id, data.status);
      }
    });

    // 后端没有 document_done 事件：文档就绪走的是上面的 status（status=ready）

    const unsubError = subscribe<{ document_id: string }>('error', (data) => {
      if (data.document_id) {
        onDocumentUpdated?.(data.document_id, 'failed');
      }
    });

    return () => {
      unsubQueue();
      unsubProgress();
      unsubStatus();

      unsubError();
    };
  }, [hasActiveDocs, subscribe, onDocumentUpdated]);

  if (activeDocs.length === 0) return null;

  return (
    <div
      className={cn(
        'flex flex-wrap items-center justify-between gap-3 rounded-xl border border-accent-ai/30 bg-accent-ai/5 px-4 py-2.5 text-xs text-foreground select-none shadow-2xs',
        className,
      )}
    >
      <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin text-accent-ai" />
        <span className="font-medium">正在处理 {activeDocs.length} 份资料</span>
        {queue && queue.waiting > 0 && (
          <span className="rounded border border-border/80 bg-background/80 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {queue.running} 篇处理中 · {queue.waiting} 篇排队
          </span>
        )}
        <span className="text-muted-foreground hidden sm:inline">
          （解析版面 → 拆分段落 → 建立检索索引与知识关联）
        </span>
      </div>

      <div className="flex max-w-full flex-wrap items-center gap-2">
        {activeDocs.slice(0, 2).map((doc) => (
          <div
            key={doc.id}
            className="flex items-center gap-1.5 rounded border border-border/80 bg-background/80 px-2 py-0.5 text-[11px]"
          >
            <span className="max-w-[140px] truncate font-medium">
              {doc.title}
            </span>
            <span className="shrink-0 text-accent-ai">
              {(
                {
                  queued: '排队中',
                  parsing: '解析中',
                  chunking: '切分中',
                  embedding: '建立索引',
                  extracting: '提炼知识',
                } as Record<string, string>
              )[doc.status] || '处理中'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
