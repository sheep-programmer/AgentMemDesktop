import { useEffect, useId, useState } from 'react';
import {
  ChevronRight,
  Search,
  FileText,
  Loader2,
  AlertTriangle,
  Lightbulb,
  Cpu,
} from 'lucide-react';

import type { ProcessStage, TraceDetail } from '@/lib/api/types.temp';
import { chatService } from '@/lib/api/services/chat';
import { isMockMode } from '@/lib/api/client';
import { cn } from '@/lib/utils';
import { describeLocation, type CitationNumbering } from '../lib/citations';

type RetrievedChunk = NonNullable<TraceDetail['retrieved']>[number];

interface RetrievalBlockProps {
  /** 这条回答的引用编号（与正文角标一致）。 */
  numbering: CitationNumbering;
  traceId?: string | null;
  /** 正在生成的那条回答才有：实时阶段与流式轨迹。 */
  liveStages?: ProcessStage[];
  liveTrace?: TraceDetail | null;
  isStreaming?: boolean;
  /** 点某条命中：在证据栏定位。 */
  onLocate?: (chunkId: string) => void;
  /** 点「打开原文」：在阅读器里高亮这段。 */
  onOpen?: (chunk: { document_id?: string | null; document_title?: string | null; chunk_id: string; kind?: string | null }) => void;
}

/** 轨迹按 id 缓存：同一条回答反复展开不重复请求，切会话回来也不重拉。 */
const traceCache = new Map<string, TraceDetail>();

function chunkCited(chunk: RetrievedChunk, numbering: CitationNumbering): number | null {
  const direct = numbering.byChunk.get(chunk.chunk_id);
  if (direct !== undefined) return direct;
  for (const id of chunk.merged_from ?? []) {
    const n = numbering.byChunk.get(id);
    if (n !== undefined) return n;
  }
  return null;
}

function liveHeadline(stages: ProcessStage[]): { text: string; running: boolean } {
  const byId = Object.fromEntries(stages.map((stage) => [stage.id, stage]));
  if (byId.rewrite?.status === 'running') return { text: '正在理解问题…', running: true };
  if (byId.retrieval?.status === 'running' || byId.retrieval?.status === 'pending')
    return { text: '正在检索知识库…', running: true };
  if (byId.retrieval?.status === 'failed') return { text: '检索未完成', running: false };
  const count = byId.retrieval?.detail?.match(/(\d+)/)?.[1];
  if (byId.insights?.status === 'running')
    return { text: `找到 ${count ?? '若干'} 条相关片段，正在参考已有经验…`, running: true };
  return { text: count ? `检索了知识库 · 找到 ${count} 条相关片段` : '检索了知识库', running: false };
}

/**
 * 回答上方的「检索知识库」过程块。
 *
 * 回答是先查资料再写的，这一步此前只藏在顶部一条全局进度条里，和具体哪条回答对不上；
 * 历史回答更是只剩一句「查看生成记录」。现在每条回答自带一块：收起时一句话说清
 * 「查了什么、找到几条、引用了几条」，展开后逐条列出命中的片段与**原文位置**
 * （第几页 / 哪一节 / 第几段），并标出哪几条被回答引用了、编号与正文角标一致。
 */
