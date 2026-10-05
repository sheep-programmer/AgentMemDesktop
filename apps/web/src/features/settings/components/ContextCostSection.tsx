import React from 'react';
import { useContextCost, type TimeRange } from '../hooks/useContextCost';
import { ContextCostMetricCards } from './ContextCostMetricCards';
import { ContextCostProviderTable } from './ContextCostProviderTable';
import { ContextCostEmptyState } from './ContextCostEmptyState';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { RefreshCw, Coins, AlertCircle, Info } from 'lucide-react';

const TIME_RANGES: { value: TimeRange; label: string }[] = [
  { value: 'today', label: '今天' },
  { value: '7d', label: '近 7 天' },
  { value: 'all', label: '全部' },
];

export function ContextCostSection() {
  const {
    timeRange,
    setTimeRange,
    items,
    metrics,
    isLoading,
    isFetching,
    isError,
    error,
    isEmpty,
    refetch,
  } = useContextCost();

  return (
    <div className="rounded-2xl border border-border bg-card p-6 space-y-5">
      {/* 头部：标题与时间范围切换 */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Coins className="h-4 w-4 text-accent-ai" />
            <h3 className="text-sm font-semibold text-foreground">
              模型用量与缓存
            </h3>
          </div>
          <p className="text-xs text-muted-foreground mt-0.5">
            查看模型收到和生成了多少内容，以及其中有多少输入被重复利用。
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* 时间范围切换 */}
          <div className="flex items-center rounded-lg border border-border/70 bg-muted/30 p-0.5 text-xs">
            {TIME_RANGES.map((r) => (
              <button
                key={r.value}
                type="button"
                onClick={() => setTimeRange(r.value)}
                aria-pressed={timeRange === r.value}
                className={`px-2.5 py-1 rounded-md transition-colors text-xs font-medium ${
                  timeRange === r.value
                    ? 'bg-card text-foreground shadow-2xs'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>

          {/* 刷新按钮 */}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => refetch()}
            disabled={isFetching}
            className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground"
            title="刷新用量数据"
            aria-label="刷新用量数据"
          >
            <RefreshCw
              className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`}
            />
          </Button>
        </div>
      </div>

      {/* 状态渲染 */}
      {isLoading ? (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Skeleton className="h-28 rounded-xl" />
            <Skeleton className="h-28 rounded-xl" />
            <Skeleton className="h-28 rounded-xl" />
          </div>
          <Skeleton className="h-40 rounded-xl" />
        </div>
      ) : isError ? (
        <div className="flex min-h-[220px] flex-col items-center justify-center rounded-xl border border-destructive/20 bg-destructive/5 p-6 text-center">
          <AlertCircle className="h-6 w-6 text-destructive mb-2" />
          <h4 className="text-sm font-semibold text-foreground">
            用量数据加载受阻
          </h4>
          <p className="mt-1 text-xs text-muted-foreground max-w-sm">
            {(error as Error)?.message ||
              '无法获取上下文用量统计，请确认后端 API 服务已启动。'}
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            className="mt-3 gap-1.5 text-xs"
          >
            <RefreshCw className="h-3 w-3" />
            重试
          </Button>
        </div>
      ) : isEmpty ? (
        <ContextCostEmptyState />
      ) : (
        <div className="space-y-5">
          {/* 三个核心指标卡 */}
          <ContextCostMetricCards
            promptTokens={metrics.promptTokens}
            cachedTokens={metrics.cachedTokens}
            cacheWriteTokens={metrics.cacheWriteTokens}
            overallHitRate={metrics.overallHitRate}
            completionTokens={metrics.completionTokens}
          />

          {/* Provider 用量表格 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground px-0.5">
              <span>各服务商的用量</span>
              <span className="font-mono tabular-nums text-[11px]">
                共 {items.length} 个服务商有记录
              </span>
            </div>
            <ContextCostProviderTable items={items} />
          </div>

          {/* 语义说明提示 */}
          <div className="flex items-start gap-2 rounded-lg border border-border/60 bg-muted/20 p-3 text-xs text-muted-foreground leading-relaxed">
            <Info className="h-4 w-4 text-accent-ai shrink-0 mt-0.5" />
            <div>
              <span className="text-foreground font-medium">
                怎样读这些数字：
              </span>
              缓存命中量已经包含在输入量中，不需要再相加。这里展示服务商返回的用量，聊天中的上下文估算另行计算。是否享有缓存优惠、折扣多少，由服务商和模型决定，实际费用以账单为准。
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
