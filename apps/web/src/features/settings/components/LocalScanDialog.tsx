import React from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Laptop, Loader2, RefreshCw, ShieldCheck } from 'lucide-react';
import type { ProviderItem } from '@/lib/api/types.temp';
import { LocalScanGroupList } from './LocalScanGroupList';
import { LocalScanScannedFiles } from './LocalScanScannedFiles';
import { LocalScanEmptyState } from './LocalScanEmptyState';
import { useLocalScan } from '../hooks/useLocalScan';

interface LocalScanDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onImport: (providers: ProviderItem[]) => void;
  existingProviders?: ProviderItem[];
}

export function LocalScanDialog({
  isOpen,
  onClose,
  onImport,
  existingProviders = [],
}: LocalScanDialogProps) {
  const {
    isScanning,
    hasScanned,
    candidates,
    scannedPaths,
    errors,
    selectedIds,
    isImporting,
    groups,
    allImported,
    handleScan,
    handleToggle,
    handleToggleGroup,
    handleConfirmImport,
  } = useLocalScan({ isOpen, existingProviders, onImport, onClose });

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-2xl max-h-[88vh] flex flex-col p-0 gap-0 overflow-hidden">
        <DialogHeader className="px-6 pt-5 pb-3 border-b border-border/50">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Laptop className="h-5 w-5 text-primary" />
              <DialogTitle className="text-base font-semibold">扫描本机模型与配置</DialogTitle>
            </div>
            {hasScanned && (
              <Button
                variant="ghost"
                size="sm"
                onClick={handleScan}
                disabled={isScanning}
                className="h-7 px-2 text-xs gap-1.5 text-muted-foreground"
              >
                <RefreshCw className={`h-3.5 w-3.5 ${isScanning ? 'animate-spin' : ''}`} />
                重新扫描
              </Button>
            )}
          </div>
          <div className="mt-2.5 rounded-lg border border-primary/20 bg-primary/5 p-2.5 text-xs text-foreground/90 flex items-start gap-2">
            <ShieldCheck className="h-4 w-4 text-primary shrink-0 mt-0.5" />
            <span className="leading-relaxed">
              只读取已知的 Agent 配置文件，不会遍历你的主目录；密钥优先以环境变量占位符导入，明文不会写入配置。
            </span>
          </div>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {isScanning && !hasScanned ? (
            <div className="py-12 flex flex-col items-center justify-center gap-3 text-muted-foreground">
              <Loader2 className="h-7 w-7 animate-spin text-primary" />
              <span className="text-xs">正在探测本机运行服务与读取 Agent 配置...</span>
            </div>
          ) : !hasScanned || candidates.length === 0 ? (
            <LocalScanEmptyState type="no_candidates" onRetry={handleScan} isScanning={isScanning} />
          ) : allImported ? (
            <LocalScanEmptyState type="all_imported" onRetry={handleScan} isScanning={isScanning} />
          ) : (
            <LocalScanGroupList
              groups={groups}
              selectedIds={selectedIds}
              onToggle={handleToggle}
              onToggleGroup={handleToggleGroup}
            />
          )}
          <LocalScanScannedFiles scannedPaths={scannedPaths} errors={errors} />
        </div>

        <div className="px-6 py-3 border-t border-border/50 bg-muted/20 flex items-center justify-between">
          <div className="text-xs text-muted-foreground font-medium">
            已选择 <span className="font-semibold text-foreground font-mono">{selectedIds.size}</span> 项
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={onClose} disabled={isImporting}>取消</Button>
            <Button
              size="sm"
              onClick={handleConfirmImport}
              disabled={selectedIds.size === 0 || isImporting || isScanning}
            >
              {isImporting ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
                  导入中...
                </>
              ) : (
                `导入选中项 (${selectedIds.size})`
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