export function RetrievalBlock({
  numbering,
  traceId,
  liveStages,
  liveTrace,
  isStreaming = false,
  onLocate,
  onOpen,
}: RetrievalBlockProps) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<TraceDetail | null>(() =>
    traceId ? (traceCache.get(traceId) ?? null) : null,
  );
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const detailsId = useId();

  const trace = liveTrace ?? loaded;

  useEffect(() => {
    if (!open || liveTrace || loaded || !traceId || isMockMode()) return;
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    chatService
      .getTrace(traceId)
      .then((detail) => {
        traceCache.set(traceId, detail);
        if (!cancelled) setLoaded(detail);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, liveTrace, loaded, traceId]);

  const citedCount = numbering.items.length;
  const docCount = new Set(numbering.items.map((item) => item.document_id)).size;
  const live = isStreaming && liveStages ? liveHeadline(liveStages) : null;
  const degraded = liveStages?.find((stage) => stage.warning)?.warning;

  // 历史回答既没有轨迹也没有引用时，说明它没有查资料（或检索被关掉了），不占位置
  if (!live && !traceId && citedCount === 0 && !liveTrace) return null;

  const headline = live
    ? live.text
    : citedCount > 0
      ? `检索了知识库 · 引用 ${citedCount} 处，来自 ${docCount} 篇资料`
      : '检索了知识库 · 没有引用资料';

  const retrieved = trace?.retrieved ?? [];
  const query = trace?.rewritten_query || trace?.query;
  const insights = (trace?.used_insights ?? []).filter(
    (item): item is Exclude<typeof item, string> => typeof item !== 'string',
  );
  const tokens =
    trace?.prompt_tokens && trace?.completion_tokens
      ? trace.prompt_tokens + trace.completion_tokens
      : null;

  // 引用了的排前面（按角标序号），没引用的按检索名次排在后面
  const rows = retrieved
    .map((chunk, rank) => ({ chunk, rank, n: chunkCited(chunk, numbering) }))
    .sort((a, b) => {
      if (a.n !== null && b.n !== null) return a.n - b.n;
      if (a.n !== null) return -1;
      if (b.n !== null) return 1;
      return a.rank - b.rank;
    });
  // 没有轨迹（老会话、mock）时退回到引用本身，至少能列出出处
  const fallbackRows = retrieved.length === 0 ? numbering.items : [];

  return (
    <div className="retrieval-block rounded-xl border border-border/80 bg-card/70 text-[12.5px]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={detailsId}
        className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left text-muted-foreground transition-colors hover:text-foreground"
      >
        <span
          className={cn(
            'flex h-5 w-5 shrink-0 items-center justify-center rounded-md',
            live?.running ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground',
          )}
        >
          {live?.running ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <Search className="h-3 w-3" />
          )}
        </span>
        <span role="status" aria-live="polite" className="min-w-0 flex-1 truncate">
          <span className={cn(live?.running ? 'shimmer-text font-medium' : 'text-foreground/85')}>
            {headline}
          </span>
        </span>
        {degraded && (
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-accent-warn" aria-label={degraded} />
        )}
        <ChevronRight
          className={cn('h-3.5 w-3.5 shrink-0 transition-transform duration-200', open && 'rotate-90')}
        />
      </button>

      {open && (
        <div id={detailsId} className="space-y-2.5 border-t border-border/60 px-3 pb-3 pt-2.5">
          {query && (
            <div className="flex items-start gap-2 text-[12px] text-muted-foreground">
              <span className="shrink-0">检索词</span>
              <span className="min-w-0 rounded-md bg-muted px-1.5 py-0.5 font-medium text-foreground">
                {query}
              </span>
            </div>
          )}
          {degraded && <p className="text-[12px] text-accent-warn">{degraded}</p>}

          {loading && (
            <div className="flex items-center gap-2 py-1 text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> 正在读取检索记录…
            </div>
          )}
          {failed && (
            <p className="text-muted-foreground">检索记录读取失败，下面只列出回答引用的出处。</p>
          )}

          <ul className="list-card-in space-y-1.5">
            {rows.map(({ chunk, rank, n }) => {
              // 被引用的命中用引用自己的定位：它已经收窄到回答依据的那一句所在的章节，
              // 切片级的章节只是切片开头所在的位置
              const cited = n !== null ? numbering.items.find((item) => item.n === n) : undefined;
              const location = describeLocation(cited ?? chunk);
              return (
                <li key={chunk.chunk_id}>
                  <div
                    className={cn(
                      'group flex items-start gap-2.5 rounded-lg border px-2.5 py-2 transition-colors',
                      n !== null
                        ? 'border-primary/25 bg-primary/[0.04] hover:border-primary/45'
                        : 'border-border/70 bg-background/60 hover:border-border',
                    )}
                  >
                    <span
                      className={cn(
                        'mt-px flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-[5px] px-1 font-mono text-[10.5px] font-semibold',
                        n !== null ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground',
                      )}
                      title={n !== null ? `回答中的引用 ${n}` : `检索第 ${rank + 1} 名，回答未引用`}
                    >
                      {n ?? '·'}
                    </span>
                    <button
                      type="button"
                      onClick={() => onLocate?.(chunk.chunk_id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="flex items-center gap-1.5">
                        <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
                        <span className="truncate font-medium text-foreground">
                          {chunk.document_title || '未命名资料'}
                        </span>
                      </span>
                      {location && (
                        <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                          {location}
                        </span>
                      )}
                    </button>
                    {onOpen && chunk.document_id && (
                      <button
                        type="button"
                        onClick={() => onOpen(cited ?? chunk)}
                        className="shrink-0 rounded-md px-1.5 py-0.5 text-[11.5px] text-muted-foreground opacity-80 transition hover:bg-muted hover:text-foreground group-hover:opacity-100"
                      >
                        原文
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
            {fallbackRows.map((item) => {
              const location = describeLocation(item);
              return (
                <li key={item.chunk_id}>
                  <div className="group flex items-start gap-2.5 rounded-lg border border-primary/25 bg-primary/[0.04] px-2.5 py-2">
                    <span className="mt-px flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-[5px] bg-primary px-1 font-mono text-[10.5px] font-semibold text-primary-foreground">
                      {item.n}
                    </span>
                    <button
                      type="button"
                      onClick={() => onLocate?.(item.chunk_id)}
                      className="min-w-0 flex-1 text-left"
                    >
                      <span className="block truncate font-medium text-foreground">
                        {item.document_title || '未命名资料'}
                      </span>
                      {location && (
                        <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                          {location}
                        </span>
                      )}
                    </button>
                    {onOpen && item.document_id && (
                      <button
                        type="button"
                        onClick={() => onOpen(item)}
                        className="shrink-0 rounded-md px-1.5 py-0.5 text-[11.5px] text-muted-foreground transition hover:bg-muted hover:text-foreground"
                      >
                        原文
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>

          {insights.length > 0 && (
            <div className="flex items-start gap-2 text-[12px] text-muted-foreground">
              <Lightbulb className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent-insight" />
              <span className="min-w-0">
                参考了 {insights.length} 条经验：
                <span className="text-foreground/85">
                  {insights
                    .slice(0, 3)
                    .map((item) => item.trigger)
                    .join('、')}
                  {insights.length > 3 ? ' 等' : ''}
                </span>
              </span>
            </div>
          )}

          {(trace?.model || tokens || trace?.latency_ms) && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/50 pt-2 font-mono text-[11px] text-muted-foreground">
              {trace?.model && (
                <span className="inline-flex items-center gap-1">
                  <Cpu className="h-3 w-3" />
                  {trace.model}
                </span>
              )}
              {tokens ? <span>{tokens.toLocaleString()} tokens</span> : null}
              {trace?.latency_ms ? <span>{(trace.latency_ms / 1000).toFixed(1)}s</span> : null}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
