import React, { useState } from 'react';
import type { Insight, ConflictGroup, InsightReviewItem } from '@/lib/api/types';
import { InsightCardItem } from './InsightCardItem';
import { ConflictResolutionBanner } from './ConflictResolutionBanner';
import { InsightReviewSection } from './InsightReviewSection';
import { CreateInsightDialog } from './CreateInsightDialog';
import { FourStateView } from '@/components/shared/FourStateView';
import { Search, Loader2, Plus } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface MemoryInsightsTabProps {
  insights: Insight[];
  /** 当前 Space：复查区里的留一法归因要用。 */
  spaceId: string;
  conflictGroups: ConflictGroup[];
  reviewItems?: InsightReviewItem[];
  minApplied?: number;
  maxSuccessRate?: number;
  pageStatus: 'loading' | 'empty' | 'error' | 'ready';
  searchQuery: string;
  onSearchChange: (q: string) => void;
  statusFilter: string;
  onStatusFilterChange: (st: string) => void;
  onToggleStatus: (id: string, st: string) => void;
  onInsightClick?: (insight: Insight) => void;
  onArchiveInsight?: (insightId: string) => Promise<void> | void;
  onResolveConflict: (groupId: string, action: 'keep_a' | 'keep_b' | 'merge') => void;
  onRetry: () => void;
  nextCursor?: string | null;
  isLoadingMore?: boolean;
  onLoadMore?: () => void;
  /** 手动新增成功：由页面插进列表。 */
  onInsightCreated?: (insight: Insight) => void;
}

const STATUSES = ['all', 'active', 'candidate', 'archived'] as const;

/** 经验状态的中文说法。 */
const STATUS_LABELS: Record<string, string> = {
  all: '全部',
  active: '已生效',
  candidate: '候选',
  conflicted: '冲突',
  archived: '已归档',
};

export function MemoryInsightsTab({
  insights,
  spaceId,
  conflictGroups,
  reviewItems = [],
  minApplied = 5,
  maxSuccessRate = 0.4,
  pageStatus,
  searchQuery,
  onSearchChange,
  statusFilter,
  onStatusFilterChange,
  onToggleStatus,
  onInsightClick,
  onArchiveInsight,
  onResolveConflict,
  onRetry,
  nextCursor,
  isLoadingMore,
  onLoadMore,
  onInsightCreated,
}: MemoryInsightsTabProps) {
  const [createOpen, setCreateOpen] = useState(false);

  const filteredInsights = insights.filter((ins) =>
    (statusFilter === 'all' || ins.status === statusFilter) &&
    (ins.trigger.toLowerCase().includes(searchQuery.toLowerCase()) ||
      ins.guidance.toLowerCase().includes(searchQuery.toLowerCase())),
  );

  return (
    <>
    <FourStateView
      status={insights.length === 0 && pageStatus === 'ready' ? 'empty' : pageStatus}
      emptyTitle="还没有沉淀下来的经验"
      emptyDescription="在对话中完成反思或人工纠偏后，系统会自动提炼经验并在此沉淀；也可以直接把你已有的经验写进来。"
      emptyActionLabel="新增经验"
      onEmptyAction={() => setCreateOpen(true)}
      error="无法加载经验条目。"
      onRetry={onRetry}
    >
      <div className="space-y-4">
        <ConflictResolutionBanner groups={conflictGroups} onResolve={onResolveConflict} />

        {reviewItems.length > 0 && (
          <InsightReviewSection
            items={reviewItems}
            spaceId={spaceId}
            minApplied={minApplied}
            maxSuccessRate={maxSuccessRate}
            onInsightClick={(ins) => onInsightClick?.(ins)}
            onArchiveInsight={(id) => onArchiveInsight?.(id)}
          />
        )}

        <div className="flex items-center justify-between gap-3">
          <div className="relative flex-1 max-w-sm">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              placeholder="搜索经验条目..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="w-full rounded-lg border border-border/80 bg-card/60 py-1.5 pl-9 pr-3 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/20 transition-all"
            />
          </div>
          <div className="flex items-center gap-1 text-xs">
            <Button
              size="sm"
              variant="outline"
              className="mr-2 h-7 gap-1 text-xs"
              onClick={() => setCreateOpen(true)}
            >
              <Plus className="h-3.5 w-3.5" />
              新增经验
            </Button>
            {STATUSES.map((st) => (
              <button
                key={st}
                type="button"
                onClick={() => onStatusFilterChange(st)}
                className={`rounded-md px-2.5 py-1 text-xs transition-colors cursor-pointer ${
                  statusFilter === st
                    ? 'bg-primary text-primary-foreground font-medium'
                    : 'bg-muted/40 text-muted-foreground hover:bg-muted'
                }`}
              >
                {STATUS_LABELS[st] ?? st}
              </button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4.5 pt-1 items-stretch">
          {filteredInsights.map((insight) => (
            <InsightCardItem
              key={insight.id}
              insight={insight}
              onClick={() => {
                if (onInsightClick) {
                  onInsightClick(insight);
                } else {
                  onToggleStatus(insight.id, insight.status);
                }
              }}
              onToggleStatus={onToggleStatus}
            />
          ))}
        </div>

        {/* 分页契约：加载更多 */}
        {nextCursor && (
          <div className="flex justify-center pt-4 pb-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onLoadMore}
              disabled={isLoadingMore}
              className="gap-2 text-xs"
            >
              {isLoadingMore ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>正在加载更多经验...</span>
                </>
              ) : (
                <span>加载更多经验</span>
              )}
            </Button>
          </div>
        )}
      </div>
    </FourStateView>
    <CreateInsightDialog
      open={createOpen}
      onOpenChange={setCreateOpen}
      spaceId={spaceId}
      onCreated={(insight) => onInsightCreated?.(insight)}
    />
    </>
  );
}
