import React from 'react';
import type { components } from '@/lib/api/types.gen';
import { markdownToPlainText } from '@/lib/markdown';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { StatusBadge } from '@/components/shared/StatusBadge';
import { TrendingUp, TrendingDown, Minus, LineChart, Archive, CheckCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

type Insight = components['schemas']['Insight'];
type InsightStatus = Insight['status'];

interface InsightCardItemProps {
  insight: Insight;
  onClick?: (insight: Insight) => void;
  onToggleStatus?: (id: string, currentStatus: string) => void;
}

export function InsightCardItem({ insight, onClick, onToggleStatus }: InsightCardItemProps) {
  const getBorderClass = (status: InsightStatus) => {
    switch (status) {
      case 'active':
        return 'border-accent-insight/40 hover:border-accent-insight bg-card';
      case 'candidate':
        return 'border-accent-ai/40 hover:border-accent-ai bg-card';
      case 'archived':
        return 'border-border/60 bg-muted/20 opacity-60 hover:opacity-100';
      default:
        return 'border-border bg-card';
    }
  };

  const delta =
    insight.eval_delta !== undefined && insight.eval_delta !== null
      ? +insight.eval_delta.toFixed(1)
      : undefined;

  const appliedCount = insight.applied_count ?? 0;
  const successCount = insight.success_count ?? 0;
  const isActive = insight.status === 'active';

  return (
    <div
      onClick={() => onClick?.(insight)}
      className={cn(
        'group flex h-full flex-col justify-between rounded-xl border p-4.5 text-xs transition-all duration-200 ease-out shadow-2xs cursor-pointer hover:-translate-y-0.5 hover:shadow-xs select-none',
        getBorderClass(insight.status),
      )}
    >
      <div>
        {/* 头部：状态徽章与置信度环 */}
        <div className="flex items-center justify-between gap-2">
          <StatusBadge status={insight.status} />
          <div className="flex items-center gap-2">
            {delta !== undefined && (
              <span
                className={cn(
                  'inline-flex items-center gap-0.5 font-mono text-[10px] font-bold px-2 py-0.5 rounded-md border tabular-nums',
                  delta > 0 && 'text-accent-insight bg-accent-insight/10 border-accent-insight/25',
                  delta < 0 && 'text-destructive bg-destructive/10 border-destructive/25',
                  delta === 0 && 'text-muted-foreground bg-muted/40 border-border',
                )}
              >
                {delta > 0 && <TrendingUp className="h-3 w-3 stroke-[2.5]" />}
                {delta < 0 && <TrendingDown className="h-3 w-3 stroke-[2.5]" />}
                {delta === 0 && <Minus className="h-3 w-3" />}
                {delta > 0 ? `+${delta}%` : `${delta}%`}
              </span>
            )}
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[11px] font-semibold text-foreground">
                {insight.confidence.toFixed(2)}
              </span>
              <ConfidenceRing value={insight.confidence} size={22} strokeWidth={2.5} />
            </div>
          </div>
        </div>

        {/* 核心内容: [场景] 与 [做法] */}
        <div className="mt-3.5 space-y-2.5">
          <div className="text-xs leading-snug">
            <span className="font-semibold text-foreground/70">[场景] </span>
            <span className="font-medium text-foreground">
              {markdownToPlainText(insight.trigger)}
            </span>
          </div>

          <div className="rounded-lg border border-border/60 bg-muted/35 p-3 leading-relaxed text-muted-foreground text-xs">
            <span className="font-semibold text-primary">[做法] </span>
            <span>{markdownToPlainText(insight.guidance)}</span>
          </div>

          {insight.rationale && (
            <div className="text-[11px] text-muted-foreground italic leading-relaxed">
              "{insight.rationale}"
            </div>
          )}
        </div>
      </div>

      {/* 底部应用计数与元数据 */}
      <div className="mt-4 border-t border-border/40 pt-2.5 space-y-2">
        <div className="flex items-center justify-between text-[10.5px] font-mono">
          <span className="text-muted-foreground">
            应用 <span className="font-semibold text-foreground">{appliedCount}</span> 次 / 成功{' '}
            <span className="font-semibold text-emerald-700 dark:text-emerald-400">{successCount}</span> 次
          </span>
          <span className="inline-flex items-center gap-1 text-[10px] text-primary group-hover:underline">
            <LineChart className="h-3 w-3" />
            查看置信度轨迹
          </span>
        </div>

        <div className="flex items-center justify-between text-[10.5px] text-muted-foreground font-mono">
          <span className="truncate max-w-[180px]">
            来源:{' '}
            {insight.origin === 'user_correction' || insight.origin === 'negative_feedback'
              ? '对话纠偏反思'
              : insight.origin === 'judge'
                ? '模型评测萃取'
                : '人工录入'}
          </span>

          {onToggleStatus && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggleStatus(insight.id, insight.status);
              }}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer border border-border/40"
              title={isActive ? '点击归档此经验' : '点击激活此经验'}
            >
              {isActive ? (
                <>
                  <Archive className="h-2.5 w-2.5" />
                  <span>归档</span>
                </>
              ) : (
                <>
                  <CheckCircle className="h-2.5 w-2.5 text-emerald-700 dark:text-emerald-400" />
                  <span>激活</span>
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
