import React from 'react';
import type { ConflictGroup } from '@/lib/api/types.temp';
import { Button } from '@/components/ui/button';
import { AlertTriangle, GitMerge, Check } from 'lucide-react';
import { toast } from 'sonner';

interface ConflictResolutionBannerProps {
  groups: ConflictGroup[];
  onResolve: (groupId: string, action: 'keep_a' | 'keep_b' | 'merge') => void;
}

export function ConflictResolutionBanner({ groups, onResolve }: ConflictResolutionBannerProps) {
  if (groups.length === 0) return null;

  const currentGroup = groups[0];
  const [insightA, insightB] = currentGroup.insights || [];

  if (!insightA || !insightB) return null;

  return (
    <div className="rounded-xl border border-accent-warn/60 bg-accent-warn/10 p-4 text-xs shadow-2xs">
      <div className="flex items-center gap-2 font-semibold text-accent-warn">
        <AlertTriangle className="h-4 w-4" />
        <span>检测到 1 组经验语义冲突，需要仲裁决断</span>
      </div>

      <p className="mt-1 text-muted-foreground">
        以下两条经验在面对相同触发场景时给出了互斥或不同的执行对策，请人工裁决：
      </p>

      <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* 经验 A */}
        <div className="rounded-lg border border-border bg-card p-3 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-foreground">方案 A</span>
            <span className="text-[10px] text-muted-foreground">置信度: {(insightA.confidence * 100).toFixed(0)}%</span>
          </div>
          <div className="text-foreground font-medium">[场景] {insightA.trigger}</div>
          <div className="text-muted-foreground">[对策] {insightA.guidance}</div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              onResolve(currentGroup.group_id, 'keep_a');
              toast.success('已采纳方案 A 为正式生效经验，方案 B 归档');
            }}
            className="w-full mt-2 text-xs"
          >
            <Check className="h-3.5 w-3.5 mr-1" />
            采纳方案 A
          </Button>
        </div>

        {/* 经验 B */}
        <div className="rounded-lg border border-border bg-card p-3 space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-foreground">方案 B</span>
            <span className="text-[10px] text-muted-foreground">置信度: {(insightB.confidence * 100).toFixed(0)}%</span>
          </div>
          <div className="text-foreground font-medium">[场景] {insightB.trigger}</div>
          <div className="text-muted-foreground">[对策] {insightB.guidance}</div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              onResolve(currentGroup.group_id, 'keep_b');
              toast.success('已采纳方案 B 为正式生效经验，方案 A 归档');
            }}
            className="w-full mt-2 text-xs"
          >
            <Check className="h-3.5 w-3.5 mr-1" />
            采纳方案 B
          </Button>
        </div>
      </div>

      <div className="mt-3 flex justify-end">
        <Button
          size="sm"
          onClick={() => {
            onResolve(currentGroup.group_id, 'merge');
            toast.success('已自动融合生成兼顾内核与用户态的综合经验');
          }}
          className="gap-1.5 text-xs bg-accent-warn text-background hover:bg-accent-warn/90"
        >
          <GitMerge className="h-3.5 w-3.5" />
          智能语义合并两者
        </Button>
      </div>
    </div>
  );
}
