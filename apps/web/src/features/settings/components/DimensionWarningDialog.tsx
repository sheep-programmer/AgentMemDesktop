import React from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from 'lucide-react';
import { useSpaceStore } from '@/stores/useSpaceStore';

interface DimensionWarningDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  oldDim?: number;
  newDim?: number;
}

export function DimensionWarningDialog({
  isOpen,
  onClose,
  onConfirm,
  oldDim,
  newDim,
}: DimensionWarningDialogProps) {
  // 影响范围用真实数字：此前写死「2 个 Space 的 4 份文档（共计 28,450 tokens）」
  const spaces = useSpaceStore((state) => state.spaces);
  const docCount = spaces.reduce((sum, space) => sum + (space.doc_count ?? 0), 0);
  const dimsKnown = oldDim != null && newDim != null;
  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="flex items-center gap-2 text-destructive">
            <AlertTriangle className="h-5 w-5" />
            <DialogTitle className="text-base font-semibold">
              {dimsKnown && oldDim !== newDim
                ? `更换向量模型（维度 ${oldDim} → ${newDim}）`
                : '更换向量模型'}
            </DialogTitle>
          </div>
          <DialogDescription className="text-xs text-muted-foreground pt-2 leading-relaxed">
            不同模型算出的向量不能混用，换模型后要把已有资料全部重新向量化：
            涉及 {spaces.length} 个知识空间、共 {docCount} 篇文档。确认后会立即开始逐个空间重建。
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive space-y-1">
          <p className="font-semibold">⚠️ 注意事项：</p>
          <p>重建期间检索只能按关键词匹配，重建完成后恢复；这段时间也不能导入新资料。</p>
        </div>

        <div className="mt-4 flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            取消
          </Button>
          <Button variant="destructive" size="sm" onClick={onConfirm}>
            确认更换并重建索引
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
