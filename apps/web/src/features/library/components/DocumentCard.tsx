import React from 'react';
import type { DocumentItem } from '@/lib/api/types.temp';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { StatusBadge } from '@/components/shared/StatusBadge';
import {
  FileText,
  FileCode,
  Globe,
  ClipboardList,
  MoreVertical,
  Trash2,
  RefreshCw,
  Loader2,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';

/** 摄取过程中后端推来的真实阶段与百分比。 */
export interface IngestProgress {
  stage: string;
  percent: number;
}

interface DocumentCardProps {
  disabled?: boolean;
  document: DocumentItem;
  progress?: IngestProgress;
  isSelected: boolean;
  onToggleSelect: (id: string) => void;
  onClick: (doc: DocumentItem) => void;
  onDelete: (id: string) => void;
  onReprocess: (id: string) => void;
}

/** 摄取阶段的中文说法：界面上不出现 parsing / embedding 这类内部取值。 */
const INGEST_STAGE_LABELS: Record<string, string> = {
  parsing: '解析文档',
  summarizing: '生成概要',
  chunking: '切分切片',
  embedding: '向量化',
  extracting: '抽取卡片',
};

export function DocumentCard({
  document: doc,
  progress,
  isSelected,
  disabled = false,
  onToggleSelect,
  onClick,
  onDelete,
  onReprocess,
}: DocumentCardProps) {
  const getFileIcon = (mime?: string | null, type?: string | null) => {
    if (type === 'url') return <Globe className="h-4.5 w-4.5 text-primary" />;
    if (type === 'paste')
      return <ClipboardList className="h-4.5 w-4.5 text-accent-warn" />;
    if (mime?.includes('markdown') || mime?.includes('text')) {
      return <FileCode className="h-4.5 w-4.5 text-accent-ai" />;
    }
    return <FileText className="h-4.5 w-4.5 text-primary" />;
  };

  const formatBytes = (bytes?: number | null) => {
    if (!bytes) return '未知大小';
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const getProgressValue = () => {
    if (doc.status === 'ready') return 100;
    // Only display a percentage when the backend has actually reported one.
    if (progress && typeof progress.percent === 'number')
      return Math.min(100, Math.max(0, Math.round(progress.percent)));
    return 0;
  };

  return (
    <div
      onClick={() => onClick(doc)}
      className={cn(
        'group relative flex h-full flex-col justify-between rounded-xl border bg-card p-4.5 text-xs transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-xs cursor-pointer select-none',
        isSelected
          ? 'border-primary ring-1 ring-primary/30 bg-primary/5'
          : 'border-border/70 hover:border-border',
      )}
    >
      {/* 头部：文件图标 + 右侧操作区 (勾选框 + 进度环 + 菜单) */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-muted/40 transition-colors group-hover:bg-primary/10 group-hover:border-primary/25">
          {getFileIcon(doc.mime, doc.source_type)}
        </div>

        <div className="flex items-center gap-2">
          {/* Checkbox: hover 或选中时显现 */}
          <div
            className={cn(
              'flex items-center transition-opacity duration-150',
              'opacity-100',
            )}
            onClick={(e) => e.stopPropagation()}
          >
            <label
              className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-md focus-within:ring-2 focus-within:ring-primary"
              onClick={(event) => event.stopPropagation()}
            >
              <input
                type="checkbox"
                aria-label={`选择资料：${doc.title}`}
                disabled={disabled}
                checked={isSelected}
                onChange={() => onToggleSelect(doc.id)}
                className="h-3.5 w-3.5 rounded border-border text-primary cursor-pointer accent-primary"
              />
            </label>
          </div>

          <span
            role="img"
            aria-label={
              doc.status === 'ready'
                ? '资料已就绪'
                : doc.status === 'failed'
                  ? '资料处理失败'
                  : progress
                    ? `资料处理进度 ${getProgressValue()}%`
                    : '正在处理资料'
            }
          >
            {doc.status === 'ready' ? (
              <CheckCircle2 className="h-4 w-4 text-accent-insight" />
            ) : doc.status === 'failed' ? (
              <AlertCircle className="h-4 w-4 text-destructive" />
            ) : progress ? (
              <ConfidenceRing
                value={getProgressValue()}
                max={100}
                size={20}
                strokeWidth={2.5}
                showText={false}
              />
            ) : (
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
            )}
          </span>

          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={`资料操作：${doc.title}`}
              onClick={(e) => e.stopPropagation()}
              className="flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary transition-colors"
            >
              <MoreVertical className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                disabled={disabled || !['ready', 'failed'].includes(doc.status)}
                onClick={() => onReprocess(doc.id)}
                className="gap-2 text-xs"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重新解析与索引
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={disabled}
                onClick={() => onDelete(doc.id)}
                className="gap-2 text-xs text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" />
                删除该资料
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* 主体：标题与元数据 (flex-1 保证等高时的垂直对齐) */}
      <div className="mt-3.5 flex-1 flex flex-col justify-between">
        <div>
          <h3 className="font-medium text-foreground group-hover:text-primary transition-colors text-sm leading-snug">
            <button
              type="button"
              aria-label={`查看资料：${doc.title}`}
              className="line-clamp-2 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              onClick={(e) => {
                e.stopPropagation();
                onClick(doc);
              }}
            >
              {doc.title}
            </button>
          </h3>
        </div>
        <div className="mt-3 flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono tabular-nums">
            {formatBytes(doc.size_bytes)}
          </span>
          <span className="text-muted-foreground">·</span>
          <span className="font-mono tabular-nums">
            {doc.token_count.toLocaleString()} 词元
          </span>
        </div>
      </div>

      {/* 底部：状态标签与日期 */}
      <div className="mt-4 flex items-center justify-between border-t border-border/40 pt-3 text-[11px] text-muted-foreground">
        <StatusBadge status={doc.status} />
        {progress && doc.status !== 'ready' && doc.status !== 'failed' && (
          <span className="text-[10px] text-muted-foreground">
            {INGEST_STAGE_LABELS[progress.stage] ?? progress.stage}
          </span>
        )}
        <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
          {new Date(doc.created_at).toLocaleDateString()}
        </span>
      </div>
    </div>
  );
}
