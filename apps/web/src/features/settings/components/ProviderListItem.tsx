import React from 'react';
import type { ProviderItem } from '@/lib/api/types.temp';
import { Button } from '@/components/ui/button';
import { Play, Pencil, Trash2, Key, ShieldCheck, Loader2 } from 'lucide-react';

interface ProviderListItemProps {
  prov: ProviderItem;
  isTesting: boolean;
  onTest: (prov: ProviderItem) => void;
  onEdit: (prov: ProviderItem) => void;
  onDelete: (id: string) => void;
}

function getKindBadgeClass(kind: string): string {
  switch (kind) {
    case 'llm':
      return 'bg-primary/10 text-primary border-primary/20';
    case 'embedding':
      return 'bg-accent-insight/5 text-accent-insight border-accent-insight/30';
    case 'rerank':
      return 'bg-accent-warn/5 text-accent-warn border-accent-warn/30';
    default:
      return 'bg-muted text-muted-foreground border-border';
  }
}

export function ProviderListItem({
  prov,
  isTesting,
  onTest,
  onEdit,
  onDelete,
}: ProviderListItemProps) {
  return (
    <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 p-3.5 text-xs hover:bg-muted/20 transition-colors">
      <div className="space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-semibold text-foreground">{prov.name || prov.id}</span>
          <span className={`rounded border px-1.5 py-0.2 font-mono text-[10px] font-medium uppercase ${getKindBadgeClass(prov.kind)}`}>
            {prov.kind}
          </span>
          <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground border border-border/50">
            {prov.adapter}
          </span>
          {prov.device && (
            <span className="rounded bg-muted/70 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              device: {prov.device}
            </span>
          )}
          {prov.dimension && (
            <span className="rounded bg-muted/70 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              {prov.dimension} 维
            </span>
          )}

          {prov.api_key_from_env ? (
            <span className="inline-flex items-center gap-1 rounded bg-accent-insight/10 px-1.5 py-0.5 text-[10px] text-accent-insight font-mono" title="密钥由环境变量注入，仅只读">
              <ShieldCheck className="h-3 w-3" />
              {/* 后端给的已经是完整占位符 `${VAR}`，别再补一个 $，否则显示成 $${VAR} */}
              {prov.api_key_from_env}
            </span>
          ) : prov.has_api_key ? (
            <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground font-mono">
              <Key className="h-3 w-3 opacity-60" />
              {prov.api_key_hint || '已配置密钥'}
            </span>
          ) : (
            <span className="text-[10px] text-muted-foreground font-mono">
              免密/本地
            </span>
          )}
        </div>

        <div className="flex items-center gap-3 text-muted-foreground text-[11px] font-mono flex-wrap">
          <span>模型: <span className="text-foreground/80">{prov.model || '未设定'}</span></span>
          {prov.base_url && (
            <>
              <span>·</span>
              <span className="truncate max-w-[280px]">{prov.base_url}</span>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 font-mono text-[11px] mr-2">
          {isTesting ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin text-primary" />
              <span className="text-muted-foreground">测试中</span>
            </>
          ) : prov.status === 'offline' ? (
            <>
              <span className="h-2 w-2 rounded-full bg-destructive" />
              <span className="text-destructive font-medium">离线</span>
            </>
          ) : prov.status === 'online' && prov.latency_ms !== undefined ? (
            <>
              <span className="h-2 w-2 rounded-full bg-accent-insight" />
              <span className="text-accent-insight font-medium">{prov.latency_ms}ms</span>
            </>
          ) : (
            <>
              <span className="h-2 w-2 rounded-full bg-muted-foreground/40" />
              <span className="text-muted-foreground">待测</span>
            </>
          )}
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => onTest(prov)}
          disabled={isTesting}
          className="h-7 text-xs gap-1"
        >
          <Play className="h-3 w-3" />
          测试连通
        </Button>

        <Button
          variant="ghost"
          size="icon"
          onClick={() => onEdit(prov)}
          title="编辑服务商"
          className="h-7 w-7 text-muted-foreground hover:text-foreground"
        >
          <Pencil className="h-3.5 w-3.5" />
        </Button>

        <Button
          variant="ghost"
          size="icon"
          onClick={() => onDelete(prov.id)}
          title="删除服务商"
          className="h-7 w-7 text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}
