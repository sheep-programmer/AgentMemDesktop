import React, { useState } from 'react';
import type { Insight, InsightReviewItem } from '@/lib/api/types';
import { Button } from '@/components/ui/button';
import {
  AlertCircle,
  Archive,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Info,
  Loader2,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { InsightAttributionPanel } from './InsightAttributionPanel';

interface InsightReviewSectionProps {
  items: InsightReviewItem[];
  /** 当前 Space：留一法归因要用。 */
  spaceId: string;
  minApplied?: number;
  maxSuccessRate?: number;
  onInsightClick: (insight: Insight) => void;
  onArchiveInsight: (insightId: string) => Promise<void> | void;
}

/** 经验状态的中文说法。 */
const STATUS_LABELS: Record<string, string> = {
  active: '已生效',
  candidate: '候选',
  conflicted: '冲突',
  archived: '已归档',
};

export function InsightReviewSection({
  items,
  spaceId,
  minApplied = 5,
  maxSuccessRate = 0.4,
  onInsightClick,
  onArchiveInsight,
}: InsightReviewSectionProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [archivingId, setArchivingId] = useState<string | null>(null);

  if (!items || items.length === 0) {
    return null;
  }

  const handleArchive = async (e: React.MouseEvent, insightId: string) => {
    e.stopPropagation();
    setArchivingId(insightId);
    try {
      await onArchiveInsight(insightId);
    } finally {
      setArchivingId(null);
    }
  };

  return (
    <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 transition-all shadow-2xs">
      {/* 顶部入口条 */}
      <button
        type="button"
        onClick={() => setIsExpanded((prev) => !prev)}
        className="w-full flex items-center justify-between p-3.5 text-left text-xs hover:bg-amber-500/10 transition-colors rounded-xl cursor-pointer"
      >
        <div className="flex items-center gap-2.5">
          <div className="p-1 rounded-md bg-amber-500/15 text-amber-700 dark:text-amber-400">
            <AlertCircle className="h-4 w-4" />
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-foreground text-sm">
                值得复查 ({items.length})
              </span>
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                注入 ≥ {minApplied} 次且成功率 ≤ {Math.round(maxSuccessRate * 100)}%
              </span>
            </div>
            <p className="text-[11px] text-muted-foreground mt-0.5">
              基于运行统计的复查线索。点击{isExpanded ? '收起' : '展开'}查看需要关注的经验列表
            </p>
          </div>
        </div>

        <div className="flex items-center gap-1.5 text-muted-foreground">
          <span className="text-xs">{isExpanded ? '收起' : '展开'}</span>
          {isExpanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </div>
      </button>

      {/* 展开的复查列表 */}
      {isExpanded && (
        <div className="border-t border-amber-500/20 p-4 space-y-3">
          {/* 语义说明卡片 */}
          <div className="rounded-lg bg-background/80 border border-border/80 p-3 text-xs text-muted-foreground flex items-start gap-2.5">
            <Info className="h-4 w-4 text-amber-700 dark:text-amber-400 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <span className="font-medium text-foreground">
                统计线索，不是判决（非有害或低质量）
              </span>
              <p className="leading-relaxed text-[11px]">
                该经验满足「注入 ≥ {minApplied} 次且成功率 ≤ {Math.round(maxSuccessRate * 100)}%」阈值。
                每次问答的反馈评分均摊给当时检索注入的多条经验，归因本身是粗略的统计线索，用来提示人工看一眼，并不代表此经验已被证明有害或低质量。
              </p>
            </div>
          </div>

          {/* 从「相关性」到「因果」：同一批经验可以直接跑一次对照实验 */}
          <InsightAttributionPanel
            spaceId={spaceId}
            insightIds={items.map((item) => item.insight.id)}
          />

          {/* 复查条目卡片列表 */}
          <div className="grid grid-cols-1 gap-3">
            {items.map((item) => {
              const { insight } = item;
              const percent = Math.min(Math.max(item.success_rate * 100, 0), 100);
              const isArchiving = archivingId === insight.id;

              return (
                <div
                  key={insight.id}
                  className="rounded-lg border border-border bg-card p-4 space-y-2.5 transition-shadow hover:shadow-xs"
                >
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-border/40 pb-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-semibold text-foreground">
                        {insight.id}
                      </span>
                      <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground">
                        置信度：{(insight.confidence * 100).toFixed(0)}%
                      </span>
                      <span className="rounded px-1.5 py-0.5 text-[10px] font-medium bg-muted text-muted-foreground">
                        {STATUS_LABELS[insight.status] ?? insight.status}
                      </span>
                    </div>

                    <div className="flex items-center gap-3 text-xs text-muted-foreground">
                      <span>
                        注入 <strong className="text-foreground">{item.applied_count}</strong> 次
                      </span>
                      <span>·</span>
                      <span>
                        好评 <strong className="text-foreground">{item.success_count}</strong> 次
                      </span>
                    </div>
                  </div>

                  {/* 触发场景与指导原则 */}
                  <div className="space-y-1 text-xs">
                    <div className="text-foreground">
                      <span className="font-semibold text-muted-foreground mr-1.5">[触发场景]</span>
                      {insight.trigger}
                    </div>
                    <div className="text-muted-foreground line-clamp-2">
                      <span className="font-semibold text-muted-foreground mr-1.5">[指导原则]</span>
                      {insight.guidance}
                    </div>
                  </div>

                  {/* 成功率进度条与原因 */}
                  <div className="space-y-1.5 pt-1">
                    <div className="flex items-center justify-between text-[11px]">
                      <span className="text-muted-foreground">
                        好评率: <strong className="text-foreground">{percent.toFixed(1)}%</strong>
                      </span>
                      <span className="text-amber-700 dark:text-amber-400 font-medium">
                        {item.reason}
                      </span>
                    </div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                      <div
                        className={cn(
                          'h-full transition-all duration-300',
                          percent <= 20
                            ? 'bg-destructive'
                            : percent <= 40
                              ? 'bg-amber-500'
                              : 'bg-primary',
                        )}
                        style={{ width: `${percent}%` }}
                      />
                    </div>
                  </div>

                  {/* 操作栏 */}
                  <div className="flex items-center justify-end gap-2 pt-1 border-t border-border/30">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onInsightClick(insight)}
                      className="h-7 gap-1 text-xs"
                    >
                      <ExternalLink className="h-3 w-3" />
                      查看详情 / 置信度轨迹
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={(e) => handleArchive(e, insight.id)}
                      disabled={isArchiving || insight.status === 'archived'}
                      className="h-7 gap-1 text-xs text-destructive hover:bg-destructive/10"
                    >
                      {isArchiving ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <Archive className="h-3 w-3" />
                      )}
                      {insight.status === 'archived' ? '已归档' : '就地归档'}
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
