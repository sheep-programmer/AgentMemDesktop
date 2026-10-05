import React from 'react';
import type { components } from '@/lib/api/types.gen';
import type { RetrievalMetrics } from '@/lib/api/types';
import { RetrievalMetricsView } from './RetrievalMetricsView';
import { CheckCircle2, XCircle, Loader2, Trash2 } from 'lucide-react';

type EvalItem = components['schemas']['EvalItem'];

export interface EvalItemRowResult {
  passed: boolean;
  score: number;
  reason?: string;
  metrics?: RetrievalMetrics | null;
}

interface EvalSetItemRowProps {
  item: EvalItem;
  idx: number;
  isRunning: boolean;
  result?: EvalItemRowResult;
  /** 删除这道题（带确认）。AI 出的偏题、错题此前删不掉，会一直留在评测集里 */
  onDelete?: (item: EvalItem) => void;
}

export function EvalSetItemRow({ item, idx, isRunning, result, onDelete }: EvalSetItemRowProps) {
  // 后端的分数固定是 0~100。此前按「≤1 就乘 100」猜刻度，0.8 分会显示成 80%
  const formattedScore = result !== undefined ? result.score.toFixed(0) : null;

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border/70 bg-background/50 p-3.5 text-xs transition-colors hover:border-border">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div className="space-y-1 max-w-2xl">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-muted-foreground">#{idx + 1}</span>
            <span className="font-medium text-foreground">{item.question}</span>
          </div>
          {item.reference && (
            <div className="text-[11px] text-muted-foreground pl-4 border-l-2 border-primary/30 mt-1">
              参考标准: {item.reference}
            </div>
          )}
          {result?.reason && (
            <div className="text-[10px] text-primary pl-4 mt-0.5">
              评测评语: {result.reason}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {isRunning && (
            <div className="flex items-center gap-1.5 text-primary">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-[11px]">校验中...</span>
            </div>
          )}

          {result !== undefined && !isRunning && formattedScore !== null && (
            <div className="flex items-center gap-1.5 font-mono tabular-nums">
              {result.passed ? (
                <div className="flex items-center gap-1 text-accent-insight font-medium">
                  <CheckCircle2 className="h-4 w-4" />
                  <span>通过 · {formattedScore} 分</span>
                </div>
              ) : (
                <div className="flex items-center gap-1 text-destructive font-medium">
                  <XCircle className="h-4 w-4" />
                  <span>未通过 · {formattedScore} 分</span>
                </div>
              )}
            </div>
          )}

          <div className="flex items-center gap-1">
            {item.tags?.map((t) => (
              <span
                key={t}
                className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground font-mono"
              >
                {t}
              </span>
            ))}
          </div>

          {onDelete && !isRunning && (
            <button
              type="button"
              onClick={() => {
                if (confirm(`删除第 ${idx + 1} 题？\n「${item.question.slice(0, 40)}」\n删除后之后的评测不再包含它。`)) {
                  onDelete(item);
                }
              }}
              className="rounded-md p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
              title="删除这道题"
              aria-label="删除这道题"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* 检索侧指标明细 (Context Recall / Context Precision / Faithfulness) */}
      {result?.metrics && (
        <div className="flex items-center justify-between border-t border-border/40 pt-2 mt-1">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-medium text-muted-foreground">检索评测指标:</span>
            <RetrievalMetricsView metrics={result.metrics} compact showAuditDetails />
          </div>
        </div>
      )}
    </div>
  );
}
