import React from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';
import type { Insight, InsightEvent } from '@/lib/api/types';
import { formatRelativeTime } from '@/lib/time';
import { Badge } from '@/components/ui/badge';
import {
  Clock,
  ArrowRight,
  Sparkles,
  History,
} from 'lucide-react';
import { cn } from '@/lib/utils';

export interface EventTypeConfig {
  label: string;
  dotColor: string;
  badgeClass: string;
  dotBorderColor: string;
}

export const INSIGHT_EVENT_CONFIG: Record<string, EventTypeConfig> = {
  distilled: {
    label: '经验提炼',
    dotColor: '#8b5cf6', // purple
    badgeClass: 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border-purple-500/30',
    dotBorderColor: '#7c3aed',
  },
  positive_feedback: {
    label: '收到好评',
    dotColor: '#10b981', // emerald
    badgeClass: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border-emerald-500/30',
    dotBorderColor: '#059669',
  },
  negative_feedback: {
    label: '收到差评',
    dotColor: '#ef4444', // red
    badgeClass: 'bg-destructive/10 text-destructive border-destructive/30',
    dotBorderColor: '#dc2626',
  },
  user_confirm: {
    label: '人工确认',
    dotColor: '#3b82f6', // blue
    badgeClass: 'bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-500/30',
    dotBorderColor: '#2563eb',
  },
  eval_improved: {
    label: 'A/B 评测提升',
    dotColor: '#059669', // dark emerald
    badgeClass: 'bg-emerald-600/15 text-emerald-700 dark:text-emerald-300 border-emerald-600/30',
    dotBorderColor: '#047857',
  },
  eval_regressed: {
    label: 'A/B 评测回退',
    dotColor: '#dc2626', // dark red
    badgeClass: 'bg-red-600/15 text-red-700 dark:text-red-300 border-red-600/30',
    dotBorderColor: '#b91c1c',
  },
  archived_manually: {
    label: '手动归档',
    dotColor: '#6b7280', // gray
    badgeClass: 'bg-gray-500/10 text-gray-600 dark:text-gray-400 border-gray-500/30',
    dotBorderColor: '#4b5563',
  },
  conflict_resolved_keep: {
    label: '冲突解决(保留)',
    dotColor: '#0ea5e9', // sky
    badgeClass: 'bg-sky-500/10 text-sky-700 dark:text-sky-400 border-sky-500/30',
    dotBorderColor: '#0284c7',
  },
  conflict_resolved_archive: {
    label: '冲突解决(归档)',
    dotColor: '#f59e0b', // amber
    badgeClass: 'bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/30',
    dotBorderColor: '#d97706',
  },
};

export const STATUS_LABELS: Record<string, string> = {
  candidate: '待定',
  active: '生效中',
  archived: '已归档',
  conflicted: '存在冲突',
};

