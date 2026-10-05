import React from 'react';
import { Button } from '@/components/ui/button';
import { ServerOff, Plus, Laptop } from 'lucide-react';

interface ProviderEmptyStateProps {
  onAddClick: () => void;
  onScanClick: () => void;
}

export function ProviderEmptyState({ onAddClick, onScanClick }: ProviderEmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center p-8 text-center rounded-xl border border-dashed border-border/80 bg-background/40 my-2">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted/60 text-muted-foreground mb-3">
        <ServerOff className="h-6 w-6" />
      </div>
      <h4 className="text-sm font-semibold text-foreground">还没有配置模型服务商</h4>
      <p className="text-xs text-muted-foreground mt-1 max-w-md leading-relaxed">
        AgentMem 需要至少配置一个 LLM 模型以启用推理与对话，配置向量模型以支持知识库检索。您可以一键复用本机已有 Agent（Claude Code / Codex / Continue）与运行中的模型服务，亦可手动添加云端 API。
      </p>
      <div className="flex flex-wrap items-center justify-center gap-3 mt-4">
        <Button size="sm" onClick={onAddClick} className="gap-1.5 text-xs">
          <Plus className="h-3.5 w-3.5" />
          添加服务商
        </Button>
        <Button size="sm" variant="outline" onClick={onScanClick} className="gap-1.5 text-xs">
          <Laptop className="h-3.5 w-3.5 text-primary" />
          扫描本机配置与服务
        </Button>
      </div>
    </div>
  );
}
