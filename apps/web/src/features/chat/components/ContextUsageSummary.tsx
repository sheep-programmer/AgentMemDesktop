import type { ContextUsage } from '@/lib/api/types';
import { Leaf } from 'lucide-react';

export function ContextUsageSummary({ usage }: { usage: ContextUsage | null }) {
  if (!usage) return null;
  const savedPercent =
    usage.original_estimated_tokens > 0
      ? Math.round(
          (100 * usage.saved_estimated_tokens) /
            usage.original_estimated_tokens,
        )
      : 0;
  return (
    <div
      role="status"
      className="mx-3 mb-2 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border/70 bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground sm:mx-6"
    >
      <span className="inline-flex items-center gap-1.5 text-foreground">
        <Leaf className="h-3.5 w-3.5 text-primary" />
        {usage.mode === 'economy' ? '节省上下文' : '标准上下文'}
      </span>
      <span>
        本轮输入估算{' '}
        <span className="font-mono tabular-nums">
          {usage.estimated_tokens.toLocaleString()}
        </span>{' '}
        token
      </span>
      {usage.saved_estimated_tokens > 0 && (
        <span>
          较原策略少{' '}
          <span className="font-mono tabular-nums">
            {usage.saved_estimated_tokens.toLocaleString()}
          </span>
          （{savedPercent}%）
        </span>
      )}
      <span
        className="ml-auto"
        title="按输入文本粗估，不代表模型账单。实际用量以服务商返回和设置页记录为准。"
      >
        估算值，非计费量
      </span>
    </div>
  );
}
