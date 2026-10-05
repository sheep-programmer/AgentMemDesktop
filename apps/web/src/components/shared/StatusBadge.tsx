import React from 'react';
import { cn } from '@/lib/utils';
import type { InsightStatus, DocumentStatus } from '@/lib/api/types.temp';

interface StatusBadgeProps {
  status: InsightStatus | DocumentStatus | string;
  className?: string;
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  let label = status;
  let styleClass = 'border-border text-muted-foreground bg-muted/40';

  switch (status) {
    case 'active':
      label = '已生效';
      styleClass = 'border-accent-insight/40 bg-accent-insight/10 text-accent-insight font-medium';
      break;
    case 'candidate':
      label = '候选待定';
      styleClass = 'border-dashed border-border text-muted-foreground bg-transparent';
      break;
    case 'conflicted':
      label = '语义冲突';
      styleClass = 'border-accent-warn/80 bg-accent-warn/10 text-accent-warn animate-pulse font-medium';
      break;
    case 'archived':
      label = '已归档';
      styleClass = 'opacity-50 border-border text-muted-foreground bg-transparent';
      break;
    case 'ready':
      label = '已就绪';
      styleClass = 'border-accent-insight/30 bg-accent-insight/10 text-accent-insight';
      break;
    case 'embedding':
      label = '向量化中';
      styleClass = 'border-accent-ai/40 bg-accent-ai/10 text-accent-ai animate-pulse';
      break;
    case 'parsing':
      label = '解析中';
      styleClass = 'border-accent-ai/40 bg-accent-ai/10 text-accent-ai animate-pulse';
      break;
    case 'chunking':
      label = '切分中';
      styleClass = 'border-accent-ai/40 bg-accent-ai/10 text-accent-ai animate-pulse';
      break;
    case 'extracting':
      label = '抽取中';
      styleClass = 'border-accent-ai/40 bg-accent-ai/10 text-accent-ai animate-pulse';
      break;
    case 'pending':
      label = '排队中';
      styleClass = 'border-border text-muted-foreground bg-muted/40';
      break;
    case 'failed':
      label = '处理失败';
      styleClass = 'border-destructive/40 bg-destructive/10 text-destructive';
      break;
    default:
      break;
  }

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs select-none transition-colors',
        styleClass,
        className,
      )}
    >
      {label}
    </span>
  );
}
