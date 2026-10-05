import React, { useState } from 'react';
import type { DocumentItem } from '@/lib/api/types';
import { documentService } from '@/lib/api';
import type {
  DocumentRetryErrorEvent,
  DocumentRetryProgressEvent,
} from '@/lib/api/types';
import { Button } from '@/components/ui/button';
import {
  AlertTriangle,
  RotateCw,
  XCircle,
  AlertCircle,
  CheckCircle2,
} from 'lucide-react';
import { toast } from 'sonner';

interface RetryFailedDocumentsBarProps {
  spaceId: string;
  failedDocuments: DocumentItem[];
  onRetryComplete: () => void;
}

interface SingleDocError {
  document_id: string;
  title: string;
  message: string;
}

export function RetryFailedDocumentsBar({
  spaceId,
  failedDocuments,
  onRetryComplete,
}: RetryFailedDocumentsBarProps) {
  const [isRetrying, setIsRetrying] = useState(false);
  const [progress, setProgress] = useState<DocumentRetryProgressEvent | null>(
    null,
  );
  const [errorLogs, setErrorLogs] = useState<SingleDocError[]>([]);
  const [spaceLockedMsg, setSpaceLockedMsg] = useState<string | null>(null);
  const [retrySummary, setRetrySummary] = useState<{
    successCount: number;
    failedCount: number;
  } | null>(null);

  // 没有失败文档时不显示操作条：重试完之后再挂一个「0 篇失败 + 全部重试」既没用
  // 也让人以为还有问题，结果已经由 toast 反馈过了
  if (failedDocuments.length === 0 && !isRetrying && !spaceLockedMsg) {
    return null;
  }

  const handleRetryAll = async () => {
    if (isRetrying || !spaceId) return;

    setIsRetrying(true);
    setProgress(null);
    setErrorLogs([]);
    setSpaceLockedMsg(null);
    setRetrySummary(null);

    const currentErrors: SingleDocError[] = [];

    try {
      await documentService.retryFailedDocumentsStream(spaceId, {
        onBegin: (b) => {
          setProgress({
            document_id: '',
            stage: 'retry',
            done: 0,
            total: b.total,
            percent: 0,
          });
        },
        onProgress: (p) => {
          setProgress(p);
        },
        onError: (err: DocumentRetryErrorEvent) => {
          const doc = failedDocuments.find((d) => d.id === err.document_id);
          const docTitle = doc?.title || err.document_id;
          const entry: SingleDocError = {
            document_id: err.document_id,
            title: docTitle,
            message: err.message,
          };
          if (
            !currentErrors.some(
              (item) => item.document_id === entry.document_id,
            )
          ) {
            currentErrors.push(entry);
            setErrorLogs((prev) => [...prev, entry]);
          }
        },
        onDone: (done) => {
          const successCount = Math.min(done.total, Math.max(0, done.retried));
          const failedCount = Math.max(0, done.total - successCount);
          const summary = { successCount, failedCount };
          setRetrySummary(summary);
          const message = `重试完成：成功 ${successCount} 篇，仍失败 ${failedCount} 篇`;
          if (failedCount === 0) toast.success(message);
          else if (successCount === 0) toast.error(message);
          else toast.warning(message);
        },
      });
    } catch (err: unknown) {
      const errorObj = err as {
        status?: number;
        code?: string;
        message?: string;
        detail?: { message?: string };
      };

      if (errorObj?.status === 409 || errorObj?.code === 'SPACE_LOCKED') {
        const msg =
          errorObj.detail?.message ||
          errorObj.message ||
          '该知识空间正在重建索引，请等它完成后再导入或重新解析';
        setSpaceLockedMsg(msg);
        toast.error(`重建索引冲突 (409): ${msg}`);
      } else {
        const msg = errorObj?.message || '重试失败，请检查网络或后端服务';
        toast.error(msg);
      }
    } finally {
      setIsRetrying(false);
      onRetryComplete();
    }
  };

  return (
    <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 space-y-3 transition-all">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-destructive/10 text-destructive shrink-0">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="space-y-0.5">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-sm text-foreground">
                {failedDocuments.length > 0
                  ? `${failedDocuments.length} 篇文档处理失败`
                  : '文档重试已完成'}
              </span>
              {isRetrying && progress && (
                <span className="text-xs font-medium text-destructive">
                  正在重试：第 {progress.done} / {progress.total} 篇 (
                  {progress.percent.toFixed(0)}%)
                </span>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              切片解析或向量化过程中断。可发起全量异步重试以恢复知识入库。
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          <Button
            size="sm"
            variant="destructive"
            onClick={handleRetryAll}
            disabled={isRetrying || !spaceId}
            className="gap-1.5 h-8 text-xs font-medium shadow-xs"
          >
            <RotateCw
              className={`h-3.5 w-3.5 ${isRetrying ? 'animate-spin' : ''}`}
            />
            {isRetrying ? '正在重试...' : '全部重试'}
          </Button>
        </div>
      </div>

      {/* 进度条指示 */}
      {isRetrying && progress && (
        <div className="space-y-1 pt-1">
          <div className="flex justify-between text-[11px] text-muted-foreground">
            <span>重试进度</span>
            <span>
              {progress.done} / {progress.total} ({progress.percent.toFixed(1)}
              %)
            </span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-destructive/20">
            <div
              className="h-full bg-destructive transition-all duration-300"
              style={{ width: `${progress.percent}%` }}
            />
          </div>
        </div>
      )}

      {/* 409 Space 正在重建索引错误提示 */}
      {spaceLockedMsg && (
        <div className="rounded-lg bg-background/90 border border-amber-500/50 p-3 flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400">
          <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold">知识空间正忙，暂时不能操作</div>
            <div className="mt-0.5 leading-relaxed">{spaceLockedMsg}</div>
          </div>
        </div>
      )}

      {/* 重试完成摘要提示 */}
      {!isRetrying && retrySummary && (
        <div className="rounded-lg bg-background/80 border border-border p-2.5 flex items-center justify-between text-xs">
          <div className="flex items-center gap-2 text-muted-foreground">
            <CheckCircle2 className="h-4 w-4 text-emerald-700 dark:text-emerald-400" />
            <span>
              重试执行结果：成功{' '}
              <strong className="text-emerald-700 dark:text-emerald-400">
                {retrySummary.successCount}
              </strong>{' '}
              篇，仍失败{' '}
              <strong className="text-destructive">
                {retrySummary.failedCount}
              </strong>{' '}
              篇
            </span>
          </div>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setRetrySummary(null)}
            className="h-6 text-[11px] px-2 text-muted-foreground"
          >
            忽略提示
          </Button>
        </div>
      )}

      {/* 单篇文档失败原因列表 */}
      {errorLogs.length > 0 && (
        <div className="rounded-lg bg-background/90 border border-destructive/20 p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-destructive">
            <XCircle className="h-3.5 w-3.5" />
            <span>单篇重试错误详情 ({errorLogs.length})</span>
          </div>
          <div className="space-y-1.5 max-h-36 overflow-y-auto pr-1">
            {errorLogs.map((err, idx) => (
              <div
                key={`${err.document_id}-${idx}`}
                className="flex items-start gap-2 text-[11px] bg-destructive/5 rounded p-1.5 border border-destructive/10"
              >
                <span className="font-medium text-foreground shrink-0 max-w-[200px] truncate">
                  {err.title}
                </span>
                <span className="text-muted-foreground font-mono text-[10px] shrink-0">
                  [{err.document_id}]
                </span>
                <span className="text-destructive break-all flex-1">
                  {err.message}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