function formatFullTime(timestamp: number | null | undefined): string {
  if (!timestamp) return '未知时间';
  const d = new Date(timestamp);
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function formatChartTick(timestamp: number | null | undefined): string {
  if (!timestamp) return '';
  const d = new Date(timestamp);
  const m = d.getMonth() + 1;
  const day = d.getDate();
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${m}/${day} ${h}:${min}`;
}

interface ChartItem {
  id: string;
  timeLabel: string;
  confidence: number;
  event: InsightEvent;
}

interface CustomDotProps {
  cx?: number;
  cy?: number;
  payload?: ChartItem;
}

function TrajectoryDot(props: CustomDotProps) {
  const { cx, cy, payload } = props;
  if (cx === undefined || cy === undefined || !payload) return null;

  const eventConfig = INSIGHT_EVENT_CONFIG[payload.event.event] || {
    dotColor: '#3b82f6',
    dotBorderColor: '#2563eb',
  };

  return (
    <g>
      <circle
        cx={cx}
        cy={cy}
        r={5}
        fill={eventConfig.dotColor}
        stroke="#ffffff"
        strokeWidth={2}
        className="transition-all duration-200 hover:scale-150 cursor-pointer drop-shadow-xs"
      />
    </g>
  );
}

interface TrajectoryTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: ChartItem }>;
}

function TrajectoryCustomTooltip({ active, payload }: TrajectoryTooltipProps) {
  if (!active || !payload || !payload.length) return null;
  const item = payload[0].payload;
  const evt = item.event;
  const eventConfig = INSIGHT_EVENT_CONFIG[evt.event] || {
    label: evt.event,
    badgeClass: 'bg-muted text-muted-foreground border-border',
  };

  const isInitial = evt.confidence_before === null || evt.confidence_before === undefined;
  const delta = !isInitial && evt.confidence_before !== null && evt.confidence_before !== undefined
    ? evt.confidence_after - evt.confidence_before
    : null;

  return (
    <div className="rounded-lg border border-border/80 bg-popover/95 p-3 text-xs shadow-md backdrop-blur-sm space-y-2 min-w-[200px] z-50">
      <div className="flex items-center justify-between gap-2 border-b border-border/40 pb-1.5">
        <Badge variant="outline" className={cn('text-[11px] px-1.5 py-0.5 font-medium', eventConfig.badgeClass)}>
          {eventConfig.label}
        </Badge>
        <span className="text-[10px] text-muted-foreground font-mono">
          {formatRelativeTime(evt.created_at)}
        </span>
      </div>

      {/* 置信度数值流转 */}
      <div className="flex items-center justify-between text-foreground text-xs">
        <span className="text-muted-foreground">置信度:</span>
        <div className="flex items-center gap-1.5 font-mono font-semibold">
          {isInitial ? (
            <span>初始 {evt.confidence_after.toFixed(2)}</span>
          ) : (
            <>
              <span className="text-muted-foreground">{evt.confidence_before?.toFixed(2)}</span>
              <ArrowRight className="h-3 w-3 text-muted-foreground" />
              <span className={cn(delta && delta > 0 ? 'text-emerald-700 dark:text-emerald-400' : delta && delta < 0 ? 'text-destructive' : 'text-foreground')}>
                {evt.confidence_after.toFixed(2)}
              </span>
              {delta !== null && (
                <span className={cn('text-[10px]', delta >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-destructive')}>
                  ({delta >= 0 ? `+${delta.toFixed(2)}` : delta.toFixed(2)})
                </span>
              )}
            </>
          )}
        </div>
      </div>

      {/* 状态流转 */}
      {evt.status_after && evt.status_before !== evt.status_after && (
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-muted-foreground">状态变更:</span>
          <div className="flex items-center gap-1 font-medium">
            {evt.status_before ? (
              <>
                <span className="text-muted-foreground">{STATUS_LABELS[evt.status_before] || evt.status_before}</span>
                <ArrowRight className="h-2.5 w-2.5 text-muted-foreground" />
              </>
            ) : null}
            <span className="text-primary">{STATUS_LABELS[evt.status_after] || evt.status_after}</span>
          </div>
        </div>
      )}

      {/* 触发判定原因 */}
      {evt.reason && (
        <div className="text-[11px] pt-1 border-t border-border/30">
          <span className="text-muted-foreground">原因: </span>
          <span className="font-medium text-foreground">{evt.reason}</span>
        </div>
      )}

      {/* 均摊权重 */}
      {typeof evt.share === 'number' && (
        <div className="text-[10.5px] text-muted-foreground flex justify-between">
          <span>均摊份额:</span>
          <span className="font-mono">{(evt.share * 100).toFixed(1)}%</span>
        </div>
      )}

      <div className="text-[10px] text-muted-foreground pt-0.5 border-t border-border/20">
        {formatFullTime(evt.created_at)}
      </div>
    </div>
  );
}

export interface InsightConfidenceTrajectoryProps {
  events: InsightEvent[];
  insight?: Insight;
  _insight?: Insight;
  className?: string;
}

export function InsightConfidenceTrajectory({
  events,
  insight: _insight,
  className,
}: InsightConfidenceTrajectoryProps) {
  // 无事件记录时的空态说明：不画空折线
  if (!events || events.length === 0) {
    return (
      <div className={cn('rounded-xl border border-dashed border-border/80 bg-muted/20 p-8 text-center', className)}>
        <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-muted text-muted-foreground mb-3">
          <History className="h-5 w-5" />
        </div>
        <div className="text-xs font-semibold text-foreground">暂无置信度轨迹记录</div>
        <p className="mt-1.5 text-[11px] text-muted-foreground max-w-sm mx-auto leading-relaxed">
          当经验经历 A/B 评测检验、收到用户正负反馈、发生冲突裁决或人工核准时，系统将在此记录完整的加减分证据链与阶跃折线。
        </p>
      </div>
    );
  }

  // 映射折线图数据
  const chartData: ChartItem[] = events.map((evt) => ({
    id: evt.id,
    timeLabel: formatChartTick(evt.created_at),
    confidence: evt.confidence_after,
    event: evt,
  }));

  return (
    <div className={cn('space-y-4', className)}>
      {/* 折线图卡片 */}
      <div className="rounded-xl border border-border/70 bg-card p-4 shadow-2xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 pb-2.5">
          <div className="flex items-center gap-1.5">
            <Sparkles className="h-4 w-4 text-primary" />
            <span className="text-xs font-semibold text-foreground">置信度演化轨迹</span>
            <span className="text-[11px] text-muted-foreground">(阶梯跃迁折线)</span>
          </div>

          <div className="flex items-center gap-3 text-[10.5px] text-muted-foreground flex-wrap">
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-[#8b5cf6]" />
              提炼初始
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-[#10b981]" />
              正向反馈/提升
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-[#ef4444]" />
              负向反馈/回退
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="h-2 w-2 rounded-full bg-[#3b82f6]" />
              人工确认
            </span>
          </div>
        </div>

        {/* Recharts 阶梯折线 */}
        <div className="h-[200px] w-full select-none pt-1">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 12, right: 16, left: -20, bottom: 4 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} opacity={0.6} />
              <XAxis
                dataKey="timeLabel"
                tick={{ fill: 'var(--muted-foreground)', fontSize: 10 }}
                axisLine={{ stroke: 'var(--border)' }}
                tickLine={{ stroke: 'var(--border)' }}
              />
              <YAxis
                domain={[0, 1]}
                ticks={[0, 0.2, 0.4, 0.6, 0.8, 1.0]}
                tickFormatter={(val: number) => val.toFixed(2)}
                tick={{ fill: 'var(--muted-foreground)', fontSize: 10 }}
                axisLine={{ stroke: 'var(--border)' }}
                tickLine={{ stroke: 'var(--border)' }}
              />
              <Tooltip content={<TrajectoryCustomTooltip />} />
              <Line
                type="stepAfter"
                dataKey="confidence"
                stroke="var(--primary)"
                strokeWidth={2}
                dot={<TrajectoryDot />}
                activeDot={{ r: 7, strokeWidth: 2, stroke: '#ffffff' }}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* 事件流水明细列表 */}
      <div className="rounded-xl border border-border/70 bg-card p-4 shadow-2xs space-y-3">
        <div className="flex items-center justify-between border-b border-border/40 pb-2">
          <div className="flex items-center gap-1.5">
            <History className="h-3.5 w-3.5 text-primary" />
            <span className="text-xs font-semibold text-foreground">置信度变更流水</span>
          </div>
          <span className="font-mono text-[10.5px] text-muted-foreground">
            共 {events.length} 次变更事件
          </span>
        </div>

        <div className="space-y-2.5">
          {events.map((evt, idx) => {
            const eventConfig = INSIGHT_EVENT_CONFIG[evt.event] || {
              label: evt.event,
              badgeClass: 'bg-muted text-muted-foreground border-border',
            };
            const isInitial = evt.confidence_before === null || evt.confidence_before === undefined;
            const delta = !isInitial && evt.confidence_before !== null && evt.confidence_before !== undefined
              ? evt.confidence_after - evt.confidence_before
              : null;

            return (
              <div
                key={evt.id || idx}
                className="group relative flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 rounded-lg border border-border/60 bg-muted/20 p-2.5 text-xs transition-colors hover:border-border hover:bg-muted/30"
              >
                {/* 左侧：事件类型与时间 */}
                <div className="flex items-center gap-2 min-w-0">
                  <Badge
                    variant="outline"
                    className={cn('text-[10.5px] px-1.5 py-0.5 font-medium shrink-0', eventConfig.badgeClass)}
                  >
                    {eventConfig.label}
                  </Badge>

                  <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground truncate">
                    <Clock className="h-3 w-3 shrink-0" />
                    <span title={formatFullTime(evt.created_at)} className="cursor-help">
                      {formatRelativeTime(evt.created_at)}
                    </span>
                  </div>

                  {/* 状态变化标签 */}
                  {evt.status_after && evt.status_before !== evt.status_after && (
                    <span className="hidden sm:inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                      {STATUS_LABELS[evt.status_after] || evt.status_after}
                    </span>
                  )}
                </div>

                {/* 右侧：数值流转与 reason */}
                <div className="flex flex-wrap items-center justify-end gap-3 shrink-0 sm:text-right">
                  {/* reason 与 share */}
                  {(evt.reason || typeof evt.share === 'number') && (
                    <div className="text-[11px] text-muted-foreground">
                      {evt.reason && <span className="font-medium text-foreground">{evt.reason}</span>}
                      {typeof evt.share === 'number' && (
                        <span className="ml-1 text-[10.5px] text-muted-foreground">
                          (份额 {(evt.share * 100).toFixed(0)}%)
                        </span>
                      )}
                    </div>
                  )}

                  {/* 置信度流转 */}
                  <div className="flex items-center gap-1 font-mono text-xs font-semibold">
                    {isInitial ? (
                      <span className="text-foreground">初始 {evt.confidence_after.toFixed(2)}</span>
                    ) : (
                      <>
                        <span className="text-muted-foreground">{evt.confidence_before?.toFixed(2)}</span>
                        <ArrowRight className="h-3 w-3 text-muted-foreground shrink-0" />
                        <span className={cn(delta && delta > 0 ? 'text-emerald-700 dark:text-emerald-400' : delta && delta < 0 ? 'text-destructive' : 'text-foreground')}>
                          {evt.confidence_after.toFixed(2)}
                        </span>
                        {delta !== null && (
                          <span className={cn('text-[10px] font-normal ml-0.5', delta >= 0 ? 'text-emerald-700 dark:text-emerald-400' : 'text-destructive')}>
                            ({delta >= 0 ? `+${delta.toFixed(2)}` : delta.toFixed(2)})
                          </span>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
