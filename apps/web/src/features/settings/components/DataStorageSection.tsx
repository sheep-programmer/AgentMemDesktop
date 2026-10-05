import React, { useEffect, useRef, useState, useCallback } from 'react';
import { useNavigate } from 'react-router';
import { Button } from '@/components/ui/button';
import {
  Database,
  Download,
  Upload,
  Loader2,
  RefreshCw,
  ArrowRight,
  CheckCircle2,
} from 'lucide-react';
import { toast } from 'sonner';
import { spaceService } from '@/lib/api/services/spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import type { SystemStats } from '@/lib/api/types.temp';

function formatBytes(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || Number.isNaN(bytes)) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

export function DataStorageSection() {
  const navigate = useNavigate();
  const {
    currentSpaceId,
    getCurrentSpace,
    spaces,
    loadSpaces,
    setCurrentSpaceId,
  } = useSpaceStore();
  const currentSpaceName = getCurrentSpace()?.name;
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [busy, setBusy] = useState<'export' | 'import' | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [imported, setImported] = useState<{
    id: string;
    fileName: string;
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { start: startStats } = useAsyncTask('storage-stats');
  const { start: startOperation } = useAsyncTask('storage-operation');

  const refreshStats = useCallback(async () => {
    const task = startStats();
    if (!task) return;
    setIsRefreshing(true);
    try {
      const data = await spaceService.getSystemStats();
      if (task.current()) {
        setStats(data);
        setLoadFailed(false);
      }
    } catch {
      if (task.current()) setLoadFailed(true);
    } finally {
      if (task.finish()) setIsRefreshing(false);
    }
  }, [startStats]);

  useEffect(() => {
    void refreshStats();
  }, [refreshStats]);

  const handleExport = async () => {
    if (!currentSpaceId) return;
    const task = startOperation();
    if (!task) return;
    setBusy('export');
    setOperationError(null);
    try {
      const blob = await spaceService.exportSpace(currentSpaceId);
      if (!task.current()) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `agentmem-space-${currentSpaceId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast.success('备份包已开始下载');
    } catch (error) {
      if (task.current()) {
        const message =
          error instanceof Error ? error.message : '备份下载失败，请重试';
        setOperationError(message);
        toast.error(message);
      }
    } finally {
      if (task.finish()) setBusy(null);
    }
  };

  const handleImportFile = async (file: File) => {
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setOperationError('请选择 AgentMem 导出的 .zip 备份文件');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }
    const task = startOperation();
    if (!task) return;
    setBusy('import');
    setOperationError(null);
    setImported(null);
    try {
      const result = await spaceService.importSpace(file);
      if (!task.current()) return;
      setImported({ id: result.space_id, fileName: file.name });
      await loadSpaces();
      if (!task.current()) return;
      void refreshStats();
      toast.success('备份已恢复，可以进入知识空间查看');
    } catch (error) {
      if (task.current()) {
        const message =
          error instanceof Error ? error.message : '恢复备份失败，请重试';
        setOperationError(message);
        toast.error(message);
      }
    } finally {
      if (task.finish()) {
        setBusy(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    }
  };

  const importedSpace = spaces.find((space) => space.id === imported?.id);
  const items = [
    { label: '知识空间', value: stats?.space_count },
    { label: '资料', value: stats?.document_count },
    { label: '资料片段', value: stats?.chunk_count },
    { label: '经验', value: stats?.insight_count },
  ];

  return (
    <div className="rounded-2xl border border-border bg-card p-5 sm:p-6 space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">
            本地数据与备份
          </h3>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            资料、对话和经验保存在本机。定期备份，方便换机或恢复数据。
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          aria-label="刷新存储统计"
          disabled={isRefreshing}
          onClick={() => void refreshStats()}
          className="h-9 shrink-0 gap-1.5 px-2 text-xs"
        >
          <RefreshCw
            className={`h-3.5 w-3.5 ${isRefreshing ? 'animate-spin' : ''}`}
          />
          刷新
        </Button>
      </div>

      <div className="rounded-xl border border-border/70 bg-background/60 p-4 space-y-3">
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="flex items-center gap-2 font-medium">
            <Database className="h-4 w-4 text-primary" />
            本机全部知识空间
          </span>
          <span className="font-mono text-foreground">
            {loadFailed ? '暂时无法统计' : formatBytes(stats?.disk_usage_bytes)}
          </span>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {items.map((item) => (
            <div
              key={item.label}
              className="rounded-lg border border-border/60 bg-muted/30 p-3"
            >
              <div className="text-xs text-muted-foreground">{item.label}</div>
              <div className="mt-1 font-mono text-lg font-semibold">
                {item.value?.toLocaleString() ?? '—'}
              </div>
            </div>
          ))}
        </div>
        {loadFailed && (
          <p role="alert" className="text-xs text-destructive">
            存储统计暂时无法更新，请点击刷新重试。
          </p>
        )}
      </div>

      {operationError && (
        <p
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {operationError}
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col rounded-xl border border-border bg-muted/20 p-4">
          <Download className="mb-3 h-5 w-5 text-primary" />
          <h4 className="text-sm font-semibold">备份当前知识空间</h4>
          <p className="mt-1 mb-4 break-words text-xs leading-relaxed text-muted-foreground">
            {currentSpaceName
              ? `将「${currentSpaceName}」的资料、对话和记忆保存为一个 .zip 文件。`
              : '先选择一个知识空间，再下载完整备份。'}
          </p>
          <Button
            variant="outline"
            onClick={handleExport}
            disabled={busy !== null || !currentSpaceId}
            className="mt-auto min-h-10 w-full gap-2 text-xs"
          >
            {busy === 'export' && <Loader2 className="h-4 w-4 animate-spin" />}
            {busy === 'export' ? '正在准备备份…' : '下载备份'}
          </Button>
        </div>
        <div className="flex flex-col rounded-xl border border-border bg-muted/20 p-4">
          <Upload className="mb-3 h-5 w-5 text-primary" />
          <h4 className="text-sm font-semibold">从备份恢复</h4>
          <p className="mt-1 mb-4 text-xs leading-relaxed text-muted-foreground">
            选择之前导出的 .zip
            文件。若对应知识空间已存在，会拒绝导入，保留现有数据。
          </p>
          <Button
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy !== null}
            className="mt-auto min-h-10 w-full gap-2 text-xs"
          >
            {busy === 'import' && <Loader2 className="h-4 w-4 animate-spin" />}
            {busy === 'import' ? '正在恢复备份…' : '选择备份文件'}
          </Button>
        </div>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        aria-label="选择 AgentMem 备份文件"
        accept=".zip"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleImportFile(file);
        }}
      />

      {imported && (
        <div
          role="status"
          aria-label="备份恢复结果"
          className="flex flex-wrap items-center gap-3 rounded-xl border border-primary/30 bg-primary/5 p-4"
        >
          <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">
              备份已恢复{importedSpace ? `：${importedSpace.name}` : ''}
            </p>
            <p className="mt-1 break-all text-xs text-muted-foreground">
              {imported.fileName}
            </p>
          </div>
          {importedSpace ? (
            <Button
              size="sm"
              className="min-h-9 gap-1.5"
              onClick={() => {
                setCurrentSpaceId(imported.id);
                navigate(`/s/${imported.id}/library`);
              }}
            >
              进入知识空间
              <ArrowRight className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={() => void loadSpaces()}
            >
              刷新空间列表
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
