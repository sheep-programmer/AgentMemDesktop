import React from 'react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import type { components } from '@/lib/api/types.gen';
import { TrendingUp } from 'lucide-react';

type ExpertiseSnapshot = components['schemas']['ExpertiseSnapshot'];

interface ExpertiseGrowthChartProps {
  history?: ExpertiseSnapshot[];
}

export function ExpertiseGrowthChart({ history }: ExpertiseGrowthChartProps) {
  if (!history || history.length === 0) {
    return (
      <div className="flex h-[280px] w-full flex-col items-center justify-center text-center p-6 select-none">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted/60 text-muted-foreground mb-2">
          <TrendingUp className="h-5 w-5" />
        </div>
        <p className="text-sm font-medium text-muted-foreground">完成首次进化后即可查看成长曲线</p>
        <p className="text-xs text-muted-foreground mt-1 max-w-sm">
          导入资料并完成问答后，系统会在每次进化时记录一次专家度。
        </p>
      </div>
    );
  }

  const data = history.map((h, idx) => ({
    date: h.created_at
      ? new Date(h.created_at).toLocaleDateString('zh-CN', {
          month: 'numeric',
          day: 'numeric',
        })
      : `第 ${idx + 1} 次`,
    score: Math.round(h.overall * 10) / 10,
    insights: Math.round(h.insight_density),
  }));

  return (
    <div className="h-[280px] w-full select-none pt-2">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
          <defs>
            <linearGradient id="scoreGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="var(--primary)" stopOpacity={0.4} />
              <stop offset="95%" stopColor="var(--primary)" stopOpacity={0.0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
            axisLine={{ stroke: 'var(--border)' }}
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fill: 'var(--muted-foreground)', fontSize: 11 }}
            axisLine={{ stroke: 'var(--border)' }}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'var(--card)',
              borderColor: 'var(--border)',
              borderRadius: '8px',
              fontSize: '12px',
              color: 'var(--card-foreground)',
              boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
            }}
            itemStyle={{ color: 'var(--card-foreground)' }}
            labelStyle={{ color: 'var(--muted-foreground)', fontWeight: 500 }}
            formatter={(value: number | string, name: string) => [
              value,
              name === 'score' ? '专家度总分' : '经验条目数',
            ]}
          />
          <Area
            type="monotone"
            dataKey="score"
            stroke="var(--primary)"
            strokeWidth={2.5}
            fillOpacity={1}
            fill="url(#scoreGradient)"
            isAnimationActive={true}
            animationDuration={1200}
            animationEasing="ease-out"
            activeDot={{ r: 5, fill: 'var(--primary)', stroke: 'var(--card)', strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
