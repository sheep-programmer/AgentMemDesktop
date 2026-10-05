import React from 'react';
import { Button } from '@/components/ui/button';
import { SearchX, CheckCircle2, RefreshCw } from 'lucide-react';

interface LocalScanEmptyStateProps {
  type: 'no_candidates' | 'all_imported';
  onRetry: () => void;
  isScanning?: boolean;
}

export function LocalScanEmptyState({
  type,
  onRetry,
  isScanning = false,
}: LocalScanEmptyStateProps) {
  if (type === 'all_imported') {
    return (
      <div className="flex flex-col items-center justify-center p-8 text-center rounded-xl border border-dashed border-border/80 bg-background/50 my-2">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 mb-3">
          <CheckCircle2 className="h-6 w-6" />
        </div>
        <h4 className="text-sm font-semibold text-foreground">所有检测到的配置均已导入</h4>
        <p className="text-xs text-muted-foreground mt-1.5 max-w-md leading-relaxed">
          本机检测到的所有 Agent 与本地模型配置均已作为服务商导入，无需重复添加。若需调整，请在列表中直接编辑。
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          disabled={isScanning}
          className="mt-4 gap-1.5 text-xs"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isScanning ? 'animate-spin' : ''}`} />
          重新扫描
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center p-8 text-center rounded-xl border border-dashed border-border/80 bg-background/50 my-2">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted/60 text-muted-foreground mb-3">
        <SearchX className="h-6 w-6" />
      </div>
      <h4 className="text-sm font-semibold text-foreground">未发现可复用的配置</h4>
      <p className="text-xs text-muted-foreground mt-1.5 max-w-md leading-relaxed">
        未发现可复用的配置。若你的 Claude Code / Codex 使用订阅登录（OAuth），则没有可复用的 API 密钥，请手动添加服务商。
      </p>
      <div className="flex items-center gap-2 mt-4">
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          disabled={isScanning}
          className="gap-1.5 text-xs"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isScanning ? 'animate-spin' : ''}`} />
          重新扫描
        </Button>
      </div>
    </div>
  );
}
