import React from 'react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { AlertCircle, RefreshCw, FolderSearch, Sparkles } from 'lucide-react';

interface FourStateViewProps {
  status: 'loading' | 'empty' | 'error' | 'ready';
  error?: string | null;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyActionLabel?: string;
  onEmptyAction?: () => void;
  emptyIcon?: React.ReactNode;
  skeletonCount?: number;
  children: React.ReactNode;
}

export function FourStateView({
  status,
  error,
  onRetry,
  emptyTitle = '开启你的知识库',
  emptyDescription = '导入第一份资料，开始搭建你的领域知识库。',
  emptyActionLabel,
  onEmptyAction,
  emptyIcon,
  skeletonCount = 3,
  children,
}: FourStateViewProps) {
  if (status === 'loading') {
    return (
      <div
        aria-busy="true"
        aria-label="正在加载内容"
        className="relative grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 p-1"
      >
        <p role="status" className="sr-only">
          正在加载，请稍候…
        </p>
        {Array.from({ length: skeletonCount }).map((_, i) => (
          <div
            key={i}
            aria-hidden="true"
            className="flex flex-col justify-between rounded-xl border border-border/80 bg-card p-4 shadow-2xs space-y-4"
          >
            {/* 模拟卡片头部 */}
            <div className="flex items-center justify-between">
              <Skeleton className="h-4 w-24 rounded-full" />
              <Skeleton className="h-6 w-6 rounded-full" />
            </div>
            {/* 模拟标题与内容段落 */}
            <div className="space-y-2">
              <Skeleton className="h-4 w-3/4 rounded-md" />
              <Skeleton className="h-3 w-full rounded-md" />
              <Skeleton className="h-3 w-5/6 rounded-md" />
            </div>
            {/* 模拟底部标签与元数据 */}
            <div className="flex items-center justify-between border-t border-border/40 pt-3">
              <Skeleton className="h-3 w-20 rounded-md" />
              <Skeleton className="h-4 w-12 rounded-full" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (status === 'error') {
    return (
      <div
        role="alert"
        className="flex min-h-[340px] flex-col items-center justify-center rounded-2xl border border-destructive/20 bg-destructive/5 p-8 text-center"
      >
        <div className="mb-4 flex h-13 w-13 items-center justify-center rounded-2xl bg-destructive/10 text-destructive shadow-xs">
          <AlertCircle className="h-6 w-6" />
        </div>
        <h3 className="text-base font-semibold text-foreground">
          暂时无法加载
        </h3>
        <p className="mt-1.5 max-w-sm text-xs sm:text-sm text-muted-foreground leading-relaxed">
          {error || '请检查服务连接后重试。加载失败不代表资料已丢失。'}
        </p>
        {onRetry && (
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            className="mt-5 gap-1.5 active:scale-[0.98]"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            重新加载
          </Button>
        )}
      </div>
    );
  }

  if (status === 'empty') {
    return (
      <div className="relative flex min-h-[380px] flex-col items-center justify-center rounded-2xl border border-dashed border-border/80 bg-card/40 p-8 text-center overflow-hidden">
        {/* 背景氛围柔光 */}
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="h-48 w-48 rounded-full bg-primary/8 blur-3xl" />
          <div className="h-32 w-32 rounded-full bg-accent-ai/8 blur-2xl -translate-y-4 translate-x-4" />
        </div>

        {/* 插画式复合图标 */}
        <div className="relative mb-5 flex items-center justify-center">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-border/80 bg-card/90 text-primary shadow-xs backdrop-blur-xs transition-transform duration-300 hover:scale-105">
            {emptyIcon || <FolderSearch className="h-7 w-7 text-primary" />}
          </div>
          <div className="absolute -top-1.5 -right-1.5 flex h-6 w-6 items-center justify-center rounded-full border border-accent-insight/30 bg-accent-insight/15 text-accent-insight shadow-2xs backdrop-blur-xs">
            <Sparkles className="h-3 w-3" />
          </div>
        </div>

        {/* 标题与引导文案 */}
        <h3 className="text-base font-semibold tracking-tight text-foreground">
          {emptyTitle}
        </h3>
        <p className="mt-2 max-w-md text-xs sm:text-sm text-muted-foreground leading-relaxed">
          {emptyDescription}
        </p>

        {emptyActionLabel && onEmptyAction && (
          <Button
            onClick={onEmptyAction}
            className="mt-5 gap-1.5 shadow-xs transition-all hover:scale-[1.02] active:scale-[0.98]"
          >
            {emptyActionLabel}
          </Button>
        )}
      </div>
    );
  }

  return <>{children}</>;
}
