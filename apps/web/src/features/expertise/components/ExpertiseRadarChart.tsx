import React from 'react';
import {
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { components } from '@/lib/api/types.gen';
import { formatRelativeTime } from '@/lib/time';
import { ShieldAlert } from 'lucide-react';

type ExpertiseScore = components['schemas']['ExpertiseScore'];
type ExpertiseSnapshot = components['schemas']['ExpertiseSnapshot'];

interface ExpertiseRadarChartProps {
  current: ExpertiseScore | ExpertiseSnapshot;
  previous?: ExpertiseScore | ExpertiseSnapshot | null;
}

export function ExpertiseRadarChart({ current, previous }: ExpertiseRadarChartProps) {
  const hasPrevious = Boolean(previous);
  const isProxy = current.consistency_source === 'proxy';

  const isAllZero =
    (current.coverage ?? 0) === 0 &&
    (current.accuracy ?? 0) === 0 &&
    (current.consistency ?? 0) === 0 &&
    (current.groundedness ?? 0) === 0 &&
    (current.insight_density ?? 0) === 0 &&
    (current.overall ?? 0) === 0;

  if (isAllZero && !hasPrevious) {
    return (
      <div className="flex h-[320px] w-full flex-col items-center justify-center text-center p-6 select-none">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted/60 text-muted-foreground mb-2">
          <ShieldAlert className="h-5 w-5" />
        </div>
        <p className="text-sm font-medium text-muted-foreground">暂无雷达基准数据</p>
        <p className="text-xs text-muted-foreground mt-1 max-w-sm">
          导入资料并完成问答后，系统将自动对空间进行五维能力测算并生成雷达图。
        </p>
      </div>
    );
  }

  const radarData = [
    {
      dimension: '知识覆盖率',
      current: current.coverage,
      previous: previous?.coverage ?? 0,
      fullMark: 100,
    },
    {
      dimension: '回答准确率',
      current: current.accuracy,
      previous: previous?.accuracy ?? 0,
      fullMark: 100,
    },
    {
      dimension: isProxy ? '逻辑一致性 (近似)' : '逻辑一致性 (实测)',
      current: current.consistency,
      previous: previous?.consistency ?? 0,
      fullMark: 100,
    },
    {
      dimension: '依据度 Grounded',
      current: current.groundedness,
      previous: previous?.groundedness ?? 0,
      fullMark: 100,
    },
    {
      dimension: '经验密度',
      current: current.insight_density,
      previous: previous?.insight_density ?? 0,
      fullMark: 100,
    },
  ];

  return (
    <div className="flex flex-col justify-between h-full select-none">
      <div className="h-[290px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart cx="50%" cy="50%" outerRadius="75%" data={radarData}>
            <PolarGrid stroke="var(--border)" strokeDasharray="3 3" />
            <PolarAngleAxis
              dataKey="dimension"
              tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
            />
            <PolarRadiusAxis
              angle={30}
              domain={[0, 100]}
              tick={{ fill: 'var(--muted-foreground)', fontSize: 9 }}
            />
            {hasPrevious && (
              <Radar
                name="上一阶段基线"
                dataKey="previous"
                stroke="var(--muted-foreground)"
                fill="var(--muted-foreground)"
                fillOpacity={0.18}
              />
            )}
            <Radar
              name="当前专家度"
              dataKey="current"
              stroke="var(--primary)"
              fill="var(--primary)"
              fillOpacity={0.35}
            />
            <Legend
              wrapperStyle={{ fontSize: '11px', paddingTop: '6px' }}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>

      {/* 维度说明与来源标注 */}
      <div className="mt-2 flex flex-wrap items-center justify-center gap-1.5 border-t border-border/40 pt-2.5 text-[11px] text-muted-foreground text-center">
        <span className="font-medium text-foreground/80">逻辑一致性口径：</span>
        {isProxy ? (
          <span className="inline-flex items-center gap-1">
            <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 dark:text-amber-400 border border-amber-500/25">
              近似
            </span>
            <span>算的是生效经验占比，不是实测</span>
          </span>
        ) : (
          <span className="inline-flex items-center gap-1">
            <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700 dark:text-emerald-400 border border-emerald-500/25">
              实测
            </span>
            <span>
              {formatRelativeTime(current.consistency_measured_at)}实测
            </span>
          </span>
        )}
      </div>
    </div>
  );
}
