import React, { useState, useId } from 'react';
import type { ProcessStage } from '@/lib/api/types.temp';
import {
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  Loader2,
  CircleDot,
  Cpu,
  AlertCircle,
} from 'lucide-react';
import { buildProcessSummary, isChainNotStarted } from '../lib/processChain';
import { cn } from '@/lib/utils';

interface ProcessBarProps {
  stages: ProcessStage[];
  isStreaming: boolean;
  className?: string;
  meta?: {
    model?: string;
    tokens?: number;
    latencyMs?: number;
  };
}

export function ProcessBar({
  stages,
  isStreaming,
  className,
  meta,
}: ProcessBarProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const detailsId = useId();

  const getStageIcon = (status: ProcessStage['status']) => {
    switch (status) {
      case 'running':
        return (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-accent-ai shrink-0" />
        );
      case 'done':
        return (
          <CheckCircle2 className="h-3.5 w-3.5 text-accent-insight shrink-0" />
        );
      case 'failed':
        return (
          <AlertCircle className="h-3.5 w-3.5 text-destructive shrink-0" />
        );
      default:
        return (
          <CircleDot className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        );
    }
  };

  const notStarted = isChainNotStarted(stages);
  const recordedHistory =
    notStarted && Boolean(meta?.model || meta?.tokens || meta?.latencyMs);
  const summary = recordedHistory
    ? '查看这条回答的生成记录'
    : buildProcessSummary(stages, isStreaming);
  const hasFailure = stages.some((stage) => stage.status === 'failed');

  return (
    <div
      className={cn(
        'rounded-lg border border-border/80 bg-card/60 text-xs shadow-2xs backdrop-blur-xs transition-all select-none',
        className,
      )}
    >
      {/* 摘要行 / 折叠触发头 */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        aria-expanded={isExpanded}
        aria-controls={detailsId}
        aria-label="查看回答生成过程"
        className="flex min-h-10 w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary transition-colors"
      >
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              'flex h-2 w-2 rounded-full shrink-0 transition-colors',
              hasFailure
                ? 'bg-destructive'
                : isStreaming
                  ? 'bg-accent-ai animate-pulse'
                  : notStarted
                    ? 'bg-muted-foreground/50'
                    : stages.some((stage) => stage.warning)
                      ? 'bg-amber-500'
                      : 'bg-accent-insight',
            )}
          />
          <span
            className={cn(
              'line-clamp-2 text-left font-medium',
              notStarted ? 'text-muted-foreground' : 'text-foreground',
            )}
          >
            <span role="status" aria-live="polite">
              {summary}
            </span>
          </span>
        </div>

        <div className="flex items-center gap-2.5 shrink-0 text-[11px]">
          {/* 完成态指标胶囊 */}
          {!isStreaming &&
            !notStarted &&
            (meta?.model || meta?.tokens || meta?.latencyMs) && (
              <div className="hidden 2xl:flex items-center gap-1.5 rounded-full border border-border/70 bg-muted/40 px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
                <Cpu className="h-3 w-3 text-primary" />
                {meta?.model && <span>{meta.model}</span>}
                {meta?.model && meta?.tokens ? <span>·</span> : null}
                {meta?.tokens ? <span>{meta.tokens} 词元</span> : null}
                {meta?.tokens && meta?.latencyMs ? <span>·</span> : null}
                {meta?.latencyMs ? (
                  <span>{(meta.latencyMs / 1000).toFixed(1)}s</span>
                ) : null}
              </div>
            )}

          <div className="flex items-center gap-0.5 text-muted-foreground">
            <span>{isExpanded ? '收起' : '详情'}</span>
            {isExpanded ? (
              <ChevronUp className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </div>
        </div>
      </button>

      {/* 展开的阶段卡片 */}
      {isExpanded && (
        <div
          id={detailsId}
          className="border-t border-border/50 bg-muted/20 px-3 py-3 space-y-2"
        >
          {recordedHistory && (
            <p className="text-xs leading-relaxed text-muted-foreground">
              这是历史回答。打开“来源”，可以查看当时引用的资料和记录。
            </p>
          )}
          {!recordedHistory && (
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2">
              {stages.map((stage) => {
                const isActive = stage.status === 'running';
                return (
                  <div
                    key={stage.id}
                    className={cn(
                      'flex flex-col gap-1 rounded-md border p-2 text-xs transition-colors',
                      isActive
                        ? 'border-accent-ai/50 bg-accent-ai/10 shadow-xs'
                        : stage.status === 'done'
                          ? 'border-border/80 bg-card/80'
                          : stage.status === 'failed'
                            ? 'border-destructive/30 bg-destructive/5'
                            : 'border-border/60 bg-muted/30',
                    )}
                  >
                    <div className="flex items-center gap-1.5 font-medium">
                      {getStageIcon(stage.status)}
                      <span
                        className={
                          isActive ? 'text-accent-ai' : 'text-foreground'
                        }
                      >
                        {stage.label}
                      </span>
                    </div>
                    <span className="text-[11px] text-muted-foreground">
                      {
                        {
                          pending: '等待中',
                          running: '进行中',
                          done: '已完成',
                          failed: '已停止或失败',
                          skipped: '本次未使用',
                        }[stage.status]
                      }
                    </span>
                    {stage.detail && (
                      <p className="line-clamp-2 text-[11px] text-muted-foreground">
                        {stage.detail}
                      </p>
                    )}
                    {stage.warning && (
                      <p className="text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                        ⚠️ {stage.warning}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {!notStarted || recordedHistory ? (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
              {meta?.model && <span>模型：{meta.model}</span>}
              {meta?.tokens !== undefined && (
                <span>总用量：{meta.tokens.toLocaleString()} token</span>
              )}
              {meta?.latencyMs !== undefined && (
                <span>耗时：{(meta.latencyMs / 1000).toFixed(1)} 秒</span>
              )}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
