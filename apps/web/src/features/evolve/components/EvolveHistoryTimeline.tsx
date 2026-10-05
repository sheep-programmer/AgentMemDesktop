import React from 'react';
import type { EvolveHistoryItem } from '@/lib/api/types.temp';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  History,
  TrendingUp,
  TrendingDown,
  Minus,
  Clock,
  AlertCircle,
  ArrowUpRight,
  ArrowDownRight,
} from 'lucide-react';
import { cn } from '@/lib/utils';

interface EvolveHistoryTimelineProps {
  history: EvolveHistoryItem[];
}

interface OutputCompositionProps {
  produced: number;
  merged: number;
  conflicts: number;
  promoted: number;
  demoted: number;
}

/**
 * 产出构成堆叠条形组件
 * produced (产出) / merged (合并) / conflicts (冲突) / promoted (晋升) / demoted (淘汰)
 */
function OutputCompositionBar({
  produced,
  merged,
  conflicts,
  promoted,
  demoted,
}: OutputCompositionProps) {
  const total = produced + merged + conflicts + promoted + demoted;

  if (total === 0) {
    return (
      <div className="rounded-lg bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        本轮无结构性产出变更
      </div>
    );
  }

  const segments = [
    {
      key: 'produced',
      label: '产出',
      count: produced,
      color: 'bg-blue-500',
      textColor: 'text-blue-700 dark:text-blue-400',
      bgColor: 'bg-blue-500/10 border-blue-500/30',
      desc: '萃取产生的新经验条目',
    },
    {
      key: 'merged',
      label: '合并',
      count: merged,
      color: 'bg-indigo-500',
      textColor: 'text-indigo-600 dark:text-indigo-300',
      bgColor: 'bg-indigo-500/10 border-indigo-500/30',
      desc: '语义去重与泛化合并的条目',
    },
    {
      key: 'conflicts',
      label: '冲突',
      count: conflicts,
      color: 'bg-amber-500',
      textColor: 'text-amber-800 dark:text-amber-400',
      bgColor: 'bg-amber-500/10 border-amber-500/30',
      desc: '检测并消除规则对抗的冲突对',
    },
    {
      key: 'promoted',
      label: '晋升',
      count: promoted,
      color: 'bg-emerald-500',
      textColor: 'text-emerald-700 dark:text-emerald-400',
      bgColor: 'bg-emerald-500/10 border-emerald-500/30',
      desc: '通过评测晋升至生产库的经验',
    },
    {
      key: 'demoted',
      label: '淘汰',
      count: demoted,
      color: 'bg-rose-500',
      textColor: 'text-rose-700 dark:text-rose-400',
      bgColor: 'bg-rose-500/10 border-rose-500/30',
      desc: '评测表现下滑淘汰归档的旧经验',
    },
  ];

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-[11px] text-muted-foreground font-medium">
        <span>产出构成 (共 {total} 项动作)</span>
        <span className="text-[10px]">产出 · 合并 · 冲突 · 晋升 · 淘汰</span>
      </div>

      {/* 堆叠横向条 */}
      <div className="h-2.5 w-full rounded-full bg-muted/60 overflow-hidden flex shadow-2xs">
        {segments.map((seg) => {
          if (seg.count === 0) return null;
          const pct = (seg.count / total) * 100;

          return (
            <Tooltip key={seg.key}>
              <TooltipTrigger className="h-full p-0 border-0" style={{ width: `${pct}%` }}>
                <div
                  className={cn('h-full w-full transition-all duration-300 hover:opacity-80 cursor-help', seg.color)}
                />
              </TooltipTrigger>
              <TooltipContent side="top" className="text-xs">
                <div className="font-semibold">{seg.label}: {seg.count} 条 ({Math.round(pct)}%)</div>
                <div className="text-[11px] text-muted-foreground">{seg.desc}</div>
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>

      {/* 底部各路指标徽章图例 */}
      <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
        {segments.map((seg) => (
          <span
            key={seg.key}
            className={cn(
              'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono text-[10.5px] border',
              seg.count > 0 ? seg.bgColor : 'bg-muted/30 border-border/40 text-muted-foreground',
            )}
            title={seg.desc}
          >
            <span className={cn('h-1.5 w-1.5 rounded-full', seg.count > 0 ? seg.color : 'bg-muted-foreground/40')} />
            <span className="font-sans font-medium text-foreground/80">{seg.label}</span>
            <span className={cn('font-bold', seg.count > 0 ? seg.textColor : 'text-muted-foreground')}>
              {seg.count}
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * 专家度变化折线/滑动条组件 (expertise_before → expertise_after)
 */
function ExpertiseChangeSlider({
  before,
  after,
}: {
  before: number;
  after: number;
}) {
  const delta = +(after - before).toFixed(1);
  const isPositive = delta > 0;
  const isNegative = delta < 0;

  // 限制到 0~100 坐标系
  const clampedBefore = Math.min(100, Math.max(0, before));
  const clampedAfter = Math.min(100, Math.max(0, after));
  const leftPct = Math.min(clampedBefore, clampedAfter);
  const widthPct = Math.max(1.5, Math.abs(clampedAfter - clampedBefore));

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-[11px]">
        <span className="font-medium text-muted-foreground">专家度变化</span>
        <div className="flex items-center gap-1.5 font-mono">
          <span className="text-muted-foreground tabular-nums">{before.toFixed(1)}</span>
          <span className="text-muted-foreground">→</span>
          <span className="font-bold text-foreground tabular-nums">{after.toFixed(1)}</span>
          <span
            className={cn(
              'ml-1 inline-flex items-center px-1.5 py-0.2 rounded text-[11px] font-bold tabular-nums border',
              isPositive && 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
              isNegative && 'border-rose-500/30 bg-rose-500/10 text-destructive',
              !isPositive && !isNegative && 'border-border bg-muted text-muted-foreground',
            )}
          >
            {isPositive && <ArrowUpRight className="h-3 w-3 mr-0.5 stroke-[2.5]" />}
            {isNegative && <ArrowDownRight className="h-3 w-3 mr-0.5 stroke-[2.5]" />}
            {isPositive ? `+${delta.toFixed(1)}` : delta.toFixed(1)}
          </span>
        </div>
      </div>

      {/* 滑动指示条 */}
      <div className="relative h-2 w-full rounded-full bg-muted/60 overflow-visible">
        {/* 背景轻微刻度 */}
        <div className="absolute inset-0 rounded-full bg-muted/80" />

        {/* 跃迁区间填充 */}
        <div
          className={cn(
            'absolute top-0 bottom-0 rounded-full transition-all duration-300',
            isPositive
              ? 'bg-gradient-to-r from-emerald-500/60 to-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.3)]'
              : isNegative
                ? 'bg-gradient-to-r from-rose-500 to-rose-500/60 shadow-[0_0_8px_rgba(244,63,94,0.3)]'
                : 'bg-muted-foreground/40',
          )}
          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
        />

        {/* 起始点圆圈 */}
        <div
          className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 h-3.5 w-3.5 rounded-full border-2 border-background bg-muted-foreground/70 shadow-2xs"
          style={{ left: `${clampedBefore}%` }}
          title={`进化前专家度: ${before.toFixed(1)}`}
        />

        {/* 终点圆圈 */}
        <div
          className={cn(
            'absolute top-1/2 -translate-y-1/2 -translate-x-1/2 h-3.5 w-3.5 rounded-full border-2 border-background shadow-xs',
            isPositive ? 'bg-emerald-500 ring-2 ring-emerald-500/30' : isNegative ? 'bg-rose-500 ring-2 ring-rose-500/30' : 'bg-muted-foreground',
          )}
          style={{ left: `${clampedAfter}%` }}
          title={`进化后专家度: ${after.toFixed(1)}`}
        />
      </div>

      {/* 标尺辅助端点 */}
      <div className="flex justify-between text-[9.5px] font-mono text-muted-foreground px-0.5">
        <span>0</span>
        <span>50</span>
        <span>100</span>
      </div>
    </div>
  );
}

export function EvolveHistoryTimeline({ history }: EvolveHistoryTimelineProps) {
  return (
    <div className="rounded-2xl border border-border/80 bg-card p-6 md:p-7 space-y-6 shadow-2xs">
      <div className="flex items-center justify-between border-b border-border/40 pb-4">
        <div className="flex items-center gap-2 font-semibold text-sm text-foreground">
          <History className="h-4 w-4 text-primary" />
          <span>进化记录</span>
        </div>
        <span className="text-[11px] font-mono text-muted-foreground">
          共 {history.length} 次进化记录
        </span>
      </div>

      {history.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-center select-none">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted/50 text-muted-foreground mb-3 shadow-2xs">
            <History className="h-6 w-6" />
          </div>
          <p className="text-sm font-medium text-muted-foreground">暂无自进化历史记录</p>
          <p className="text-xs text-muted-foreground mt-1 max-w-sm leading-relaxed">
            当完成对话纠偏并触发闭环进化后，系统将在此呈现历次进化的产出构成、专家度跃迁与基准评测差值。
          </p>
        </div>
      ) : (
        /* 真实时间线视觉 */
        <div className="relative pl-6 space-y-8 before:absolute before:left-2 before:top-3 before:bottom-3 before:w-px before:bg-border/80">
          {history.map((item, idx) => {
            const runAtTimestamp = item.run_at || item.created_at;
            let dateStr = item.date || '未定日期';
            let timeStr = '';

            if (runAtTimestamp) {
              const ms = runAtTimestamp > 1e11 ? runAtTimestamp : runAtTimestamp * 1000;
              const d = new Date(ms);
              dateStr = d.toLocaleDateString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
              });
              timeStr = d.toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit',
              });
            }

            const produced = item.produced ?? 0;
            const merged = item.merged ?? 0;
            const conflicts = item.conflicts ?? 0;
            const promoted = item.promoted ?? item.promoted_count ?? 0;
            const demoted = item.demoted ?? item.demoted_count ?? 0;

            const expertiseBefore = item.expertise_before ?? item.score_before ?? 0;
            const expertiseAfter = item.expertise_after ?? item.score_after ?? expertiseBefore;

            // eval_delta 为 null 时是「未跑评测」，与评测无变化 (0.0) 区别对待
            const evalDelta = item.eval_delta !== undefined ? item.eval_delta : null;

            return (
              <div key={item.id || idx} className="relative group">
                {/* 时间线节点圆点 */}
                <div className="absolute -left-6 top-1.5 h-4 w-4 -translate-x-[1px] flex items-center justify-center">
                  <span className="h-2.5 w-2.5 rounded-full border-2 border-card bg-primary ring-2 ring-primary/25 group-hover:scale-125 transition-transform" />
                </div>

                {/* 卡片主体 */}
                <div className="rounded-xl border border-border/80 bg-card/70 p-5 space-y-4 transition-all duration-200 hover:border-border hover:shadow-xs">
                  {/* 顶栏：时间、耗时、评测收益状态 */}
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 border-b border-border/40 pb-3">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-xs font-semibold text-foreground tabular-nums">
                        {dateStr}
                      </span>
                      {timeStr && (
                        <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
                          {timeStr}
                        </span>
                      )}
                      {item.duration_ms ? (
                        <span className="inline-flex items-center gap-1 rounded-md bg-muted/60 px-1.5 py-0.5 font-mono text-[10.5px] text-muted-foreground">
                          <Clock className="h-3 w-3" />
                          {(item.duration_ms / 1000).toFixed(1)}s
                        </span>
                      ) : null}
                    </div>

                    {/* 评测 Delta 状态：明确区分未跑评测与 0 */}
                    <div className="flex items-center gap-2">
                      {evalDelta === null ? (
                        <span
                          className="inline-flex items-center gap-1 rounded-md border border-dashed border-border/80 bg-muted/40 px-2 py-0.5 font-mono text-[11px] text-muted-foreground"
                          title="这一轮没有跑测验验证"
                        >
                          <AlertCircle className="h-3 w-3" />
                          <span>未跑评测</span>
                        </span>
                      ) : (
                        <span
                          className={cn(
                            'inline-flex items-center gap-1 rounded-md px-2 py-0.5 font-mono text-xs font-bold tabular-nums border',
                            evalDelta > 0 &&
                              'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
                            evalDelta < 0 &&
                              'border-rose-500/30 bg-rose-500/10 text-destructive',
                            evalDelta === 0 &&
                              'border-border bg-muted/50 text-muted-foreground',
                          )}
                          // 评测涨、专家度跌并不矛盾，得把两者口径讲清楚，否则看起来像系统自相矛盾
                          title={
                            '这一轮候选经验启用前后，测验题平均分的变化' +
                            `（${evalDelta > 0 ? `+${evalDelta.toFixed(1)}` : evalDelta.toFixed(1)} 分）。` +
                            '\n专家度是五项指标的综合分，测验只影响其中的「准确率」，' +
                            '依据度、覆盖率等变化也会让它往另一个方向走。'
                          }
                        >
                          {evalDelta > 0 && <TrendingUp className="h-3 w-3 stroke-[2.5]" />}
                          {evalDelta < 0 && <TrendingDown className="h-3 w-3 stroke-[2.5]" />}
                          {evalDelta === 0 && <Minus className="h-3 w-3" />}
                          <span>
                            评测 {evalDelta > 0 ? `+${evalDelta.toFixed(1)}` : evalDelta.toFixed(1)}
                          </span>
                        </span>
                      )}
                    </div>
                  </div>

                  {/* 摘要文字（如果有） */}
                  {item.summary && (
                    <p className="text-xs text-foreground/90 leading-relaxed font-normal">
                      {item.summary}
                    </p>
                  )}

                  {/* 核心可视化 1：专家度滑动变化 */}
                  <div className="rounded-lg border border-border/50 bg-background/50 p-3">
                    <ExpertiseChangeSlider
                      before={expertiseBefore}
                      after={expertiseAfter}
                    />
                  </div>

                  {/* 核心可视化 2：产出构成堆叠条形 */}
                  <div className="rounded-lg border border-border/50 bg-background/50 p-3">
                    <OutputCompositionBar
                      produced={produced}
                      merged={merged}
                      conflicts={conflicts}
                      promoted={promoted}
                      demoted={demoted}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
