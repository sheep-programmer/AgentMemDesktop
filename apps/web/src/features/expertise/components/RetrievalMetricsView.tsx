import React from 'react';
import type { RetrievalMetrics, RetrievalMetricsDelta } from '@/lib/api/types';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';
import { Search, ArrowUpDown, ShieldCheck } from 'lucide-react';

interface MetricDisplayProps {
  label: string;
  value: number | null | undefined;
  diagnostic?: string;
  description: string;
  icon: React.ReactNode;
  compact?: boolean;
  isDelta?: boolean;
}

function MetricBadge({
  label,
  value,
  diagnostic,
  description,
  icon,
  compact = false,
  isDelta = false,
}: MetricDisplayProps) {
  const isUnmeasured = value === null || value === undefined;

  let displayStr = '未测到';
  let colorClass = 'text-muted-foreground bg-muted/70 border-border/80';

  if (isDelta) {
    if (!isUnmeasured) {
      const num = value;
      if (Math.abs(num) < 0.0001) {
        displayStr = '±0.000';
        colorClass = 'text-muted-foreground bg-muted/70 border-border/80';
      } else if (num > 0) {
        displayStr = `+${num.toFixed(3)}`;
        colorClass = 'text-emerald-700 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/25';
      } else {
        displayStr = num.toFixed(3);
        colorClass = 'text-destructive bg-destructive/10 border-destructive/25';
      }
    }
  } else {
    const percent = typeof value === 'number' ? Math.round(value * 100) : null;
    if (!isUnmeasured) {
      displayStr = `${percent}%`;
      if (percent! >= 80) {
        colorClass = 'text-emerald-700 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/25';
      } else if (percent! >= 50) {
        colorClass = 'text-amber-700 dark:text-amber-400 bg-amber-500/10 border-amber-500/25';
      } else {
        colorClass = 'text-destructive bg-destructive/10 border-destructive/25';
      }
    }
  }

  return (
    <Tooltip>
      <TooltipTrigger>
        <span
          className={cn(
            'inline-flex items-center gap-1 rounded-md border font-mono tabular-nums transition-colors cursor-help',
            compact ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-1 text-xs',
            colorClass,
          )}
        >
          <span className="opacity-70">{icon}</span>
          <span className="font-sans font-medium text-[10px] text-muted-foreground">
            {label}:
          </span>
          <span className="font-semibold">
            {displayStr}
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs space-y-1 p-2.5 text-xs">
        <div className="font-semibold flex items-center justify-between gap-2 text-foreground">
          <span>{isDelta ? `${label} 差值 (Δ)` : label}</span>
          <span className="font-mono">
            {isUnmeasured
              ? '未测到 (null)'
              : isDelta
                ? (value! > 0 ? `+${value!.toFixed(4)}` : value!.toFixed(4))
                : `${Math.round(value! * 100)}% (${value?.toFixed(3)})`}
          </span>
        </div>
        <p className="text-muted-foreground text-[11px] leading-relaxed">
          {isDelta
            ? '相较基准臂（Baseline）的检索指标差值。正值代表相较基准表现提升，负值代表下降。'
            : description}
        </p>
        {diagnostic && (
          <div className="pt-1 border-t border-border/50 text-[10.5px] font-medium text-primary">
            诊断指引: {diagnostic}
          </div>
        )}
        {isUnmeasured && (
          <div className="text-[10px] text-muted-foreground italic">
            * {isDelta ? '某一方未测到或未审计时差值留空，不以 0 顶替。' : '题目无 must_include、检索为空或 judge 未审计时为未测到，不等于 0 分。'}
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

export interface RetrievalMetricsViewProps {
  metrics?: RetrievalMetrics | null;
  delta?: RetrievalMetricsDelta | null;
  isDelta?: boolean;
  compact?: boolean;
  showAuditDetails?: boolean;
}

export function RetrievalMetricsView({
  metrics,
  delta,
  isDelta: forceDelta = false,
  compact = false,
  showAuditDetails = false,
}: RetrievalMetricsViewProps) {
  const isDelta = forceDelta || Boolean(delta);
  const data = delta || metrics;

  if (!data) {
    return (
      <span className="text-[10px] text-muted-foreground italic">
        {isDelta ? '无检索指标差值' : '未采集检索指标'}
      </span>
    );
  }

  const recallLabel = isDelta ? '召回差' : '检索召回';
  const precisionLabel = isDelta ? '精度差' : '排序精度';
  const faithfulnessLabel = isDelta ? '忠实差' : '忠实度';

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <MetricBadge
        label={recallLabel}
        value={data.context_recall}
        description="参考答案要点中被检索到的证据支持的比例。"
        diagnostic={isDelta ? undefined : '数值偏低说明检索阶段未捞到关键证据。'}
        icon={<Search className="h-3 w-3" />}
        compact={compact}
        isDelta={isDelta}
      />
      <MetricBadge
        label={precisionLabel}
        value={data.context_precision}
        description="被用到的证据在检索结果中排得靠前不靠前 (Precision@K)。"
        diagnostic={isDelta ? undefined : '数值偏低说明重排或相关度排序策略存在问题。'}
        icon={<ArrowUpDown className="h-3 w-3" />}
        compact={compact}
        isDelta={isDelta}
      />
      <MetricBadge
        label={faithfulnessLabel}
        value={data.faithfulness}
        description="答案论断中能在检索证据里找到充分依据的比例。"
        diagnostic={isDelta ? undefined : '数值偏低说明模型未充分利用检索证据（存在幻觉臆造）。'}
        icon={<ShieldCheck className="h-3 w-3" />}
        compact={compact}
        isDelta={isDelta}
      />

      {showAuditDetails && (
        <div className="flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground ml-1">
          <span className="bg-muted/80 px-1.5 py-0.5 rounded border border-border/50">
            论断: {isDelta && (data.claims ?? 0) > 0 ? `+${data.claims}` : `${data.claims ?? 0}`}
          </span>
          <span className="bg-muted/80 px-1.5 py-0.5 rounded border border-border/50">
            证据: {isDelta && (data.evidence ?? 0) > 0 ? `+${data.evidence}` : `${data.evidence ?? 0}`}
          </span>
          <span
            className={cn(
              'px-1.5 py-0.5 rounded border',
              data.audited
                ? 'text-emerald-700 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20'
                : 'text-muted-foreground bg-muted border-border/50',
            )}
          >
            {data.audited
              ? (isDelta ? '双臂已审' : '已审计')
              : (isDelta ? '未全审' : '未审计')}
          </span>
        </div>
      )}
    </div>
  );
}
