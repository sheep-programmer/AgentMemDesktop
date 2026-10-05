import React, { useState, useEffect, useCallback } from 'react';
import type { Insight, InsightEvent } from '@/lib/api/types';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { Badge } from '@/components/ui/badge';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { InsightConfidenceTrajectory } from './InsightConfidenceTrajectory';
import { memoryService } from '@/lib/api/services/memory';
import {
  Lightbulb,
  RefreshCw,
  Archive,
  CheckCircle,
  Clock,
  Sparkles,
  Award,
  Zap,
} from 'lucide-react';
import { toast } from 'sonner';
import { formatRelativeTime } from '@/lib/time';
import { cn } from '@/lib/utils';

export interface InsightDetailDrawerProps {
  insight: Insight | null;
  spaceId: string;
  isOpen: boolean;
  onClose: () => void;
  onStatusChange?: (updated: Insight) => void;
}

export function InsightDetailDrawer({
  insight,
  spaceId,
  isOpen,
  onClose,
  onStatusChange,
}: InsightDetailDrawerProps) {
  const [events, setEvents] = useState<InsightEvent[]>([]);
  const [isLoadingHistory, setIsLoadingHistory] = useState<boolean>(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [isUpdatingStatus, setIsUpdatingStatus] = useState<boolean>(false);

  // 按需拉取历史流水（仅在展开且存在 insight 时请求）
  const fetchHistory = useCallback(async () => {
    if (!insight?.id) return;
    setIsLoadingHistory(true);
    setHistoryError(null);
    try {
      const res = await memoryService.getInsightHistory(spaceId, insight.id);
      setEvents(res.events || []);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '获取经验轨迹流水失败';
      setHistoryError(msg);
    } finally {
      setIsLoadingHistory(false);
    }
  }, [insight?.id, spaceId]);

  useEffect(() => {
    if (isOpen && insight) {
      fetchHistory();
    } else if (!isOpen) {
      setEvents([]);
      setHistoryError(null);
    }
  }, [isOpen, insight, fetchHistory]);

  if (!insight) return null;

  const isActive = insight.status === 'active';
  const isCandidate = insight.status === 'candidate';

  const handleToggleStatus = async () => {
    if (!insight) return;
    setIsUpdatingStatus(true);
    try {
      const nextStatus = isActive ? 'archived' : 'active';
      const updated = await memoryService.updateInsight(spaceId, insight.id, {
        status: nextStatus,
      });
      toast.success(nextStatus === 'active' ? '经验已激活生效' : '经验已归档');
      onStatusChange?.(updated);
      // 重新刷新轨迹以包含最新的状态流水
      fetchHistory();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '更新经验状态失败';
      toast.error(msg);
    } finally {
      setIsUpdatingStatus(false);
    }
  };

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-2xl md:max-w-3xl p-6 flex flex-col justify-between overflow-y-auto">
        <div className="space-y-5">
          {/* Header 区域 */}
          <SheetHeader className="border-b border-border/40 pb-4">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs uppercase text-muted-foreground">
                  [L3 Insight] 经验详情
                </span>
                <Badge
                  variant="outline"
                  className={cn(
                    'text-[10.5px] px-2 py-0.5 font-medium',
                    isActive
                      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                      : isCandidate
                        ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                        : 'border-muted-foreground/30 bg-muted text-muted-foreground',
                  )}
                >
                  {isActive ? '生效中' : isCandidate ? '待定评估' : '已归档'}
                </Badge>
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                  {insight.scope === 'space' ? '空间专有' : '全局通用'}
                </span>
              </div>

              <div className="flex items-center gap-2.5">
                <div className="text-right">
                  <div className="text-[10px] text-muted-foreground">当前置信度</div>
                  <div className="font-mono font-bold text-sm text-foreground">
                    {insight.confidence.toFixed(2)}
                  </div>
                </div>
                <ConfidenceRing value={insight.confidence} size={30} strokeWidth={3} />
              </div>
            </div>

            <SheetTitle className="text-base font-semibold mt-2 flex items-center gap-2 text-foreground">
              <Lightbulb className="h-4 w-4 text-accent-insight" />
              <span>经验溯源与演化轨迹</span>
            </SheetTitle>
          </SheetHeader>

          {/* 经验核心内容看板 */}
          <div className="rounded-xl border border-border/70 bg-card p-4 shadow-2xs space-y-3 select-none">
            <div>
              <div className="text-[11px] font-medium text-muted-foreground flex items-center gap-1 mb-1">
                <Zap className="h-3 w-3 text-primary" />
                <span>[适用场景 / 触发条件]</span>
              </div>
              <div className="text-xs font-semibold text-foreground leading-relaxed bg-muted/30 p-2.5 rounded-lg border border-border/40 prose prose-sm dark:prose-invert max-w-none prose-p:my-0">
                <MarkdownView>{insight.trigger}</MarkdownView>
              </div>
            </div>

            <div>
              <div className="text-[11px] font-medium text-muted-foreground flex items-center gap-1 mb-1">
                <Sparkles className="h-3 w-3 text-emerald-700 dark:text-emerald-400" />
                <span>[指导对策 / 经验做法]</span>
              </div>
              <div className="text-xs text-foreground/90 leading-relaxed bg-emerald-500/5 dark:bg-emerald-500/10 p-2.5 rounded-lg border border-emerald-500/20 prose prose-sm dark:prose-invert max-w-none prose-p:my-0">
                <MarkdownView>{insight.guidance}</MarkdownView>
              </div>
            </div>

            {insight.rationale && (
              <div>
                <div className="text-[11px] font-medium text-muted-foreground flex items-center gap-1 mb-1">
                  <Award className="h-3 w-3 text-amber-700 dark:text-amber-400" />
                  <span>[背景论据与纠错溯源]</span>
                </div>
                <div className="text-[11.5px] text-muted-foreground leading-relaxed bg-muted/20 p-2 rounded-lg border border-border/40 prose prose-sm dark:prose-invert max-w-none prose-p:my-0">
                  <MarkdownView>{insight.rationale}</MarkdownView>
                </div>
              </div>
            )}

            {/* 统计指标 */}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-border/40 text-[11px] text-muted-foreground">
              <div className="flex items-center gap-3 font-mono">
                <span>应用: <strong className="text-foreground">{insight.applied_count ?? 0}</strong> 次</span>
                <span>成功: <strong className="text-emerald-700 dark:text-emerald-400">{insight.success_count ?? 0}</strong> 次</span>
                {typeof insight.eval_delta === 'number' && (
                  <span>A/B 增益: <strong className={cn(insight.eval_delta >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-destructive')}>
                    {insight.eval_delta >= 0 ? `+${insight.eval_delta.toFixed(1)}` : insight.eval_delta.toFixed(1)}
                  </strong></span>
                )}
              </div>
              <div className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                <span>创建于 {formatRelativeTime(insight.created_at)}</span>
              </div>
            </div>
          </div>

          {/* 置信度轨迹区域 */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-foreground">经验证伪与置信度证据链</span>
              <Button
                variant="ghost"
                size="sm"
                onClick={fetchHistory}
                disabled={isLoadingHistory}
                className="h-7 gap-1 text-[11px] text-muted-foreground hover:text-foreground"
              >
                <RefreshCw className={cn('h-3 w-3', isLoadingHistory && 'animate-spin')} />
                <span>刷新轨迹</span>
              </Button>
            </div>

            {isLoadingHistory ? (
              <div className="flex h-40 flex-col items-center justify-center rounded-xl border border-border/70 bg-muted/20 p-6 text-center">
                <RefreshCw className="h-6 w-6 animate-spin text-primary mb-2" />
                <span className="text-xs font-medium text-foreground">正在加载置信度演化流水...</span>
              </div>
            ) : historyError ? (
              <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-xs text-destructive flex items-center justify-between">
                <span>{historyError}</span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={fetchHistory}
                  className="h-7 text-xs border-destructive/40"
                >
                  重试
                </Button>
              </div>
            ) : (
              <InsightConfidenceTrajectory events={events} insight={insight} />
            )}
          </div>
        </div>

        {/* 底部操作栏 */}
        <div className="flex items-center justify-between border-t border-border/50 pt-4 mt-6">
          <Button
            variant="outline"
            size="sm"
            onClick={onClose}
            className="h-8 text-xs"
          >
            关闭
          </Button>

          <div className="flex items-center gap-2">
            <Button
              variant={isActive ? 'destructive' : 'default'}
              size="sm"
              onClick={handleToggleStatus}
              disabled={isUpdatingStatus}
              className="h-8 gap-1.5 text-xs"
            >
              {isActive ? (
                <>
                  <Archive className="h-3.5 w-3.5" />
                  <span>归档此经验</span>
                </>
              ) : (
                <>
                  <CheckCircle className="h-3.5 w-3.5" />
                  <span>激活生效</span>
                </>
              )}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
