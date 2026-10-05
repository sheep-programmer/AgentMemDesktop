import React, { useState } from 'react';
import type { ConsistencyProbe } from '@/lib/api/types';
import { formatRelativeTime } from '@/lib/time';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Sparkles,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  MessageSquareQuote,
  CheckCircle2,
  X,
  Clock,
} from 'lucide-react';
import { cn } from '@/lib/utils';

export interface ConsistencyProbeResultProps {
  probe: ConsistencyProbe | null;
  isLoading: boolean;
  error: string | null;
  onMeasure: () => void;
  onClose?: () => void;
}

export function ConsistencyProbeResult({
  probe,
  isLoading,
  error,
  onMeasure,
  onClose,
}: ConsistencyProbeResultProps) {
  // 记录每个题目是否展开
  const [expandedIndices, setExpandedIndices] = useState<Record<number, boolean>>({
    0: true, // 默认展开第一题
  });

  const toggleExpand = (idx: number) => {
    setExpandedIndices((prev) => ({
      ...prev,
      [idx]: !prev[idx],
    }));
  };

  const toggleAll = (expand: boolean) => {
    if (!probe?.detail) return;
    const next: Record<number, boolean> = {};
    probe.detail.forEach((_, idx) => {
      next[idx] = expand;
    });
    setExpandedIndices(next);
  };

  if (!isLoading && !error && !probe) {
    return null;
  }

  return (
    <div className="relative overflow-hidden rounded-2xl border border-border/80 bg-card p-6 shadow-xs select-none">
      {/* 柔和微光 */}
      <div className="pointer-events-none absolute -top-10 -right-10 h-36 w-36 rounded-full bg-primary/10 blur-2xl" />

      {/* 顶部标题与操作栏 */}
      <div className="relative z-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 border-b border-border/40 pb-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Sparkles className="h-4 w-4" />
            </div>
            <h3 className="text-sm font-semibold text-foreground">
              回答一致性检测
            </h3>
            {probe && (
              <Badge
                variant="outline"
                className={cn(
                  'font-mono text-xs px-2 py-0.5',
                  probe.similarity >= 0.8
                    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                    : probe.similarity >= 0.6
                      ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
                      : 'border-destructive/30 bg-destructive/10 text-destructive',
                )}
              >
                整体相似度 {(probe.similarity * 100).toFixed(1)}%
              </Badge>
            )}
          </div>
          <p className="text-xs text-muted-foreground leading-relaxed">
            同问多答真实探测：对近期问题重复生成回答并比对语义相似度，是核对模型是否发生表述漂移的依据。
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Button
            variant="outline"
            size="sm"
            onClick={onMeasure}
            disabled={isLoading}
            className="h-8 gap-1.5 text-xs"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', isLoading && 'animate-spin')} />
            {isLoading ? '实测计算中...' : probe ? '重新实测' : '开始实测'}
          </Button>
          {onClose && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onClose}
              className="h-8 w-8 text-muted-foreground hover:text-foreground"
              title="收起结果面板"
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* 加载中状态 */}
      {isLoading && (
        <div className="relative z-10 my-8 flex flex-col items-center justify-center py-6 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary mb-3">
            <RefreshCw className="h-6 w-6 animate-spin" />
          </div>
          <p className="text-sm font-semibold text-foreground">正在调用模型进行一致性实测...</p>
          <p className="text-xs text-muted-foreground mt-1.5 max-w-md leading-relaxed">
            系统正对最近问过的 3 个问题各重复生成 3 次回答（共 9 次模型调用）并计算语义相似度矩阵。
            此过程通常耗时 10~20 秒，请稍候。
          </p>
        </div>
      )}

      {/* 失败状态 */}
      {!isLoading && error && (
        <div className="relative z-10 my-4 rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-xs text-destructive">
          <div className="flex items-start gap-3">
            <AlertCircle className="h-5 w-5 shrink-0 mt-0.5" />
            <div className="space-y-1 flex-1">
              <div className="font-semibold">实测请求未成功</div>
              <p className="leading-relaxed opacity-90">{error}</p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={onMeasure}
              className="h-7 text-xs border-destructive/40 text-destructive hover:bg-destructive/15 shrink-0"
            >
              重试
            </Button>
          </div>
        </div>
      )}

      {/* 探测成功结果展示 */}
      {!isLoading && probe && (
        <div className="relative z-10 mt-5 space-y-4">
          {/* 元信息条 */}
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-muted/40 px-3.5 py-2 text-xs text-muted-foreground">
            <div className="flex items-center gap-4">
              <span className="inline-flex items-center gap-1">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-700 dark:text-emerald-400" />
                <span>实测规模：{probe.questions} 题 × {probe.repeats} 次生成</span>
              </span>
              <span className="inline-flex items-center gap-1 font-mono">
                <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                <span>{formatRelativeTime(probe.created_at)}实测完成</span>
              </span>
            </div>

            {probe.detail && probe.detail.length > 0 && (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => toggleAll(true)}
                  className="text-[11px] text-primary hover:underline cursor-pointer"
                >
                  全部展开
                </button>
                <span className="text-muted-foreground">|</span>
                <button
                  type="button"
                  onClick={() => toggleAll(false)}
                  className="text-[11px] text-muted-foreground hover:text-foreground cursor-pointer"
                >
                  全部收起
                </button>
              </div>
            )}
          </div>

          {/* 逐题列表 */}
          <div className="space-y-3">
            {probe.detail && probe.detail.length > 0 ? (
              probe.detail.map((item, idx) => {
                const isExpanded = Boolean(expandedIndices[idx]);
                const simPct = (item.similarity * 100).toFixed(1);
                return (
                  <div
                    key={idx}
                    className="rounded-xl border border-border/70 bg-card/60 transition-colors hover:border-border"
                  >
                    {/* 题目头部 */}
                    <div
                      className="flex items-start justify-between gap-3 p-3.5 cursor-pointer select-none"
                      onClick={() => toggleExpand(idx)}
                    >
                      <div className="flex items-start gap-2.5 flex-1 min-w-0">
                        <span className="inline-flex items-center justify-center rounded-md bg-muted px-2 py-0.5 font-mono text-[11px] font-semibold text-muted-foreground shrink-0 mt-0.5">
                          Q{idx + 1}
                        </span>
                        <div className="space-y-0.5 min-w-0">
                          <p className="text-xs font-medium text-foreground leading-snug">
                            {item.question}
                          </p>
                          <p className="text-[11px] text-muted-foreground">
                            {item.answers ? `${item.answers.length} 次独立回答` : '暂无回答'}
                          </p>
                        </div>
                      </div>

                      <div className="flex items-center gap-3 shrink-0">
                        <div className="text-right">
                          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                            相似度
                          </div>
                          <div
                            className={cn(
                              'font-mono font-bold text-sm tabular-nums',
                              item.similarity >= 0.85
                                ? 'text-emerald-700 dark:text-emerald-400'
                                : item.similarity >= 0.7
                                  ? 'text-amber-700 dark:text-amber-400'
                                  : 'text-destructive',
                            )}
                          >
                            {simPct}%
                          </div>
                        </div>

                        <Button
                          variant="ghost"
                          size="icon-sm"
                          className="h-7 w-7 text-muted-foreground"
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleExpand(idx);
                          }}
                          title={isExpanded ? '收起多次回答' : '查看多次回答'}
                        >
                          {isExpanded ? (
                            <ChevronUp className="h-4 w-4" />
                          ) : (
                            <ChevronDown className="h-4 w-4" />
                          )}
                        </Button>
                      </div>
                    </div>

                    {/* 折叠查看多次回答 */}
                    {isExpanded && (
                      <div className="border-t border-border/40 bg-muted/20 p-3.5 space-y-3">
                        <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                          <MessageSquareQuote className="h-3.5 w-3.5 text-primary" />
                          <span>各次生成回答比对（人工核对是否存在事实漂移）：</span>
                        </div>

                        {item.answers && item.answers.length > 0 ? (
                          <div className="space-y-2.5">
                            {item.answers.map((ans, aIdx) => (
                              <div
                                key={aIdx}
                                className="rounded-lg border border-border/60 bg-background/80 p-3 shadow-2xs space-y-1.5"
                              >
                                <div className="flex items-center justify-between text-[11px] text-muted-foreground border-b border-border/30 pb-1 font-mono">
                                  <span className="font-semibold text-foreground/80">
                                    第 {aIdx + 1} 次独立生成
                                  </span>
                                </div>
                                <p className="text-xs text-foreground/90 leading-relaxed whitespace-pre-wrap">
                                  {ans}
                                </p>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground italic">
                            暂无本题的多次回答原始文本。
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            ) : (
              <p className="text-xs text-muted-foreground py-4 text-center">
                本次实测未返回逐题明细。
              </p>
            )}
          </div>

          {/* 核对指引说明 */}
          <div className="rounded-xl border border-primary/20 bg-primary/5 p-3 text-[11px] text-muted-foreground leading-relaxed">
            <span className="font-semibold text-foreground">💡 人工核对提示：</span>
            若多次生成的回答在关键事实、数据或核心结论上存在冲突，表明该知识域在检索或推理阶段存在发散漂移；可前往「自进化」沉淀针对性的正/反向规则进行约束。
          </div>
        </div>
      )}
    </div>
  );
}
