import React, { useState } from 'react';
import { ChevronDown, ChevronRight, FileText, AlertTriangle, Shield } from 'lucide-react';

interface LocalScanScannedFilesProps {
  scannedPaths: string[];
  errors: string[];
}

export function LocalScanScannedFiles({ scannedPaths, errors }: LocalScanScannedFilesProps) {
  const [isOpen, setIsOpen] = useState(false);

  if (scannedPaths.length === 0 && errors.length === 0) {
    return null;
  }

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 text-xs overflow-hidden">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-3.5 py-2.5 text-left hover:bg-muted/40 transition-colors cursor-pointer select-none"
      >
        <div className="flex items-center gap-2 text-muted-foreground">
          <Shield className="h-3.5 w-3.5 text-primary" />
          <span className="font-medium text-foreground">本次读取的文件</span>
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground font-mono">
            {scannedPaths.length} 个配置
          </span>
          {errors.length > 0 && (
            <span className="rounded-full bg-destructive/15 px-2 py-0.5 text-[10px] text-destructive font-medium">
              {errors.length} 个异常
            </span>
          )}
        </div>
        <div className="flex items-center gap-1 text-muted-foreground text-[11px]">
          <span>{isOpen ? '收起详情' : '展开查看'}</span>
          {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </div>
      </button>

      {isOpen && (
        <div className="px-3.5 pb-3 pt-1 space-y-2.5 border-t border-border/40">
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            为保证安全与隐私，AgentMem 仅检查以下已知的配置文件与环境变量，不会遍历任何其他目录：
          </p>

          {/* Paths */}
          {scannedPaths.length > 0 && (
            <div className="space-y-1">
              {scannedPaths.map((p) => (
                <div
                  key={p}
                  className="flex items-center gap-2 rounded-md bg-background/80 px-2 py-1 font-mono text-[11px] text-muted-foreground border border-border/40 truncate"
                >
                  <FileText className="h-3 w-3 text-primary shrink-0" />
                  <span className="truncate">{p}</span>
                </div>
              ))}
            </div>
          )}

          {/* Errors */}
          {errors.length > 0 && (
            <div className="rounded-lg border border-destructive/20 bg-destructive/10 p-2.5 space-y-1">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-destructive">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>解析提示 / 警告</span>
              </div>
              <ul className="list-disc list-inside space-y-0.5 text-[11px] text-destructive/90">
                {errors.map((err) => (
                  <li key={err}>{err}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
