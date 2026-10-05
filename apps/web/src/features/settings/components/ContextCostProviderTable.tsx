import React from 'react';
import type { UsageGroupItem } from '@/lib/api/types';
import { Badge } from '@/components/ui/badge';

interface ProviderTableProps {
  items: UsageGroupItem[];
}

function formatNumber(num: number): string {
  return num.toLocaleString();
}

function getHitRateColor(rate: number) {
  if (rate >= 0.3) {
    return {
      text: 'text-accent-insight',
      bar: 'bg-accent-insight',
      bg: 'bg-accent-insight/10 border-accent-insight/30',
      tag: '高复用',
    };
  }
  if (rate > 0) {
    return {
      text: 'text-accent-ai',
      bar: 'bg-accent-ai',
      bg: 'bg-accent-ai/10 border-accent-ai/30',
      tag: '已生效',
    };
  }
  return {
    text: 'text-muted-foreground',
    bar: 'bg-muted-foreground/30',
    bg: 'bg-muted/40 border-border/50',
    tag: '未命中',
  };
}

export function ContextCostProviderTable({ items }: ProviderTableProps) {
  return (
    <div className="rounded-xl border border-border/70 overflow-hidden bg-background/50">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-border bg-muted/30 text-muted-foreground text-[11px] uppercase tracking-wider font-medium">
              <th className="py-2.5 px-4 align-middle">模型服务商</th>
              <th className="py-2.5 px-4 text-right align-middle">调用次数</th>
              <th className="py-2.5 px-4 text-right align-middle">
                输入 Token (含缓存)
              </th>
              <th className="py-2.5 px-4 text-right align-middle">
                输出 Token
              </th>
              <th className="py-2.5 px-4 text-right align-middle">平均延迟</th>
              <th className="py-2.5 px-4 text-right min-w-[160px] align-middle">
                缓存命中率
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40 font-mono">
            {items.map((item) => {
              const hitRate =
                item.cache_hit_rate ??
                (item.prompt_tokens > 0
                  ? item.cached_tokens / item.prompt_tokens
                  : 0);
              const hitPercent = (hitRate * 100).toFixed(1);
              const style = getHitRateColor(hitRate);

              return (
                <tr
                  key={item.key}
                  className="hover:bg-muted/20 transition-colors"
                >
                  <td className="py-3 px-4 align-middle">
                    <div className="flex items-center gap-2">
                      <span className="font-sans font-medium text-foreground text-xs">
                        {item.key}
                      </span>
                      <Badge
                        variant="outline"
                        className={`text-[10px] px-1.5 py-0 border ${style.bg} ${style.text}`}
                      >
                        {style.tag}
                      </Badge>
                    </div>
                  </td>
                  <td className="py-3 px-4 text-right tabular-nums text-foreground align-middle">
                    {formatNumber(item.calls)}
                  </td>
                  <td className="py-3 px-4 text-right tabular-nums align-middle">
                    <div className="text-foreground font-medium">
                      {formatNumber(item.prompt_tokens)}
                    </div>
                    {item.cached_tokens > 0 ? (
                      <div className="text-[10px] text-accent-ai/90 font-sans">
                        含{' '}
                        <span className="font-mono tabular-nums">
                          {formatNumber(item.cached_tokens)}
                        </span>{' '}
                        缓存
                      </div>
                    ) : (
                      <div className="text-[10px] text-muted-foreground font-sans">
                        无缓存命中
                      </div>
                    )}
                  </td>
                  <td className="py-3 px-4 text-right tabular-nums text-muted-foreground align-middle">
                    {formatNumber(item.completion_tokens)}
                  </td>
                  <td className="py-3 px-4 text-right tabular-nums text-muted-foreground align-middle">
                    {item.avg_latency_ms != null
                      ? `${Math.round(item.avg_latency_ms)}ms`
                      : '-'}
                  </td>
                  <td className="py-3 px-4 text-right align-middle">
                    <div className="flex items-center justify-end gap-2.5">
                      <div
                        className="w-16 sm:w-24 h-1.5 rounded-full bg-muted/60 border border-border/40 overflow-hidden"
                        title={`前缀缓存命中率: ${hitPercent}%`}
                      >
                        <div
                          className={`h-full rounded-full transition-all duration-300 ${style.bar}`}
                          style={{
                            width: `${Math.min(Math.max(hitRate * 100, 0), 100)}%`,
                          }}
                        />
                      </div>
                      <span
                        className={`tabular-nums font-semibold text-xs min-w-[42px] text-right ${style.text}`}
                      >
                        {hitPercent}%
                      </span>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
