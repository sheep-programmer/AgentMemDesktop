import React from 'react';
import type { components } from '@/lib/api/types.gen';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { AnimatedCounter } from '@/components/shared/AnimatedCounter';
import { TrendingUp, TrendingDown, Minus, ShieldCheck, Sparkles, RefreshCw } from 'lucide-react';
import { formatRelativeTime } from '@/lib/time';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

type ExpertiseScore = components['schemas']['ExpertiseScore'];
type ExpertiseSnapshot = components['schemas']['ExpertiseSnapshot'];

interface ExpertiseScoreOverviewProps {
  current: ExpertiseScore | ExpertiseSnapshot;
  previous?: ExpertiseScore | ExpertiseSnapshot | null;
  onMeasureConsistency?: () => void;
  isProbing?: boolean;
  hasProbeResult?: boolean;
  onToggleProbeResult?: () => void;
}

export function ExpertiseScoreOverview({
  current,
  previous,
  onMeasureConsistency,
  isProbing = false,
  hasProbeResult = false,
  onToggleProbeResult,
}: ExpertiseScoreOverviewProps) {
  const hasPrevious = Boolean(previous);
  const delta = hasPrevious ? +(current.overall - previous!.overall).toFixed(1) : null;
  const isProxy = current.consistency_source === 'proxy';

  // 依据度按最近的回答统计：只有两三条时 100% 说明不了什么，要把分母亮出来
  const samples = current.groundedness_samples ?? 0;
  const groundednessHint =
    samples === 0
      ? '还没有可统计的回答'
      : samples < 5
        ? `回答里有出处的句子占比 · 仅 ${samples} 条回答，仅供参考`
        : `回答里有出处的句子占比 · 基于最近 ${samples} 条回答`;

  const dimensions = [
    { key: 'groundedness', label: '依据度', value: current.groundedness, hint: groundednessHint },
    { key: 'accuracy', label: '准确率', value: current.accuracy, hint: '最近一次评测的测验题平均分' },
    {
      key: 'coverage',
      label: '覆盖率',
      value: current.coverage,
      // 0% 最容易被误读成「资料没读进来」：其实是大纲里的主题还没有对应的知识卡片
      hint:
        current.coverage === 0
          ? '大纲里的主题还没有对应的知识卡片，具体缺哪些见下方「知识盲区」'
          : '领域大纲里已有资料覆盖的主题占比',
    },
    { key: 'consistency', label: '一致性', value: current.consistency, hint: '' },
    { key: 'insight_density', label: '经验密度', value: current.insight_density, hint: '已沉淀的高置信度经验多不多' },
  ];

  return (
    <div className="relative overflow-hidden flex flex-col justify-between rounded-2xl border border-border/80 bg-card p-6 shadow-xs select-none">
      {/* 柔和微光 */}
      <div className="pointer-events-none absolute -top-12 -right-12 h-40 w-40 rounded-full bg-primary/10 blur-2xl" />

      <div className="relative z-10 flex items-start justify-between gap-4">
        <div>
          <div className="inline-flex items-center gap-1.5 rounded-full border border-primary/25 bg-primary/10 px-2.5 py-0.5 text-xs font-semibold text-primary">
            <ShieldCheck className="h-3.5 w-3.5" />
            <span>综合专家度</span>
          </div>
          <p className="mt-2 text-xs text-muted-foreground leading-relaxed">
            综合覆盖率、真值校验、事实依据度与实战经验密度的客观加权评分。
          </p>
        </div>

        <ConfidenceRing value={current.overall} max={100} size={50} strokeWidth={4} showText={false} />
      </div>

      <div className="relative z-10 my-5 flex items-baseline gap-3">
        <span className="font-mono text-5xl font-extrabold tracking-tight text-foreground tabular-nums">
          <AnimatedCounter value={current.overall} decimals={1} />
        </span>
        {delta !== null ? (
          <span
            className={cn(
              'inline-flex items-center rounded-full px-2.5 py-1 font-mono text-xs font-semibold shadow-2xs tabular-nums',
              delta > 0 && 'bg-accent-insight/15 text-accent-insight border border-accent-insight/25',
              delta < 0 && 'bg-destructive/15 text-destructive border border-destructive/25',
              delta === 0 && 'bg-muted text-muted-foreground',
            )}
          >
            {delta > 0 && <TrendingUp className="h-3.5 w-3.5 mr-1 stroke-[2.5]" />}
            {delta < 0 && <TrendingDown className="h-3.5 w-3.5 mr-1 stroke-[2.5]" />}
            {delta === 0 && <Minus className="h-3.5 w-3.5 mr-1" />}
            {delta > 0 ? `+${delta}` : `${delta}`} 分
          </span>
        ) : (
          <span className="inline-flex items-center rounded-full bg-muted/70 px-2.5 py-1 text-xs font-medium text-muted-foreground">
            首次评估
          </span>
        )}
      </div>

      {/* 五维核心指标卡：大数字加粗放大、小标签调淡拉开反差 */}
      <div className="relative z-10 grid grid-cols-2 sm:grid-cols-3 gap-2.5 border-t border-border/40 pt-4 text-xs">
        {dimensions.map((dim) => {
          if (dim.key === 'consistency') {
            return (
              <div
                key={dim.key}
                className={cn(
                  'relative rounded-xl border p-3 transition-colors flex flex-col justify-between col-span-2 sm:col-span-1',
                  isProxy
                    ? 'border-amber-500/30 bg-amber-500/5 hover:border-amber-500/50'
                    : 'border-emerald-500/30 bg-emerald-500/5 hover:border-emerald-500/50',
                )}
              >
                <div>
                  <div className="flex items-center justify-between gap-1.5 mb-0.5">
                    {/* 这一格比别的格多一枚「实测/近似」徽章，英文后缀会被截掉，
                        补个全称提示（中文前缀始终完整可见） */}
                    <span
                      title={dim.label}
                      className="text-[11px] font-medium text-muted-foreground truncate"
                    >
                      {dim.label}
                    </span>
                    <Badge
                      variant="outline"
                      className={cn(
                        'text-[10px] px-1.5 py-0 font-medium shrink-0',
                        isProxy
                          ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                          : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
                      )}
                    >
                      {isProxy ? '近似' : '实测'}
                    </Badge>
                  </div>

                  <div className="font-mono font-extrabold text-foreground text-lg tabular-nums">
                    {dim.value}%
                  </div>

                  <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">
                    {isProxy
                      ? '算的是生效经验占比，不是实测'
                      : `${formatRelativeTime(current.consistency_measured_at)}实测`}
                  </p>
                </div>

                <div className="mt-2 pt-1.5 border-t border-border/40 flex items-center justify-between gap-1">
                  {onMeasureConsistency && (
                    <button
                      type="button"
                      onClick={onMeasureConsistency}
                      disabled={isProbing}
                      className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline cursor-pointer disabled:opacity-50"
                    >
                      {isProbing ? (
                        <RefreshCw className="h-3 w-3 animate-spin" />
                      ) : (
                        <Sparkles className="h-3 w-3" />
                      )}
                      <span>{isProbing ? '正在实测...' : isProxy ? '测一次回答一致性' : '重新实测'}</span>
                    </button>
                  )}

                  {hasProbeResult && onToggleProbeResult && (
                    <button
                      type="button"
                      onClick={onToggleProbeResult}
                      className="text-[10px] text-muted-foreground hover:text-foreground underline cursor-pointer"
                    >
                      查看明细
                    </button>
                  )}
                </div>
              </div>
            );
          }

          return (
            <div
              key={dim.key}
              className="rounded-xl border border-border/70 bg-card/60 p-3 transition-colors hover:border-primary/30"
            >
              <div className="text-[11px] font-medium text-muted-foreground truncate">{dim.label}</div>
              <div className="font-mono font-extrabold text-foreground text-lg mt-1 tabular-nums">
                {dim.value}%
              </div>
              {dim.hint && (
                <p className="text-[10px] text-muted-foreground mt-0.5 leading-tight">{dim.hint}</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
